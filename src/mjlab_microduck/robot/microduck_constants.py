import os
from pathlib import Path

import mujoco
from mjlab.actuator import XmlActuatorCfg
from mjlab_microduck.actuator import (
    BacklashEncoderBamActuatorCfg,
    FeetechSts3215BamActuatorCfg,
    FrictionDRBamActuatorCfg,
)
from mjlab.entity import EntityArticulationInfoCfg, EntityCfg
from mjlab.utils.spec_config import CollisionCfg


_ROBOT_DIR: Path = Path(os.path.dirname(__file__)) / "microduck"

MICRODUCK_WALK_XML: Path = _ROBOT_DIR / "robot_walk.xml"
# Full-collision model, shared by standup / ground-pick / walk-rollers tasks.
MICRODUCK_ALLCOLLISIONS_XML: Path = _ROBOT_DIR / "robot_allcollisions.xml"
# 70mm / 15g ball prop for the BallKick task.
MICRODUCK_BALL_XML: Path = _ROBOT_DIR / "ball.xml"
# Roller-skate model: 14 actuated joints + passive wheel hinges (passive_*wheel).
MICRODUCK_ALLCOLLISIONS_ROLLERS_XML: Path = _ROBOT_DIR / "robot_allcollisions_rollers.xml"
# Backlash models: every servo joint gets an unactuated passive_<joint>_backlash
# hinge in series (±1° play, 2° total). Exported via
# config_mjcf_{allcollisions,walk}_backlash.json (add_backlash.py post-processor).
MICRODUCK_ALLCOLLISIONS_BACKLASH_XML: Path = _ROBOT_DIR / "robot_allcollisions_backlash.xml"
MICRODUCK_WALK_BACKLASH_XML: Path = _ROBOT_DIR / "robot_walk_backlash.xml"
MICRODUCK_ALLCOLLISIONS_ROLLERS_BACKLASH_XML: Path = _ROBOT_DIR / "robot_allcollisions_rollers_backlash.xml"

assert MICRODUCK_WALK_XML.exists(), f"XML not found: {MICRODUCK_WALK_XML}"
assert MICRODUCK_ALLCOLLISIONS_XML.exists(), f"XML not found: {MICRODUCK_ALLCOLLISIONS_XML}"
assert MICRODUCK_BALL_XML.exists(), f"XML not found: {MICRODUCK_BALL_XML}"
assert MICRODUCK_ALLCOLLISIONS_ROLLERS_XML.exists(), f"XML not found: {MICRODUCK_ALLCOLLISIONS_ROLLERS_XML}"
assert MICRODUCK_ALLCOLLISIONS_BACKLASH_XML.exists(), f"XML not found: {MICRODUCK_ALLCOLLISIONS_BACKLASH_XML}"
assert MICRODUCK_WALK_BACKLASH_XML.exists(), f"XML not found: {MICRODUCK_WALK_BACKLASH_XML}"
assert MICRODUCK_ALLCOLLISIONS_ROLLERS_BACKLASH_XML.exists(), f"XML not found: {MICRODUCK_ALLCOLLISIONS_ROLLERS_BACKLASH_XML}"


def get_walk_spec() -> mujoco.MjSpec:
    return mujoco.MjSpec.from_file(str(MICRODUCK_WALK_XML))


def get_standup_spec() -> mujoco.MjSpec:
    return mujoco.MjSpec.from_file(str(MICRODUCK_ALLCOLLISIONS_XML))


def get_ground_pick_spec() -> mujoco.MjSpec:
    return mujoco.MjSpec.from_file(str(MICRODUCK_ALLCOLLISIONS_XML))


def get_walk_rollers_spec() -> mujoco.MjSpec:
    # NOTE: was loading robot_allcollisions.xml (no wheels) — the roller env
    # silently ran on the wheel-less standup model.
    return mujoco.MjSpec.from_file(str(MICRODUCK_ALLCOLLISIONS_ROLLERS_XML))


def get_ball_spec() -> mujoco.MjSpec:
    return mujoco.MjSpec.from_file(str(MICRODUCK_BALL_XML))


def get_backlash_spec() -> mujoco.MjSpec:
    return mujoco.MjSpec.from_file(str(MICRODUCK_ALLCOLLISIONS_BACKLASH_XML))


def get_walk_backlash_spec() -> mujoco.MjSpec:
    return mujoco.MjSpec.from_file(str(MICRODUCK_WALK_BACKLASH_XML))


