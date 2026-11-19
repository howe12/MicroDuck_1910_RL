#!/usr/bin/env python3
"""Package a self-trained ONNX into the kissqy firmware's model-directory format.

The HD-1910 firmware does not load a bare ``.onnx``: it loads a *directory* whose
``deployment-contract.json`` must agree, field by field, with the graph itself.
That mismatch is what produced

    /opt/robot/policies/local/deployment-contract.json: No such file or directory

What this does: takes the shipped walking package as the template (so the
actuator/BAM block, joint table and observation layout keep the values the
firmware already accepts), then rewrites only the parts that describe *our*
training run — task name, ONNX metadata, training HOME, hashes.

The task name must become ``Mjlab-VelStand-Flat-MicroDuck`` (no ``-HD1910``
suffix): ``model_packages.slot_for`` skips the VelStand prefix in its general
match and only accepts that exact string for the walk slot.

Filters are deliberately left out of the package: ``deployment-execution.json``
is only accepted for the vendor's rough-joint walk model, and the contract's
``action_filter`` block is not validated for this task. The runtime's own
``[policy] action_scale`` still applies — set it to 1.0 if the policy was
trained at 1.0.

    uv run --no-sync scripts/pack_deployment.py \
        --onnx policies/hd1910_walk_all.onnx \
        --template /tmp/tpl/m \
        --out /tmp/pkg_walk
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

# Same order the firmware uses for policy joints (mouth is the 15th servo and is
# not part of the action space).
NAMES = (
    "left_hip_yaw", "left_hip_roll", "left_hip_pitch", "left_knee", "left_ankle",
    "neck_pitch", "head_pitch", "head_yaw", "head_roll",
    "right_hip_yaw", "right_hip_roll", "right_hip_pitch", "right_knee", "right_ankle",
)

# The one task name the firmware maps to the walk slot.
WALK_TASK = "Mjlab-VelStand-Flat-MicroDuck"


def onnx_metadata(path: Path) -> dict[str, str]:
    import onnx

    model = onnx.load(str(path), load_external_data=False)
    return {p.key: p.value for p in model.metadata_props}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--onnx", required=True)
    ap.add_argument("--template", required=True, help="shipped model directory to copy from")
    ap.add_argument("--out", required=True)
    ap.add_argument("--task", default=WALK_TASK)
    ap.add_argument("--keep-backlash-suffix", action="store_true",
                    help="template came from a backlash model; default strips _backlash")
    args = ap.parse_args(argv)

    onnx_path = Path(args.onnx)
    template = Path(args.template)
    out = Path(args.out)

    digest = hashlib.sha256(onnx_path.read_bytes()).hexdigest()
    metadata = onnx_metadata(onnx_path)
    home = [float(v) for v in metadata["default_joint_pos"].split(",")]
    if len(home) != len(NAMES):
        raise SystemExit(f"default_joint_pos has {len(home)} values, expected {len(NAMES)}")
    if metadata.get("joint_names", "").split(",") != list(NAMES):
        raise SystemExit("ONNX joint_names is not the firmware's 14-joint order")

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for name in ("training-request.json", "upstream-params", "model-info.json",
                 "training-config.json", "使用说明.md"):
        src = template / name
        if src.is_dir():
            shutil.copytree(src, out / name)
        elif src.exists():
            shutil.copy2(src, out / name)
    shutil.copy2(onnx_path, out / "policy.onnx")

    contract = json.loads((template / "deployment-contract.json").read_text(encoding="utf-8"))
    contract["task"] = args.task
    contract["onnx_metadata"] = metadata
    contract["action_ema_old_weight"] = 0.0
    if not args.keep_backlash_suffix:
        for key in ("joint_pos", "joint_vel"):
            callable_ = contract["observations"]["terms"][key]["func"]["callable"]
            contract["observations"]["terms"][key]["func"]["callable"] = callable_.replace(
                "_backlash", ""
            )
    for joint, value in zip(contract["model"]["joints"], home):
        if joint["name"] not in NAMES:  # keep table order aligned with NAMES instead
            break
    by_name = {j["name"]: j for j in contract["model"]["joints"]}
    for name, value in zip(NAMES, home):
        by_name[name]["home_rad"] = value
    contract["model"]["joints"] = [by_name[n] for n in NAMES]
    if "runtime_home_rad" in contract:
        contract["runtime_home_rad"] = home
    contract.pop("gear_backlash", None)
    (out / "deployment-contract.json").write_text(
        json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    manifest = json.loads((template / "manifest.json").read_text(encoding="utf-8"))
    manifest["task"] = args.task
    manifest["onnx_sha256"] = digest
    manifest.pop("gear_backlash", None)
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(json.dumps({
        "out": str(out),
        "task": args.task,
        "sha256": digest,
        "model_id": digest[:32],
        "files": sorted(p.name for p in out.iterdir()),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
