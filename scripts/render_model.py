#!/usr/bin/env python3
"""Render the MicroDuck model so a human can see what the RL agent is given.

Produces two images under ``docs/hd1910/images/``:

* ``model_views.png``  — front / three-quarter / side renders of the MJCF in the
  HOME pose (the pose every reset returns to).
* ``mass_map.png``     — every body's centre of mass in world coordinates,
  sized by mass, plus the whole-robot CoM and the support polygon edge.

Usage:
    MUJOCO_GL=egl uv run --no-sync scripts/render_model.py
    MUJOCO_GL=egl uv run --no-sync scripts/render_model.py --xml scene.xml   # XL330 版
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import mujoco
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "src/mjlab_microduck/robot/microduck"
OUT_DIR = ROOT / "docs/hd1910/images"

# HOME_FRAME from microduck_constants.py (joint name -> angle)
HOME = {
    "left_hip_yaw": 0.0, "left_hip_roll": -0.0873, "left_hip_pitch": -0.4579,
    "left_knee": -0.0049, "left_ankle": 0.4530,
    "neck_pitch": 0.3491, "head_pitch": 0.3491, "head_yaw": 0.0, "head_roll": 0.0,
    "right_hip_yaw": 0.0, "right_hip_roll": 0.0873, "right_hip_pitch": 0.4579,
    "right_knee": 0.0049, "right_ankle": -0.4530,
}
TRUNK_Z = 0.115


def load(xml: str):
    m = mujoco.MjModel.from_xml_path(str(MODEL_DIR / xml))
    d = mujoco.MjData(m)
    d.qpos[:] = m.qpos0
    # free joint is the trunk: place it at standing height and apply HOME angles
    if m.nq >= 7 and m.jnt_type[0] == mujoco.mjtJoint.mjJNT_FREE:
        d.qpos[2] = TRUNK_Z
    for name, angle in HOME.items():
        jid = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_JOINT, name)
        if jid >= 0:
            d.qpos[m.jnt_qposadr[jid]] = angle
    mujoco.mj_forward(m, d)
    return m, d


def render_views(m, d, size: int = 480):
    lookat = np.array([0.0, 0.0, 0.10])
    dist = 0.52
    views = [
        ("front", 180.0, -8.0),
        ("three-quarter", 135.0, -12.0),
        ("side", 90.0, -8.0),
    ]
    cam = mujoco.MjvCamera()
    mujoco.mjv_defaultCamera(cam)
    cam.lookat[:] = lookat
    cam.distance = dist
    cam.elevation = -10.0

    renderer = mujoco.Renderer(m, size, size)
    imgs = []
    for _name, azim, elev in views:
        cam.azimuth = azim
        cam.elevation = elev
        renderer.update_scene(d, camera=cam)
        imgs.append(renderer.render())
    renderer.close()
    return [v[0] for v in views], imgs


def make_views_figure(names, imgs, m, d, out: Path):
    mass = float(mujoco.mj_getTotalmass(m))
    fig, axes = plt.subplots(1, 3, figsize=(15, 5.4), facecolor="white")
    for ax, name, img in zip(axes, names, imgs):
        ax.imshow(img)
        ax.set_title(name, fontsize=12)
        ax.axis("off")
    com = d.subtree_com[0]
    fig.suptitle(
        f"MicroDuck + HD-1910   |   mass {mass * 1000:.1f} g   |   "
        f"CoM ({com[0] * 1000:.1f}, {com[1] * 1000:.1f}, {com[2] * 1000:.1f}) mm   |   "
        f"14 actuated joints, 61-dim obs, 50 Hz",
        fontsize=12, y=0.97,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(out, dpi=110, facecolor="white")
    plt.close(fig)


def make_mass_map(m, d, out: Path):
    """Two orthogonal views of the mass distribution (much more legible than 3D)."""
    bodies, masses, pts = [], [], []
    for i in range(1, m.nbody):
        if m.body_mass[i] > 1e-6:
            bodies.append(mujoco.mj_id2name(m, mujoco.mjtObj.mjOBJ_BODY, i) or f"body{i}")
            masses.append(m.body_mass[i])
            pts.append(d.xipos[i].copy())
    pts = np.asarray(pts)
    masses = np.asarray(masses)
    com = d.subtree_com[0]
    total = mujoco.mj_getTotalmass(m)
    sizes = 30 + 700 * (masses / masses.max()) ** 0.7

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13.5, 5.6), facecolor="white",
                                   gridspec_kw={"width_ratios": [1.25, 1]})

    # ---- side view: x (forward) vs z (up) ----
    ax1.scatter(pts[:, 0] * 1000, pts[:, 2] * 1000, s=sizes, c=masses * 1000,
                cmap="viridis", alpha=0.85, edgecolors="k", linewidths=0.4, zorder=3)
    ax1.axhline(0, color="0.6", lw=1)
    ax1.scatter([com[0] * 1000], [com[2] * 1000], marker="X", s=260, c="red",
                zorder=5, label=f"total CoM  z = {com[2] * 1000:.1f} mm")
    for name, mass in sorted(zip(bodies, masses), key=lambda x: -x[1])[:5]:
        i = bodies.index(name)
        ax1.annotate(f"{name}\n{mass * 1000:.0f} g",
                     (pts[i, 0] * 1000, pts[i, 2] * 1000),
                     textcoords="offset points", xytext=(10, 8), fontsize=7.5,
                     arrowprops=dict(arrowstyle="-", lw=0.6, color="0.4"))
    ax1.set_xlabel("x  [mm]  (forward)")
    ax1.set_ylabel("z  [mm]  (up)")
    ax1.set_title("side view: body CoM (bubble area ~ mass)", pad=14)
    ax1.set_ylim(-12, pts[:, 2].max() * 1000 + 45)
    ax1.grid(alpha=0.3)
    ax1.legend(loc="upper left", fontsize=8)
    ax1.set_xlim(min(pts[:, 0].min() * 1000, -60) - 15, max(pts[:, 0].max() * 1000, 40) + 15)

    # ---- top view: x vs y + support polygon ----
    ax2.scatter(pts[:, 0] * 1000, pts[:, 1] * 1000, s=sizes, c=masses * 1000,
                cmap="viridis", alpha=0.85, edgecolors="k", linewidths=0.4, zorder=3)
    feet = {}
    for name in ("left_foot", "right_foot"):
        sid = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_SITE, name)
        if sid >= 0:
            feet[name] = d.site_xpos[sid][:2].copy()
    if len(feet) == 2:
        poly = np.array([feet["left_foot"], feet["right_foot"]])
        ax2.plot(poly[:, 0] * 1000, poly[:, 1] * 1000, "s--", color="tab:blue",
                 ms=9, lw=1.2, label="foot sites")
    ax2.scatter([com[0] * 1000], [com[1] * 1000], marker="X", s=260, c="red", zorder=5,
                label="total CoM")
    ax2.set_xlabel("x  [mm]  (forward)")
    ax2.set_ylabel("y  [mm]  (left)")
    ax2.set_title("top view: left/right mass symmetry", pad=14)
    ax2.grid(alpha=0.3)
    ax2.legend(loc="upper left", fontsize=8)
    ax2.axis("equal")

    fig.suptitle(f"HD-1910 model mass distribution   |   total {total * 1000:.1f} g   |   "
                 f"CoM ({com[0] * 1000:+.1f}, {com[1] * 1000:+.1f}, {com[2] * 1000:.1f}) mm",
                 fontsize=11.5)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(out, dpi=110, facecolor="white")
    plt.close(fig)
    return bodies, masses, com


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--xml", default="scene_walk_hd1910.xml")
    ap.add_argument("--tag", default="hd1910")
    args = ap.parse_args(argv)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    m, d = load(args.xml)
    names, imgs = render_views(m, d)
    make_views_figure(names, imgs, m, d, OUT_DIR / f"model_views_{args.tag}.png")

    bodies, masses, com = make_mass_map(m, d, OUT_DIR / f"mass_map_{args.tag}.png")

    print(f"{args.xml}: total {mujoco.mj_getTotalmass(m) * 1000:.2f} g, "
          f"CoM z {com[2] * 1000:.2f} mm, {m.nu} actuators")
    for b, mm in sorted(zip(bodies, masses), key=lambda x: -x[1]):
        print(f"  {b:20s} {mm * 1000:8.3f} g")
    print("wrote:", OUT_DIR / f"model_views_{args.tag}.png")
    print("wrote:", OUT_DIR / f"mass_map_{args.tag}.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