def get_rollers_backlash_spec() -> mujoco.MjSpec:
    return mujoco.MjSpec.from_file(str(MICRODUCK_ALLCOLLISIONS_ROLLERS_BACKLASH_XML))


HOME_FRAME = EntityCfg.InitialStateCfg(
    joint_pos={
        # Lower body — STAND2 pose: trunk shifted ~5mm forward over the feet so
        # the CoM sits over the ankle axis (was ~5mm behind it at the old HOME,
        # which biased the robot backward and made the standup policy droop its
        # head forward as a counterweight). Leg pitch chain leaned forward:
        # hip_pitch 30°→26.24°, ankle 30°→25.95°, knee 0°→0.28°. Matches the
        # STAND keyframe in scene.xml / scene_walk.xml.
        r".*hip_yaw.*": 0.0,
        r".*left_hip_roll.*": -0.0873,
        r".*right_hip_roll.*": 0.0873,
        r".*left_hip_pitch.*": -0.4579,
        r".*right_hip_pitch.*": 0.4579,
        r".*left_knee.*": -0.0049,
        r".*right_knee.*": 0.0049,
        r".*left_ankle.*": 0.4530,
        r".*right_ankle.*": -0.4530,
        # Head
        r".*neck_pitch.*": 0.3491,
        r".*head_pitch.*": 0.3491,
        r".*head_yaw.*": 0.0,
        r".*head_roll.*": 0.0,
    },
    joint_vel={".*": 0.0},
)

FULL_COLLISION = CollisionCfg(
    geom_names_expr=[".*_collision"],
    condim={r"^(left|right)_foot_collision$": 3, ".*_collision": 1},
    priority={r"^(left|right)_foot_collision$": 1},
    friction={r"^(left|right)_foot_collision$": (1.0,)},
)

# -- Old actuator (XML position, MuJoCo built-in PD + friction) --
# actuators = DelayedActuatorCfg(
    # delay_min_lag=0,
    # delay_max_lag=3,
    # base_cfg=XmlPositionActuatorCfg(joint_names_expr=(r".*",)),
# )

# -- BAM M6 actuator (full voltage control + load-dependent friction) --
# Exclude passive_* joints (jaw linkage in the new model has no XML actuator).
# Voltage domain randomization (mirrors mjlab_microban):
#   - vin_range: per-env battery voltage sampled at startup (replaces fixed vin)
#   - vin_drop_gain_range: load-dependent voltage sag V_drop = gain * sum(|tau|)
#   - vin_min: hard floor on the effective voltage after sag
# kp_fw kept at 200 (microduck's preserved firmware stiffness; microban uses 125).
_BAM_ACTUATOR_KWARGS = dict(
    motor_name="xl330",
    model="m6",
    target_names_expr=(r"^(?!passive_).*",),
    kp_fw=200.0,  # microduck's preserved firmware stiffness (microban uses 125)
    # vin_range=(6.9, 7.9),
    vin_range=(6.5, 8.2),
    vin_drop_gain_range=(0.0, 0.2),
    vin_min=6.0,
    # max_current=1.75,
    delay_min_lag=3,
    delay_max_lag=6,
)
actuators = FrictionDRBamActuatorCfg(**_BAM_ACTUATOR_KWARGS)

# Same BAM actuator, but the firmware position loop reads the encoder THROUGH
# the passive_<joint>_backlash hinges (the real encoder is on the output side
# of the gear play). Only for the backlash model; the target regex already
# excludes the passive_* backlash joints from actuation.
backlash_actuators = BacklashEncoderBamActuatorCfg(**_BAM_ACTUATOR_KWARGS)

# -- BAM M4 actuator
# actuators = DelayedActuatorCfg(
    # delay_min_lag=0,
    # delay_max_lag=3,
    # base_cfg=make_bam_m4_actuator_cfg(),
# )

# HOME frame for the backlash model. HOME_FRAME's unanchored patterns
# (e.g. r".*left_hip_roll.*") would also match passive_left_hip_roll_backlash
# and try to initialize it at -0.0873 rad — outside its ±1° range. Pattern
# matching is first-match-wins in declaration order, so the anchored backlash
# rule placed FIRST pins every backlash joint at 0 and the servo joints fall
# through to the normal HOME values.
BACKLASH_HOME_FRAME = EntityCfg.InitialStateCfg(
    joint_pos={r".*_backlash$": 0.0, **HOME_FRAME.joint_pos},
    joint_vel={".*": 0.0},
)

