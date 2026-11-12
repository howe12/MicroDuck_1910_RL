#!/usr/bin/env python3
"""Acceptance for the forward-roll policy.

A roulade is one full rotation about the pitch axis. The reward curve says
nothing about whether the robot actually went over its head, so this measures
the trunk pitch angle directly:

    pitch(t) = atan2(g_x, -g_z)      from the projected gravity in the body frame

and unwraps it over the episode. A completed roll traverses >= 340 deg and ends
within the standing envelope.

    uv run --no-sync scripts/accept_roulade.py \
        --onnx policies/roulade.onnx --out docs/hd1910/artifacts/roulade_accept.json

Reported per run: max unwrapped pitch span, completed-roll ratio, end-standing
ratio, and the fraction of time the trunk was inverted (|pitch| > 120 deg),
which is the part that only happens if the robot really goes over.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import torch

STAND_Z = 0.10
STAND_TILT_DEG = 25.0
ROLL_DEG = 340.0
INVERTED_DEG = 120.0


def _closeup_viewer(width: int = 480):
    from mjlab.viewer.viewer_config import ViewerConfig

    return ViewerConfig(
        origin_type=ViewerConfig.OriginType.ASSET_BODY,
        entity_name="robot",
        body_name="trunk_base",
        distance=0.50,
        elevation=-10.0,
        azimuth=110.0,
        height=width,
        width=width,
        max_extra_envs=0,
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--onnx", required=True)
    ap.add_argument("--task", default="Mjlab-Roulade-Flat-MicroDuck-HD1910")
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--num-envs", type=int, default=16)
    ap.add_argument("--seconds", type=float, default=8.0)
    ap.add_argument("--video", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    import onnxruntime as ort
    from mjlab.envs import ManagerBasedRlEnv
    from mjlab.tasks.registry import load_env_cfg

    sess = ort.InferenceSession(args.onnx)
    inp, out = sess.get_inputs()[0].name, sess.get_outputs()[0].name
    batch = sess.get_inputs()[0].shape[0]

    cfg = load_env_cfg(args.task)
    cfg.scene.num_envs = min(args.num_envs, 8) if args.video else args.num_envs
    cfg.auto_reset = False  # keep the whole attempt, a reset would hide a botched landing
    # The task's own episode is 5 s; a longer measurement window would trip the
    # time_out termination and step() then refuses (auto_reset=False). The
    # measurement is pitch-based, so the task's termination logic is not needed.
    cfg.terminations = {}
    cfg.episode_length_s = args.seconds + 5.0
    if args.video:
        cfg.viewer = _closeup_viewer()

    env = ManagerBasedRlEnv(cfg=cfg, device=args.device,
                            render_mode="rgb_array" if args.video else None)
    robot = env.scene["robot"]

    frames = []
    try:
        env.reset()
        pitches = []
        for _ in range(int(args.seconds * 50)):
            obs = env.obs_buf["actor"].detach().cpu().numpy().astype(np.float32)
            if batch == 1 and obs.shape[0] > 1:
                actions = np.concatenate(
                    [sess.run([out], {inp: obs[i: i + 1]})[0] for i in range(obs.shape[0])]
                ).astype(np.float32)
            else:
                actions = sess.run([out], {inp: obs})[0].astype(np.float32)
            env.step(torch.from_numpy(actions).to(env.device))

            g = robot.data.projected_gravity_b.detach().cpu().numpy()
            pitch = np.degrees(np.arctan2(g[:, 0], -g[:, 2]))
            pitches.append(pitch)
            if args.video and len(frames) < 250:
                img = env.render()
                if img is not None:
                    frames.append(img)
        z_end = robot.data.root_link_pos_w[:, 2].detach().cpu().numpy()
        g = robot.data.projected_gravity_b.detach().cpu().numpy()
        tilt_end = np.degrees(np.arccos(np.clip(-g[:, 2], -1, 1)))
    finally:
        env.close()

    p = np.array(pitches)                      # (T, N)
    unwrapped = np.degrees(np.unwrap(np.radians(p), axis=0))
    span = unwrapped.max(axis=0) - unwrapped.min(axis=0)
    inverted = (np.abs(p) > INVERTED_DEG).mean(axis=0)
    standing_end = (z_end > STAND_Z) & (tilt_end < STAND_TILT_DEG)

    result = {
        "onnx": args.onnx,
        "seconds": args.seconds,
        "pitch_span_median_deg": round(float(np.median(span)), 1),
        "pitch_span_p90_deg": round(float(np.percentile(span, 90)), 1),
        "completed_roll_ratio": round(float((span >= ROLL_DEG).mean()), 3),
        "inverted_time_ratio": round(float(inverted.mean()), 3),
        "standing_at_end_ratio": round(float(standing_end.mean()), 3),
        "z_end_mean": round(float(z_end.mean()), 4),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if frames:
        import imageio.v2 as imageio

        imageio.mimsave(args.video, frames, fps=50)
        print(f"video: {args.video} ({len(frames)} frames)")
    if args.out:
        Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
