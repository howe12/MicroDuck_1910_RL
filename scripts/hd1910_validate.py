#!/usr/bin/env python3
"""Staged validation for the HD1910 robot (deliverable G).

Four stages, cheapest first. Each one answers a question that a training run
cannot answer on its own, and each one prints numbers you can paste into the
acceptance record:

    home   Is the HOME pose still a stable equilibrium after the servo swap?
           (3 s hold from a noisy reset, report TILT not just height — a
           settle test that only checks z calls a fallen robot "resting".)
    drift  Does the BAM HD1910 model hold position under gravity?
           (10 s hold, report per-joint creep and the voltage sag envelope.)
    step   What does a single joint do to a step command?
           (per-joint +0.2 rad step: rise time, overshoot, steady-state error.
           This is the curve to overlay against scripts/testbench_sim2real.py
           recordings from the real HD1910.)
    walk   Does a trained ONNX policy survive? (optional, --onnx)

Nothing here trains anything, and none of it is a real-robot acceptance test:
stages run in simulation on the model selected by the mass table. If that table
is still `placeholder`, every number below describes the XL330 dynamics.

Usage:
    uv run --no-sync scripts/hd1910_validate.py --stage home
    uv run --no-sync scripts/hd1910_validate.py --stage step --out /tmp/step.json
    uv run --no-sync scripts/hd1910_validate.py --stage walk --onnx walk.onnx
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import torch

CONTROL_HZ = 50.0  # must match the env: 1 / (timestep * decimation)


# --------------------------------------------------------------------------- #
# Env plumbing
# --------------------------------------------------------------------------- #
def make_env(task: str, device: str, *, keep_terminations: bool, zero_gravity: bool = False):
    from mjlab.envs import ManagerBasedRlEnv
    from mjlab.tasks.registry import load_env_cfg

    cfg = load_env_cfg(task)
    if zero_gravity:
        # Isolates the servo from the standing load: a joint that tracks here
        # but not while standing is torque-limited, not mis-configured.
        cfg.sim.mujoco.gravity = (0.0, 0.0, 0.0)
    cfg.scene.num_envs = 1
    cfg.auto_reset = False  # a fall must not silently reset mid-measurement
    if not keep_terminations:
        # Diagnostics should measure the failure, not be interrupted by it.
        cfg.terminations = {
            k: v for k, v in cfg.terminations.items() if "nan" in k.lower()
        }
    env = ManagerBasedRlEnv(cfg=cfg, device=device)
    return env


def robot(env):
    return env.scene["robot"]


def tilt_deg(env) -> float:
    """Angle between the trunk +z axis and world +z, in degrees."""
    g = robot(env).data.projected_gravity_b[0]  # gravity in body frame, unit-ish
    # At rest, body-frame gravity is (0, 0, -1); tilt = angle away from that.
    gz = float(torch.clamp(-g[2], -1.0, 1.0))
    return math.degrees(math.acos(gz))


def trunk_z(env) -> float:
    return float(robot(env).data.root_link_pos_w[0, 2])


def joint_pos(env) -> np.ndarray:
    return robot(env).data.joint_pos[0].detach().cpu().numpy()


def act(env, action: torch.Tensor):
    """Step the env without letting a termination abort the measurement."""
    if action.dim() == 1:
        action = action.unsqueeze(0)
    return env.step(action.to(env.device))


# --------------------------------------------------------------------------- #
# Stages
# --------------------------------------------------------------------------- #
def stage_home(env, seconds: float = 3.0) -> dict:
    """Hold the default pose from a noisy reset; check it is an equilibrium."""
    env.reset()
    zero = torch.zeros(1, 14)
    zs, tilts = [], []
    for _ in range(int(seconds * CONTROL_HZ)):
        act(env, zero)
        zs.append(trunk_z(env))
        tilts.append(tilt_deg(env))
    q = joint_pos(env)
    q_default = robot(env).data.default_joint_pos[0].detach().cpu().numpy()
    drift = np.abs(q - q_default)
    return {
        "stage": "home",
        "seconds": seconds,
        "trunk_z_mean_m": round(float(np.mean(zs)), 4),
        "trunk_z_final_m": round(zs[-1], 4),
        "tilt_mean_deg": round(float(np.mean(tilts)), 2),
        "tilt_max_deg": round(float(np.max(tilts)), 2),
        "max_joint_drift_rad": round(float(drift.max()), 4),
        "verdict": "stable" if np.max(tilts) < 30 and zs[-1] > 0.05 else "NOT STABLE",
    }


def stage_drift(env, seconds: float = 10.0) -> dict:
    """Long hold: does the BAM model creep, and does the pose actually survive?

    A 3 s hold can look stable while a soft joint is still sagging; 10 s is
    where a kp that is too low for the load shows up as a fall.
    """
    env.reset()
    zero = torch.zeros(1, 14)
    q0 = joint_pos(env)
    zs, tilts = [], []
    fell_at = None
    for i in range(int(seconds * CONTROL_HZ)):
        act(env, zero)
        zs.append(trunk_z(env))
        t = tilt_deg(env)
        tilts.append(t)
        if fell_at is None and t > 60.0:
            fell_at = i / CONTROL_HZ
    q1 = joint_pos(env)
    creep = np.abs(q1 - q0)
    return {
        "stage": "drift",
        "seconds": seconds,
        "trunk_z_start_m": round(zs[0], 4),
        "trunk_z_end_m": round(zs[-1], 4),
        "trunk_z_delta_mm": round((zs[-1] - zs[0]) * 1000, 2),
        "tilt_max_deg": round(float(np.max(tilts)), 2),
        "max_joint_creep_mrad": round(float(creep.max()) * 1000, 2),
        "mean_joint_creep_mrad": round(float(creep.mean()) * 1000, 2),
        "fell_at_s": fell_at,
        "verdict": "held" if fell_at is None else f"FELL at {fell_at:.1f}s",
    }


def stage_step(env, amplitude: float = 0.2, seconds: float = 2.0, hold: float = 0.5, tag: str = "standing") -> dict:
    """Per-joint step response of the BAM HD1910 model."""
    names = list(robot(env).joint_names)
    servo_names = [n for n in names if not n.startswith("passive_")]
    out = []
    n_hold = int(hold * CONTROL_HZ)
    n_total = int((hold + seconds) * CONTROL_HZ)

    for j, name in enumerate(servo_names):
        env.reset()
        zero = torch.zeros(1, 14)
        for _ in range(n_hold):
            act(env, zero)
        q0 = joint_pos(env)[j]

        action = torch.zeros(1, 14)
        action[0, j] = amplitude
        trace = []
        for _ in range(n_total - n_hold):
            act(env, action)
            trace.append(joint_pos(env)[j])
        trace = np.asarray(trace)
        target = q0 + amplitude
        final = float(np.mean(trace[-10:]))
        peak = float(trace[np.argmax(np.abs(trace - q0))])
        overshoot = (abs(peak - q0) - abs(amplitude)) / abs(amplitude) * 100.0
        # 10-90 % rise time on the first crossing of each threshold.
        d = trace - q0
        lo, hi = 0.1 * amplitude, 0.9 * amplitude
        try:
            t10 = int(np.argmax(d >= lo)) / CONTROL_HZ
            t90 = int(np.argmax(d >= hi)) / CONTROL_HZ
            rise_ms = (t90 - t10) * 1000.0 if t90 >= t10 else None
        except Exception:
            rise_ms = None
        out.append(
            {
                "joint": name,
                "target_delta_rad": amplitude,
                "final_delta_rad": round(final - q0, 4),
                "steady_state_error_rad": round(final - target, 4),
                "overshoot_pct": round(float(overshoot), 1),
                "rise_ms": None if rise_ms is None else round(float(rise_ms), 0),
            }
        )
    worst = max(out, key=lambda r: abs(r["steady_state_error_rad"]))
    return {
        "stage": "step",
        "condition": tag,
        "amplitude_rad": amplitude,
        "joints": out,
        "worst_steady_state": worst["joint"],
        "worst_steady_state_error_rad": worst["steady_state_error_rad"],
    }


def stage_walk(env, onnx_path: str, seconds: float = 10.0) -> dict:
    """Roll out a trained policy with a zero (stand) command."""
    import onnxruntime as ort

    sess = ort.InferenceSession(onnx_path)
    inp = sess.get_inputs()[0].name
    out_name = sess.get_outputs()[0].name
    shape = sess.get_inputs()[0].shape
    if shape[-1] != 61:
        raise SystemExit(f"{onnx_path}: expected 61-D input, got {shape}")

    env.reset()
    zs, tilts, xys = [], [], []
    for _ in range(int(seconds * CONTROL_HZ)):
        obs = env.obs_buf["actor"][0].detach().cpu().numpy().astype(np.float32)[None]
        a = sess.run([out_name], {inp: obs})[0].astype(np.float32)
        act(env, torch.from_numpy(a))
        zs.append(trunk_z(env))
        tilts.append(tilt_deg(env))
        p = robot(env).data.root_link_pos_w[0].detach().cpu().numpy()
        xys.append(p[:2].copy())
    xy = np.asarray(xys)
    return {
        "stage": "walk",
        "onnx": str(onnx_path),
        "seconds": seconds,
        "trunk_z_mean_m": round(float(np.mean(zs)), 4),
        "tilt_max_deg": round(float(np.max(tilts)), 2),
        "xy_travel_m": round(float(np.linalg.norm(xy[-1] - xy[0])), 3),
        "survived": bool(np.max(tilts) < 60 and np.mean(zs) > 0.05),
        "note": "sim-only; a 'survived' here is NOT a deployment acceptance result",
    }


# --------------------------------------------------------------------------- #
def _json_default(o):
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(f"not JSON serializable: {type(o)}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--task", default="Mjlab-Velocity-Flat-MicroDuck-HD1910")
    ap.add_argument("--stage", default="home", choices=["home", "drift", "step", "walk", "all"])
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--seconds", type=float, default=None)
    ap.add_argument("--onnx", default=None, help="walk stage: exported policy")
    ap.add_argument("--zero-gravity", action="store_true", help="step stage: remove gravity to isolate the servo")
    ap.add_argument("--out", default=None, help="write the results as JSON here")
    args = ap.parse_args(argv)

    if args.stage == "walk" and not args.onnx:
        raise SystemExit("--stage walk requires --onnx")

    env = make_env(args.task, args.device, keep_terminations=False, zero_gravity=args.zero_gravity)
    results = []
    try:
        stages = ["home", "drift", "step"] + (["walk"] if args.onnx else [])
        if args.stage != "all":
            stages = [args.stage]
        for stage in stages:
            if stage == "home":
                r = stage_home(env, args.seconds or 3.0)
            elif stage == "drift":
                r = stage_drift(env, args.seconds or 10.0)
            elif stage == "step":
                r = stage_step(env, tag="zero-g" if args.zero_gravity else "standing")
            else:
                r = stage_walk(env, args.onnx, args.seconds or 10.0)
            results.append(r)
            print(json.dumps(r, ensure_ascii=False, indent=2, default=_json_default))
    finally:
        env.close()

    if args.out:
        Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
