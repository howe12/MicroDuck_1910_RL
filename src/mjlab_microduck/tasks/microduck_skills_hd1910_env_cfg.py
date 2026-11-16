"""HD-1910 twins of the remaining skill tasks: sit/stand, ball kick, ground pick, spin.

Same pattern as the other HD-1910 wrappers — upstream recipe verbatim, robot
entity swapped — with two wrinkles worth knowing:

* ``ball_kick`` keeps a *ball* entity next to the robot, and the robot must stay
  the first entity (the base reset events write the root state at qpos[0:7]).
  So the wrapper replaces ``entities["robot"]`` instead of stomping the dict.
* ``spin`` is the roller variant (the one with free-spinning wheels), so it uses
  ``MICRODUCK_ROLLERS_HD1910_ROBOT_CFG`` rather than the full-collision one.

SitStand is the one to know about: ONE policy, both directions, driven by the
posture command in the twist slot (0 = stand, 1 = sit). The command flips
mid-episode, so each 12 s episode trains descent, seated rest, rise and standing
rest. Sit pose is knee +-1.35 / hip_pitch -+0.4079 / trunk z 0.060 m.
"""

from __future__ import annotations

import dataclasses

from mjlab.envs import ManagerBasedRlEnvCfg

from mjlab_microduck.robot.microduck_constants import (
    MICRODUCK_ALLCOLLISIONS_HD1910_ROBOT_CFG,
    MICRODUCK_ROLLERS_HD1910_ROBOT_CFG,
)
from mjlab_microduck.tasks.microduck_ball_kick_env_cfg import (
    MicroduckBallKickRlCfg,
    make_microduck_ball_kick_env_cfg,
)
from mjlab_microduck.tasks.microduck_ground_pick_env_cfg import (
    MicroduckGroundPickRlCfg,
    make_microduck_ground_pick_env_cfg,
)
from mjlab_microduck.tasks.microduck_sitstand_env_cfg import (
    MicroduckSitStandRlCfg,
    make_microduck_sitstand_env_cfg,
)
from mjlab_microduck.tasks.microduck_spin_env_cfg import (
    MicroduckSpinRlCfg,
    make_microduck_spin_env_cfg,
)


def _swap(cfg: ManagerBasedRlEnvCfg, robot_cfg) -> ManagerBasedRlEnvCfg:
    """Replace only the robot entry — skill scenes may carry extra entities."""
    cfg.scene.entities = {**cfg.scene.entities, "robot": robot_cfg}
    return cfg


def make_microduck_sitstand_hd1910_env_cfg(
    play: bool = False, rough: bool = False
) -> ManagerBasedRlEnvCfg:
    return _swap(
        make_microduck_sitstand_env_cfg(play=play, rough=rough),
        MICRODUCK_ALLCOLLISIONS_HD1910_ROBOT_CFG,
    )


def make_microduck_ball_kick_hd1910_env_cfg(play: bool = False) -> ManagerBasedRlEnvCfg:
    return _swap(
        make_microduck_ball_kick_env_cfg(play=play),
        MICRODUCK_ALLCOLLISIONS_HD1910_ROBOT_CFG,
    )


def make_microduck_ground_pick_hd1910_env_cfg(
    play: bool = False, rough: bool = False
) -> ManagerBasedRlEnvCfg:
    return _swap(
        make_microduck_ground_pick_env_cfg(play=play, rough=rough),
        MICRODUCK_ALLCOLLISIONS_HD1910_ROBOT_CFG,
    )


def make_microduck_spin_hd1910_env_cfg(play: bool = False) -> ManagerBasedRlEnvCfg:
    return _swap(
        make_microduck_spin_env_cfg(play=play),
        MICRODUCK_ROLLERS_HD1910_ROBOT_CFG,
    )


MicroduckSitStandHd1910RlCfg = dataclasses.replace(
    MicroduckSitStandRlCfg, experiment_name="sitstand_hd1910", run_name="sitstand_hd1910"
)
MicroduckBallKickHd1910RlCfg = dataclasses.replace(
    MicroduckBallKickRlCfg, experiment_name="ballkick_hd1910", run_name="ballkick_hd1910"
)
MicroduckGroundPickHd1910RlCfg = dataclasses.replace(
    MicroduckGroundPickRlCfg,
    experiment_name="groundpick_hd1910",
    run_name="groundpick_hd1910",
)
MicroduckSpinHd1910RlCfg = dataclasses.replace(
    MicroduckSpinRlCfg, experiment_name="spin_hd1910", run_name="spin_hd1910"
)

__all__ = [
    "make_microduck_sitstand_hd1910_env_cfg",
    "make_microduck_ball_kick_hd1910_env_cfg",
    "make_microduck_ground_pick_hd1910_env_cfg",
    "make_microduck_spin_hd1910_env_cfg",
    "MicroduckSitStandHd1910RlCfg",
    "MicroduckBallKickHd1910RlCfg",
    "MicroduckGroundPickHd1910RlCfg",
    "MicroduckSpinHd1910RlCfg",
]
