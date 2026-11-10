# `1910_m6.json` — 来源与使用边界

## 来源

| 字段 | 值 |
|---|---|
| 上游 | `LuwuDynamics/xgoduck_rl`（其 BAM 参数目录） |
| 文件名 | `1910_m6.json` |
| 引入本仓库日期 | 2026-11-10 |
| 本仓库副本路径 | `src/mjlab_microduck/robot/microduck/hd1910/1910_m6.json` |

**本文件按原样保留，未做任何数值修改**，作为 HD1910 执行器参数的唯一事实来源
（single source of truth）。任何再拟合结果请另存新文件，不要覆盖它。

## 这些数字是什么

它们是一次 **BAM 参数辨识（fit）的输出**，不是 HD1910 的寄存器配置：

- 不是 `P`/`I`/`D`、不是 `Torque_Enable`、不是 `Offset`、不是 `Max_Torque`；
- 不要把它们写进舵机 EEPROM，也不要用它们解释上位机的控制模式；
- `q_offset` **不存在于本文件**，即使存在也不能当作实机机械零位
  （BAM 的 `q_offset` 是拟合出的电角度偏置，实机零位必须用百分表/工装单独标定）。

## 三个参数文件的关系（2026-11-10 更新）

| 文件 | 内容 | 状态 |
|---|---|---|
| `1910_m6.json` | LuwuDynamics 同一份拟合的**截断版**（7 个字段 + 2 个元数据） | 保留原件，不再作为默认 |
| `1910_m6_full.json` | 同一份拟合的**完整版**（18 个字段） | 备选，含 `error_gain_ratio=1.3217` |
| **`1910_datasheet.json`** | **规格书推导的电气参数 + 拟合的摩擦项** | **当前默认（`HD1910_ACTIVE_PARAMS_JSON`）** |

`1910_datasheet.json` 的依据：规格书 HD-1910-C001 A/0 给出 Kt = 7.5 kg·cm/A、
堵转 15 kg·cm @7.4 V / 2.0 A。由此 `kt = 0.7355 N·m/A`、`R = 7.4/2.0 = 3.7 Ω`，
于是 `kt·7.4/R = 1.471 N·m`（规格书 1.47 ✓）、堵转电流 2.0 A ✓。

**摩擦项与 armature 没有规格书来源**，沿用 LuwuDynamics 的拟合值——这是明确的混用，
写在这里以免后人误以为是同一套辨识结果。`q_offset` 强制为 0（别人的台架零点，
且 `bam.mjlab` 根本不读它）。

## 与 BAM m6 模型的字段差异（重要）

**修复记录（2026-11-10 补）**：本文件最初被当作"只给了 7 个字段、其余未知"。
后续核对上游仓库发现，同一份拟合的**完整版**在
`LuwuDynamics/xgoduck_rl` → `src/mjlab_microduck/robot/xgoduck/params/1910_m6.json`，
本仓库副本为 `hd1910/1910_m6_full.json`。**两者的公共字段逐个完全一致**，
说明交给我们的 7 个值是这份完整拟合的截断，不是另一份拟合。

| 参数 | 截断版（在用） | 完整版 | BAM 默认（截断版实际生效值） | XL330 m6（参照） |
|---|---|---|---|---|
| `kt` | 0.623761 | 0.6237611 | — | 0.366013 |
| `R` | 4.910192 | 4.9101916 | — | 2.811392 |
| `armature` | 0.0017819 | 0.0017819 | — | 0.001808 |
| `friction_base` | 0.015803 | 0.0158034 | — | 0.004771 |
| `friction_stribeck` | 0.009590 | 0.0095901 | — | 0.004676 |
| `error_gain_ratio` | **缺失** | **1.3217** | 1.0 | 1.0 |
| `q_offset` | 缺失 | 0.0327 | 0.0 | 0.0271 |
| `friction_viscous` | 缺失 | 0.000310 | 0.1 | 0.005360 |
| `load_friction_motor` | 缺失 | 0.0685 | 0.05 | 0.266786 |
| `load_friction_external` | 缺失 | 0.1582 | 0.05 | 8.516e-06 |
| `load_friction_motor_stribeck` | 缺失 | 0.1220 | 0.05 | 1.072e-05 |
| `load_friction_external_stribeck` | 缺失 | 0.0786 | 0.05 | 0.080779 |
| `load_friction_motor_quad` | 缺失 | 0.00197 | 0.0 | 0.009972 |
| `load_friction_external_quad` | 缺失 | 0.000885 | 0.0 | 0.004903 |
| `dtheta_stribeck` | 缺失 | 0.9465 | 0.2 | 2.890 |
| `alpha` | 缺失 | 1.5221 | 1.35 | 8.683 |
| `max_velocity` | 100 | 100 | 5.2155（STS3215 默认） | — |
| `command_delay` | 0.006005 | 0.006005 | **BAM 不读取此字段** | — |

结论与决策：

1. 这些字段**不是未知量**，是同一份拟合的其余部分。默认仍使用截断版，
   因为 2026-11-10 的硬件规格只授权了这 7 个值；完整版作为 `HD1910_PARAMS_JSON_FULL`
   备用，一行切换（`HD1910_ACTIVE_PARAMS_JSON`）。
2. **切换前必须知道的副作用**：`STS3215Actuator.compute_control()` 会把占空比乘上
   `error_gain_ratio`，所以切到完整版会让有效刚度从 0.79 变成
   `0.79 × 1.3217 ≈ 1.04 N·m/rad`（约 XL330 的 1.85 倍）。这不是小事，
   要切就切全套并重跑 `scripts/hd1910_validate.py --stage step --zero-gravity`。
3. `q_offset = 0.0327` 只被 `bam/simulate.py`（离线台架）使用，`bam.mjlab` 全路径
   **不读它**，所以训练不受影响。但它绝不能当作实机机械零位——那是台架编码器零点。
4. `command_delay = 6.005 ms` 仍只具文档意义：BAM 用 `delay_min_lag/delay_max_lag`
   （仿真步）建模延迟，训练取 3–6 步 × 5 ms = 15–30 ms。
5. `max_velocity = 100 rad/s` 会把固件目标限速抬到失效（默认 5.22），需台架复核。
6. `tests/test_hd1910_cfg.py` 把"公共字段一致 + 完整版多出哪些键 + 默认用截断版"
   全部锁死，防止两个文件被混用。

## 待办（按优先级）

1. **决定用截断版还是完整版**：完整版有三个已知优势（`error_gain_ratio`、
   真实 `friction_viscous`、6 个负载摩擦项），代价是刚度 ×1.32。建议先跑
   `--stage step --zero-gravity` 两版对比，再决定。
2. 在 `scripts/testbench_sim2real.py` 上跑 HD1910 单关节阶跃，用**这台舵机**的
   数据重新拟合（上游那份是在别人的台架上拟合的，`q_offset=-0.008` 与 `+0.033`
   两个版本的存在说明台架零点会漂）；
3. 复核 `max_velocity`（100 vs 固件默认 5.22）与 `command_delay` 的物理含义。
