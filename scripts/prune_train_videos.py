#!/usr/bin/env python3
"""Prune the training video stream down to milestones.

Training with ``--video --video-interval 2400`` writes a 6 s clip every 100
iterations — useful while babysitting a run, pointless once it is moving. This
keeps one clip every ``--keep-every`` steps, copies it into
``docs/hd1910/videos/`` for the record, and deletes the rest.

The whole 5000-iteration run only produces ~11 MB, so this is housekeeping, not
a disk-space necessity. Run it on a timer while training:

    while true; do python scripts/prune_train_videos.py; sleep 300; done
"""

from __future__ import annotations

import argparse
import re
import shutil
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_ROOT = ROOT / "logs" / "rsl_rl" / "velocity_hd1910"
ARCHIVE = ROOT / "docs" / "hd1910" / "videos"
STEP_RE = re.compile(r"rl-video-step-(\d+)\.mp4$")
STEPS_PER_ITER = 24


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-every", type=int, default=12000,
                    help="keep one clip every N env steps (default 12000 = 500 iterations)")
    ap.add_argument("--min-age", type=float, default=60.0,
                    help="never touch a file written in the last N seconds")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    ARCHIVE.mkdir(parents=True, exist_ok=True)
    now = time.time()
    kept = removed = 0

    for vid_dir in sorted(LOG_ROOT.glob("*/videos/train")):
        files = []
        for f in vid_dir.glob("rl-video-step-*.mp4"):
            m = STEP_RE.search(f.name)
            if m:
                files.append((int(m.group(1)), f))
        files.sort()
        if not files:
            continue
        newest = files[-1][1]

        for step, f in files:
            if now - f.stat().st_mtime < args.min_age:
                continue  # possibly still being written
            milestone = step % args.keep_every == 0
            if milestone:
                dst = ARCHIVE / f"iter{step // STEPS_PER_ITER:04d}.mp4"
                if args.dry_run:
                    print(f"keep   {f.name} -> {dst.name}")
                else:
                    shutil.copy2(f, dst)
                kept += 1
            elif f != newest:
                if args.dry_run:
                    print(f"remove {f.name}")
                else:
                    f.unlink()
                removed += 1

    print(f"[prune] kept {kept} milestone(s), removed {removed} intermediate clip(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
