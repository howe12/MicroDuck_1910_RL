"""HD-1910 reward rebalance, shared by every task that inherits the walk layer.

The velocity recipe shipped upstream was tuned on the XL330. Run 1 on the
HD-1910 actuator (identical recipe, heavier and 2.2x stiffer robot) converged to
"stand still and march in place": at iteration 4072 a standing robot still
collected upright 1.77 + head_pose 1.71 + air_time 1.02 + pose 0.61 of the ~6.5
available, while walking added at most ~0.6 but risked all of it. Run 2 with the
changes below cut the measured steady-state velocity error from 0.41 to 0.06 m/s
(evaluate_policy.py, per-axis).

Any task that builds on ``make_microduck_velocity_env_cfg`` — today Velocity and
VelStand — needs the same treatment, or it silently inherits the recipe we know
fails here.
"""

from __future__ import annotations

from mjlab.managers import RewardTermCfg

from mjlab_microduck.tasks import mdp as microduck_mdp


def apply_velocity_rebalance(
    cfg,
    *,
    air_time_progress: bool = True,
    track_linear_weight: float = 4.0,
    head_pose_weight: float = 1.0,
    no_progress_weight: float = 1.5,
):
    """Make walking worth more than standing, on the HD-1910 dynamics.

    Args:
        cfg: a ``ManagerBasedRlEnvCfg`` that already has the velocity rewards.
        air_time_progress: replace ``air_time`` with the version that only pays
            for displacement along the command. Turn off for tasks that already
            wrap ``air_time`` with their own gate (VelStand gates on upright).
    """
    cfg.rewards["track_linear_velocity"].weight = track_linear_weight
    cfg.rewards["head_pose_tracking"].weight = head_pose_weight

    if air_time_progress:
        cfg.rewards["air_time"].func = microduck_mdp.feet_air_time_with_progress

    # 1.5 x dt(0.02) = 0.03/step at full standstill, vs 0.067/step the tracking
    # Gaussian still pays for standing under a 0.15 m/s command. Run 3 measured
    # that 0.5 was 8x too weak to matter.
    cfg.rewards["no_progress"] = RewardTermCfg(
        func=microduck_mdp.no_progress_penalty,
        # Self-negating function -> POSITIVE weight (see AGENTS.md).
        weight=no_progress_weight,
        params={"command_name": "twist", "min_speed": 0.02},
    )
    return cfg