MICRODUCK_WALK_ROBOT_CFG = EntityCfg(
    spec_fn=get_walk_spec,
    init_state=HOME_FRAME,
    collisions=(FULL_COLLISION,),
    articulation=EntityArticulationInfoCfg(
        actuators=(actuators,),
        soft_joint_pos_limit_factor=0.9,
    ),
)

MICRODUCK_STANDUP_ROBOT_CFG = EntityCfg(
    spec_fn=get_standup_spec,
    init_state=HOME_FRAME,
    collisions=(FULL_COLLISION,),
    articulation=EntityArticulationInfoCfg(
        actuators=(actuators,),
        soft_joint_pos_limit_factor=0.9,
    ),
)

MICRODUCK_GROUND_PICK_ROBOT_CFG = EntityCfg(
    spec_fn=get_ground_pick_spec,
    init_state=HOME_FRAME,
    collisions=(FULL_COLLISION,),
    articulation=EntityArticulationInfoCfg(
        actuators=(actuators,),
        soft_joint_pos_limit_factor=0.9,
    ),
)

# Backlash robots: base model + ±1° serial backlash hinge per servo.
# Encoder reads through the backlash (BacklashEncoderBamActuator feedback +
# joint_pos/vel_rel_backlash observations — see tasks/backlash.py).
# Allcollisions variant → VelStand/StandUp backlash tasks (mirrors
# MICRODUCK_STANDUP_ROBOT_CFG); walk variant → Velocity backlash
# tasks (mirrors MICRODUCK_WALK_ROBOT_CFG, keeps backlash-vs-base comparisons
# unconfounded by the collision model).
MICRODUCK_BACKLASH_ROBOT_CFG = EntityCfg(
    spec_fn=get_backlash_spec,
    init_state=BACKLASH_HOME_FRAME,
    collisions=(FULL_COLLISION,),
    articulation=EntityArticulationInfoCfg(
        actuators=(backlash_actuators,),
        soft_joint_pos_limit_factor=0.9,
    ),
)

MICRODUCK_WALK_BACKLASH_ROBOT_CFG = EntityCfg(
    spec_fn=get_walk_backlash_spec,
    init_state=BACKLASH_HOME_FRAME,
    collisions=(FULL_COLLISION,),
    articulation=EntityArticulationInfoCfg(
        actuators=(backlash_actuators,),
        soft_joint_pos_limit_factor=0.9,
    ),
)

# Roller-skate backlash robot: wheels stay free (passive_*wheel untouched by
# add_backlash.py). collisions=() mirrors MICRODUCK_WALK_ROLLERS_ROBOT_CFG —
# roller wheel collision geoms have no explicit names; XML defaults apply.
MICRODUCK_ROLLERS_BACKLASH_ROBOT_CFG = EntityCfg(
    spec_fn=get_rollers_backlash_spec,
    init_state=BACKLASH_HOME_FRAME,
    collisions=(),
    articulation=EntityArticulationInfoCfg(
        actuators=(backlash_actuators,),
        soft_joint_pos_limit_factor=0.9,
    ),
)

# Free-floating, non-articulated ball prop for the BallKick task. Position is
# set each episode by the reset_ball_in_front_of_foot event; the init pos here
# only matters for the pristine pre-first-reset state.
MICRODUCK_BALL_CFG = EntityCfg(
    spec_fn=get_ball_spec,
    init_state=EntityCfg.InitialStateCfg(pos=(0.3, 0.0, 0.035)),
)

# Roller skate robot: the 4 passive wheel joints (passive_*wheel) have no XML
# actuators; the BAM cfg's target regex already excludes them, so the action
# space stays 14-dimensional. Uses the SAME canonical BAM actuator as every
# other variant (was a plain XmlActuatorCfg PD — an actuator-physics mismatch
# vs the rest of the family, and joint-friction DR was impossible).
MICRODUCK_WALK_ROLLERS_ROBOT_CFG = EntityCfg(
    spec_fn=get_walk_rollers_spec,
    init_state=HOME_FRAME,
    collisions=(),  # roller wheel collision geoms have no explicit names; XML defaults apply
    articulation=EntityArticulationInfoCfg(
        actuators=(actuators,),
        soft_joint_pos_limit_factor=0.9,
    ),
)

