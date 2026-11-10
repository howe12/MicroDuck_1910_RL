#!/usr/bin/env python3
"""Generate the HD1910 MJCF models from a per-body mass table.

Why a generator instead of hand-editing the XMLs:

* The mass table is the thing that gets re-measured. Re-weighing must be a
  one-line CSV edit plus a regeneration, never 16 hand edits across 3 files.
* ``--check`` re-derives the models in memory and fails if the committed XMLs
  drift from the table, so the two can never disagree silently.
* The output keeps every other byte of the source file (comments, geom order,
  mesh references) — the diff is exactly the ``<inertial .../>`` lines plus a
  banner. That is what makes the MJCF diff reviewable.

Inertia policy per body (``inertia_mode`` column):

* ``scale``    — scale the baseline inertia tensor by mass_new/mass_base about
                 the body origin (isotropic, COM-independent). The honest
                 fallback when you only own a scale.
* ``explicit`` — use the 6 values in ``inertia_kgm2`` (ixx iyy izz ixy ixz iyz).
* ``keep``     — keep the baseline tensor (only mass/COM change).

A body whose ``status`` is not ``measured`` keeps its baseline inertial and the
generated file is stamped ``PLACEHOLDER``. Models stamped PLACEHOLDER are fine
for smoke tests and pipeline validation, and are NOT valid HD1910 dynamics.

Usage:
    python build_hd1910_mjcf.py --init-table   # write a blank table from the MJCF
    python build_hd1910_mjcf.py                # regenerate the 3 models
    python build_hd1910_mjcf.py --check        # verify committed models match
    python build_hd1910_mjcf.py --audit        # print the current mass/inertia table
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODEL_DIR = HERE.parent  # robot/microduck — same dir as the source XMLs (meshdir="assets")
TABLE = HERE / "mass_table.csv"

SOURCE_MODELS = {
    "walk": "robot_walk.xml",
    "allcollisions": "robot_allcollisions.xml",
    "rollers": "robot_allcollisions_rollers.xml",
}

# body -> what a human has to put on the scale. "推定" = inferred from the mesh
# part names, confirm against the real hardware before trusting it.
PART_LABELS: dict[str, str] = {
    "trunk_base": "躯干壳体 + 电池 + 主控/扩展 PCB + 线束（推定）",
    "yaw2roll": "左髋 yaw→roll 支架 + 舵机（推定）",
    "hip_l": "左髋 roll 支架 + 轴承（推定）",
    "upper_leg_left": "左大腿 + 舵机 + 舵盘（推定）",
    "leg": "左小腿 + 舵机（推定）",
    "ankle_left": "左踝/足 + 舵机 + 脚垫（推定）",
    "neck": "颈座 + 舵机（推定）",
    "neck_pitch": "颈俯仰摇臂 + 舵机（推定）",
    "yaw_roll_motion": "头部 yaw/roll 联动件 + 舵机（推定）",
    "jaw_soft": "头部上下壳 + 喙 + 镜头（推定）",
    "bearing_roll": "右髋 yaw→roll 支架 + 舵机（推定）",
    "hip_l_2": "右髋 roll 支架 + 轴承（推定）",
    "upper_leg_right": "右大腿 + 舵机 + 舵盘（推定）",
    "leg_2": "右小腿 + 舵机（推定）",
    "ankle_right": "右踝/足 + 舵机 + 脚垫（推定）",
    "ankle_l_v1": "左踝/足（轮滑版）+ 舵机（推定）",
    "ankle_r_v1": "右踝/足（轮滑版）+ 舵机（推定）",
    "tire": "滚动轮 1（轮滑版）",
    "tire_2": "滚动轮 2（轮滑版）",
    "tire_3": "滚动轮 3（轮滑版）",
    "tire_4": "滚动轮 4（轮滑版）",
}

TABLE_FIELDS = [
    "body",
    "part_label",
    "mass_g",
    "com_mm",
    "inertia_mode",
    "inertia_kgm2",
    "status",
    "source",
    "notes",
]


@dataclass
class BodyRow:
    body: str
    mass_g: float | None
    com_mm: tuple[float, float, float] | None
    inertia_mode: str
    inertia_kgm2: tuple[float, float, float, float, float, float] | None
    status: str

    @property
    def measured(self) -> bool:
        return self.status.strip().lower() == "measured" and self.mass_g is not None

    @property
    def estimated(self) -> bool:
        """Datasheet/CAD-derived, not weighed. Applies to the model but is stamped."""
        return self.status.strip().lower() == "estimated" and self.mass_g is not None

    @property
    def applied(self) -> bool:
        return self.measured or self.estimated


# --------------------------------------------------------------------------- #
# Table I/O
# --------------------------------------------------------------------------- #
def read_table(path: Path = TABLE) -> dict[str, BodyRow]:
    if not path.exists():
        raise FileNotFoundError(f"mass table not found: {path} (run --init-table)")
    rows: dict[str, BodyRow] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for raw in csv.DictReader(f):
            if not raw.get("body"):
                continue
            rows[raw["body"]] = BodyRow(
                body=raw["body"],
                mass_g=_f(raw.get("mass_g")),
                com_mm=_vec3(raw.get("com_mm")),
                inertia_mode=(raw.get("inertia_mode") or "scale").strip(),
                inertia_kgm2=_vec6(raw.get("inertia_kgm2")),
                status=(raw.get("status") or "placeholder").strip(),
            )
    return rows


def _f(v: str | None) -> float | None:
    if v is None or not str(v).strip():
        return None
    return float(str(v).strip())


def _floats(v: str | None, n: int) -> tuple | None:
    if v is None or not str(v).strip():
        return None
    parts = [p for p in re.split(r"[,\s]+", str(v).strip()) if p]
    if len(parts) != n:
        raise ValueError(f"expected {n} numbers, got {len(parts)}: {v!r}")
    return tuple(float(p) for p in parts)


def _vec3(v: str | None):
    return _floats(v, 3)


def _vec6(v: str | None):
    return _floats(v, 6)


# --------------------------------------------------------------------------- #
# MJCF rewriting (text-level, so every unrelated byte is preserved)
# --------------------------------------------------------------------------- #
_INERTIAL_RE = re.compile(r"^(?P<indent>\s*)<inertial\b[^>]*/>\s*$")
_BODY_OPEN_RE = re.compile(r"^\s*<body\b[^>]*\bname=\"(?P<name>[^\"]+)\"")
_INERTIAL_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')


def _fmt(x: float) -> str:
    """Compact, round-trippable float formatting (matches MJCF style)."""
    return f"{x:.9g}"


def _rewrite_inertial(line: str, attrs: dict[str, str]) -> str:
    indent = re.match(r"^(\s*)", line).group(1)
    ordered = ["pos", "mass", "fullinertia"]
    tail = " ".join(f'{k}="{attrs[k]}"' for k in ordered)
    return f"{indent}<inertial {tail}/>\n"


def _scaled_inertia(base: tuple, ratio: float) -> tuple:
    return tuple(v * ratio for v in base)


def apply_table(xml_text: str, rows: dict[str, BodyRow], *, strict: bool) -> tuple[str, bool]:
    """Return (new_xml, used_placeholder_data)."""
    lines = xml_text.splitlines(keepends=True)
    stack: list[str] = []
    out: list[str] = []
    placeholder = False

    for line in lines:
        m = _BODY_OPEN_RE.match(line)
        if m:
            stack.append(m.group("name"))
            out.append(line)
            continue
        if "</body>" in line:
            if stack:
                stack.pop()
            out.append(line)
            continue

        mi = _INERTIAL_RE.match(line)
        if mi and stack:
            body = stack[-1]
            row = rows.get(body)
            attrs = dict(_INERTIAL_ATTR_RE.findall(line))
            if row is None:
                if strict:
                    raise KeyError(f"body {body!r} has an <inertial> but no row in {TABLE.name}")
                out.append(line)
                continue
            if not row.applied:
                # `baseline` = reviewed, intentionally unchanged (no servo here).
                # Anything else means the row was never looked at.
                if row.status.strip().lower() != "baseline":
                    placeholder = True
                out.append(line)
                continue

            base_mass = float(attrs["mass"])
            new_mass = row.mass_g / 1000.0
            attrs["mass"] = _fmt(new_mass)

            if row.com_mm is not None:
                attrs["pos"] = " ".join(_fmt(c / 1000.0) for c in row.com_mm)

            if row.inertia_mode == "explicit":
                if row.inertia_kgm2 is None:
                    raise ValueError(f"{body}: inertia_mode=explicit but inertia_kgm2 is empty")
                attrs["fullinertia"] = " ".join(_fmt(v) for v in row.inertia_kgm2)
            elif row.inertia_mode == "scale":
                base = [float(v) for v in attrs["fullinertia"].split()]
                ratio = new_mass / base_mass
                attrs["fullinertia"] = " ".join(_fmt(v * ratio) for v in base)
            elif row.inertia_mode == "keep":
                pass
            else:
                raise ValueError(f"{body}: unknown inertia_mode {row.inertia_mode!r}")

            out.append(_rewrite_inertial(line, attrs))
            continue

        out.append(line)

    return "".join(out), placeholder


def _banner(placeholder: bool, source: str, *, estimated: bool = False) -> str:
    if placeholder:
        tag = "PLACEHOLDER MASSES — NOT HD1910 DYNAMICS"
    elif estimated:
        tag = "ESTIMATED MASSES — datasheet-derived, NOT weighed (replace via mass_table.csv)"
    else:
        tag = "MEASURED MASSES — HD1910"
    return (
        f"<!-- GENERATED by hd1910/build_hd1910_mjcf.py from {source} + hd1910/mass_table.csv -->\n"
        f"<!-- {tag} -->\n"
        f"<!-- chosen_actuator PD = CPU-rehearsal equivalent of the BAM HD1910 model; "
        f"BAM overwrites it during training -->\n"
    )


# --------------------------------------------------------------------------- #
# CPU-rehearsal PD (the `chosen_actuator` default class)
# --------------------------------------------------------------------------- #
# infer_policy.py / scene_*.xml run MuJoCo's BUILT-IN position servo, not BAM.
# During training BAM overwrites forcerange, armature, damping and frictionloss
# from the actuator model (bam/mjlab.py:_configure_motors), so the values below
# only affect the CPU rehearsal path. They are still worth deriving instead of
# copying XL330's numbers, otherwise the rehearsal is wrong by ~1.4x in
# stiffness and ~1.06x in torque ceiling.
#
# Derived from hd1910/1910_m6.json + BAM's STS3215 actuator constants
# (bam/feetech/actuator.py: error_gain=0.166, max_pwm=0.97; bam/mjlab.py:
# forcerange = vin_max*kt/R). tests/test_hd1910_actuator.py re-derives every
# one of these numbers from the installed bam package and fails on mismatch.
#
#   k_eff      = error_gain * ratio * kp_fw * vin_nom * max_pwm * kt / R
#              = 0.166 * 0.981928 * 5 * 7.7 * 0.97 * 0.7355 / 3.7     = 1.210000
#   force_rng  = kt * vin_max / R = 0.7355 * 8.0 / 3.7               = 1.590300
#   damping    = XL330 rehearsal damping scaled by stiffness ratio
#              = 0.053 * (1.210000 / 0.55)                          = 0.116604
#   friction   = friction_base from the fitted model                = 0.015803
#   armature   = armature from the fitted model                     = 0.0017819
HD1910_PD = {
    "kp": "1.210000",
    "kv": "0.0",
    "forcerange": "-1.590300 1.590300",
    "damping": "0.116604",
    "frictionloss": "0.015803",
    "armature": "0.0017819",
    "ctrlrange": "-10.0 10.0",
}

def apply_actuator_defaults(xml_text: str) -> str:
    """Rewrite the `chosen_actuator` default class with the HD1910 PD values.

    The class body is replaced wholesale (including its comments), because the
    values inside it are derivable and the XL330 provenance is misleading once
    the servo changes.
    """
    start = xml_text.find('<default class="chosen_actuator">')
    if start < 0:
        raise ValueError('no <default class="chosen_actuator"> block in the XML')
    end = xml_text.find("</default>", start)
    if end < 0:
        raise ValueError("unterminated chosen_actuator block")
    end += len("</default>")
    # Preserve the indentation of the opening tag line.
    line_start = xml_text.rfind("\n", 0, start) + 1
    indent = xml_text[line_start:start]
    inner = indent + "  "
    block = (
        f'{indent}<default class="chosen_actuator">\n'
        f'{inner}<!-- HD1910 (BAM-derived, CPU rehearsal only) -->\n'
        f'{inner}<geom contype="0" conaffinity="0"/>\n'
        f'{inner}<joint damping="{HD1910_PD["damping"]}" frictionloss="{HD1910_PD["frictionloss"]}" '
        f'armature="{HD1910_PD["armature"]}"/>\n'
        f'{inner}<position kp="{HD1910_PD["kp"]}" kv="{HD1910_PD["kv"]}" '
        f'forcerange="{HD1910_PD["forcerange"]}" ctrlrange="{HD1910_PD["ctrlrange"]}"/>\n'
        f"{indent}</default>"
    )
    return xml_text[:line_start] + block + xml_text[end:]


def render_model(name: str, rows: dict[str, BodyRow], *, strict: bool) -> tuple[str, bool]:
    src = MODEL_DIR / SOURCE_MODELS[name]
    text = src.read_text(encoding="utf-8")
    body, placeholder = apply_table(text, rows, strict=strict)
    body = apply_actuator_defaults(body)
    lines = body.splitlines(keepends=True)
    # Insert the banner after the XML declaration.
    insert_at = 1 if lines and lines[0].startswith("<?xml") else 0
    has_estimated = any(r.estimated for r in rows.values())
    lines.insert(insert_at, _banner(placeholder, src.name, estimated=has_estimated))
    return "".join(lines), placeholder


def out_path(name: str) -> Path:
    return MODEL_DIR / f"{SOURCE_MODELS[name].removesuffix('.xml')}_hd1910.xml"


# --------------------------------------------------------------------------- #
# Table bootstrap / audit
# --------------------------------------------------------------------------- #
def _bodies_from_mjcf(model: str = "walk") -> list[tuple[str, str, float]]:
    """(body, parent, mass_g) in model order, read from the MJCF text."""
    text = (MODEL_DIR / SOURCE_MODELS[model]).read_text(encoding="utf-8")
    stack: list[str] = []
    found: list[tuple[str, str, float]] = []
    for line in text.splitlines():
        m = _BODY_OPEN_RE.match(line)
        if m:
            stack.append(m.group("name"))
            continue
        if "</body>" in line:
            if stack:
                stack.pop()
            continue
        mi = _INERTIAL_RE.match(line)
        if mi and stack:
            attrs = dict(_INERTIAL_ATTR_RE.findall(line))
            parent = stack[-2] if len(stack) > 1 else "world"
            found.append((stack[-1], parent, float(attrs["mass"]) * 1000.0))
    return found


def _all_bodies() -> list[tuple[str, str, float]]:
    """Union of bodies across the 3 models, walk model first (stable order).

    The roller model replaces the feet with ankle_*_v1 + 4 tires, so a table
    built from the walk model alone would silently miss 6 rows.
    """
    seen: dict[str, tuple[str, str, float]] = {}
    for model in SOURCE_MODELS:
        for body, parent, mass in _bodies_from_mjcf(model):
            seen.setdefault(body, (body, parent, mass))
    return list(seen.values())


def init_table(force: bool = False) -> None:
    if TABLE.exists() and not force:
        raise SystemExit(f"{TABLE} already exists (use --force to overwrite)")
    with TABLE.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TABLE_FIELDS)
        w.writeheader()
        for body, _parent, _mass in _all_bodies():
            w.writerow(
                {
                    "body": body,
                    "part_label": PART_LABELS.get(body, ""),
                    "mass_g": "",
                    "com_mm": "",
                    "inertia_mode": "scale",
                    "inertia_kgm2": "",
                    "status": "placeholder",
                    "source": "",
                    "notes": "",
                }
            )
    print(f"wrote template: {TABLE}")


def audit() -> None:
    rows = read_table()
    bodies = _all_bodies()
    base_total = sum(m for _, _, m in bodies)
    new_total = 0.0
    print(f"{'body':20s}{'baseline_g':>11s}{'hd1910_g':>10s}{'delta_g':>9s}{'status':>13s}")
    for body, _parent, mass in bodies:
        row = rows.get(body)
        new = row.mass_g if row and row.applied else None
        new_total += new if new is not None else mass
        delta = f"{new - mass:+.3f}" if new is not None else "-"
        state = row.status if row else "MISSING"
        print(f"{body:20s}{mass:11.3f}{(f'{new:.3f}' if new is not None else '-'):>10s}{delta:>9s}{state:>13s}")
    print(f"{'UNION TOTAL':20s}{base_total:11.3f}{new_total:10.3f}{new_total - base_total:+9.3f}  (walk+rollers bodies, not a robot mass)")

    for model in SOURCE_MODELS:
        b = _bodies_from_mjcf(model)
        base = sum(m for _, _, m in b)
        proj = sum((rows[x].mass_g if x in rows and rows[x].applied else m) for x, _, m in b)
        print(f"  {model:14s} baseline {base:8.3f} g -> table {proj:8.3f} g  ({proj - base:+.3f} g)")

    measured = sum(1 for b, _, _ in bodies if (r := rows.get(b)) and r.measured)
    estimated = sum(1 for b, _, _ in bodies if (r := rows.get(b)) and r.estimated)
    baseline = sum(1 for b, _, _ in bodies if (r := rows.get(b)) and r.status.strip().lower() == "baseline")
    print(f"\nbodies: {measured} measured, {estimated} estimated, {baseline} baseline-unchanged, "
          f"{len(bodies) - measured - estimated - baseline} untouched")
    if measured + estimated + baseline < len(bodies):
        print("WARNING: models generated now would be stamped PLACEHOLDER.")


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--init-table", action="store_true", help="write a blank mass table from the MJCF")
    ap.add_argument("--force", action="store_true", help="overwrite an existing table")
    ap.add_argument("--check", action="store_true", help="verify committed models match the table")
    ap.add_argument("--audit", action="store_true", help="print baseline vs table masses")
    ap.add_argument("--strict", action="store_true", help="fail if a body has no table row")
    args = ap.parse_args(argv)

    if args.init_table:
        init_table(force=args.force)
        return 0
    if args.audit:
        audit()
        return 0

    rows = read_table()
    rc = 0
    for name in SOURCE_MODELS:
        text, placeholder = render_model(name, rows, strict=args.strict)
        dst = out_path(name)
        if args.check:
            current = dst.read_text(encoding="utf-8") if dst.exists() else ""
            if current != text:
                print(f"DRIFT: {dst.name} does not match mass_table.csv", file=sys.stderr)
                rc = 1
            else:
                print(f"ok: {dst.name}{'  (PLACEHOLDER)' if placeholder else ''}")
        else:
            dst.write_text(text, encoding="utf-8")
            print(f"wrote {dst.name}{'  (PLACEHOLDER)' if placeholder else ''}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
