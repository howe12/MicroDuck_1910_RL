"""HD1910 adaptation invariants (CPU only, no GPU).

These tests lock the things that would otherwise fail silently and cost a full
training run:

* every body in the generated MJCF corresponds to a row in the mass table, and
  the committed XML has not drifted from that table;
* the actuator really is the BAM model driven by ``1910_m6.json`` with the
  agreed kp/vin/delay — not the XL330 defaults that look almost right;
* the parameters the upstream JSON does not specify are still at BAM's
  defaults, so that a BAM upgrade changing them is caught here;
* both HD1910 tasks are registered and keep the 61-D / 14-D contract.
"""

from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

import pytest

# Import the task package first: importing microduck_constants directly trips
# the pre-existing circular import through mjlab's plugin loader.
import mjlab_microduck.tasks  # noqa: F401  (registers tasks, loads constants)
from mjlab.tasks.registry import list_tasks, load_env_cfg, load_rl_cfg

from mjlab_microduck.robot.microduck_constants import (
    HD1910_DELAY_LAGS,
    HD1910_KP_FW,
    HD1910_MASS_TABLE,
    HD1910_PARAMS_JSON,
    HD1910_ROBOT_CFGS,
    HD1910_VIN_MIN,
    HD1910_VIN_RANGE,
    MICRODUCK_WALK_HD1910_XML,
    hd1910_actuators,
)

ROBOT_DIR = Path(__file__).resolve().parents[1] / "src/mjlab_microduck/robot/microduck"
GEN_PATH = ROBOT_DIR / "hd1910" / "build_hd1910_mjcf.py"

HD1910_TASKS = (
    "Mjlab-Velocity-Flat-MicroDuck-HD1910",
    "Mjlab-Velocity-Rough-MicroDuck-HD1910",
)


