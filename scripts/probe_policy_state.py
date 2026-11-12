#!/usr/bin/env python3
"""Probe what an episodic policy actually ends up doing.

The trick tasks log rewards, not state: a stand-up policy that never rises can
still collect ``pose_stand_*`` and finish its episode without terminating. This
runs the exported policy from a *forced* reset pose and reports where the robot
physically ends up.

    # one pose
    uv run --no-sync scripts/probe_policy_state.py \
        --task Mjlab-StandUp-Flat-MicroDuck-HD1910 --onnx policies/standup.onnx \
        --force-state up

    # acceptance: all four poses in one table
    uv run --no-sync scripts/probe_policy_state.py \
        --task Mjlab-StandUp-Flat-MicroDuck-HD1910 --onnx policies/standup.onnx \
        --force-state all --out docs/hd1910/artifacts/standup_accept.json

``trunk_base`` height and tilt are the quantities the rewards are proxies for:

    standing  z > 0.10 m and tilt < 20 deg      (HOME is ~0.115 m)
    crouched  0.075 < z <= 0.10
    down      z <= 0.075                        (prone/supine rest is 0.048-0.075)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import torch

STATES = ("down", "up", "sitting", "standing")


def _closeup_viewer(width: int = 480):
    """Track the trunk at ~45 cm so the 25 cm robot fills the frame.

    The default ViewerConfig (distance 5 m, 320x240, 2 extra envs) renders the
    robot as a handful of pixels surrounded by neighbours — useless for review.
    """
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


def probe(task, onnx, device, num_envs, seconds, control_hz, forced, video=None):
    import onnxruntime as ort
    from mjlab.envs import ManagerBasedRlEnv
    from mjlab.tasks.registry import load_env_cfg

    sess = ort.InferenceSession(onnx)
    inp, out = sess.get_inputs()[0].name, sess.get_outputs()[0].name
    batch = sess.get_inputs()[0].shape[0]

    cfg = load_env_cfg(task)
    cfg.scene.num_envs = num_envs
    cfg.auto_reset = False  # measure the fate of the first attempt, not a reset loop

    if forced:
        ev = cfg.events.get("set_ground_state")
        if ev is not None:
            ev.params.update(
                face_down_prob=1.0 if forced == "down" else 0.0,
                face_up_prob=1.0 if forced == "up" else 0.0,
                sitting_prob=1.0 if forced == "sitting" else 0.0,
                standing_prob=1.0 if forced == "standing" else 0.0,
            )
            # The ground_state_mix curriculum rewrites those probabilities on
            # every step — drop it or the override survives exactly one reset.
            cfg.curriculum.pop("ground_state_mix", None)

    if video:
        cfg.viewer = _closeup_viewer()
    env = ManagerBasedRlEnv(cfg=cfg, device=device,
                            render_mode="rgb_array" if video else None)
    robot = env.scene["robot"]

    try:
        env.reset()
        z0 = robot.data.root_link_pos_w[:, 2].detach().cpu().numpy()
        zs, tilts, frames = [], [], []
        for _ in range(int(seconds * control_hz)):
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
            tilts.append(tilt)
            if video and len(frames) < 200:
                try:
                    img = env.render()
                    if img is not None:
                        frames.append(img)
                except Exception:
                    pass
    finally:
        env.close()

    z_end, tilt_end = zs[-1], tilts[-1]
    standing = (z_end > 0.10) & (tilt_end < 20)
    crouched = (z_end > 0.075) & (z_end <= 0.10)
    started_down = z0 < 0.09
    result = {
        "forced_state": forced or "task default",
        "z_start_mean": round(float(z0.mean()), 4),
        "started_down_ratio": round(float(started_down.mean()), 3),
        "z_end_mean": round(float(z_end.mean()), 4),
        "z_max_reached_mean": round(float(np.max(zs, axis=0).mean()), 4),
        "tilt_end_mean_deg": round(float(tilt_end.mean()), 1),
        "recovered_ratio": round(
            float((started_down & (z_end > 0.10)).sum() / max(started_down.sum(), 1)), 3
        ),
        "standing_ratio": round(float(standing.mean()), 3),
        "crouched_ratio": round(float(crouched.mean()), 3),
        "down_ratio": round(float(1 - standing.mean() - crouched.mean()), 3),
    }
    if frames:
        import imageio.v2 as imageio

        imageio.mimsave(video, frames, fps=int(control_hz))
        print(f"video: {video} ({len(frames)} frames)")
        result["video"] = str(video)
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--onnx", required=True)
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--num-envs", type=int, default=32)
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--control-hz", type=float, default=50.0)
    ap.add_argument("--force-state", choices=(*STATES, "all"), default=None,
                    help="force every env to start in this ground state; 'all' loops the four")
    ap.add_argument("--video", default=None, help="write an mp4 (first 200 control steps)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    states = STATES if args.force_state == "all" else (args.force_state,)
    results = []
    for state in states:
        video = args.video
        if video and len(states) > 1:
            video = f"{Path(video).with_suffix('')}_{state}.mp4"
        results.append(
            probe(args.task, args.onnx, args.device, args.num_envs, args.seconds,
                  args.control_hz, state, video)
        )

    if len(results) > 1:
        hdr = f"{'state':10s}{'z_start':>9}{'z_end':>8}{'tilt':>7}{'recovered':>11}{'standing':>10}{'down':>7}"
        print(hdr)
        print("-" * len(hdr))
        for r in results:
            print(f"{r['forced_state']:10s}{r['z_start_mean']:9.3f}{r['z_end_mean']:8.3f}"
                  f"{r['tilt_end_mean_deg']:7.1f}{r['recovered_ratio']:11.2f}"
                  f"{r['standing_ratio']:10.2f}{r['down_ratio']:7.2f}")
        print()
    for r in results:
        print(json.dumps(r, ensure_ascii=False))

    if args.out:
        Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
