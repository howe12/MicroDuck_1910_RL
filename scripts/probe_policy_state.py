#!/usr/bin/env python3
"""Probe what an episodic policy actually ends up doing.

The trick tasks log rewards, not state: a stand-up policy that never rises can
still collect ``pose_stand_*`` and finish its episode without terminating. This
runs the exported policy from the task's own reset distribution and reports
where the robot physically ends up.

    uv run --no-sync scripts/probe_policy_state.py \
        --task Mjlab-StandUp-Flat-MicroDuck-HD1910 --onnx policies/standup.onnx

Stands on ``trunk_base`` height and tilt, which are the two quantities the
rewards are proxies for:

    standing  z > 0.10 m and tilt < 20 deg      (HOME is ~0.115 m)
    crouched  0.075 < z <= 0.10
    down      z <= 0.075                        (prone/supine rest is 0.048-0.075)
"""

from __future__ import annotations

import argparse
import json
import sys

import numpy as np
import torch


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--onnx", required=True)
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--num-envs", type=int, default=32)
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--control-hz", type=float, default=50.0)
    ap.add_argument("--video", default=None, help="write an mp4 of the first 6 s")
    ap.add_argument("--force-down", action="store_true",
                    help="make every env start face-down (worst case, for video)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    import onnxruntime as ort
    from mjlab.envs import ManagerBasedRlEnv
    from mjlab.tasks.registry import load_env_cfg

    sess = ort.InferenceSession(args.onnx)
    inp, out = sess.get_inputs()[0].name, sess.get_outputs()[0].name
    batch = sess.get_inputs()[0].shape[0]

    cfg = load_env_cfg(args.task)
    cfg.scene.num_envs = args.num_envs
    cfg.auto_reset = False  # measure the fate of the first attempt, not a reset loop
    if args.force_down:
        ev = cfg.events.get("set_ground_state")
        if ev is not None:
            ev.params.update(face_down_prob=1.0, face_up_prob=0.0,
                             sitting_prob=0.0, standing_prob=0.0)
    if args.video:
        cfg.scene.num_envs = min(args.num_envs, 8)
    env = ManagerBasedRlEnv(cfg=cfg, device=args.device,
                            render_mode="rgb_array" if args.video else None)
    robot = env.scene["robot"]

    try:
        env.reset()
        z0 = robot.data.root_link_pos_w[:, 2].detach().cpu().numpy()
        zs, tilts, frames = [], [], []
        for _ in range(int(args.seconds * args.control_hz)):
            obs = env.obs_buf["actor"].detach().cpu().numpy().astype(np.float32)
            if batch == 1 and obs.shape[0] > 1:
                actions = np.concatenate(
                    [sess.run([out], {inp: obs[i: i + 1]})[0] for i in range(obs.shape[0])]
                ).astype(np.float32)
            else:
                actions = sess.run([out], {inp: obs})[0].astype(np.float32)
            env.step(torch.from_numpy(actions).to(env.device))

            z = robot.data.root_link_pos_w[:, 2].detach().cpu().numpy()
            g = robot.data.projected_gravity_b.detach().cpu().numpy()
            tilt = np.degrees(np.arccos(np.clip(-g[:, 2], -1, 1)))
            zs.append(z)
            if args.video and len(frames) < 200:
                try:
                    img = env.render()
                    if img is not None:
                        frames.append(img)
                except Exception:
                    pass
            tilts.append(tilt)

        z_end = zs[-1]
        tilt_end = tilts[-1]
        standing = (z_end > 0.10) & (tilt_end < 20)
        crouched = (z_end > 0.075) & (z_end <= 0.10)
        result = {
            "task": args.task,
            "onnx": args.onnx,
            "seconds": args.seconds,
            "num_envs": args.num_envs,
            "z_end_mean": round(float(z_end.mean()), 4),
            "z_end_p10": round(float(np.percentile(z_end, 10)), 4),
            "z_end_p90": round(float(np.percentile(z_end, 90)), 4),
            "tilt_end_mean_deg": round(float(tilt_end.mean()), 1),
            "z_max_reached_mean": round(float(np.max(zs, axis=0).mean()), 4),
            "z_start_mean": round(float(z0.mean()), 4),
            "started_down_ratio": round(float((z0 < 0.09).mean()), 3),
            "recovered_ratio": round(
                float(((z0 < 0.09) & (z_end > 0.10)).sum() / max((z0 < 0.09).sum(), 1)), 3
            ),
            "standing_ratio": round(float(standing.mean()), 3),
            "crouched_ratio": round(float(crouched.mean()), 3),
            "down_ratio": round(float(1 - standing.mean() - crouched.mean()), 3),
        }
        if frames:
            import imageio.v2 as imageio

            imageio.mimsave(args.video, frames, fps=50)
            print(f"video: {args.video} ({len(frames)} frames)")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.out:
            from pathlib import Path

            Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        env.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