@pytest.fixture(scope="module")
def generator():
    """Load build_hd1910_mjcf.py as a module (it lives beside the data, not in the package)."""
    spec = importlib.util.spec_from_file_location("build_hd1910_mjcf", GEN_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build_hd1910_mjcf"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def table(generator):
    return generator.read_table()


# --------------------------------------------------------------------------- #
# Mass table <-> MJCF
# --------------------------------------------------------------------------- #
def test_mass_table_covers_every_body(generator, table):
    bodies = {b for b, _p, _m in generator._bodies_from_mjcf()}
    missing = bodies - set(table)
    assert not missing, f"mass table is missing bodies: {sorted(missing)}"


def test_committed_models_match_the_table(generator, table):
    """The generated XMLs must be exactly what the table says — no hand edits."""
    for name in generator.SOURCE_MODELS:
        expected, _placeholder = generator.render_model(name, table, strict=True)
        actual = generator.out_path(name).read_text(encoding="utf-8")
        assert actual == expected, (
            f"{generator.out_path(name).name} drifted from mass_table.csv; "
            f"run: python {GEN_PATH}"
        )


def test_models_are_stamped_with_their_data_provenance(generator, table):
    """A model must say whether its masses are measured, estimated, or placeholders."""
    measured = sum(1 for r in table.values() if r.measured)
    estimated = sum(1 for r in table.values() if r.estimated)
    for name in generator.SOURCE_MODELS:
        text = generator.out_path(name).read_text(encoding="utf-8")
        if measured == len(table):
            assert "MEASURED MASSES" in text
        elif estimated:
            assert "ESTIMATED MASSES" in text, (
                f"{generator.out_path(name).name} carries datasheet-derived masses but "
                f"is not stamped as such"
            )
        else:
            assert "PLACEHOLDER MASSES" in text


def test_measured_row_rewrites_mass_com_and_inertia(generator, table, tmp_path):
    """A measured row must change mass, COM and the inertia tensor together."""
    import copy

    rows = copy.deepcopy(table)
    target = "ankle_left"
    base_mass = next(m for b, _p, m in generator._bodies_from_mjcf() if b == target)
    rows[target].mass_g = base_mass * 1.5
    rows[target].com_mm = (1.0, 2.0, 3.0)
    rows[target].inertia_mode = "scale"
    rows[target].status = "measured"

    xml, _ = generator.render_model("walk", rows, strict=True)
    line = next(l for l in xml.splitlines() if "<inertial" in l and f'{base_mass * 1.5 / 1000:.9g}' in l)
    assert 'pos="0.001 0.002 0.003"' in line
    assert "fullinertia" in line


def test_walk_model_has_14_actuated_joints_and_a_free_joint():
    import mujoco

    m = mujoco.MjModel.from_xml_path(str(MICRODUCK_WALK_HD1910_XML))
    assert m.nu == 14, f"expected 14 actuators (14-D action), got {m.nu}"
    assert m.njnt == 15, "14 servo joints + 1 passive jaw joint"
    # 14 hinges + 1 free joint
    assert (m.jnt_type == mujoco.mjtJoint.mjJNT_FREE).sum() == 1


@pytest.mark.parametrize("model_name", sorted(HD1910_ROBOT_CFGS))
def test_robot_cfg_specs_load(model_name):
    spec = HD1910_ROBOT_CFGS[model_name].spec_fn()
    assert spec is not None


# --------------------------------------------------------------------------- #
# Actuator configuration
# --------------------------------------------------------------------------- #
def test_actuator_uses_the_active_hd1910_params_json():
    from mjlab_microduck.robot.microduck_constants import HD1910_ACTIVE_PARAMS_JSON

    assert Path(hd1910_actuators.json_path) == Path(HD1910_ACTIVE_PARAMS_JSON)
    assert hd1910_actuators.motor_name is None, (
        "HD1910 must use json_path — BAM has no bundled hd1910 motor, and "
        "motor_name='xl330' would silently load the Dynamixel parameters"
    )


def test_training_actuator_values_match_the_spec():
    assert hd1910_actuators.kp_fw == HD1910_KP_FW == 5.0
    assert hd1910_actuators.vin_range == HD1910_VIN_RANGE == (7.4, 8.0)
    assert hd1910_actuators.vin_min == HD1910_VIN_MIN == 7.0
    assert (hd1910_actuators.delay_min_lag, hd1910_actuators.delay_max_lag) == HD1910_DELAY_LAGS == (3, 6)


def test_delay_envelope_is_15_to_30_ms_at_5ms():
    dt = 0.005
    lo = hd1910_actuators.delay_min_lag * dt
    hi = hd1910_actuators.delay_max_lag * dt
    assert (lo, hi) == pytest.approx((0.015, 0.030))


def test_kp_fw_is_not_the_xl330_value():
    """Guard against copy-paste: XL330 trained at kp_fw=200, HD1910 at 5."""
    assert hd1910_actuators.kp_fw != 200.0


def test_stall_torque_matches_the_hd1910_datasheet():
    """The actuator must reproduce the datasheet's 1.47 N·m @7.4 V, 2.0 A."""
    import json

    from mjlab_microduck.robot.microduck_constants import HD1910_PARAMS_JSON_DATASHEET

    d = json.loads(HD1910_PARAMS_JSON_DATASHEET.read_text())
    stall_torque = d["kt"] * 7.4 / d["R"]
    stall_current = 7.4 / d["R"]
    assert stall_torque == pytest.approx(1.47, abs=0.01), f"stall {stall_torque:.3f} N·m"
    assert stall_current == pytest.approx(2.0, abs=0.01), f"stall current {stall_current:.3f} A"


def test_active_params_is_the_datasheet_variant():
    from mjlab_microduck.robot.microduck_constants import (
        HD1910_ACTIVE_PARAMS_JSON,
        HD1910_PARAMS_JSON_DATASHEET,
    )

    assert Path(HD1910_ACTIVE_PARAMS_JSON) == HD1910_PARAMS_JSON_DATASHEET


def test_effective_stiffness_is_in_the_xl330_ballpark():
    """kp_fw=5 must land near the XL330's effective joint stiffness.

    k_eff = error_gain * kp_fw * vin * max_pwm * kt / R, with BAM's STS3215
    actuator constants. XL330 at kp_fw=200 / 7.5 V is ~0.56 N·m/rad.
    """
    from bam.feetech.actuator import STS3215Actuator
    from bam.model import load_model
    from bam.testbench import Pendulum

    model = load_model(json_file=str(HD1910_PARAMS_JSON))
    act = STS3215Actuator(Pendulum)
    k_eff = (
        act.error_gain
        * hd1910_actuators.kp_fw
        * sum(HD1910_VIN_RANGE) / 2
        * act.max_pwm
        * model.kt.value
        / model.R.value
    )
    assert 0.4 < k_eff < 1.2, f"effective stiffness {k_eff:.3f} N·m/rad is implausible"


def test_upstream_json_is_preserved_verbatim():
    """The source-of-truth file must not be edited into a BAM-shaped file."""
    data = __import__("json").loads(HD1910_PARAMS_JSON.read_text())
    assert data == {
        "kt": 0.623761,
        "R": 4.910192,
        "armature": 0.0017819,
        "friction_base": 0.015803,
        "friction_stribeck": 0.009590,
        "command_delay": 0.006005,
        "max_velocity": 100,
        "actuator": "sts3215",
        "model": "m6",
    }


def test_full_upstream_params_extend_the_subset_without_contradicting_it():
    """The supplied file is a strict truncation of the same upstream fit.

    LuwuDynamics/xgoduck_rl publishes the complete m6 set; the hardware spec gave
    us 7 of its 18 keys. Every shared key must match exactly, otherwise they are
    two different fits and mixing them would be meaningless.
    """
    import json

    from mjlab_microduck.robot.microduck_constants import HD1910_PARAMS_JSON_FULL

    subset = json.loads(HD1910_PARAMS_JSON.read_text())
    full = json.loads(HD1910_PARAMS_JSON_FULL.read_text())

    for key, value in subset.items():
        assert key in full, f"shared key {key} missing from the full file"
        if isinstance(value, str):
            assert full[key] == value
        else:
            assert full[key] == pytest.approx(value, rel=1e-4), f"{key} differs between the two files"

    missing = set(full) - set(subset)
    assert {
        "error_gain_ratio",
        "q_offset",
        "friction_viscous",
        "load_friction_motor",
        "load_friction_external",
        "dtheta_stribeck",
        "alpha",
    } <= missing


def test_bam_silently_defaults_the_unmeasured_m6_fields():
    """Document, in a test, which m6 fields the upstream JSON does NOT pin.

    If a BAM upgrade changes these defaults, the HD1910 model changes under us
    without any file changing — that is exactly the kind of drift this locks.
    """
    from bam.model import load_model

    model = load_model(json_file=str(HD1910_PARAMS_JSON))
    defaults = {
        name: p.value
        for name, p in model.get_parameters().items()
        if name
        in {
            "friction_viscous",
            "load_friction_motor",
            "load_friction_external",
            "load_friction_motor_stribeck",
            "load_friction_external_stribeck",
            "load_friction_motor_quad",
            "load_friction_external_quad",
            "dtheta_stribeck",
            "alpha",
        }
    }
    assert defaults == pytest.approx(
        {
            "friction_viscous": 0.1,
            "load_friction_motor": 0.05,
            "load_friction_external": 0.05,
            "load_friction_motor_stribeck": 0.05,
            "load_friction_external_stribeck": 0.05,
            "load_friction_motor_quad": 0.0,
            "load_friction_external_quad": 0.0,
            "dtheta_stribeck": 0.2,
            "alpha": 1.35,
        }
    ), "BAM defaults changed — re-read hd1910/PROVENANCE.md before training"


def test_preserved_params_are_actually_loaded():
    from bam.model import load_model

    model = load_model(json_file=str(HD1910_PARAMS_JSON))
    assert model.kt.value == pytest.approx(0.623761)
    assert model.R.value == pytest.approx(4.910192)
    assert model.armature.value == pytest.approx(0.0017819)
    assert model.friction_base.value == pytest.approx(0.015803)
    assert model.friction_stribeck.value == pytest.approx(0.009590)
    assert model.max_velocity.value == pytest.approx(100.0)


# --------------------------------------------------------------------------- #
# Task registration / observation contract
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("task_id", HD1910_TASKS)
def test_task_is_registered(task_id):
    assert task_id in list_tasks()


@pytest.mark.parametrize("task_id", HD1910_TASKS)
def test_task_uses_the_hd1910_robot(task_id):
    cfg = load_env_cfg(task_id)
    robot = cfg.scene.entities["robot"]
    assert robot.spec_fn.__name__.startswith("get_") and "hd1910" in robot.spec_fn.__name__
    from mjlab_microduck.robot.microduck_constants import HD1910_ACTIVE_PARAMS_JSON

    (act,) = robot.articulation.actuators
    assert Path(act.json_path) == Path(HD1910_ACTIVE_PARAMS_JSON)


@pytest.mark.parametrize("task_id", HD1910_TASKS)
def test_obs_slots_are_the_61d_contract(task_id):
    """48 proprio + twist(3) + head(4) + body(6), same term order as every policy."""
    cfg = load_env_cfg(task_id)
    terms = list(cfg.observations["actor"].terms)
    assert terms == [
        "base_ang_vel",
        "projected_gravity",
        "joint_pos",
        "joint_vel",
        "actions",
        "command",
        "head_command",
        "body_command",
    ]
    assert list(cfg.commands) == ["twist", "head_pose", "body_pose"]


@pytest.mark.parametrize("task_id", HD1910_TASKS)
def test_run_name_does_not_collide_with_xl330(task_id):
    rl = load_rl_cfg(task_id)
    assert rl.experiment_name == "velocity_hd1910"


def test_control_rate_is_still_50hz():
    cfg = load_env_cfg(HD1910_TASKS[0])
    dt = cfg.sim.mujoco.timestep * cfg.decimation
    assert dt == pytest.approx(0.02), f"policy runs at {1 / dt:.1f} Hz, must stay 50 Hz"


def test_mass_table_file_is_readable():
    with HD1910_MASS_TABLE.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows and {"body", "mass_g", "status"} <= set(rows[0])
