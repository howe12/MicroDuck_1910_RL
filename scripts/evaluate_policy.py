#!/usr/bin/env python3
"""Per-command-axis evaluation of an exported policy.

Training logs a single ``error_vel_xy`` that averages forward, lateral and
turning commands together — which hides *which* axis the policy cannot do.
This script pins the command to one bucket at a time and reports the achieved
velocity, the error, the gait metrics and the fall rate for each.

    uv run --no-sync scripts/evaluate_policy.py --onnx policies/hd1910_stand_v1.onnx
    uv run --no-sync scripts/evaluate_policy.py --onnx run.onnx --buckets fwd_slow,lat,wz

Buckets (name: [vx, vy, wz]):
    stand     0.00, 0.00, 0.0
    fwd_slow  0.15, 0.00, 0.0
    fwd_fast  0.40, 0.00, 0.0
    back     -0.20, 0.00, 0.0
    lat       0.00, 0.20, 0.0     (left)
    wz        0.00, 0.00, 0.8     (yaw left)
    mix       0.25, 0.10, 0.4
"""

from __future__ import annotations

import argparse
import json
import sys

import numpy as np
import torch

BUCKETS = {
    "stand": (0.00, 0.00, 0.0),
    "fwd_slow": (0.15, 0.00, 0.0),
    "fwd_fast": (0.40, 0.00, 0.0),
    "back": (-0.20, 0.00, 0.0),
    "lat": (0.00, 0.20, 0.0),
    "wz_slow": (0.00, 0.00, 0.3),
    "wz": (0.00, 0.00, 0.8),
    "mix": (0.25, 0.10, 0.4),
}


def build_env(task: str, device: str, num_envs: int, render: bool = False):
    from mjlab.envs import ManagerBasedRlEnv
    from mjlab.tasks.registry import load_env_cfg

    cfg = load_env_cfg(task)
    cfg.scene.num_envs = num_envs
    cfg.auto_reset = True
    # Measurement, not robustness: pushes would dominate the error numbers.
    for name in ("push_robot", "push_by_setting_velocity"):
        cfg.events.pop(name, None)
    # The training recipe uses heading_command=True, which rewrites the yaw slot
    # from a heading-P controller every step — a pinned yaw command would be
    # silently overwritten. Evaluation wants the raw yaw rate.
    cfg.commands["twist"].heading_command = False
    cfg.commands["twist"].ranges.heading = None
    if render:
        cfg.viewer = _closeup_viewer()
    return ManagerBasedRlEnv(cfg=cfg, device=device, render_mode="rgb_array" if render else None)


def pin_command(env, vec):
    """Overwrite the twist command every step (resampling is irrelevant then)."""
    term = env.command_manager.get_term("twist")
    buf = getattr(term, "vel_command_b", None)
    assert buf is not None, f"{type(term).__name__} has no vel_command_b buffer"
    buf[:] = torch.tensor(vec, device=env.device, dtype=buf.dtype)
    # Keep the 'standing' flag consistent with the pinned command.
    if hasattr(term, "is_standing_env"):
        standing = float((vec[0] ** 2 + vec[1] ** 2) ** 0.5) < 0.05 and abs(vec[2]) < 0.05
        term.is_standing_env[:] = standing


