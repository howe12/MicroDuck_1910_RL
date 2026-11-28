#!/usr/bin/env python3
"""Run every policy through its acceptance test in one pass and print a verdict table.

Each policy is measured against the check that matches what it is supposed to do:
a walker gets the bucket ladder, a one-shot trick gets its own measurement. Results
land in ``docs/hd1910/artifacts/accept_all/<policy>.json`` so a regression shows up
as a diff rather than a memory.

The point is to have one command that answers "is this build still good?", because
acceptance has until now lived in five separate scripts with different arguments and
nobody re-ran all of them after a training round.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/hd1910/artifacts/accept_all"
OUT.mkdir(parents=True, exist_ok=True)

# policy -> (script, task, extra args). The walkers share one ladder so their
# numbers stay directly comparable.
WALK = [
    ("walk_all", "Mjlab-Velocity-Flat-MicroDuck-HD1910", []),
    ("harsh", "Mjlab-Velstand-Harsh-MicroDuck-HD1910", []),
    ("headplant", "Mjlab-HeadPlant-MicroDuck-HD1910", []),
]
TRICKS = [
    ("roulade", "Mjlab-Roulade-Flat-MicroDuck-HD1910", ["accept_roulade.py"]),
    ("sitstand", "Mjlab-SitStand-Flat-MicroDuck-HD1910", ["accept_skill.py", "--skill", "sitstand"]),
    ("ballkick", "Mjlab-BallKick-Flat-MicroDuck-HD1910", ["accept_skill.py", "--skill", "ball_kick"]),
    ("groundpick", "Mjlab-GroundPick-Flat-MicroDuck-HD1910", ["accept_skill.py", "--skill", "ground_pick"]),
]


def run(cmd: list[str]) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def tail(text: str, n: int = 6) -> str:
    return "\n    ".join(text.strip().splitlines()[-n:])


def main() -> int:
    rows: list[tuple[str, str, str]] = []
    only = sys.argv[1] if len(sys.argv) > 1 else None

    for name, task, extra in WALK:
        if only and only not in name:
            continue
        onnx = ROOT / f"policies/hd1910_{name}.onnx"
        if not onnx.exists():
            rows.append((name, "SKIP", "policy missing"))
            continue
        out = OUT / f"{name}_velocity.json"
        rc, text = run([sys.executable, "scripts/accept_velocity.py", "--task", task,
                        "--onnx", str(onnx), "--num-envs", "16", "--seconds", "6",
                        "--out", str(out)])
        passed = "?"
        if out.exists():
            try:
                d = json.loads(out.read_text())
                results = d.get("results") or []
                total = len(results)
                ok = total - len(d.get("failed_checks") or [])
                passed = f"{ok}/{total}" if total else "?"
            except Exception:
                pass
        rows.append((name, "OK" if rc == 0 else "FAIL", f"walk buckets {passed}"))

    for name, task, extra in TRICKS:
        if only and only not in name:
            continue
        onnx = ROOT / f"policies/hd1910_{name}.onnx"
        if not onnx.exists():
            rows.append((name, "SKIP", "policy missing"))
            continue
        script, *rest = extra
        out = OUT / f"{name}.json"
        rc, text = run([sys.executable, f"scripts/{script}", "--task", task,
                        "--onnx", str(onnx), "--num-envs", "16", *rest, "--out", str(out)])
        detail = ""
        if out.exists():
            try:
                d = json.loads(out.read_text())
                detail = "; ".join(f"{k}={v}" for k, v in list(d.items())[:3]
                                   if isinstance(v, (int, float, str)))
            except Exception:
                pass
        rows.append((name, "OK" if rc == 0 else "FAIL", detail or tail(text, 2)))

    width = max(len(r[0]) for r in rows) + 2
    print(f"\n{'policy':<{width}}{'run':<6}detail")
    print("-" * (width + 46))
    for name, status, detail in rows:
        print(f"{name:<{width}}{status:<6}{detail[:60]}")
    bad = [r for r in rows if r[1] in ("FAIL", "?")]
    print(f"\n{len(rows) - len(bad)}/{len(rows)} ran clean" + ("" if not bad else f"  ⚠ {[r[0] for r in bad]}"))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
