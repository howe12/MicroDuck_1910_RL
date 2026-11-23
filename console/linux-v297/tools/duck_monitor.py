#!/usr/bin/env python3
"""Continuously record a running Microduck console for stop/stall diagnosis.

It polls the console's local /api/snapshot endpoint, appends every new frame
to telemetry.jsonl, emits simple event markers (gaps, stale channels, joint
error, current, temperature), and writes a summary on exit.

The console must already be running and connected to the robot. This tool does
not send any motion command.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

DEFAULT_URL = "http://127.0.0.1:8090/api/snapshot"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def fetch_json(url: str, timeout: float = 2.0) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def metrics(frame: dict) -> dict:
    servos = frame.get("servos") or []
    errors = [abs(s["error_deg"]) for s in servos if number(s.get("error_deg")) is not None]
    currents = [s["current_ma"] for s in servos if number(s.get("current_ma")) is not None]
    temps = [s["temperature_c"] for s in servos if number(s.get("temperature_c")) is not None]
    channels = frame.get("channels") or {}
    # These are the channels that must be live for a moving robot. Optional
    # channels such as tof/startup/calibration may legitimately be waiting.
    critical = ("state", "health", "bus")
    bad = [name for name in critical
           if isinstance(channels.get(name), dict)
           and channels[name].get("status") not in ("live",)]
    health = frame.get("health") or {}
    battery = health.get("battery") if isinstance(health.get("battery"), dict) else {}
    motors = health.get("motors") if isinstance(health.get("motors"), dict) else {}
    calibration = frame.get("calibration") or {}
    calibrations = (calibration.get("calibration") or {}).get("joints") if isinstance(calibration.get("calibration"), dict) else None
    limits = {}
    for row in calibrations or []:
        if isinstance(row, dict) and isinstance(row.get("name"), str):
            lo, hi = number(row.get("min_rad")), number(row.get("max_rad"))
            if lo is not None and hi is not None:
                limits[row["name"]] = (math.degrees(lo), math.degrees(hi))
    violations = []
    for servo in servos:
        if not isinstance(servo, dict):
            continue
        name, target = servo.get("name"), number(servo.get("target_deg"))
        if target is None or name not in limits:
            continue
        lo, hi = limits[name]
        if target < lo - 1 or target > hi + 1:
            violations.append((name, round(target, 1), round(lo, 1), round(hi, 1)))
    return {
        "max_error_deg": max(errors) if errors else None,
        "max_current_ma": max(currents) if currents else None,
        "max_temp_c": max(temps) if temps else number(motors.get("max_c")),
        "battery_v": number(battery.get("volts")),
        "bad_channels": bad,
        "bad_details": [f"{n}: {str((channels.get(n) or {}).get('error','')[:120])}" for n in bad],
        "target_violations": violations,
        "servo_count": len(servos),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL, help="console recording endpoint")
    parser.add_argument("--interval", type=float, default=0.5, help="poll interval seconds")
    parser.add_argument("--out", type=pathlib.Path, help="output directory")
    parser.add_argument("--max-seconds", type=float, default=0, help="stop after N seconds (0 = forever)")
    parser.add_argument("--quiet", action="store_true", help="only print events")
    args = parser.parse_args()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir = args.out or (pathlib.Path.home() / ".local/share/MicroduckConsole/monitor" / stamp)
    outdir.mkdir(parents=True, exist_ok=True)
    telemetry_path = outdir / "telemetry.jsonl"
    events_path = outdir / "events.jsonl"
    summary_path = outdir / "summary.json"

    telemetry = telemetry_path.open("a", encoding="utf-8")
    events = events_path.open("a", encoding="utf-8")
    started = time.time()
    last_at = -1.0
    last_new = time.time()
    last_bad = set()
    last_event_at = {}
    frame_count = 0
    event_counts = {}
    last_status_print = 0.0
    stopped = False

    def emit_event(kind: str, message: str, **data) -> None:
        nonlocal event_counts, last_event_at
        now = time.time()
        # De-duplicate noisy events: one per kind per 2 seconds.
        if now - last_event_at.get(kind, 0) < 2.0:
            return
        last_event_at[kind] = now
        event_counts[kind] = event_counts.get(kind, 0) + 1
        row = {"at": now, "iso": now_iso(), "type": kind, "message": message, **data}
        events.write(json.dumps(row, ensure_ascii=False) + "\n")
        events.flush()
        print(f"\n!! [{row['iso']}] {kind}: {message}", flush=True)

    def stop(_signum=None, _frame=None):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    print(f"监控输出: {outdir}")
    print(f"轮询: {args.url}  间隔 {args.interval:.1f}s")
    print("按 Ctrl+C 停止并写 summary。\n")
    try:
        while not stopped:
            if args.max_seconds and time.time() - started >= args.max_seconds:
                break
            try:
                data = fetch_json(args.url)
            except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
                emit_event("console_unreachable", f"无法读取中控接口: {exc}")
                time.sleep(max(0.5, args.interval))
                continue

            if isinstance(data, dict) and isinstance(data.get("frames"), list):
                # Compatibility with older /api/recording responses.
                frames = data.get("frames") or []
            elif isinstance(data, dict) and ("at" in data or "servos" in data):
                # /api/snapshot returns one live frame.
                frames = [data]
            else:
                frames = []
            new_frames = []
            for frame in frames:
                at = number(frame.get("at")) if isinstance(frame, dict) else None
                if at is not None and at > last_at:
                    last_at = at
                    new_frames.append(frame)
            new_frames.sort(key=lambda f: number(f.get("at")) or 0)
            for frame in new_frames:
                telemetry.write(json.dumps(frame, ensure_ascii=False) + "\n")
                frame_count += 1
                m = metrics(frame)
                if m["bad_channels"]:
                    bad = set(m["bad_channels"])
                    if bad != last_bad:
                        detail = "; ".join(m.get("bad_details") or [])
                        emit_event("channel_not_live", "通道未就绪: " + ", ".join(sorted(bad)) + ((" · " + detail) if detail else ""),
                                   channels=sorted(bad), errors=detail)
                    last_bad = bad
                else:
                    last_bad = set()
                if m["max_error_deg"] is not None and m["max_error_deg"] > 15:
                    emit_event("joint_error", f"最大关节误差 {m['max_error_deg']:.1f}°",
                               max_error_deg=m["max_error_deg"])
                if m.get("target_violations"):
                    text = ", ".join(f"{n} {t}° (限 {lo}..{hi}°)" for n, t, lo, hi in m["target_violations"])
                    emit_event("target_out_of_range", "目标超出关节限位: " + text,
                               violations=m["target_violations"])
                if m["max_current_ma"] is not None and m["max_current_ma"] > 1500:
                    emit_event("high_current", f"最大电流 {m['max_current_ma']:.0f} mA",
                               max_current_ma=m["max_current_ma"])
                if m["max_temp_c"] is not None and m["max_temp_c"] > 70:
                    emit_event("high_temperature", f"最高舵机温度 {m['max_temp_c']:.1f} °C",
                               max_temp_c=m["max_temp_c"])
                if m["battery_v"] is not None and m["battery_v"] < 6.8:
                    emit_event("low_battery", f"电池电压 {m['battery_v']:.2f} V", battery_v=m["battery_v"])
            if new_frames:
                last_new = time.time()
                telemetry.flush()
            elif time.time() - last_new > 1.5:
                emit_event("no_new_frames", f"{time.time()-last_new:.1f}s 没有新帧")
                last_new = time.time()

            if not args.quiet and time.time() - last_status_print >= 2:
                last_status_print = time.time()
                latest = new_frames[-1] if new_frames else (frames[-1] if frames else {})
                m = metrics(latest) if isinstance(latest, dict) else {}
                print(
                    f"[{now_iso()}] frames={frame_count} "
                    f"servos={m.get('servo_count','-')} "
                    f"err={m.get('max_error_deg') if m.get('max_error_deg') is None else round(m['max_error_deg'],1)}° "
                    f"cur={m.get('max_current_ma') if m.get('max_current_ma') is None else round(m['max_current_ma'])}mA "
                    f"temp={m.get('max_temp_c') if m.get('max_temp_c') is None else round(m['max_temp_c'],1)}°C "
                    f"batt={m.get('battery_v') if m.get('battery_v') is None else round(m['battery_v'],2)}V "
                    f"bad={','.join(m.get('bad_channels') or []) or '-'}",
                    flush=True,
                )
            time.sleep(max(0.1, args.interval))
    finally:
        telemetry.close()
        events.close()
        summary = {
            "started_at": started,
            "ended_at": time.time(),
            "duration_s": round(time.time() - started, 2),
            "frame_count": frame_count,
            "event_counts": event_counts,
            "telemetry": str(telemetry_path),
            "events": str(events_path),
            "last_frame_at": last_at if last_at >= 0 else None,
        }
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\n已保存: {summary_path}")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
