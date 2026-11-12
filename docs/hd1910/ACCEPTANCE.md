# 验收记录（MicroDuck + HD-1910）

每一行都是一次**实测**，不是训练日志的推测。所有判据来自
`scripts/evaluate_policy.py`（分轴）或 `scripts/probe_policy_state.py`（分姿态），
两者都直接跑导出的 ONNX，与部署路径一致。

> **总原则**：本文任何结论都只覆盖仿真。没有实机验收前，不得声称"可部署"。

---

## 1. Velocity（走路）

| | run 1 | run 2 | run 3 (含 3b/3c) |
|---|---|---|---|
| 起点 | 从零 | 从零 | 从 run 2 `model_4000.pt` 续训 |
| 迭代 | 5000（实跑 4165） | 5000（实跑 4085） | 3c 结束于 model_6000 |
| 奖励改动 | 无（上游 XL330 配方） | track_linear 2→4、head_pose 2→1、air_time 加位移门控、新增 no_progress(0.5) | 转向权重 2→3、turn-in-place 0.15→0.35；(3b) no_progress 改连续缺口权重 1.5；(3c) 新增 no_turn_progress 权重 1.0 |
| 训练期 `error_vel_xy` | 0.41 横盘 | 0.245 横盘 | — |

### 分轴实测（`evaluate_policy.py`，16 环境 × 6 s，无推力）

| 指令桶 | 指令 (vx, vy, ωz) | run 2 | **run 3** | 判定 |
|---|---|---:|---:|---|
| stand | 0, 0, 0 | 0.00 | 0.00 | PASS |
| fwd_slow | 0.15, 0, 0 | 0.12 | **0.18** | **PASS** |
| fwd_fast | 0.40, 0, 0 | 0.33 | **0.37** | **PASS** |
| back | −0.20, 0, 0 | −0.19 | **−0.23** | **PASS** |
| lat | 0, 0.20, 0 | 0.17 | **0.20** | **PASS** |
| **wz_slow** | 0, 0, **0.30** | 0.01 | **0.01** | **FAIL** |
| wz | 0, 0, **0.80** | 0.07 | **0.99** | **PASS** |
| mix | 0.25, 0.10, 0.40 | 0.23/0.08/0.35 | **0.26/0.10/0.50** | — |

**判定（run 3 最终）**：7 个桶里 **6 个 PASS，1 个 FAIL**。

- 线性四轴全部达标：前进 0.37/0.40、后退 0.23/0.20（略超）、横移 0.20/0.20（满分）、
  慢走 0.18/0.15（超过指令，说明小指令不再被忽略）
- **原地转向从 0.07 提到 0.99 rad/s（14 倍）**，指令 0.8
- **唯独慢速转向 0.30 rad/s 完全不会（0.01）**

慢速转向的失败模式与线性轴当初相同（角速度高斯 std=0.71 太宽，站着不动仍拿 84%），
加 `no_turn_progress` 后依然没有学会——这已经不是"奖励没给对"，而更像
**步态层面的限制**：这台机器人靠迈步转向，一步的转角有下限，难以做出 0.3 rad/s 的
缓慢持续旋转。留作已知限制，后续可用"小步快频"的专门课程或直接接受。

验收命令与产物：

```bash
uv run --no-sync scripts/accept_velocity.py --onnx policies/hd1910_v3_walk.onnx   --video-dir docs/hd1910/videos --out docs/hd1910/artifacts/accept_v3.json
```

**为什么判断这是学习问题、不是观测缺失**（2026-11-12 核查）：

| 检查 | 结果 |
|---|---|
| actor 是否能看到自身偏航角速度 | ✅ `base_ang_vel`（3 维，含 ωz）在 actor 观测里 |
| 是否能拿到 ωz 指令 | ✅ `command`（3 维，含 ωz）在 actor 观测里 |
| 两者是否同一尺度 | ✅ 两个 term 的 `scale` 都是 `None`（原始 rad/s），不存在量纲错配 |
| 动作空间能否表达转向 | ✅ 14 维关节角里含 `left/right_hip_yaw`（限位 ±0.44/±0.52 rad） |

