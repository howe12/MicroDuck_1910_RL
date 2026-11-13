#!/usr/bin/env python3
"""Merge a walking policy and a stand-up policy into ONE ONNX with a gate.

Why not merge the weights: two independently trained policies sit in different
basins of the loss surface. Averaging them produces a network that is good at
neither — there is no meaningful "sum" of two PPO solutions.

Why a gate works: every MicroDuck policy in this repo speaks the same contract
(``obs[1,61] -> actions[1,14]``, identical joint order, identical normalisation
baked in). So the two networks can simply both run on the same observation and
a switch picks one output. That switch is a single comparison on the projected
gravity, which is already in the observation vector.

    obs layout (indices): 0-2 base_ang_vel, 3-5 projected_gravity,
    6-19 joint_pos, 20-33 joint_vel, 34-47 last_action, 48-50 command,
    51-54 head_command, 55-60 body_command

``projected_gravity_z`` is -1 upright and +1 lying on the back.

    uv run --no-sync scripts/merge_policies.py \
        --walk policies/hd1910_v3_walk.onnx \
        --stand policies/hd1910_standup.onnx \
        --out policies/hd1910_walk_stand.onnx
"""

from __future__ import annotations

import argparse
import sys

import numpy as np

GRAVITY_Z_INDEX = 5      # projected_gravity[:, 2]
STAND_THRESHOLD = -0.5   # g_z above this => not upright => use the stand-up net


def _rename(model, prefix: str, out_name: str):
    """Prefix every node/initializer so two graphs can coexist, and rename the output.

    Graph INPUTS are left alone — they are shared between the two subgraphs (both
    models take the same ``obs``), so they must keep their original name.
    """
    import onnx

    graph_inputs = {i.name for i in model.graph.input}
    for node in model.graph.node:
        node.name = f"{prefix}{node.name}"
        node.input[:] = [
            i if (not i or i in graph_inputs) else f"{prefix}{i}" for i in node.input
        ]
        node.output[:] = [f"{prefix}{o}" if o else o for o in node.output]
    for init in model.graph.initializer:
        init.name = f"{prefix}{init.name}"
    for vi in model.graph.value_info:
        vi.name = f"{prefix}{vi.name}"

    graph_out = model.graph.output[0]
    graph_out.name = f"{prefix}{graph_out.name}"
    # Re-point the producing node at the renamed output.
    for node in model.graph.node:
        node.output[:] = [out_name if o == graph_out.name else o for o in node.output]
    return model


def build(walk_path: str, stand_path: str, out_path: str, threshold: float) -> None:
    import onnx
    from onnx import TensorProto, helper

    walk = _rename(onnx.load(walk_path), "w_", "walk_actions")
    stand = _rename(onnx.load(stand_path), "s_", "stand_actions")

    obs_in = walk.graph.input[0]
    merged = helper.make_model(
        helper.make_graph(
            nodes=[],
            name="microduck_walk_stand_gate",
            inputs=[obs_in],
            outputs=[],
            initializer=[],
        ),
        opset_imports=[helper.make_opsetid("", 18)],
    )
    merged.graph.node.extend(walk.graph.node)
    merged.graph.node.extend(stand.graph.node)
    merged.graph.initializer.extend(walk.graph.initializer)
    merged.graph.initializer.extend(stand.graph.initializer)
    for name in ("walk_actions", "stand_actions"):
        merged.graph.value_info.append(
            helper.make_tensor_value_info(name, TensorProto.FLOAT, [1, 14])
        )

    # cond = obs[:, 5] > threshold   -> (1,1) via a constant index tensor
    idx = helper.make_tensor("gz_idx", TensorProto.INT64, [1, 1], [GRAVITY_Z_INDEX])
    thr = helper.make_tensor("thr", TensorProto.FLOAT, [1], [threshold])
    nodes = [
        helper.make_node("GatherElements", ["obs", "gz_idx"], ["gz"], axis=1),
        helper.make_node("Greater", ["gz", "thr"], ["cond"]),
        helper.make_node("Where", ["cond", "stand_actions", "walk_actions"], ["actions"]),
    ]
    merged.graph.node.extend(nodes)
    merged.graph.initializer.extend([idx, thr])
    merged.graph.output.append(
        helper.make_tensor_value_info("actions", TensorProto.FLOAT, [1, 14])
    )
    onnx.checker.check_model(merged)
    onnx.save(merged, out_path)
    print(f"wrote {out_path} (g_z > {threshold} -> stand-up net)")


def self_check(path: str, walk_path: str, stand_path: str, threshold: float) -> None:
    """Both branches must reproduce their source model exactly on a fixed input."""
    import onnxruntime as ort

    rng = np.random.default_rng(0)
    obs = rng.normal(0, 1, (1, 61)).astype(np.float32)

    merged = ort.InferenceSession(path)
    walk = ort.InferenceSession(walk_path)
    stand = ort.InferenceSession(stand_path)

    def out(sess, o):
        return sess.run(None, {"obs": o})[0]

    # Force the gate both ways by writing the gravity slot.
    upright = obs.copy(); upright[0, GRAVITY_Z_INDEX] = -1.0
    fallen = obs.copy(); fallen[0, GRAVITY_Z_INDEX] = +1.0

    d_walk = np.abs(out(merged, upright) - out(walk, upright)).max()
    d_stand = np.abs(out(merged, fallen) - out(stand, fallen)).max()
    print(f"upright -> walk net  : max|diff| = {d_walk:.2e}")
    print(f"fallen  -> stand net : max|diff| = {d_stand:.2e}")
    ok = d_walk < 1e-5 and d_stand < 1e-5
    print("SELF-CHECK", "PASS" if ok else "FAIL")
    if not ok:
        raise SystemExit(1)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--walk", required=True)
    ap.add_argument("--stand", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--threshold", type=float, default=STAND_THRESHOLD)
    ap.add_argument("--no-check", action="store_true")
    args = ap.parse_args(argv)

    build(args.walk, args.stand, args.out, args.threshold)
    if not args.no_check:
        self_check(args.out, args.walk, args.stand, args.threshold)
    return 0


if __name__ == "__main__":
    sys.exit(main())
