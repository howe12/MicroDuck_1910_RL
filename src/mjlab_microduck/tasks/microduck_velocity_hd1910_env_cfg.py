"""Microduck velocity (walking) environment on Feetech HD1910 servos.

This is the *same* locomotion recipe as ``microduck_velocity_env_cfg`` — same
rewards, same DR, same 61-D observation contract, same 50 Hz control rate —
with exactly two things swapped:

1. the robot model  → ``robot_*_hd1910.xml`` (generated from the mass table),
2. the actuator     → BAM ``m6`` driven by ``hd1910/1910_m6.json`` at
   ``kp_fw=5``, ``vin∈[7.4, 8.0]``, ``vin_min=7.0``, ``delay∈[3, 6]`` steps.

Why a thin wrapper and not a copy: the 949-line velocity cfg is the file the
upstream project keeps auditing (2026-07/08 lessons are encoded in its
comments). Forking it would freeze all of those lessons at today's date. The
only thing that should ever differ between the XL330 and HD1910 walker is the
robot entity — if you find yourself editing a reward here, edit the shared
recipe instead, or the two policies stop being comparable.

⚠️ The HD1910 models are generated from ``hd1910/mass_table.csv``. As long as
that table says ``placeholder``, the dynamics are the XL330's and this task is
only valid for pipeline/smoke validation — never for a deployment claim.
"""

from __future__ import annotations

import csv
import dataclasses
from pathlib import Path

from mjlab.envs import ManagerBasedRlEnvCfg
from mjlab.rl import RslRlModelCfg, RslRlOnPolicyRunnerCfg

from mjlab_microduck.robot.microduck_constants import (
    HD1910_MASS_TABLE,
    MICRODUCK_WALK_HD1910_ROBOT_CFG,
)
from mjlab.managers import CurriculumTermCfg, RewardTermCfg
from mjlab_microduck.tasks import mdp as microduck_mdp
from mjlab_microduck.tasks.microduck_velocity_env_cfg import (
    MicroduckRlCfg,
    make_microduck_velocity_env_cfg,
)

# Distinct experiment name → logs/rsl_rl/velocity_hd1910/... so HD1910 runs can
# never be confused with (or resumed into) an XL330 run.
MicroduckHd1910RlCfg = dataclasses.replace(
    MicroduckRlCfg,
    experiment_name="velocity_hd1910",
    run_name="velocity_hd1910",
)


_WARNED = False


def mass_table_status(table: Path = HD1910_MASS_TABLE) -> tuple[int, int, int, int]:
    """(measured, estimated, total) body rows in the HD1910 mass table."""
    if not table.exists():
        return 0, 0, 0
    measured = estimated = baseline = total = 0
    with table.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not row.get("body"):
                continue
            total += 1
            status = (row.get("status") or "").strip().lower()
            if (row.get("mass_g") or "").strip() and status == "measured":
                measured += 1
            elif (row.get("mass_g") or "").strip() and status == "estimated":
                estimated += 1
            elif status == "baseline":
                baseline += 1
    return measured, estimated, baseline, total


def _warn_if_placeholder() -> None:
    global _WARNED
    if _WARNED:
        return
    _WARNED = True
    measured, estimated, baseline, total = mass_table_status()
    if measured == total:
        print(f"[HD1910] mass table: {measured}/{total} bodies MEASURED")
    elif measured + estimated + baseline == total:
        print(
            f"[HD1910] mass table: {estimated}/{total} bodies datasheet-ESTIMATED "
            f"(servo 21 g vs XL330 18 g), {baseline} unchanged, 0 weighed.\n"
            f"[HD1910]          not a deployment claim — weigh, update {HD1910_MASS_TABLE.name}, rerun the generator"
        )
    else:
        print(
            f"[HD1910] WARNING: mass table is {measured} measured / {estimated} estimated / {baseline} baseline of {total} — "
            f"the rest still carry XL330 masses. Valid for smoke/pipeline tests ONLY."
        )


def make_microduck_velocity_hd1910_env_cfg(
    play: bool = False,
    rough: bool = False,
) -> ManagerBasedRlEnvCfg:
    """HD1910 twin of ``Mjlab-Velocity-{Flat,Rough}-MicroDuck``."""
    _warn_if_placeholder()
    cfg = make_microduck_velocity_env_cfg(play=play, rough=rough)

    # The one and only difference from the XL330 walker. Everything else —
    # sensors, commands, DR, noise, delays, curricula, 61-D obs — is inherited
    # by construction so the two policies stay A/B comparable.
    cfg.scene.entities = {"robot": MICRODUCK_WALK_HD1910_ROBOT_CFG}

    # ── run-2 reward rebalance (2026-11-11) ────────────────────────────────
    # Run 1 converged to "stand still and march in place" (velocity error
    # 0.40 m/s after 4000 iterations). The XL330-tuned weights hand a standing
    # robot ~5.1 of the ~6.5 available reward; walking adds ~0.6 but risks it
    # all. These four changes flip the economics — see the block at the bottom
    # of tasks/mdp.py for the measurements behind them.
    cfg.rewards["track_linear_velocity"].weight = 4.0      # was 2.0
    cfg.rewards["head_pose_tracking"].weight = 1.0         # was 2.0 (largest term)
    cfg.rewards["air_time"].func = microduck_mdp.feet_air_time_with_progress
    cfg.rewards["no_progress"] = RewardTermCfg(
        func=microduck_mdp.no_progress_penalty,
        weight=0.5,          # positive: the function self-negates (AGENTS.md)
        params={"command_name": "twist", "min_speed": 0.02},
    )
    # ── run-3: 补原地转向 (2026-11-12) ─────────────────────────────────────
    # 分轴评估显示 run 2 的线性轴很好（前向 0.33/0.40、后向 0.19/0.20、横移
    # 0.17/0.20），但原地转向完全不会：指令 0.3 和 0.8 rad/s 都只到 0.01~0.12，
    # 而"边走边转"能达到指令的 88%。所以只加压转向，不动已经work的线性项。
    cfg.rewards["track_angular_velocity"].weight = 3.0        # was 2.0（线性权重 4.0 压制了转向）
    cfg.commands["twist"].rel_turn_in_place_envs = 0.35       # was 0.15（转向样本密度翻倍）

    # Live retuning: write {"track_linear_velocity": 4.5} to weights_override.json
    cfg.curriculum["weight_override"] = CurriculumTermCfg(
        func=microduck_mdp.weight_override_curriculum,
        params={"path": "weights_override.json"},
    )

    # Defensive assertions: a silent obs/action drift here would produce an
    # ONNX the runtime refuses to load, hours into a run.
    assert len(cfg.actions) == 1, "unexpected action-term count"
    return cfg
