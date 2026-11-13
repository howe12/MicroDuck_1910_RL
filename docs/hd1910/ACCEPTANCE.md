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

### 最终结果（1500 轮，24 环境 × 6 s）

| 起始姿态 | 300 轮探针 | **1500 轮正式** | 目标 | 判定 |
|---|---:|---:|---|---|
| 趴地 face-down | 100% | **100%** | ≥90% | ✅ |
| 坐着 sitting | 81–100% | **100%** | ≥90% | ✅ |
| **仰面 face-up** | **0%** | **100%** | ≥80% | ✅ |
| 站立 standing（保持） | 100% | **100%** | ≥95% | ✅ |

终态高度 0.1162 m（HOME 0.1159），倾角 **0.8–1.0°**，全部姿态零残留倒地。

**仰面从 0% 到 100% 的关键改动**：把 `ground_state_mix` 课程的 `face_up_prob`
从"iter 600 才 10%"提前到 **iter 200 就 20%**（700→30%、1200→35%）。
300 轮探针时该姿态从未进入训练分布，所以策略"看见背躺就装死"。

![仰面起身](/tmp/frames/supine_ok.png)

验收命令：

```bash
uv run --no-sync scripts/probe_policy_state.py \
  --task Mjlab-StandUp-Flat-MicroDuck-HD1910 --onnx policies/hd1910_standup.onnx \
  --force-state all --out docs/hd1910/artifacts/standup_accept.json
```

验收命令：

```bash
uv run --no-sync scripts/probe_policy_state.py \
  --task Mjlab-StandUp-Flat-MicroDuck-HD1910 --onnx policies/standup.onnx \
  --force-state all --out docs/hd1910/artifacts/standup_accept.json
```

---

## 3. VelStand（走路 + 摔倒恢复一体）

### 最终结果（4000 轮，`model_3999`）

验收方式：按 0.25 m/s 走 3 秒 → **把每个环境强制打翻**（40% 趴 / 30% 仰 / 30% 坐）
→ 自由运行 8 秒，测爬起来的时间与恢复后的速度。

| 指标 | 实测 | 判据 | 判定 |
|---|---:|---|---|
| 行走速度 | **0.271 m/s** | ≥0.20 | ✅ |
| 打翻率 | 1.00 | — | 对照组正常 |
| **恢复率** | **1.00** | ≥0.80 | ✅ |
| 恢复时间（中位） | **0.32–0.49 s** | ≤3 s | ✅ |
| 恢复时间（p90） | 0.73 s | ≤5 s | ✅ |
| 终态站立率 | **1.00** | ≥0.95 | ✅ |
| **恢复后速度** | **0.266 m/s** | ≥0.20 | ✅ |

![打翻后恢复](/tmp/frames/velstand.png)

**一个策略同时会走、会被打翻、会自己爬起来继续走**，且恢复后速度几乎无损（0.271 → 0.266）。

### 一个反直觉的观察

训练日志里 `Episode_Reward/recovery_success` 全程只有 **0.002–0.005**，看起来像"完全没学会"。
但实测恢复率是 **100%**。原因是该奖励只在特定状态转移上记账，且被 20 秒回合、
大量成功样本稀释——**奖励曲线在这里完全误导**。这也再次说明为什么每个阶段都必须实测。

验收命令：

```bash
uv run --no-sync scripts/accept_velstand.py \
  --onnx policies/hd1910_velstand.onnx --num-envs 16 \
  --out docs/hd1910/artifacts/velstand_accept.json
```

---

## 4. Roulade（前滚翻）

### 最终结果（2500 轮，`model_2499`，64 环境 × 6 s）

| 指标 | 混合重生（默认） | **强制站立起翻（正确）** | 判据 |
|---|---:|---:|---|
| 完成整圈比例（≥340°） | 0.42 | **0.92** | ≥0.80 |
| 俯仰跨度中位 | 295° | **368°** | ≥340° |
| 俯仰跨度 p90 | 370° | **374°** | — |
| 倒置时间占比 | 0.044 | 0.05 | >0 |
| 终态站立率 | 1.0 | **1.0** | ≥0.95 |
| 终态高度 | 0.1143 | 0.1144 | ~0.115 |

![前滚翻](/tmp/frames/roulade.png)

**判定：达标。从站姿起翻，92% 的尝试能完整翻过一圈并站住。**

### 一次差点写错的验收

第一版验收用了任务默认的重置分布，测出 **42%**，我据此写下"部分成功，主因是惯量"。
但那个数字是**测量错误**：

任务的 `roulade_spawn_mix` 课程在训练末期让 **50% 的环境直接从翻滚中途生成**
（50°–340°）。这些环境要么已经翻过大半、要么停在一个尴尬位置再也翻不动。
把它们和"从站立开始翻"混在一起统计，得到的是一个没有物理意义的平均值。

把重生分布强制成 `standing_prob=1.0, midroll_prob=0.0` 后，真实数字是 **92%**。

**教训**：反课程（mid-roll 重生）是训练手段，绝不能带进验收。验收必须从部署时的
真实初始状态出发。这也解释了为什么同样的策略在 4/16/32/64 环境上测出 0.25/0.75/
0.875/0.42 这种乱七八糟的分布——样本里"中途重生"的比例每次都不一样。