# --------------------------------------------------------------------------- #
# HD1910 variant (Feetech STS3215-class servo)
# --------------------------------------------------------------------------- #
# Models are GENERATED from the XL330 baseline + hd1910/mass_table.csv by
# hd1910/build_hd1910_mjcf.py — never hand-edited. Regenerate after weighing.
HD1910_MASS_TABLE: Path = _ROBOT_DIR / "hd1910" / "mass_table.csv"
HD1910_PARAMS_JSON: Path = _ROBOT_DIR / "hd1910" / "1910_m6.json"
# The supplied file is a strict TRUNCATION of the same fit published upstream
# (LuwuDynamics/xgoduck_rl → .../xgoduck/params/1910_m6.json): every value in it
# is byte-identical, but that file also carries 11 more m6 fields
# (error_gain_ratio=1.3217, q_offset=0.0327, the six load_friction_* terms,
# dtheta_stribeck, alpha, friction_viscous). Without them BAM silently uses its
# own defaults — see hd1910/PROVENANCE.md.
HD1910_PARAMS_JSON_FULL: Path = _ROBOT_DIR / "hd1910" / "1910_m6_full.json"
# Datasheet-derived variant (ACTIVE since 2026-11-10, per "堵转等信息按 1910 规格书"):
# kt = 7.5 kg·cm/A = 0.7355 N·m/A, R = 7.4 V / 2.0 A stall = 3.7 Ω
#   → stall 1.471 N·m @7.4 V (datasheet 1.47), stall current 2.0 A (datasheet)
#   → forcerange (8 V) = 1.590 N·m, k_eff @kp_fw=5 = 1.21 N·m/rad
#   → no-load = 10.06 rad/s vs datasheet 11.8 (-15%: BAM uses one kt for both
#     torque and back-EMF; the datasheet's no-load implies kt_emf≈0.627)
# error_gain_ratio = 0.163/0.166 uses Feetech's official duty/rad/P constant.
# Friction terms are carried from the LuwuDynamics fit (no datasheet has friction).
HD1910_PARAMS_JSON_DATASHEET: Path = _ROBOT_DIR / "hd1910" / "1910_datasheet.json"
# Which file the training config actually uses. Kept on the supplied subset per
# the 2026-11-10 hardware spec; switching to _FULL multiplies the effective
# stiffness by error_gain_ratio (1.32x), so change it deliberately, then re-run
# scripts/hd1910_validate.py --stage step --zero-gravity.
HD1910_ACTIVE_PARAMS_JSON: Path = HD1910_PARAMS_JSON_DATASHEET

MICRODUCK_WALK_HD1910_XML: Path = _ROBOT_DIR / "robot_walk_hd1910.xml"
MICRODUCK_ALLCOLLISIONS_HD1910_XML: Path = _ROBOT_DIR / "robot_allcollisions_hd1910.xml"
MICRODUCK_ALLCOLLISIONS_ROLLERS_HD1910_XML: Path = _ROBOT_DIR / "robot_allcollisions_rollers_hd1910.xml"

for _p in (
    MICRODUCK_WALK_HD1910_XML,
    MICRODUCK_ALLCOLLISIONS_HD1910_XML,
    MICRODUCK_ALLCOLLISIONS_ROLLERS_HD1910_XML,
    HD1910_PARAMS_JSON,
    HD1910_PARAMS_JSON_FULL,
    HD1910_PARAMS_JSON_DATASHEET,
    HD1910_MASS_TABLE,
):
    assert _p.exists(), (
        f"HD1910 asset not found: {_p}\n"
        f"  run: python {_ROBOT_DIR / 'hd1910' / 'build_hd1910_mjcf.py'}"
    )

# Training-side actuator parameters (hardware spec, 2026-11-10).
# These are TRAINING values, NOT servo registers — see hd1910/PROVENANCE.md.
# kp_fw is in STS3215 firmware-P units (BAM's default for this actuator is 32,
# XL330's is 400 → microduck trained it at 200) — do NOT reuse 200 here.
HD1910_KP_FW: float = 5.0
HD1910_VIN_RANGE: tuple[float, float] = (7.4, 8.0)
HD1910_VIN_NOMINAL: float = 7.7
HD1910_VIN_MIN: float = 7.0
# 3..6 lags x 5 ms sim step = 15..30 ms command delay. BAM does not read the
# fitted model's own command_delay (6.0 ms); this envelope is deliberately
# wider and must be confirmed on the bench.
HD1910_DELAY_LAGS: tuple[int, int] = (3, 6)

