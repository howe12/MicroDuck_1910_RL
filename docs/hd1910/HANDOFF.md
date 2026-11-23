# 交接说明：MicroDuck HD-1910 自训策略与实机部署

> 给接手分析的 Agent。本文只写**已经实测验证的事实**，未验证的一律标注。
> 对应仓库：`https://github.com/howe12/MicroDuck_1910_RL`（分支 `master`）。
> 相关文档：`docs/hd1910/ACCEPTANCE.md`（逐阶段验收）、`docs/hd1910/DEPLOY.md`（部署细节）。

---

## 1. 背景

| 项 | 值 |
|---|---|
| 机器人 | MicroDuck（kissqy 缝合怪版），飞特 **HD-1910** 舵机 ×15 |
| 主控 | Radxa ZERO 3W（RK3566，aarch64，Armbian 26.2 / Linux 6.1.115） |
| 固件 | `robotd 0.15.1` + `control.102`（build `1fa8438-feetech-ft6-control.40`） |
| 中控 | Console Studio V260 |
| 总线 | Feetech 协议，`/dev/ttyS2`，50 Hz |
| 实称 | 整机 **833 g**，电池 103.9 g |
| 训练机 | RTX 2070 SUPER 8 GB（平地 4096 env，地形 1024–2048 env） |

原始项目（Pollen Robotics）是 **XL330 + Dynamixel**；本仓库做的事是**把仿真与策略适配到 HD-1910 + Feetech**。

---

## 2. 训练了什么

训练框架：`mjlab 1.3.0`（MuJoCo Warp）+ `rsl_rl` PPO，50 Hz 控制
（`timestep=0.005` × `decimation=4`），网络 61 → 512 → 256 → 128 → 14（约 19.8 万参数）。

### 2.1 任务与策略清单

| # | 任务 ID（HD1910 版） | 训练轮数 / 环境 | 产物 | 实测结论 |
|---|---|---|---|---|
| 1 | `Mjlab-Velocity-Flat-…-HD1910` | 5000 / 4096 | `hd1910_v3_walk.onnx` | **6/7 桶达标** |
| 2 | `Mjlab-Velocity-Rough-…-HD1910` | 2500 / 1024 | `hd1910_rough.onnx` | **7/7 桶全过，摔倒率 0%** |
| 3 | `Mjlab-Velocity-Slope-…-HD1910` | 2500 / 1024 | `hd1910_slope_v2.onnx` | 4 达标 / 2 失败（不会转向）|
| 4 | `Mjlab-StandUp-Flat-…-HD1910` | 2000 / 4096 | `hd1910_standup.onnx` | **四种姿态 100% 起身** |
| 5 | `Mjlab-VelStand-Flat-…-HD1910` | 4000 / 4096 | `hd1910_velstand.onnx` | 走动 + **打翻后 100% 恢复**，但**不会转向**（0.03 rad/s）|
| 6 | **`Mjlab-VelStand-Flat-…-HD1910`（续训）** | +2500 / 4096 | **`hd1910_walk_all.onnx`** | **走 + 转(0.98) + 打翻 100% 爬起** ← 当前主力 |
| 7 | `Mjlab-Roulade-Flat-…-HD1910` | 2500 / 4096 | `hd1910_roulade.onnx` | 站姿起翻 **92%**，跨度 368° |
| 8 | `Mjlab-SitStand-Flat-…-HD1910` | 2000 / 1024 | `hd1910_sitstand.onnx` | 指令站 **100%**；指令坐 z=0.0748（目标 0.060，只到 75%）|
| 9 | `Mjlab-BallKick-Flat-…-HD1910` | 2000 / 2048 | `hd1910_ballkick.onnx` | 球推出 **5.44 m**，峰值 1.62 m/s，摔倒率 0% |
| 10 | `Mjlab-GroundPick-Flat-…-HD1910` | 1500 / 2048 | `hd1910_groundpick.onnx` | 嘴尖最低 **2.0–2.6 cm**，回正率 66.7% |
| 11 | `Mjlab-Spin-Flat-…-HD1910` | 1000 / 2048 | `hd1910_spin.onnx` | 累计偏航 275°，完整一圈 41.7%，**摔倒率 100%** |

