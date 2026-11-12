"""HD-1910 twins of the episodic "trick" tasks: stand-up, vel-stand, roulade.

Same pattern as ``microduck_velocity_hd1910_env_cfg``: the upstream recipe is
reused verbatim and only the robot entity is swapped, so the XL330 and HD-1910
versions stay directly comparable.

What each task trains (all three use the full-collision model, which is why
``MICRODUCK_ALLCOLLISIONS_HD1910_ROBOT_CFG`` exists):

* ``standup``  — sitting / prone / supine → standing. The recovery curriculum
  ``ground_state_mix`` ramps the reset distribution from mostly-standing to
  mostly-supine, and the economics are tuned so "stay down and keep the pose
  reward" is a net loss.
* ``velstand`` — walking AND fall recovery in ONE policy. 2/3 of resets start
  fallen and episodes are long, so fallen-state data is abundant; that is the
  data-density fix the upstream notes call "the walk is stable by ~750".
* ``roulade``  — forward roll over the head. Phase gates on trunk pitch split
  the episode into roll / landing / rise, mid-roll spawns give the second half
  of the roll on-policy data, and a tucked pose anchor keeps the head from
  taking the impact.

Deliberately NOT rebalanced like the velocity recipe: those reward stacks were
tuned against the XL330 and have not been re-validated on the HD-1910 actuator.
Run them as-is first, compare the curves against the XL330 runs, and only then
apply the same kind of rebalance that fixed run 2 of the velocity task.
"""

from __future__ import annotations

import dataclasses

from mjlab.envs import ManagerBasedRlEnvCfg
from mjlab.rl import RslRlOnPolicyRunnerCfg

from mjlab_microduck.robot.microduck_constants import (
    MICRODUCK_ALLCOLLISIONS_HD1910_ROBOT_CFG,
)
from mjlab_microduck.tasks import hd1910_tuning
from mjlab_microduck.tasks.microduck_roulade_env_cfg import (
    MicroduckRouladeRlCfg,
    make_microduck_roulade_env_cfg,
)
from mjlab_microduck.tasks.microduck_standup_env_cfg import (
    MicroduckStandUpRlCfg,
    make_microduck_standup_env_cfg,
)
from mjlab_microduck.tasks.microduck_velstand_env_cfg import (
    MicroduckVelStandRlCfg,
    make_microduck_velstand_env_cfg,
)


def _swap_robot(cfg: ManagerBasedRlEnvCfg) -> ManagerBasedRlEnvCfg:
    """Replace the XL330 full-collision robot with the HD-1910 one."""
    cfg.scene.entities = {"robot": MICRODUCK_ALLCOLLISIONS_HD1910_ROBOT_CFG}
    return cfg


def make_microduck_standup_hd1910_env_cfg(
    play: bool = False, rough: bool = False
) -> ManagerBasedRlEnvCfg:
    cfg = _swap_robot(make_microduck_standup_env_cfg(play=play, rough=rough))

    # face-up (supine) earlier than upstream. Measured on the 300-iteration
    # probe: prone 100% and sitting 100% recovery, but supine 0% — because the
    # default curriculum keeps face_up_prob at 0 until iteration 600 and the
    # probe policy had never seen the pose. The duck is head-heavy (38% of the
    # mass), so falling on its back is common in the real world and cannot wait.
    # Upstream's own notes record the same failure ("face-up froze into do
    # nothing"), so this keeps their shape but pulls the hard pose forward.
    stages = [
        # step,        standing, sitting, face_down, face_up
        {"step": 0,        "params": {"standing_prob": 0.50, "sitting_prob": 0.30,
                                      "face_down_prob": 0.20, "face_up_prob": 0.00}},
        {"step": 200 * 24, "params": {"standing_prob": 0.30, "sitting_prob": 0.25,
                                      "face_down_prob": 0.25, "face_up_prob": 0.20}},
        {"step": 700 * 24, "params": {"standing_prob": 0.25, "sitting_prob": 0.20,
                                      "face_down_prob": 0.25, "face_up_prob": 0.30}},
        {"step": 1200 * 24, "params": {"standing_prob": 0.20, "sitting_prob": 0.20,
                                       "face_down_prob": 0.25, "face_up_prob": 0.35}},
    ]
    cfg.curriculum["ground_state_mix"].params["param_stages"] = stages
    return cfg


def make_microduck_velstand_hd1910_env_cfg(
    play: bool = False, rough: bool = False
) -> ManagerBasedRlEnvCfg:
    cfg = _swap_robot(make_microduck_velstand_env_cfg(play=play, rough=rough))
    # VelStand builds its walk layer from the *unmodified* upstream velocity
    # recipe, so without this it would inherit exactly the economics that made
    # run 1 stand still on the HD-1910. Apply the run-2 rebalance, but keep
    # VelStand's own air_time (it gates on upright so a fallen robot cannot farm
    # it by shaking a leg).
    hd1910_tuning.apply_velocity_rebalance(cfg, air_time_progress=False)
    return cfg


def make_microduck_roulade_hd1910_env_cfg(play: bool = False) -> ManagerBasedRlEnvCfg:
    return _swap_robot(make_microduck_roulade_env_cfg(play=play))


# Separate experiment names → logs never mix with the XL330 trick runs.
MicroduckStandUpHd1910RlCfg = dataclasses.replace(
    MicroduckStandUpRlCfg, experiment_name="standup_hd1910", run_name="standup_hd1910"
)
MicroduckVelStandHd1910RlCfg = dataclasses.replace(
    MicroduckVelStandRlCfg, experiment_name="velstand_hd1910", run_name="velstand_hd1910"
)
MicroduckRouladeHd1910RlCfg = dataclasses.replace(
    MicroduckRouladeRlCfg, experiment_name="roulade_hd1910", run_name="roulade_hd1910"
)


__all__ = [
    "make_microduck_standup_hd1910_env_cfg",
    "make_microduck_velstand_hd1910_env_cfg",
    "make_microduck_roulade_hd1910_env_cfg",
    "MicroduckStandUpHd1910RlCfg",
    "MicroduckVelStandHd1910RlCfg",
    "MicroduckRouladeHd1910RlCfg",
]
