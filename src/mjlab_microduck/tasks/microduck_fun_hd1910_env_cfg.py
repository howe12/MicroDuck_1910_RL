"""Fun skills: a cute sway-and-nod dance, and a crouch-jump probe.

Both start from the velocity task rather than the harsh one: a dance should not be
fighting a randomised power envelope, and the jump question ("how high can it
actually go?") is best answered without extra disturbances muddying the number.
"""
from __future__ import annotations

import dataclasses

from mjlab.envs import ManagerBasedRlEnvCfg
from mjlab.managers.reward_manager import RewardTermCfg

from mjlab_microduck.tasks import mdp as microduck_mdp
from mjlab_microduck.tasks.microduck_velocity_env_cfg import (
    make_microduck_velocity_env_cfg,
)


def _pin_zero_command(cfg: ManagerBasedRlEnvCfg) -> ManagerBasedRlEnvCfg:
    """Force a zero velocity command for the whole run.

    A dance or a jump happens in place. Left on the walking task's random command
    ranges the policy would be asked to dance *and* track a random velocity at the
    same time - two objectives that disagree, so it would learn neither.
    """
    r = cfg.commands["twist"].ranges
    r.lin_vel_x = (0.0, 0.0)
    r.lin_vel_y = (0.0, 0.0)
    r.ang_vel_z = (0.0, 0.0)
    cfg.commands["twist"].heading_command = False
    r.heading = None
    return cfg


def make_microduck_dance_hd1910_env_cfg(play: bool = False) -> ManagerBasedRlEnvCfg:
    cfg = _pin_zero_command(make_microduck_velocity_env_cfg(play=play))

    # Command is pinned to zero by the caller: a dance happens in place.
    cfg.rewards["dance_sway_nod"] = RewardTermCfg(
        func=microduck_mdp.dance_sway_nod_reward,
        weight=3.0,
        params={"sway_amp_rad": 0.16, "sway_hz": 0.45,
                "nod_amp_rad": 0.28, "nod_hz": 0.9},
    )
    # Standing still must not be the cheapest way to collect the shaping term.
    cfg.rewards["dance_liveliness"] = RewardTermCfg(
        func=microduck_mdp.upright_hold_reward,
        weight=0.5,
        params={"base_height": 0.10},
    )
    cfg.episode_length_s = 12.0
    return cfg


def make_microduck_jump_hd1910_env_cfg(play: bool = False) -> ManagerBasedRlEnvCfg:
    cfg = _pin_zero_command(make_microduck_velocity_env_cfg(play=play))

    cfg.rewards["jump_airborne"] = RewardTermCfg(
        func=microduck_mdp.jump_airborne_reward,
        weight=12.0,
        params={"base_height": 0.115, "foot_clear_m": 0.02},
    )
    cfg.rewards["jump_landing"] = RewardTermCfg(
        func=microduck_mdp.upright_hold_reward,
        weight=1.0,
        params={"base_height": 0.10},
    )
    cfg.episode_length_s = 6.0
    return cfg
