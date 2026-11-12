#!/usr/bin/env python3
"""Acceptance for the walk-and-recover policy (VelStand).

The reward curve cannot answer the only question that matters for this task:
*does it get back up and keep walking?* This script runs the exported policy,
lets it walk, knocks it down on purpose, and measures the recovery.

    uv run --no-sync scripts/accept_velstand.py \
        --onnx policies/velstand.onnx --out docs/hd1910/artifacts/velstand_accept.json

Per episode (N envs run in lockstep):

    t < knock_time          walk with a pinned forward command
    t = knock_time          teleport every env into a randomly chosen down pose
                            (face-down / face-up / sitting)
    t > knock_time          free run; measure time to first stand and the speed
                            reached afterwards

Reported: fall→stand success rate, median recovery time, post-recovery speed,
and the share of robots still down at the end of the horizon.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import torch

STAND_Z = 0.10
STAND_TILT_DEG = 20.0


def _closeup_viewer(width: int = 480):
    from mjlab.viewer.viewer_config import ViewerConfig

    return ViewerConfig(
        origin_type=ViewerConfig.OriginType.ASSET_BODY,
        entity_name="robot",
        body_name="trunk_base",
        distance=0.45,
        elevation=-12.0,
        azimuth=135.0,
        height=width,
        width=width,
        max_extra_envs=0,
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--onnx", required=True)
    ap.add_argument("--task", default="Mjlab-VelStand-Flat-MicroDuck-HD1910")
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--num-envs", type=int, default=16)
    ap.add_argument("--cmd-vx", type=float, default=0.25)
    ap.add_argument("--walk-s", type=float, default=3.0)
    ap.add_argument("--recover-s", type=float, default=8.0)
    ap.add_argument("--video", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    import onnxruntime as ort
    from mjlab.envs import ManagerBasedRlEnv
    from mjlab.tasks.registry import load_env_cfg

    from mjlab_microduck.tasks import mdp as microduck_mdp

    sess = ort.InferenceSession(args.onnx)
    inp, out = sess.get_inputs()[0].name, sess.get_outputs()[0].name
    batch = sess.get_inputs()[0].shape[0]

    cfg = load_env_cfg(args.task)
    cfg.scene.num_envs = args.num_envs
    cfg.auto_reset = False  # one continuous episode; a reset would fake the recovery
    # The knock-down is the point of the test: a ``fell_over`` termination would
    # end the episode at exactly the moment we want to watch, and the task's own
    # episode length is shorter than walk+recover on the StandUp variant.
    cfg.terminations = {}
    cfg.episode_length_s = args.walk_s + args.recover_s + 5.0
    if args.video:
        cfg.scene.num_envs = min(args.num_envs, 6)
        cfg.viewer = _closeup_viewer()

    env = ManagerBasedRlEnv(cfg=cfg, device=args.device,
                            render_mode="rgb_array" if args.video else None)
    robot = env.scene["robot"]
    n = env.num_envs
    cmd = torch.tensor([args.cmd_vx, 0.0, 0.0], device=env.device)

    def pin():
        term = env.command_manager.get_term("twist")
        buf = getattr(term, "vel_command_b", None)
        if buf is not None:
            buf[:] = cmd

    def state():
        z = robot.data.root_link_pos_w[:, 2].detach().cpu().numpy()
        g = robot.data.projected_gravity_b.detach().cpu().numpy()
        tilt = np.degrees(np.arccos(np.clip(-g[:, 2], -1, 1)))
        v = robot.data.root_link_lin_vel_b[:, :2].detach().cpu().numpy()
        return z, tilt, v

    def step(actions=None):
        obs = env.obs_buf["actor"].detach().cpu().numpy().astype(np.float32)
        if actions is None:
            if batch == 1 and obs.shape[0] > 1:
                actions = np.concatenate(
                    [sess.run([out], {inp: obs[i: i + 1]})[0] for i in range(obs.shape[0])]
                ).astype(np.float32)
            else:
                actions = sess.run([out], {inp: obs})[0].astype(np.float32)
        env.step(torch.from_numpy(actions).to(env.device))
        pin()

    frames = []
    try:
        env.reset()
        pin()

        # ── phase 1: walk ────────────────────────────────────────────────
        speeds = []
        for _ in range(int(args.walk_s * 50)):
            step()
            speeds.append(state()[2][:, 0].mean())
            if args.video and len(frames) < 120:
                img = env.render()
                if img is not None:
                    frames.append(img)
        walk_speed = float(np.mean(speeds[-50:]))

        # ── knock down: reuse the task's own ground-state reset ──────────
        env_ids = torch.arange(n, device=env.device)
        microduck_mdp.set_random_ground_state(
            env, env_ids,
            face_down_prob=0.4, face_up_prob=0.3, sitting_prob=0.3, standing_prob=0.0,
        )
        # The written qpos only reaches the data buffers on the next sim step.
        step()
        z, tilt, _ = state()
        started_down = (z < STAND_Z) | (tilt > STAND_TILT_DEG)

        # ── phase 2: recover ─────────────────────────────────────────────
        recovery_step = np.full(n, -1)
        for k in range(int(args.recover_s * 50)):
            step()
            z, tilt, v = state()
            standing = (z > STAND_Z) & (tilt < STAND_TILT_DEG)
            newly = standing & (recovery_step < 0)
            recovery_step[newly] = k
            if args.video and len(frames) < 200:
                img = env.render()
                if img is not None:
                    frames.append(img)

        z, tilt, v = state()
        standing_now = (z > STAND_Z) & (tilt < STAND_TILT_DEG)
        recovered = (recovery_step >= 0) & started_down
        post_speed = float(v[standing_now][:, 0].mean()) if standing_now.any() else 0.0
        times = recovery_step[recovered] / 50.0

        result = {
            "onnx": args.onnx,
            "cmd_vx": args.cmd_vx,
            "walk_speed_mps": round(walk_speed, 3),
            "knocked_down_ratio": round(float(started_down.mean()), 3),
            "recovered_ratio": round(
                float(recovered.sum() / max(started_down.sum(), 1)), 3
            ),
            "recovery_time_median_s": round(float(np.median(times)), 2) if len(times) else None,
            "recovery_time_p90_s": round(float(np.percentile(times, 90)), 2) if len(times) else None,
            "standing_at_end_ratio": round(float(standing_now.mean()), 3),
            "post_recovery_speed_mps": round(post_speed, 3),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if frames:
            import imageio.v2 as imageio

            imageio.mimsave(args.video, frames, fps=50)
            print(f"video: {args.video} ({len(frames)} frames)")
        if args.out:
            Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"wrote {args.out}")
    finally:
        env.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