> `hd1910_walk_all.onnx` 是**单一策略**同时具备：走（0.36 m/s）、转（0.98 rad/s）、
> 被打翻后自己爬起（恢复率 100%，中位 0.30 s / p90 0.86 s），恢复后速度 0.239 m/s。
> 它是"把技能塞进同一 MDP"路线的成品——**不需要门控或状态机**。

### 2.2 统一的观测/动作契约（所有策略可互换的前提）

**观测 61 维**（顺序固定）：

| 段 | 内容 | 维度 |
|---|---|---:|
| 0–2 | 躯干角速度（IMU 机体系）| 3 |
| 3–5 | 重力投影（`projected_gravity`，站立时 z ≈ −1）| 3 |
| 6–19 | 关节角（相对 HOME）| 14 |
| 20–33 | 关节角速度 | 14 |
| 34–47 | 上一步动作 | 14 |
| 48–50 | 速度指令 `[vx, vy, wz]` | 3 |
| 51–54 | 头部姿态指令 | 4 |
| 55–60 | 身体姿态指令 | 6 |

**动作 14 维**：关节目标角，语义是 **相对 `default_joint_pos` 的偏移**，
`scale=1.0`、`offset=default_joint_pos`。导出时**观测归一化烘焙进 ONNX**（`Sub`/`Div` 在计算图内），
运行时喂原始观测即可。

**14 个策略关节（固定顺序）**：
`left_hip_yaw, left_hip_roll, left_hip_pitch, left_knee, left_ankle,
neck_pitch, head_pitch, head_yaw, head_roll,
right_hip_yaw, right_hip_roll, right_hip_pitch, right_knee, right_ankle`

（第 15 个舵机 `mouth` 只做表情，不进策略动作空间。）

**训练 HOME（`default_joint_pos`，与固件要求逐项差 ≤ 0.000501 rad）**：

```
[ 0.000, -0.087, -0.458, -0.005, +0.453, +0.349, +0.349, 0.000, 0.000,
  0.000, +0.087, +0.458, +0.005, -0.453 ]
```

### 2.3 执行器模型（本项目的核心适配）

HD-1910 用 BAM（电压控制）模型，`hd1910_m6.json`（sha256 `ef2d51ad…`，固件同样要求这个哈希）：

| 参数 | HD-1910 |
|---|---:|
| `kt` | 0.6238 |
| `R` | 4.910 Ω |
| `kp_fw` | 5 |
| 电压范围 / 下限 | 7.4–8.0 V / 7.0 V |
| 指令延迟 | 3–6 个控制周期（60–120 ms）|
| 有效刚度 `k_eff` | **1.21**（XL330 为 0.56）|

> 换执行器后同一套奖励配方会**掉进"站着不动"的局部最优**——这是项目第一个坑。

### 2.4 质量与惯量

- 实称整机 **833 g**（原估算 782 g，偏轻 6.5%）。已按实称更新
  `src/mjlab_microduck/robot/microduck/hd1910/mass_table.csv` 并重生成三个 MJCF（总重 832.96 g）。
- 余额（约 50.8 g）按"含舵机的 11 个刚体"均摊 → 每只 HD-1910 约 **25.6 g**（规格书 21 g，差值应为舵盘/螺丝/线缆）。
- **21 行质量表除整机与电池外全部仍是估算；惯量全部按质量比缩放，无一实测。**

---

## 3. 如何放进 Duck

### 3.1 固件要求：一个"模型"是**目录**，不是一个文件

`robotd` 从 **onnx 所在目录**读配套文件。最小的四件套：

```
policy.onnx                 ← 网络本体
deployment-contract.json    ← 部署合同（必须与 ONNX 图内元数据逐字节一致）
manifest.json               ← 哈希、task 名、slot
training-request.json       ← 源训练记录（仅要求存在）
```

可选：`deployment-execution.json`（动作缩放/低通）、`deployment-runtime.json`、
`upstream-params/`、`model-info.json`。

标准目录：`/opt/robot/feetech-ft5-r5/models/<sha256(policy.onnx)[:32]>/`

### 3.2 合同的硬性校验（`model_packages.validate_export`，15 条）

