# 验收记录（MicroDuck + HD-1910）

每一行都是一次**实测**，不是训练日志的推测。所有判据来自
`scripts/evaluate_policy.py`（分轴）或 `scripts/probe_policy_state.py`（分姿态），
两者都直接跑导出的 ONNX，与部署路径一致。

> **总原则**：本文任何结论都只覆盖仿真。没有实机验收前，不得声称"可部署"。

---

## 1. Velocity（走路）

| | run 1 | run 2 | run 3 |
|---|---|---|---|
| 起点 | 从零 | 从零 | 从 run 2 `model_4000.pt` 续训 |
| 迭代 | 5000（实跑 4165） | 5000（实跑 4085） | 3000（进行中） |
| 奖励改动 | 无（上游 XL330 配方） | track_linear 2→4、head_pose 2→1、air_time 加位移门控、新增 no_progress | 转向权重 2→3、turn-in-place 采样 0.15→0.35 |
| 训练期 `error_vel_xy` | 0.41 横盘 | 0.245 横盘 | — |

### 分轴实测（`evaluate_policy.py`，16 环境 × 6 s，无推力）

| 指令桶 | 指令 (vx, vy, ωz) | run 2 实测 | 误差 | 摔倒率 |
|---|---|---:|---:|---:|
| stand | 0, 0, 0 | 0, 0, 0 | 0.00 | 0% |
| fwd_slow | 0.15, 0, 0 | 0.12 | 0.03 | 0% |
| **fwd_fast** | **0.40, 0, 0** | **0.33** | 0.06 | 0% |
| back | −0.20, 0, 0 | −0.19 | 0.01 | 0% |
| lat | 0, 0.20, 0 | 0.17 | 0.05 | 0% |
| **wz_slow** | 0, 0, **0.30** | **0.01** | **0.29** | 0% |
| **wz** | 0, 0, **0.80** | **0.07** | **0.73** | 0% |
| mix | 0.25, 0.10, 0.40 | 0.23 / 0.08 / 0.35 | 0.02 / 0.06 / 0.16 | 0% |

**判定**：线性三轴达标（≥83%），**原地转向不达标**（≈0，而"边走边转"能到 88%）。
→ run 3 专治此项。

### 一个必须记住的坑

训练日志的 `error_vel_xy` **不能**直接当跟踪精度用：它把指令切换瞬态算进去了，
而且 `heading_command=True` 会让 yaw 槽由航向 P 控制器每步改写。评估时必须
`heading_command=False` + 清空 `ranges.heading`，否则钉住的指令被静默覆盖
（第一版评估因此读出了"转向全 0"的假象）。

---

## 2. StandUp（起身）

| 起始姿态 | 300 轮策略实测 | 目标 |
|---|---:|---|
| 趴地 face-down | **100%** | ≥90% |
| 坐着 sitting | 81–100% | ≥90% |
| **仰面 face-up** | **0%** | **≥80%** |
| 站立 standing（保持） | 100% | ≥95% |

**判定**：仰面未达标。原因已定位——300 轮时 `face_up_prob = 0`，该姿态从未出现在
训练分布里（上游课程 iter 600 才引入 10%）。已把 HD1910 版课程表改为
iter 200 起 20%、700 起 30%、1200 起 35%，待 1500 轮训练后复测。

验收命令：

```bash
uv run --no-sync scripts/probe_policy_state.py \
  --task Mjlab-StandUp-Flat-MicroDuck-HD1910 --onnx policies/standup.onnx \
  --force-state all --out docs/hd1910/artifacts/standup_accept.json
```

---

## 3. VelStand（走路 + 摔倒恢复一体）

待训练。已确认其 walk 层继承 run 2 的重配平（`hd1910_tuning.apply_velocity_rebalance`），
且保留自带的 `feet_air_time_upright` 门控。

验收判据：
- 从趴/仰/坐三种姿态起身（同 StandUp 的表格）
- 起身后能按指令走（fwd_fast ≥ 0.3 m/s）
- 行走中被推倒 → 自主爬起 → 继续走（`push_robot` 打开，看 30 秒内的恢复次数）

---

## 4. Roulade（前滚翻）

待训练。注意：**这项对惯量最敏感**，而当前 21 个连杆的惯量仍是按质量比缩放的
（`ESTIMATED`），翻滚是绕俯仰轴的整体转动，误差会被放大。建议实称/ CAD 重算后再练。

验收判据：完成 ≥340° 的躯干俯仰变化并回到站立，落地阶段无头壳触地终止。

---

## 附：数据与视频索引

| 文件 | 内容 |
|---|---|
| `docs/hd1910/artifacts/eval_v2.json` | run 2 分轴评估原始数据 |
| `docs/hd1910/artifacts/probe_standup_*.json` | 起身分姿态探测 |
| `docs/hd1910/videos/walk_fwd_closeup.mp4` | run 2 前向行走（0.33/0.40 m/s） |
| `docs/hd1910/videos/standup_prone_closeup.mp4` | 从趴地起身 |
| `docs/hd1910/videos/standup_from_supine_FAIL.mp4` | 仰面起不来（失败证据） |
| `policies/hd1910_v2_walk_candidate.onnx` | run 2 策略（会走，不会原地转） |
