"""HD-1910 terrain tasks: uneven ground and explicit slopes.

MicroDuck already has a *rough* terrain (``MICRODUCK_ROUGH_TERRAINS_CFG``: random
boxes, roughness tiles, a gentle 1.7-5.7 deg pyramid slope). That covers "uneven
ground" and is registered as ``Mjlab-Velocity-Rough-MicroDuck-HD1910`` with the
same HD-1910 rebalance as the flat task.

What it does NOT cover is a real slope. ``slope_terrain.FlatRampTerrainCfg``
builds flat-run-up / ramp / run-out tiles whose angle ramps 2-20 deg with
difficulty — the shape used by the roller-slope tasks, and the closest match to
"walk up and down a hill".

Two tasks are registered here:

* ``Mjlab-Velocity-Rough-MicroDuck-HD1910``  — uneven ground (existing recipe)
* ``Mjlab-Velocity-Slope-MicroDuck-HD1910``  — 2-20 deg ramps (new)

Env count: both use the full-collision-free walk model, but rough/slope terrains
allocate far more collision geometry than the flat plane. Measured on this box:
512 and 1024 envs train fine, 2048 OOMs on the 8 GB card. Launch with
``--env.scene.num-envs 1024`` (about 2.4x slower per iteration than 4096 flat,
so budget accordingly).
"""

from __future__ import annotations

import dataclasses

from mjlab.envs import ManagerBasedRlEnvCfg
from mjlab.terrains.terrain_generator import TerrainGeneratorCfg

from mjlab_microduck.tasks import hd1910_tuning
from mjlab_microduck.tasks.microduck_velocity_env_cfg import (
    MICRODUCK_ROUGH_TERRAINS_CFG,
    make_microduck_velocity_env_cfg,
)
from mjlab_microduck.tasks.slope_terrain import FlatRampTerrainCfg

# Reuse the flat task's RL cfg (same observation/action contract, same net).
from mjlab_microduck.tasks.microduck_velocity_hd1910_env_cfg import (
    MicroduckHd1910RlCfg,
)
from mjlab_microduck.robot.microduck_constants import (
    MICRODUCK_WALK_HD1910_ROBOT_CFG,
)

# (width, height). Width must fit flat+ramp_max+runout = 13 m. Height was 6 m in
# the first slope run and 11.5 out-of-terrain terminations per iteration (vs 0.17
# falls) drowned the signal: with lateral commands up to +-0.3 m/s a robot leaves
# a 6 m wide tile in ~10 s, so most episodes ended by walking into the void
# rather than by walking badly. 12 m keeps it on the tile for a full episode.
TILE_SIZE = (14.0, 12.0)

# Ramps are walked BOTH ways: the robot spawns on the flat run-up, the command
# resamples as it goes, so it climbs, descends and traverses at random. 2 deg at
# difficulty 0 keeps the first curriculum row trivially walkable.
SLOPE_TERRAINS_CFG = TerrainGeneratorCfg(
    size=TILE_SIZE,
    curriculum=True,
    num_rows=10,          # 10 difficulty levels -> ~2 deg per row up to 20 deg
    num_cols=1,
    difficulty_range=(0.0, 1.0),
    sub_terrains={
        "ramp": FlatRampTerrainCfg(
            flat_length=2.0,
            ramp_length_range=(4.0, 8.0),
            runout_length=3.0,
            spawn_on_ramp=0.0,   # start on the flat, walk onto the ramp
            deg_min=2.0,
            deg_max=20.0,
        )
    },
)


def _build(rough: bool, slope: bool, play: bool) -> ManagerBasedRlEnvCfg:
    cfg = make_microduck_velocity_env_cfg(play=play, rough=rough or slope)
    cfg.scene.entities = {"robot": MICRODUCK_WALK_HD1910_ROBOT_CFG}
    hd1910_tuning.apply_velocity_rebalance(cfg)
    if slope:
        # play=True clears the terrain generator (flat plane for play/export);
        # only override when actually training on terrain.
        if not play:
            cfg.scene.terrain.terrain_generator = SLOPE_TERRAINS_CFG
        # Ramp walking needs the terrain-level curriculum (distance-based)
        # enabled so robots climb the difficulty rows as they succeed.
        cfg.scene.terrain.max_init_terrain_level = 2
    return cfg


def make_microduck_velocity_slope_hd1910_env_cfg(
    play: bool = False,
) -> ManagerBasedRlEnvCfg:
    return _build(rough=False, slope=True, play=play)


MicroduckHd1910SlopeRlCfg = dataclasses.replace(
    MicroduckHd1910RlCfg, experiment_name="velocity_slope_hd1910",
    run_name="velocity_slope_hd1910",
)

__all__ = [
    "make_microduck_velocity_slope_hd1910_env_cfg",
    "MicroduckHd1910SlopeRlCfg",
    "SLOPE_TERRAINS_CFG",
    "MICRODUCK_ROUGH_TERRAINS_CFG",
]