_HD1910_ACTUATOR_KWARGS = dict(
    # Custom params JSON: BAM has no bundled "hd1910" motor (see PROVENANCE.md
    # for which m6 fields the file leaves at BAM defaults).
    json_path=str(HD1910_ACTIVE_PARAMS_JSON),
    target_names_expr=(r"^(?!passive_).*",),
    kp_fw=HD1910_KP_FW,
    vin_range=HD1910_VIN_RANGE,
    vin_min=HD1910_VIN_MIN,
    # NOT measured for the HD1910. Same envelope as the XL330 recipe so the
    # sim2real machinery stays at parity; vin_min=7.0 implies sag modeling is
    # expected. Set to None to disable once battery sag is characterized.
    vin_drop_gain_range=(0.0, 0.2),
    delay_min_lag=HD1910_DELAY_LAGS[0],
    delay_max_lag=HD1910_DELAY_LAGS[1],
)

# FeetechSts3215BamActuatorCfg (not FrictionDRBamActuatorCfg): the STS3215
# control law keeps state in q_target_smooth that BAM only allocates in
# load_log(), so the plain BAM actuator crashes on the first compute().
hd1910_actuators = FeetechSts3215BamActuatorCfg(**_HD1910_ACTUATOR_KWARGS)


def get_walk_hd1910_spec() -> mujoco.MjSpec:
    return mujoco.MjSpec.from_file(str(MICRODUCK_WALK_HD1910_XML))


def get_allcollisions_hd1910_spec() -> mujoco.MjSpec:
    return mujoco.MjSpec.from_file(str(MICRODUCK_ALLCOLLISIONS_HD1910_XML))


def get_rollers_hd1910_spec() -> mujoco.MjSpec:
    return mujoco.MjSpec.from_file(str(MICRODUCK_ALLCOLLISIONS_ROLLERS_HD1910_XML))


# HOME_FRAME is reused verbatim: the joint-space pose is a property of the
# linkage, not of the servo. The standing EQUILIBRIUM HEIGHT is not — once the
# HD1910 masses change the weight distribution, re-derive trunk z with
# scripts/hd1910_validate.py --stage home before trusting any height-based
# reward or reset range.
MICRODUCK_WALK_HD1910_ROBOT_CFG = EntityCfg(
    spec_fn=get_walk_hd1910_spec,
    init_state=HOME_FRAME,
    collisions=(FULL_COLLISION,),
    articulation=EntityArticulationInfoCfg(
        actuators=(hd1910_actuators,),
        soft_joint_pos_limit_factor=0.9,
    ),
)

MICRODUCK_ALLCOLLISIONS_HD1910_ROBOT_CFG = EntityCfg(
    spec_fn=get_allcollisions_hd1910_spec,
    init_state=HOME_FRAME,
    collisions=(FULL_COLLISION,),
    articulation=EntityArticulationInfoCfg(
        actuators=(hd1910_actuators,),
        soft_joint_pos_limit_factor=0.9,
    ),
)

MICRODUCK_ROLLERS_HD1910_ROBOT_CFG = EntityCfg(
    spec_fn=get_rollers_hd1910_spec,
    init_state=HOME_FRAME,
    collisions=(),
    articulation=EntityArticulationInfoCfg(
        actuators=(hd1910_actuators,),
        soft_joint_pos_limit_factor=0.9,
    ),
)

# name -> robot cfg, for the mass/actuator audit test.
HD1910_ROBOT_CFGS = {
    "walk": MICRODUCK_WALK_HD1910_ROBOT_CFG,
    "allcollisions": MICRODUCK_ALLCOLLISIONS_HD1910_ROBOT_CFG,
    "rollers": MICRODUCK_ROLLERS_HD1910_ROBOT_CFG,
}


if __name__ == "__main__":
    import mujoco.viewer as viewer
    from mjlab.scene import Scene, SceneCfg
    from mjlab.terrains import TerrainImporterCfg

    SCENE_CFG = SceneCfg(
        terrain=TerrainImporterCfg(terrain_type="plane"),
        entities={"robot": MICRODUCK_WALK_ROBOT_CFG},
    )

    scene = Scene(SCENE_CFG, device="cuda:0")
    viewer.launch(scene.compile())