关键项：`schema = microduck-deployment-contract/v1`、`task_adapter = hd1910-official0151`、
`input_dim/output_dim = 61/14`、`obs_normalizer = baked_in_onnx`、
`sha256(policy.onnx) == manifest.onnx_sha256`、`contract.onnx_metadata` 必须与 ONNX 图内元数据**完全相等**、
14 个 `home_rad` 与 `default_joint_pos` 逐项差 ≤ 0.000501、BAM 文件哈希固定、
`kp_fw` 为 1..32 整数、观测 8 项且 `joint_pos/joint_vel` 用 `_rel`、
ONNX 算子只允许 Sub/Div/Gemm/Elu/Tanh/Relu/Add/Mul/Clip/Identity/Constant/MatMul/LeakyRelu/Sigmoid/Softplus。

**task 名必须落在固件白名单里**：walk 槽位只认精确串 `Mjlab-VelStand-Flat-MicroDuck`
（`slot_for()` 对 VelStand 前缀做特例跳过）。

### 3.3 实测有效的打包方法

`scripts/pack_deployment.py`：**拿同槽位的随包模型目录当模板**，
只重写 `policy.onnx`、`onnx_metadata`、两个哈希、14 个 `home_rad`；
actuator/BAM 块、关节表、观测布局、动作滤波、技能窗口原样保留。
这样一次就能过校验（四个槽位实测一次通过）。

### 3.4 当前实机上的部署

模型目录（目录名 = `sha256(policy.onnx)[:32]`）：

| 槽位 | 目录 | 来源 | 合同 task |
|---|---|---|---|
| `walk` | `models/9f4774d02832baebd69b79ffa038d85e/` | 自训 | `Mjlab-VelStand-Flat-MicroDuck` |
| `sitstand` | `models/3942213bc1c5fa5d7a6346cf435079cb/` | 自训 | `Mjlab-SitStand-Flat-MicroDuck` |
| `ground_pick` | `models/3a3a61d8fd3a95ecf72f181c56ec50bb/` | 自训 | `Mjlab-GroundPick-Flat-Backlash-MicroDuck` |
| `kick_right` | `models/d0ea8cd0fe4184797d0567f6715ebbba/` | 自训 | `Mjlab-BallKick-Flat-MicroDuck` |
| `roulade` | `models/8472981cf2592fbaca28fc316c07d1bd/` | 自训 | `Mjlab-Roulade-Flat-MicroDuck` |
| `kick_left` | `models/ee0376c927…/` | 随包 | — |
| `stand` | `"none"` | — | 不启用（`walk` 零指令即站住）|

配置文件：`/etc/robot/feetech-ft5/robotd.toml` 的 `[policy]` 段（**注意不是** `/etc/robot/robotd.toml`）。
模型清单（中控列表来源）：`/opt/robot/feetech-ft5-r5/management-profile.json` 的 `builtin_models`。

**激活判定**：`catalog()` 只遍历 `builtin_models`，且用字符串相等判断
`str(models/<id>/policy.onnx) == cfg['policy'][slot]` —— 因此模型必须放在 `models/<id>/`
且 `builtin_models` 里有该 id，中控才显示"已绑定"。

### 3.5 关键运维事实

| 事实 | 说明 |
|---|---|
| **commissioning 模式拒绝运动** | `robotd --commissioning` 时 `policy load` 报 `robotd is read-only`；标定后须 `runtime.py configure-startup motion` |
| **`[[policy.skill]].path` 独立于槽位** | 只改槽位不改 skill 的 path，按 RB 触发的仍是原模型 |
| **切换用 `duck-profile`** | `list / status / save / diff / use`，版本化 `robotd.toml` + `management-profile.json`；已存 `factory` 与 `selftrained` |
| **改 TOML 用整串替换** | 按行重建 TOML 曾把配置插坏（`replace('', new)` 在每个字符间隙插入），robotd 无法启动 |
| **`robotctl pad bindings` 读错文件** | 它读 `/etc/robot/robotd.toml`（无 `[pad]` 段），显示的是内置默认值，不代表真实绑定 |
| **中控按键映射** | A→ground_pick、X→roulade、LB→kick_left、RB→kick_right、十字键下→sitstand；Start 第一次回 HOME、第二次启用策略 |

