"""Friction ladder: does the walk policy survive friction outside its training range?

Training randomises foot friction over (0.7, 1.3) on a rigid plane. This asks whether
the policy breaks outside that band — the hypothesis being that a real carpet (higher
friction, and compliant) is out of distribution.

Reuses evaluate_policy.run_bucket, which pins the command correctly: the observation
term reads `term.command` (world frame), so writing only `vel_command_b` leaves the
policy seeing the sampled command and it just stands still.

    uv run --no-sync scripts/friction_ladder.py
"""
import importlib.util, sys, numpy as np
sys.path.insert(0, 'scripts')
import evaluate_policy as ev
from mjlab.envs import ManagerBasedRlEnv
from mjlab.tasks.registry import load_env_cfg
import onnxruntime as ort

s = ort.InferenceSession('policies/hd1910_walk_all.onnx')
MUS = (0.7, 1.3, 1.8, 2.5)
SPEEDS = (0.30, 0.45)
print(f"{'μ':>5s} {'指令':>6s} {'实测vx':>8s} {'摔倒率':>7s} {'倾角':>7s}")
rows = []
for mu in MUS:
    for v in SPEEDS:
        cfg = load_env_cfg('Mjlab-Velocity-Flat-MicroDuck-HD1910')
        cfg.scene.num_envs = 32
        cfg.episode_length_s = 30.0
        cfg.commands['twist'].heading_command = False
        cfg.commands['twist'].ranges.heading = None
        cfg.events['foot_friction'].params['ranges'] = (mu, mu)
        env = ManagerBasedRlEnv(cfg=cfg, device='cuda:0')
        try:
            r = ev.run_bucket(env, s, f"mu{mu}_v{v}", [v, 0.0, 0.0], 6.0)
        finally:
            env.close()
        print(f"{mu:5.1f} {v:6.2f} {r['achieved_vx']:8.3f} {r['fall_ratio']:7.0%} {r['tilt_deg']:7.1f}°")
        rows.append((mu, v, r['achieved_vx'], r['fall_ratio']))
ok = [r for r in rows if r[3] == 0 and r[2] > 0.6 * r[1]]
print(f"\n既没摔又跟得上指令(>60%)的组合 {len(ok)}/{len(rows)}")
