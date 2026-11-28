"""Walk/stand policy trained for a harsh real world: carpet, soft ground, sagging power.

Motivation (all measured, see docs/hd1910/ACCEPTANCE.md and DEPLOY.md):

* friction alone does NOT break the current policy — the friction ladder ran 0.5..2.5
  with 0% falls. So this task does not exist because "friction is out of range"; it
  exists because the *combination* below is what a real floor looks like and only the
  flat/rigid corner of it has ever been trained on.
* the real robot walks ~20 s then falls, and falls sooner on carpet. Both point at the
  actuator/power chain rather than the contact model, so voltage sag and the power
  ceiling get randomised here, not just friction.

What changes versus ``Mjlab-VelStand-Flat-MicroDuck-HD1910``:

1. foot friction (0.4, 2.2) — was (0.7, 1.3)
2. soft ground: the terrain geoms get a compliant contact (solref/solimp), which is
   what a carpet does — it yields, and the foot sinks instead of sitting on a rigid plane
3. voltage envelope widened to (7.0, 8.2) with a lower floor (6.6 V) — a sagging pack
4. load-dependent voltage drop widened to (0.0, 0.45) — this is the power-limit proxy:
   the harder the policy pushes, the less voltage it actually gets

Deliberately NOT done: editing ``hd1910_m6.json`` to "cover the high-load region".
The deployment contract pins that file's sha256 (``ef2d51ad…``) and the firmware refuses
any model whose BAM hash differs, so a modified parameter file could not be deployed.
Power limits are therefore expressed as voltage/DR on top of the unchanged file.
"""

from __future__ import annotations

import dataclasses

from mjlab.envs import ManagerBasedRlEnvCfg
from mjlab.managers.curriculum_manager import CurriculumTermCfg
from mjlab.managers.reward_manager import RewardTermCfg
from mjlab.managers.event_manager import EventTermCfg
from mjlab.utils.spec_config import CollisionCfg

from mjlab_microduck.tasks import mdp as microduck_mdp

from mjlab_microduck.tasks.microduck_tricks_hd1910_env_cfg import (
    make_microduck_velstand_hd1910_env_cfg,
)

FOOT_FRICTION = (0.4, 2.2)
VIN_RANGE = (7.0, 8.2)
VIN_DROP_GAIN = (0.0, 0.45)
VIN_MIN = 6.6
GROUND_SOLREF = (0.06, 1.0)          # softer than MuJoCo's default (0.02, 1.0)
GROUND_SOLIMP = (0.85, 0.95, 0.002, 0.5, 2.0)


def make_microduck_velstand_harsh_hd1910_env_cfg(
    play: bool = False,
) -> ManagerBasedRlEnvCfg:
    cfg = make_microduck_velstand_hd1910_env_cfg(play=play)

    # 1. friction
    cfg.events["foot_friction"].params["ranges"] = FOOT_FRICTION

    # 3+4. power envelope: wider supply, deeper sag under load, lower floor
    robot = cfg.scene.entities["robot"]
    actuators = tuple(
        dataclasses.replace(
            a, vin_range=VIN_RANGE, vin_drop_gain_range=VIN_DROP_GAIN, vin_min=VIN_MIN
        )
        for a in robot.articulation.actuators
    )
    cfg.scene.entities["robot"] = dataclasses.replace(
        robot,
        articulation=dataclasses.replace(robot.articulation, actuators=actuators),
    )

    # 2. soft ground — a carpet yields, a rigid plane does not
    cfg.scene.terrain.collisions = (
        CollisionCfg(
            geom_names_expr=("terrain$",),
            solref=GROUND_SOLREF,
            solimp=GROUND_SOLIMP,
        ),
    )
    return cfg


# Head-plant recovery curriculum. Observed on the robot: some fall poses leave the
# head folded under as a third support, the centre of mass sits inside that
# head+feet polygon, and the policy pushes until it stalls — a hand's nudge is what
# escapes it. Speed/scale tuning cannot fix a support-polygon trap, so the pose is
# seeded and the escape is trained.
HEAD_PLANT_STAGES = [{"step": 0, "params": {"head_plant_prob": 0.10}},
                     {"step": 8000, "params": {"head_plant_prob": 0.25}}]


def make_microduck_headplant_hd1910_env_cfg(play: bool = False) -> ManagerBasedRlEnvCfg:
    cfg = make_microduck_velstand_harsh_hd1910_env_cfg(play=play)

    cfg.events["head_plant_init"] = EventTermCfg(
        func=microduck_mdp.maybe_set_random_head_plant_orientation,
        mode="reset",
        params={"head_plant_prob": 0.10},
    )
    cfg.curriculum["head_plant_prob"] = CurriculumTermCfg(
        func=microduck_mdp.event_param_curriculum,
        params={"event_name": "head_plant_init", "param_stages": HEAD_PLANT_STAGES},
    )
    # The head is a support the policy must stop relying on: price its contact harder
    # than the walk tasks do.
    if "head_impact" in cfg.rewards:
        cfg.rewards["head_impact"].weight = -0.05
    return cfg


# Combined "gimbal-ish head + escape the headstand" task.
#
# Two goals that look contradictory but belong to different phases:
#   * walking -> the camera should stop swinging (the head is a 36%-of-mass balance
#     lever, and the policy currently uses it: measured camera 211 deg/s vs trunk
#     54 deg/s, i.e. the head quadruples the motion)
#   * fallen  -> the head MUST stay free to swing; escaping a headstand is done by
#     trading angular momentum about the head contact
# So the camera penalty is gated on uprightness (``upright_deg``), and the headstand
# pose is seeded so the escape is trained at all.
HEADSTAND_STAGES = [{"step": 0, "params": {"headstand_prob": 0.10}},
                    {"step": 12000, "params": {"headstand_prob": 0.25}}]
# Ramp the camera penalty instead of dropping it in: at full weight from step 0 the
# gait collapses before it finds a leg-based balance strategy.
CAMERA_PENALTY_STAGES = [{"step": 0, "weight": 0.0},
                         {"step": 9600, "weight": -0.010},
                         {"step": 24000, "weight": -0.030}]


def make_microduck_gimbal_hd1910_env_cfg(play: bool = False) -> ManagerBasedRlEnvCfg:
    cfg = make_microduck_headplant_hd1910_env_cfg(play=play)

    cfg.events["headstand_init"] = EventTermCfg(
        func=microduck_mdp.maybe_set_inverted_headstand,
        mode="reset",
        params={"headstand_prob": 0.10},
    )
    cfg.curriculum["headstand_prob"] = CurriculumTermCfg(
        func=microduck_mdp.event_param_curriculum,
        params={"event_name": "headstand_init", "param_stages": HEADSTAND_STAGES},
    )

    cfg.rewards["camera_ang_vel"] = RewardTermCfg(
        func=microduck_mdp.camera_ang_vel_upright_penalty,
        weight=-0.010,
        params={"site_name": "head_camera", "upright_deg": 30.0},
    )
    cfg.curriculum["camera_ang_vel_weight"] = CurriculumTermCfg(
        func=microduck_mdp.reward_weight,
        params={"reward_name": "camera_ang_vel", "weight_stages": CAMERA_PENALTY_STAGES},
    )
    return cfg