def run_bucket(env, sess, name, vec, seconds, control_hz=50.0, record=False):
    inp = sess.get_inputs()[0].name
    out = sess.get_outputs()[0].name
    batch = sess.get_inputs()[0].shape[0]
    env.reset()
    pin_command(env, vec)

    robot = env.scene["robot"]
    cmd = np.array(vec, dtype=np.float64)
    steps = int(seconds * control_hz)
    vx, vy, wz, zs, tilts = [], [], [], [], []
    frames = []
    falls = 0

    for _ in range(steps):
        obs = env.obs_buf["actor"].detach().cpu().numpy().astype(np.float32)
        if batch == 1 and obs.shape[0] > 1:
            # Exported ONNX has a fixed batch of 1 — run env-by-env (the net is tiny).
            actions = np.concatenate(
                [sess.run([out], {inp: obs[i: i + 1]})[0] for i in range(obs.shape[0])], axis=0
            ).astype(np.float32)
        else:
            actions = sess.run([out], {inp: obs})[0].astype(np.float32)
        env.step(torch.from_numpy(actions).to(env.device))
        pin_command(env, vec)  # resets may resample

        vel_b = robot.data.root_link_lin_vel_b[:, :2].detach().cpu().numpy()
        ang_b = robot.data.root_link_ang_vel_b[:, 2:3].detach().cpu().numpy()
        vx.append(vel_b[:, 0].mean())
        vy.append(vel_b[:, 1].mean())
        wz.append(ang_b[:, 0].mean())
        g = robot.data.projected_gravity_b.detach().cpu().numpy()
        tilt = np.degrees(np.arccos(np.clip(-g[:, 2], -1, 1)))
        tilts.append(tilt.mean())
        falls += int((tilt > 60).sum())
        if record and frames is not None and len(frames) < 150:
            try:
                img = env.render()
                if img is not None:
                    frames.append(img)
            except Exception:
                frames = None
        zs.append(robot.data.root_link_pos_w[:, 2].detach().cpu().numpy().mean())

    def err(actual, target):
        return float(np.mean(np.abs(np.array(actual) - target)))

    return {
        "bucket": name,
        "cmd": {"vx": vec[0], "vy": vec[1], "wz": vec[2]},
        "achieved_vx": round(float(np.mean(vx)), 3),
        "achieved_vy": round(float(np.mean(vy)), 3),
        "achieved_wz": round(float(np.mean(wz)), 3),
        "err_vx": round(err(vx, cmd[0]), 3),
        "err_vy": round(err(vy, cmd[1]), 3),
        "err_wz": round(err(wz, cmd[2]), 3),
        "trunk_z_m": round(float(np.mean(zs)), 3),
        "tilt_deg": round(float(np.mean(tilts)), 1),
        "fall_ratio": round(falls / (steps * env.num_envs), 3),
        "_frames": frames,
    }


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

def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--onnx", required=True)
    ap.add_argument("--task", default="Mjlab-Velocity-Flat-MicroDuck-HD1910")
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--num-envs", type=int, default=16)
    ap.add_argument("--seconds", type=float, default=8.0)
    ap.add_argument("--buckets", default=",".join(BUCKETS))
    ap.add_argument("--video", default=None, help="record this bucket to an mp4")
    ap.add_argument("--video-bucket", default="fwd_fast")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    import onnxruntime as ort

    sess = ort.InferenceSession(args.onnx)
    shape = sess.get_inputs()[0].shape
    if shape[-1] != 61:
        raise SystemExit(f"{args.onnx}: expected 61-D input, got {shape}")

    env = build_env(args.task, args.device, args.num_envs, render=args.video is not None)
    results = []
    try:
        for name in args.buckets.split(","):
            name = name.strip()
            if not name:
                continue
            r = run_bucket(env, sess, name, BUCKETS[name], args.seconds,
                           record=(args.video is not None and name == args.video_bucket))
            frames = r.pop("_frames", None)
            if frames and args.video:
                import imageio.v2 as imageio

                imageio.mimsave(args.video, frames, fps=50)
                print(f"video: {args.video} ({len(frames)} frames)")
            results.append(r)
            print(json.dumps(r, ensure_ascii=False))
    finally:
        env.close()

    print("\n bucket      cmd(vx,vy,wz)      achieved          err          fall")
    for r in results:
        print(f" {r['bucket']:10s} {r['cmd']['vx']:+.2f},{r['cmd']['vy']:+.2f},{r['cmd']['wz']:+.2f}   "
              f"{r['achieved_vx']:+.2f},{r['achieved_vy']:+.2f},{r['achieved_wz']:+.2f}   "
              f"{r['err_vx']:.2f},{r['err_vy']:.2f},{r['err_wz']:.2f}   {r['fall_ratio']:.2f}")

    if args.out:
        from pathlib import Path

        Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
