#!/usr/bin/env python3
"""One-command acceptance for the walking policy.

Runs the per-axis evaluation, records review videos, and prints a pass/fail
table against the thresholds this project actually cares about. Combines what
used to be three manual steps (export -> evaluate -> record).

    uv run --no-sync scripts/accept_velocity.py \
        --onnx policies/hd1910_v3.onnx --out docs/hd1910/artifacts/accept_v3.json

Thresholds (measured on the real acceptance runs, see docs/hd1910/ACCEPTANCE.md):

    fwd_fast   >= 0.30 m/s   the headline "it walks" number
    fwd_slow   >= 0.10 m/s   small commands must not be ignored (run 3 regression)
    wz         >= 0.40 rad/s turning in place (run 2 shipped at 0.07)
    wz_slow    >= 0.15 rad/s slow turning, the run-3b target
    fall_ratio <= 0.05       no falling in any bucket
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import evaluate_policy as ev  # noqa: E402

THRESHOLDS = {
    "fwd_fast": ("achieved_vx", ">=", 0.30),
    "fwd_slow": ("achieved_vx", ">=", 0.10),
    "wz": ("achieved_wz", ">=", 0.40),
    "wz_slow": ("achieved_wz", ">=", 0.15),
    "back": ("achieved_vx", "<=", -0.15),
    "lat": ("achieved_vy", ">=", 0.12),
}


def check(name, result):
    if name not in THRESHOLDS:
        return None
    field, op, limit = THRESHOLDS[name]
    got = result[field]
    ok = got >= limit if op == ">=" else got <= limit
    return ok, got, f"{op} {limit}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--onnx", required=True)
    ap.add_argument("--task", default="Mjlab-Velocity-Flat-MicroDuck-HD1910")
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--num-envs", type=int, default=16)
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--video-dir", default=None, help="record fwd_fast and wz clips here")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    import onnxruntime as ort

    sess = ort.InferenceSession(args.onnx)

    env = ev.build_env(args.task, args.device, args.num_envs)
    results = []
    try:
        for name in ev.BUCKETS:
            results.append(ev.run_bucket(env, sess, name, ev.BUCKETS[name], args.seconds))
    finally:
        env.close()

    if args.video_dir:
        outdir = Path(args.video_dir)
        outdir.mkdir(parents=True, exist_ok=True)
        venv = ev.build_env(args.task, args.device, min(args.num_envs, 6), render=True)
        try:
            for name in ("fwd_fast", "wz"):
                r = ev.run_bucket(venv, sess, name, ev.BUCKETS[name], args.seconds,
                                  record=True)
                frames = r.pop("_frames", None)
                if frames:
                    import imageio.v2 as imageio

                    path = outdir / f"accept_{name}.mp4"
                    imageio.mimsave(path, frames, fps=50)
                    print(f"video: {path} ({len(frames)} frames)")
        finally:
            venv.close()

    # ── report ───────────────────────────────────────────────────────────
    print(f"{'bucket':10s}{'cmd':>18}{'achieved':>18}{'verdict':>12}")
    print("-" * 60)
    failed = 0
    summary = []
    for r in results:
        c = r["cmd"]
        ach = (r["achieved_vx"], r["achieved_vy"], r["achieved_wz"])
        verdict = check(r["bucket"], r)
        if verdict is None:
            mark = "  —"
        else:
            ok, got, expr = verdict
            mark = "  PASS" if ok else "  FAIL"
            if not ok:
                failed += 1
        print(f"{r['bucket']:10s}"
              f"{c['vx']:+.2f},{c['vy']:+.2f},{c['wz']:+.2f}".rjust(18)
              + f"{ach[0]:+.2f},{ach[1]:+.2f},{ach[2]:+.2f}".rjust(18)
              + f"{mark:>12}")
        summary.append({**r, "verdict": mark.strip()})

    print()
    print("PASS" if failed == 0 else f"{failed} bucket(s) below threshold")

    payload = {"onnx": args.onnx, "results": summary, "failed_checks": failed}
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {args.out}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