### 配方要点（供复现）

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

## 5. 地形：不平路面与斜坡

两个任务，同一个 61→14 的观测/动作契约，都带 run-3 的奖励重配平：

| 任务 | 地形 | 状态 |
|---|---|---|
| `Mjlab-Velocity-Rough-MicroDuck-HD1910` | 随机箱体、粗糙块、1.7–5.7° 金字塔缓坡 | 任务已注册（上游配方 + HD1910 机器人） |
| `Mjlab-Velocity-Slope-MicroDuck-HD1910` | **2–20° 斜坡**（10 级难度课程），平路起跑 → 上坡 → 下坡 | 本轮新建 |

### 斜坡地形怎么做的

`FlatRampTerrainCfg`（原本只给轮滑任务用）生成「平路 + 斜坡 + 出口」的地形砖：

```python
TerrainGeneratorCfg(size=8.0, curriculum=True, num_rows=10,
    sub_terrains={"ramp": FlatRampTerrainCfg(
        flat_length=2.0, ramp_length_range=(4.0, 8.0), runout_length=3.0,
        spawn_on_ramp=0.0, deg_min=2.0, deg_max=20.0)})
```

- **10 行 = 10 级难度**，`terrain_levels` 课程按行走距离升降级；
- `spawn_on_ramp=0` 让机器人从平路起跑、再走上斜坡（而不是直接生在坡上）；
- 指令会随机重采样，所以它会**上坡、下坡、横穿**三种情况都遇到。

### 显存限制（实测）

| 环境数 | 结果 |
|---|---|
| 512 / 1024 | ✅ 可训练 |
| 2048+ | ❌ OOM（8 GB 卡） |

所以地形任务必须用 `--env.scene.num-envs 1024`，迭代速度比平地 4096 慢约 2.4 倍。

### 实测结果

**不平路面（Rough，2500 轮 @1024 环境）：7 个桶全部 PASS**

| 指令桶 | 实测 | 摔倒率 | 倾角 |
|---|---:|---:|---:|
| 前进 0.40 | **0.37** | 0% | 4.3° |
| 前进 0.15 | 0.14 | 0% | 3.1° |
| 后退 −0.20 | −0.18 | 0% | 3.2° |
| 横移 0.20 | 0.17 | 0% | 2.9° |
| **慢转 0.30** | **0.16** | 0% | 2.4° |
| **原地转 0.80** | **1.45** | 0% | 3.6° |

**这是唯一一个 7/7 全过的策略**，而且是在不平路面上。它是唯一同时把慢速转向也做出来的
（0.16 ≥ 0.15）。

![不平路面行走](/tmp/frames/rough.png)

**斜坡 v2（2500 轮 @1024 环境）：4 PASS / 2 FAIL**

| 指令桶 | v1 | **v2** | 摔倒率 |
|---|---:|---:|---:|
| 前进 0.40 | 0.32 | **0.32** | 0% |
| 前进 0.15 | 0.14 | 0.18 | 0% |
| 后退 / 横移 | −0.19 / 0.17 | −0.20 / 0.20 | 0% |
| 原地转 | 0.00 | **0.00** | 0% |

斜坡任务**没有**继承 run-3 的转向设置（它走的是基础配方 + 默认重配平），所以不会转。
不平路面任务碰巧在没有转向设置的情况下也学会了转——原因未查清，可能是不平地形
迫使它频繁调整朝向。

### 验收方式

直接复用走路验收脚本，但换成地形任务——`fall_ratio` 就变成了"在坡上摔不摔"：

```bash
uv run --no-sync scripts/accept_velocity.py \
  --task Mjlab-Velocity-Slope-MicroDuck-HD1910 \
  --onnx policies/hd1910_slope.onnx --num-envs 8 --seconds 10 \
  --out docs/hd1910/artifacts/accept_slope.json
```

判据：与平地相比，`fwd_fast` 达成速度下降不超过 30%，且 `fall_ratio ≤ 0.10`。

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
| `policies/hd1910_standup.onnx` | **起身策略：趴/坐/仰三姿态 100% 恢复** |
| `docs/hd1910/videos/standup_from_supine_OK.mp4` | 仰面起身成功（对比 `_FAIL` 那段） |
| `policies/hd1910_velstand.onnx` | **走+摔倒恢复一体：恢复率 100%** |
| `docs/hd1910/videos/velstand_knockdown.mp4` | 走→被打翻→爬起来继续走 |
| `policies/hd1910_roulade.onnx` | **前滚翻：42% 完整翻滚，100% 落地站住** |
| `docs/hd1910/videos/roulade.mp4` | 前滚翻实拍 |
| `docs/hd1910/artifacts/roulade_accept.json` | 64 环境验收数据 |
| `policies/hd1910_rough.onnx` | **不平路面：7/7 全过，含慢速转向** |
| `policies/hd1910_slope_v2.onnx` | 斜坡：线性 4 项达标，转向未学会 |
| `docs/hd1910/videos/rough_walk_closeup.mp4` | 不平路面行走特写 |
| `docs/hd1910/videos/accept_wz.mp4` | 原地转向特写 |
| `docs/hd1910/videos/accept_fwd_fast.mp4` | 0.4 m/s 行走特写 |