所以策略**看得见该转多少、也知道自己转了多少**，只是没学到。这正是 run 3 提高
转向奖励权重与采样比例的依据。

### 步态形状：为什么它是"蹭着走"

`evaluate_policy.py` 现在会同时测脚部腾空时间与抬脚高度。run 2 策略在 0.4 m/s 指令下：

| 指标 | 实测 | 奖励的"好区间" | 结论 |
|---|---:|---|---|
| 腾空时间均值 | **0.054 s** | (0.125, 0.30) s | 远低于下限 |
| 腾空时间最大值 | 0.090 s | 同上 | **最好的那一步也没够到** |
| 抬脚高度 | 9–11 mm | 目标 20 mm | 一半不到 |

也就是说 **`air_time`（权重 3.0，第三大项）几乎从不发放**——它设定的"好步态"是
腾空 125–300 ms，而这台机器人实际只有 54–90 ms。这一项事实上是失效的，
既不给梯度也不给压力，抬脚就一直停在 9 mm 左右。

修法有两种，方向相反，必须先定策略：

- **降低阈值**（如 0.08）：立刻能拿到奖励，但**等于承认"蹭着走"就是好步态**，
  很可能把现有步态焊死；
- **改成连续函数**（如 `min(air_time/target, 1)`）：保留"越长越好"的梯度，
  不会在阈值下方出现梯度悬崖——这是我认为更正确的方向，但需要改奖励函数并重训验证。

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

待训练。配方要点（供复现）：

| 机制 | 取值 / 做法 |
|---|---|
| 相位门控 | `MIDROLL_PITCH_MIN 50°`、`MAX 340°`，落地门 260–330° |
| 中滚重生（反向课程） | 直接 spawn 到 50°–340° 的滚动中途，`ω ∈ (0, 3) rad/s` |
| TUCK 锚点 | 收腿 + **收下巴**（`neck_pitch −1.0`），让头顶平面贴地形成 latch |
| 助跑钩子 | `ROULADE_FORWARD_VEL_RANGE (0,0)`，改 `(0, 0.3)` 可练“走着翻” |
| 对称性增强 | 唯一开 `ENABLE_SYMMETRY=True` 的任务 |
| 回合长度 | 5 s |

验收命令（本轮新建）：

```bash
uv run --no-sync scripts/accept_roulade.py --onnx policies/roulade.onnx \
  --out docs/hd1910/artifacts/roulade_accept.json
```

判据：`pitch_span ≥ 340°`（真的翻过去）、`inverted_time_ratio > 0`、终态站立。

### 在 HD-1910 上必须先想的四条风险

| # | 风险 | 依据 | 对策 |
|---|---|---|---|
| 1 | **惯量仍是 ESTIMATED** | 21 个连杆的惯量按质量比从 XL330 缩放；翻滚是绕俯仰轴的整体转动，对惯量最敏感 | 先实测/ CAD 重算，或把 `MASS_INERTIA_RANDOMIZATION_RANGE` 从 ±5% 放宽到 ±20% |
| 2 | **头部质量变了，latch 压力变了** | 收下巴靠头顶贴地产生支撑；HD1910 舵机 21 g vs XL330 18 g，头部总重变化 | 训练前先在 `home` 姿态量一次头顶接触力 |
| 3 | **落地冲击更大** | `k_eff` 1.21 vs XL330 0.56（刚 2.2 倍），整机 782 g vs 737 g | 关注 `gentle_landing` 项与 `dof_pos_limits` 是否频繁触发 |
| 4 | **助跑钩子默认关闭** | `ROULADE_FORWARD_VEL_RANGE=(0,0)`，只从静止起翻 | 先按静止练；能翻之后再用 `(0, 0.3)` 接走路策略 |

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
| `policies/hd1910_v3_walk.onnx` | **run 3 策略：线性四轴达标 + 原地转向 0.99 rad/s** |
| `docs/hd1910/videos/accept_wz.mp4` | 原地转向特写 |
| `docs/hd1910/videos/accept_fwd_fast.mp4` | 0.4 m/s 行走特写 |