---

## 4. 已知问题（给接手 Agent 的重点）

### 4.1 实机“一动就倒”，且**作者的策略同样如此**

- 板上随包的策略（同一作者、训练总重 820 g）在实机上也是**能站、一动就倒**。
- 因此**不是自训策略的问题**，也不是质量匹配问题（820 vs 833 仅差 1.6%）。

### 4.2 关节方向 `direction` 存在 `+1`，与作者参考（全 `-1`）不一致

- 作者参考标定：15 个关节 `direction` 全为 `-1`。
- 实机标定：同时存在 `-1` 与 `+1`。
- 用户解释：用 `-1` 时中控 3D 的运动方向与手动掰关节的方向相反，改成 `+1` 后 3D 才一一对应。
- **`direction` 的语义是控制层的**（中控代码原文：“编码器 counts 递增时，关节角度增加(+1)/减少(−1)”），
  而 3D 显示用的就是同一个 `angle_deg`，所以"显示同向"与"送给策略的符号"是同一件事。
- **待查**：改成 `+1` 之后，`zero_raw` 与 `min_rad/max_rad` 是否也随之重新标定/镜像。
  若只改 `direction` 未重标，零位与限位会落在错侧 → 关节顶机械限位 → 一动就倒。

### 4.3 未验证项

| 项 | 状态 |
|---|---|
| 实机行走能力 | **完全没有验收**，只确认过 `health` healthy、`loaded_models` 有值、无总线错误 |
| 惯量 | 按质量比缩放，无实测 |
| 21 行质量表 | 仅整机与电池实称，其余估算 |
| 零位与仿真 HOME 的偏差 | 未量化 |
| `action_scale` | 配置为 **0.7**，而策略按 **1.0** 训练（等效只发挥 66% 动作幅度，其中 0.94 来自质量差、0.7 来自该参数）|

### 4.4 已知的能力缺口（仿真侧）

| 缺口 | 证据 |
|---|---|
| 慢速转向（0.3 rad/s）学不会 | 所有策略该桶均 ≈0.02，判断为步态层物理限制 |
| 斜坡策略不会转向 | `slope_v2` 转向桶为 0 |
| 坐下只蹲到 0.0748（目标 0.060）| `sitstand` 实测 |
| 转圈会倒 | `spin` 摔倒率 100% |
| 踢球超速 6.5 倍 | 目标 0.25 m/s，实测 1.62 m/s |

---

## 5. 关键文件索引

| 路径 | 内容 |
|---|---|
| `policies/*.onnx` | 16 个导出策略（8 个交付用，其余为中间产物）|
| `docs/hd1910/ACCEPTANCE.md` | 五阶段验收记录，含失败证据与方法学教训 |
| `docs/hd1910/DEPLOY.md` | 部署机制、两个坑、全槽位打包、版本切换 |
| `docs/hd1910/BOM.md` / `WEIGHING_PROTOCOL.md` | 物料与称重清单 |
| `scripts/pack_deployment.py` | 按固件契约打包模型目录 |
| `scripts/duck-profile` | 配置版本快照与切换（装到板子 `/usr/local/bin`）|
| `scripts/accept_velocity.py` | 走路 7 桶分轴验收（阈值 + 录像）|
| `scripts/accept_velstand.py` | 打翻 → 恢复率与恢复时间 |
| `scripts/accept_skill.py` | 四种技能分别验收（含特写录像）|
| `scripts/accept_roulade.py` | 翻滚跨度测量（必须强制站姿起翻）|
| `docs/hd1910/artifacts/*.json` | 上述验收的原始数据 |
| `src/mjlab_microduck/tasks/microduck_*_hd1910_env_cfg.py` | HD-1910 任务定义 |

---

## 6. 三条值得先读的教训

1. **奖励曲线会骗人。** 出现过"奖励 127 但机器人站着不动"；也有"日志恢复率 0.002 但实测 100%"。**只有实测算数。**
2. **换执行器 = 换问题。** XL330 调好的配方在 HD-1910 上会掉进"站着不动"，必须重新配平奖励。
3. **改重置分布比调权重有效。** 把"仰面姿态出现概率"从 0 提到 20%，仰面恢复率从 0% 变成 100%。
