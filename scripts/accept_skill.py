#!/usr/bin/env python3
"""Acceptance for the one-shot skill policies: sit/stand, ball kick, ground pick, spin.

Each skill optimises a different objective, so each needs a different measured
outcome — the reward curve answers none of them.

    uv run --no-sync scripts/accept_skill.py --skill sitstand \\
        --task Mjlab-SitStand-Flat-MicroDuck-HD1910 --onnx policies/x.onnx

    sitstand    cmd 0 -> trunk ends near STAND_Z; cmd 1 -> near SIT_Z (0.060)
                reported separately for both directions, plus reset-between
    ballkick    ball displacement and peak speed after the kick, robot stays up
    groundpick  minimum mouth-tip height (how close it gets to the floor) and
                whether it returns to a standing trunk afterwards
    spin        accumulated trunk yaw over the episode

Measurements use the exported ONNX exactly like deployment, with auto_reset off
and the task's terminations cleared so a fall does not hide the attempt.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import torch

STAND_Z = 0.115
SIT_Z = 0.060
UPRIGHT_TILT_DEG = 25.0


def _closeup(width: int = 480, distance: float = 0.45):  # pragma: no cover
    from mjlab.viewer.viewer_config import ViewerConfig

    return ViewerConfig(
        origin_type=ViewerConfig.OriginType.ASSET_BODY,
        entity_name="robot",
        body_name="trunk_base",
        distance=distance,
        elevation=-12.0,
        azimuth=135.0,
        height=width,
        width=width,
        max_extra_envs=0,
    )


def _yaw(quat: np.ndarray) -> np.ndarray:
    w, x, y, z = quat[:, 0], quat[:, 1], quat[:, 2], quat[:, 3]
    return np.degrees(np.arctan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skill", required=True,
                    choices=("sitstand", "ballkick", "groundpick", "spin"))
    ap.add_argument("--task", required=True)
    ap.add_argument("--onnx", required=True)
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--num-envs", type=int, default=16)
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--video", default=None, help="record this skill to an mp4")
    ap.add_argument("--cam-distance", type=float, default=0.45,
                    help="camera distance in m; ball kick needs ~1.2 to keep the ball in frame")
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
    cfg.auto_reset = False
    cfg.terminations = {}          # a fall must not end the attempt we are measuring
    cfg.episode_length_s = args.seconds + 5.0

    if args.video:
        cfg.scene.num_envs = min(args.num_envs, 6)
        cfg.viewer = _closeup(distance=args.cam_distance)
    env = ManagerBasedRlEnv(cfg=cfg, device=args.device,
                            render_mode="rgb_array" if args.video else None)
    robot = env.scene["robot"]
    n = env.num_envs
    steps = int(args.seconds * 50)

    def set_posture(value: float):
        term = env.command_manager.get_term("twist")
        buf = getattr(term, "vel_command_b", None)
        if buf is not None:
            buf[:, 0] = value

    def rollout(posture: float | None = None, record: bool = False):
        env.reset()
        frames = []
        if posture is not None:
            set_posture(posture)
        zs, tilts, yaws, mouths = [], [], [], []
        ball_pos, ball_vel = [], []
        for _ in range(steps):
            obs = env.obs_buf["actor"].detach().cpu().numpy().astype(np.float32)
            if batch == 1 and obs.shape[0] > 1:
                actions = np.concatenate(
                    [sess.run([out], {inp: obs[i: i + 1]})[0] for i in range(obs.shape[0])]
                ).astype(np.float32)
            else:
                actions = sess.run([out], {inp: obs})[0].astype(np.float32)
            env.step(torch.from_numpy(actions).to(env.device))
            if posture is not None:
                set_posture(posture)

            if record and len(frames) < 200:
                try:
                    img = env.render()
                    if img is not None:
                        frames.append(img)
                except Exception:
                    pass
            zs.append(robot.data.root_link_pos_w[:, 2].detach().cpu().numpy())
            g = robot.data.projected_gravity_b.detach().cpu().numpy()
            tilts.append(np.degrees(np.arccos(np.clip(-g[:, 2], -1, 1))))
            q = robot.data.root_link_quat_w.detach().cpu().numpy()
            yaws.append(_yaw(q))
            if "mouth_tip" in [s.name for s in env.scene["robot"].sites.values()] if hasattr(env.scene["robot"], "sites") else True:
                pass
            try:
                ids, _ = robot.find_sites("mouth_tip")
                mouths.append(robot.data.site_pos_w[:, ids[0], 2].detach().cpu().numpy())
            except Exception:
                pass
            if "ball" in env.scene.entities:
                b = env.scene["ball"]
                ball_pos.append(b.data.root_link_pos_w.detach().cpu().numpy())
                ball_vel.append(b.data.root_link_lin_vel_w.detach().cpu().numpy())
        return dict(zs=zs, tilts=tilts, yaws=yaws, mouths=mouths,
                    ball_pos=ball_pos, ball_vel=ball_vel, frames=frames)

    def save_video(r, suffix=""):
        if not args.video or not r.get("frames"):
            return
        import imageio.v2 as imageio

        path = args.video if not suffix else str(Path(args.video).with_name(
            Path(args.video).stem + suffix + ".mp4"))
        imageio.mimsave(path, r["frames"], fps=50)
        result.setdefault("videos", []).append(path)
        print(f"video: {path} ({len(r['frames'])} frames)")

    result = {"skill": args.skill, "task": args.task, "onnx": args.onnx}
    try:
        if args.skill == "sitstand":
            for label, cmd in (("stand", 0.0), ("sit", 1.0)):
                r = rollout(posture=cmd, record=args.video is not None)
                save_video(r, f"_{label}")
                z_end = r["zs"][-1]
                tilt_end = r["tilts"][-1]
                result[f"{label}_z_end_mean"] = round(float(z_end.mean()), 4)
                result[f"{label}_tilt_end_deg"] = round(float(tilt_end.mean()), 1)
                result[f"{label}_ok_ratio"] = round(
                    float(((z_end > 0.10) if cmd == 0.0 else (z_end < 0.075)).mean()), 3
                )
        elif args.skill == "ballkick":
            r = rollout(record=args.video is not None)
            save_video(r)
            p0, p1 = r["ball_pos"][0], r["ball_pos"][-1]
            disp = np.linalg.norm(p1 - p0, axis=1)
            speed = np.array([np.linalg.norm(v, axis=1) for v in r["ball_vel"]])
            result["ball_displacement_mean_m"] = round(float(disp.mean()), 3)
            result["ball_displacement_max_m"] = round(float(disp.max()), 3)
            result["ball_peak_speed_mean_mps"] = round(float(speed.max(axis=0).mean()), 3)
            result["robot_fell_ratio"] = round(
                float((np.array(r["tilts"]) > 60).any(axis=0).mean()), 3)
        elif args.skill == "groundpick":
            r = rollout(record=args.video is not None)
            save_video(r)
            mouths = np.array(r["mouths"])
            result["mouth_min_height_mean_m"] = round(float(mouths.min(axis=0).mean()), 4)
            result["mouth_min_height_best_m"] = round(float(mouths.min()), 4)
            result["z_end_mean"] = round(float(r["zs"][-1].mean()), 4)
            result["tilt_end_deg"] = round(float(r["tilts"][-1].mean()), 1)
            result["returned_upright_ratio"] = round(
                float(((r["zs"][-1] > 0.10) & (r["tilts"][-1] < UPRIGHT_TILT_DEG)).mean()), 3)
        else:  # spin
            r = rollout(record=args.video is not None)
            save_video(r)
            yaw = np.degrees(np.unwrap(np.radians(np.array(r["yaws"])), axis=0))
            total = np.abs(yaw[-1] - yaw[0])
            result["yaw_turned_mean_deg"] = round(float(total.mean()), 1)
            result["yaw_turned_max_deg"] = round(float(total.max()), 1)
            result["full_turn_ratio"] = round(float((total >= 340).mean()), 3)
            result["robot_fell_ratio"] = round(
                float((np.array(r["tilts"]) > 60).any(axis=0).mean()), 3)
    finally:
        env.close()

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
