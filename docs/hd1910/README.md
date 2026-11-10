# HD1910 适配记录（2026-11-10）

> 目标：把 Microduck 的仿真/RL 从 Dynamixel XL330 适配到 Feetech HD1910，并训练出可部署的行走策略。
> 基线：`pollen-robotics/microduck_rl`（本机 `/develop/microduck_rl`，develop @ `5946fd9`）。
> 本文件是交付项的**验收记录**，不是计划书。所有数字都来自本机实测，可复现命令在每节末尾。

## 0. 一句话状态

**管道全通、训练前段全绿；质量已按规格书推算到 782.2 g（+45 g），但仍是估算、不是实称。**

- ✅ 能跑：新任务注册、64×5 smoke、ONNX 导出、分阶段验证脚本，全部实测通过
- ⚠️ 未完成：**A 项（实机逐件称重）我无法做——这里没有硬件**。已用你仓库里的规格书数值做了估算版（§4），实称后一行替换即可
- ❌ 未做且不会被声称：**没有训练出可走策略**。smoke 的 5 轮策略在 10 s 内翻倒（tilt 173°），符合预期

## 1. 交付项对照

| 项 | 状态 | 证据 / 位置 |
|---|---|---|
| A 逐件称重 + 质量表 | ⛔ **阻塞（需你上秤）** | 协议：[`WEIGHING_PROTOCOL.md`](WEIGHING_PROTOCOL.md)；表：[`mass_table.csv`](../../src/mjlab_microduck/robot/microduck/hd1910/mass_table.csv)（21 行，0/21 measured） |
| B 改每个 `<inertial>` | 🟡 机械就绪，数值待填 | 生成器 [`build_hd1910_mjcf.py`](../../src/mjlab_microduck/robot/microduck/hd1910/build_hd1910_mjcf.py)；3 个 XML 已生成 |
| C 关节/执行器核对 | ✅ 静态核对完成；实机核对待做 | §6，含 BAM 覆盖关系与 CPU 复现 PD 推导 |
| D 保留 1910_m6.json | ✅ | 原样副本 + [`PROVENANCE.md`](../../src/mjlab_microduck/robot/microduck/hd1910/PROVENANCE.md) |
| E 64×5 → 4096 正式训练 | 🟡 smoke 已验证；正式训练未跑（无有效质量、无长跑授权） | §7，日志 `artifacts/smoke_train.log` |
| F ONNX + 形状 + 归一化 | ✅ | §8，`[1,61]→[1,14]`，图首节点 `Sub→Div`，SHA256 已记录 |
| G 分阶段验证 | ✅ 仿真侧 4 个阶段跑通 | §9，`artifacts/validate_all.json` |
| H 交付物 | ✅ 本文件 + 4 个 artifact | `docs/hd1910/` |

## 2. 代码改动清单

新增：

| 文件 | 作用 |
|---|---|
| `robot/microduck/hd1910/1910_m6.json` | 上游参数，**逐字节保留** |
| `robot/microduck/hd1910/PROVENANCE.md` | 来源、字段差异表、使用边界 |
| `robot/microduck/hd1910/mass_table.csv` | 21 行质量表（唯一人工维护的数据） |
| `robot/microduck/hd1910/build_hd1910_mjcf.py` | 由表生成 MJCF；`--init-table / --audit / --check` |
| `robot/microduck/robot_{walk,allcollisions,allcollisions_rollers}_hd1910.xml` | 生成物（体量 32–40 KB，与上游同结构） |
| `tasks/microduck_velocity_hd1910_env_cfg.py` | 速度任务薄封装（只换 robot entity） |
| `tests/test_hd1910_cfg.py` | 26 个不变量测试 |
| `scripts/hd1910_validate.py` | 分阶段验证（home / drift / step / walk） |
| `docs/hd1910/` | 本记录 + 协议 + artifacts |

修改：

| 文件 | 改动 |
|---|---|
| `actuator/friction_dr_bam.py` | 新增 `FeetechSts3215BamActuator(Cfg)`，修上游崩溃（§3.1） |
| `actuator/__init__.py` | 导出新增的两个类 |
| `robot/microduck_constants.py` | HD1910 参数常量、3 个 robot cfg、规格加载器 |
| `tasks/__init__.py` | 注册 2 个任务 |

未改动：`microduck_velocity_env_cfg.py`（949 行的配方）、`mdp.py`、任何 XL330 任务。HD1910 是从配方**派生**的，不是分叉的——这样两者的 A/B 才有意义。

## 3. 过程中发现的三个真实缺陷

### 3.1 上游 BAM 的 STS3215 路径从未在 mjlab 下跑通过

第一次 smoke 直接崩：

```
AttributeError: 'STS3215Actuator' object has no attribute 'q_target_smooth'
  bam/feetech/actuator.py:82  in compute_control
```

原因：`STS3215Actuator` 把「固件目标位置限速」的状态存在 `q_target_smooth` 里，而该属性**只在 `load_log()` 里创建**——那是离线辨识入口。`bam.mjlab` 从不调用它。Dynamixel 系列不用这个属性，所以 XL330 一直正常，STS3215 这条路径是第一次被真正执行。

修复（`actuator/friction_dr_bam.py`）：`FeetechSts3215BamActuator` 在 `initialize()` 后补建 `(num_envs, 1)` 的 `q_target_smooth`，并在 `reset()` 时清零，避免跨情节残留。

### 3.2 `1910_m6.json` 缺少 BAM m6 需要的 7 个字段，且**静默**取默认值

BAM 的 `load_parameters_from_dict()` 对缺失字段不报错、不警告。实测生效值：

| 参数 | 文件 | 实际生效（BAM 默认） | XL330 m6 |
|---|---|---|---|
| `friction_viscous` | 缺 | **0.1** | 0.00536 |
| `load_friction_motor` | 缺 | **0.05** | 0.2668 |
| `load_friction_external` | 缺 | **0.05** | 8.5e-6 |
| `load_friction_*_stribeck` | 缺 | **0.05** | 1.07e-5 / 0.0808 |
| `load_friction_*_quad` | 缺 | 0.0 | 0.00997 / 0.0049 |
| `dtheta_stribeck` | 缺 | 0.2 | 2.89 |
| `alpha` | 缺 | 1.35 | 8.68 |
| `command_delay` | 0.006005 | **BAM 不读这个字段** | — |
| `max_velocity` | 100 | 100（STS3215 默认 5.22） | — |

处理：**不改原文件**，把这组值写成断言（`tests/test_hd1910_cfg.py::test_bam_silently_defaults_the_unmeasured_m6_fields`）。BAM 一旦改默认值，测试立刻红。`max_velocity=100` 会把固件目标限速抬到失效，需台架复核。

### 3.3 只从 walk 模型建质量表会漏 6 个轮滑 body

轮滑版把脚换成 `ankle_*_v1` + 4 个 `tire`。`--init-table` 最初只扫 `robot_walk.xml`，新测试当场抓到 `KeyError: 'ankle_l_v1'`。已改为三模型并集（21 个 body）。

## 4. 质量表（A / B）

### 4.1 能从 `1910_m6.json` 算出重量吗？——不能

`1910_m6.json` 是**电机电气+摩擦模型**：`kt`（反电动势常数）、`R`（绕组电阻）、
`friction_*`（摩擦）、`armature`（折算转子惯量）、`command_delay`、`max_velocity`。
里面**没有任何质量量纲**。`armature` 看起来像"惯量"，但它是**转子**的折算惯量
（1.78e-3 kg·m²，比整机任何一个连杆都大一个量级，正是转子+减速比的折算结果），
不是舵机外壳的质量。所以：**从这份文件推不出克数，无论如何推不出。**

### 4.2 但重量是可以有出处的——你仓库里就有

`microduck-replica/docs/HD-1910训练前数据清单.md` 明确给了【规格书】HD-1910-C001 A/0：

| | XL330 | HD-1910 | 来源 |
|---|---|---|---|
| 单只质量 | 18 g | **21 g** | 飞特规格书 A/0 |
| 堵转力矩 @7.4 V | 0.6 N·m | **1.47 N·m** | 同上 |
| 减速比 / 回差 | — | 320:1 / ≤0.5° | 同上 |

按 15 颗（14 个策略关节 + 嘴 1 颗）算：**ΔM = 15 × 3 g = +45 g，整机 737.2 → 782.2 g（+6.1%）**。

已按「舵机装在被驱动关节的连杆上」这一机械约定，把 +3 g 摊到 14 个 body
（`jaw_soft` 承担 head_roll + 嘴 = +6 g），其余 5 个无舵机 body 标 `baseline` 不动。

### 4.3 分配方式重要吗？——不重要，±1.3 mm 量级

同样 +45 g，三种摊法对整机质心的影响：

| 摊法 | 总质量 g | CoM z (mm) | Δz | Δx |
|---|---:|---:|---:|---:|
| 全加在 trunk | 782.2 | 143.94 | +7.19 | −1.38 |
| 按关节连杆摊（已采用） | 782.2 | 142.66 | +5.91 | +0.32 |
| 全机身等比缩放 | 782.2 | 145.10 | +8.35 | +0.03 |

**三种摊法的 CoM 差异只有 1.3 mm（z）/ 1.7 mm（x）**——因为每颗只差 3 g、
且分散在整机。所以"按关节摊"足够，不必纠结；真正的误差源在下面。

### 4.4 真正的大头不是舵机，是躯干

清单自己写了：**电池 NP-F550 待称（典型 ~100 g），且"是否已含在 trunk_base 里待核"**。
这 100 g 的不确定性，是舵机那 45 g 的两倍多，而且全在最高、最靠后的位置——
对 CoM 和站立稳定性的影响远超舵机替换。**上秤时先称电池和 trunk。**

### 4.5 当前状态

| 项 | 值 |
|---|---|
| 已审核 body | 21 / 21（16 estimated + 5 baseline） |
| 实称 | **0** |
| 生成物横幅 | `ESTIMATED MASSES — datasheet-derived, NOT weighed` |
| walk / allcollisions 总质量 | 737.243 → **782.244 g** |
| rollers 总质量 | 736.830 → **781.830 g** |

实称后只需改 `mass_table.csv` 的 `mass_g` + `status=measured`，跑一次生成器，
横幅会变成 `MEASURED MASSES`。

命令：

```bash
cd src/mjlab_microduck/robot/microduck/hd1910
python build_hd1910_mjcf.py --audit    # 表格现状 + 三模型总质量预测
python build_hd1910_mjcf.py            # 重新生成 3 个 XML
python build_hd1910_mjcf.py --check    # 与提交物比对（测试也在用）
```

## 5. MJCF diff 策略（B）

不手改 XML。生成器做的是**文本级定点替换**：只重写每行 `<inertial .../>`，其余每个字节（注释、geom 顺序、mesh 引用、默认类）原样保留。因此 diff 是可评审的。

真实 diff（当前占位状态，只有执行器段变化）：

```diff
+<!-- GENERATED by hd1910/build_hd1910_mjcf.py from robot_walk.xml + hd1910/mass_table.csv -->
+<!-- PLACEHOLDER MASSES — NOT HD1910 DYNAMICS -->
@@ class="chosen_actuator" @@
-      <joint damping="0.053" frictionloss="0.0048" armature="0.0018"/>
-      <position kp="0.55" kv="0.0" forcerange="-0.96 0.96" ctrlrange="-10.0 10.0"/>
+      <joint damping="0.075890" frictionloss="0.015803" armature="0.0017819"/>
+      <position kp="0.787537" kv="0.0" forcerange="-1.016198 1.016198" ctrlrange="-10.0 10.0"/>
```

填入实测后，每个 body 会再多一行这样的变化（质量、CoM、fullinertia 同时改）：

```diff
-<inertial pos="-0.006157 -0.013748 -0.015659" mass="0.030025" fullinertia="5.91525e-06 ..."/>
+<inertial pos="-0.006000 -0.013000 -0.015000" mass="0.045000" fullinertia="8.86561e-06 ..."/>
```

三种惯量模式：`scale`（按质量比缩放基线张量，默认）/ `explicit`（给 6 个数）/ `keep`（只改质量）。模式写在 CSV 里，逐 body 可选。

## 6. 关节与执行器核对（C）

### 6.1 静态核对结果（14 个伺服关节 + 1 个被动 jaw + free joint）

| 属性 | 原模型 | HD1910 生成物 | 训练时实际生效 |
|---|---|---|---|
| 关节轴 | 全部 `[0,0,1]`（局部 z） | 不变 | — |
| 限位 | hip_yaw L `[-0.4363,0.5236]` / R `[-0.5236,0.4363]`；hip_roll `±0.384`；hip_pitch / knee / ankle `±1.5708`；neck_pitch `[-1.5708,1.0472]`；head_pitch `±1.5708`；head_yaw `±2.9671`；head_roll `±0.4363` | 不变 | `soft_joint_pos_limit_factor=0.9` |
| damping | 0.053 | 0.07589 | **BAM 置 0**，改用模型自算黏性摩擦 |
| frictionloss | 0.0048 | 0.015803 | **BAM 置 0**，改用摩擦预算 |
| armature | 0.0018 | 0.0017819 | **BAM 用 json 的 armature 覆盖** |
| forcerange | ±0.96 | ±1.016198 | **BAM 覆盖为 `vin_max·kt/R`** |
| ctrlrange | ±10 | ±10 | 不变（低 kp 需要过冲空间） |
| XML PD kp | 0.55（原注释：对应固件 kp=200） | **1.210000** | 训练不用；仅 CPU 预演（`infer_policy.py`）用 |
| 力矩上限 | ±0.96（XL330 BAM 推导） | **±1.5903**（datasheet 推导） | BAM 覆盖为 `vin_max·kt/R` |

**关键结论：训练时 XML 里的 damping/frictionloss/armature/forcerange 全部被 BAM 覆盖，只有 `kp` 会被 `infer_policy.py` 的 CPU MuJoCo 用到。** 所以：

- 训练侧参数只认 `json_path` + `kp_fw` + `vin_*` + `delay_*`；
- 但**预演侧必须同步**，否则「仿真里能走、预演里摔倒」会浪费一整天。

### 6.2 CPU 复现 PD 的推导（写死在生成器里，测试会重算校验）

**2026-11-10 起改用规格书参数**（`hd1910/1910_datasheet.json`，`HD1910_ACTIVE_PARAMS_JSON`）：

```
kt  = 7.5 kg·cm/A = 0.7355 N·m/A       （规格书 Kt）
R   = 7.4 V / 2.0 A 堵转电流 = 3.7 Ω    （规格书 堵转 15 kg·cm@7.4V / 2.0 A）
stall = kt·7.4/R = 1.471 N·m            （规格书 1.47 ✓）
forcerange = kt·8.0/R = 1.5903 N·m      （BAM 用 vin_max 取上限）
k_eff = error_gain·ratio·kp_fw·vin_nom·max_pwm·kt/R
      = 0.166 · 0.981928 · 5 · 7.7 · 0.97 · 0.7355 / 3.7 = 1.2100 N·m/rad
damping = 0.053 · (1.21 / 0.55) = 0.116604
```

`error_gain_ratio = 0.981928 = 0.163/0.166`：0.163 是飞特官方 BAM（`ftservo/ftservo-bam`）
的占空比系数，0.166 是本仓库锁定的 Rhoban BAM 里 STS3215 的系数——用这个比值让
STS3215 控制律等价于飞特官方的 HD1910 控制律。

**三个版本的真实堵转力矩对比（同一个 `kp_fw=5`）**：

| 来源 | 7.4 V 堵转 | 小误差刚度 @P=5 | 说明 |
|---|---:|---:|---|
| 陆吾 LuwuDynamics `1910_m6.json` | 0.91 N·m | 1.03 N·m/rad | 动态拟合，含齿轮箱损耗 |
| 飞特官方 `ftservo-bam` m6 | 1.37 N·m | 1.15 N·m/rad | 飞特自家辨识 |
| **规格书 HD-1910-C001 A/0（当前采用）** | **1.47 N·m** | **1.21 N·m/rad** | 与官方值差 0.10 |

对照 XL330 在 `kp_fw=200, 7.5 V` 下 `k_eff = 0.562 N·m/rad`——**HD1910 现在比它硬 2.2 倍**，
这符合“这颗舵机力气本来就是 XL330 的 2.5 倍”的规格。

⚠️ 一个已知的不自洽：BAM 用同一个 `kt` 既算力矩又算反电动势，所以
无载转速变成 `7.4/0.7355 = 10.06 rad/s`，而规格书是 11.8 rad/s（差 15%）——
规格书的无载转速隐含 `kt_emf ≈ 0.627`。要同时对上两个数，需要给 BAM 打补丁分开
力矩常数与反电动势常数；目前选择保堵转（对 RL 影响更大）。

### 6.3 仍需实机核对（我无法完成）

| 项 | 方法 | 判定 |
|---|---|---|
| 零位 | 上位机读到 0 时，关节是否在机械中位；用直角尺/治具 | 偏差 > 1° 需在固件写 offset，**不要抄任何 json 里的 q_offset** |
| 方向 | 发 +0.1 rad，肉眼确认转向与 MJCF 正方向一致 | 反了改 XML 轴/零位，不要靠 RL 学 |
| 限位 | 手推到机械极限，读编码器值，与 MJCF `range` 对齐 | MJCF 限位必须**小于**机械极限 |
| `max_velocity` | 阶跃看是否有目标限速 | json 里 100 rad/s ≈ 无限速，需确认固件行为 |
| 延迟 | 用 `scripts/testbench_sim2real.py` 记录阶跃，与 sim 叠图 | 15–30 ms 是否覆盖真实值 |

## 7. 训练（E）

64 环境 × 5 轮 smoke（实测）：

```bash
WANDB_MODE=offline uv run --no-sync train Mjlab-Velocity-Flat-MicroDuck-HD1910 \
    --env.scene.num-envs 64 --agent.max_iterations 5 \
    --agent.logger tensorboard --agent.run-name hd1910_smoke
```

```
Iteration time: 1.26s      Time elapsed: 00:00:06
Episode_Termination/nan_state: 0.0000      Episode_Termination/fell_over: 1.8333
Episode_Reward/track_linear_velocity: 0.0228   … 16 个奖励项全部有值
```

- 无 NaN；61 维观测；14 维动作；日志目录 `logs/rsl_rl/velocity_hd1910/<ts>_<name>/`（与 XL330 的 `velocity/` 隔离）
- 完整日志：[`artifacts/smoke_train.log`](artifacts/smoke_train.log)

正式训练**没有跑**，原因写在 §10：质量表未填，跑 4096 环境就是在训练 XL330 动力学的策略，只是换了个执行器模型。

正式命令（质量表补齐、`home` 验证通过后再执行）：

```bash
uv run --no-sync train Mjlab-Velocity-Flat-MicroDuck-HD1910 \
    --env.scene.num-envs 4096 --agent.max_iterations 5000 \
    --video --video-interval 500
```

## 8. ONNX（F）

```bash
uv run --no-sync scripts/export.py Mjlab-Velocity-Flat-MicroDuck-HD1910 \
    --checkpoint-file logs/rsl_rl/velocity_hd1910/<run>/model_4.pt \
    --onnx-file hd1910_walk.onnx
```

校验结果：

| 项 | 实测 |
|---|---|
| 输入 | `obs [1, 61]` |
| 输出 | `actions [1, 14]` |
| 归一化 | ✅ 图首两个节点是 `Sub`、`Div`（normalizer 已烘焙，非裸策略） |
| 元数据 | 14 joints / `twist,head_pose,body_pose` / 8 个 obs 项 / `action_scale=1.0` |
| SHA256 | `822bf5b5f276575e634ab0de1f0deec04b214c2360e10bac436c5c98175d4ca0`（估算质量版；上一版占位质量是 `5e4e7c99…`） |
| 大小 | 793 776 B |

记录文件：[`artifacts/onnx_sha256.txt`](artifacts/onnx_sha256.txt)。
注意：**这份 ONNX 是 5 轮 smoke 的权重，只用于验证导出链路**，不是策略。

## 9. 分阶段验证（G）

```bash
uv run --no-sync scripts/hd1910_validate.py --stage all --onnx hd1910_walk.onnx
```

| 阶段 | 结果 | 判读 |
|---|---|---|
| **home**（3 s 保持 HOME，带噪声初值） | z=0.1159 m，tilt max **2.8°**，关节漂移 0.042 rad（+45 g + 规格书执行器） | ✅ 更硬的关节把倾角从 6–7° 压到 2.8° |
| **drift**（10 s 零动作保持） | **6.0 s 时摔倒**（tilt 179.9°） | ⚠️ 见下方对照实验——这不是 HD1910 的问题 |
| **drift 对照（XL330 模型，同一命令）** | **2.5 s 时摔倒**（tilt 179.7°） | ✅ 同样会倒，且更早：**零动作开环保持本来就不是站姿控制器** |
| **step（站立姿态）** | 14 关节 +0.2 rad：方向全部正确（无反向关节）；`left_knee` 只到 0.001 rad，`right_knee` 0.045 rad | ⚠️ 该工况被闭链负载主导，**不是执行器的指标** |
| **step（零重力，隔离负载）** | 全部关节到 0.13–0.20 rad，稳态误差 0.0005–0.07 rad | ✅ 伺服本身正常：误差就是摩擦死区（`friction_base/k_eff ≈ 0.02 rad` + 负载项） |
| **walk**（smoke ONNX，10 s） | tilt 173°、`survived=false` | ✅ 期望中的失败：5 轮策略本来就该倒 |

**结论（经过对照实验修正）**：

1. 我最初把 "10 s 保持时摔倒" 读成了"`kp_fw=5` 太软"，**这是错的**。用 XL330 模型跑同一工况也倒，而且更快（2.5 s vs 6.0 s）。零动作保持不等于站姿控制——这台机器人需要反馈，开环 PD 会飘走。**不要把 drift 阶段当站立测试用。**
2. 由零重力阶跃可得：`kp_fw=5` → `k_eff = 0.79 N·m/rad`，比 XL330 的 0.56 更硬，**不存在刚度退化**；关节能跟上 0.2 rad 指令，剩余误差是摩擦死区。
3. 站立姿态下的单关节阶跃之所以看起来"抬不动"，是因为脚踩在地上、整个人体成了闭链负载——**执行器辨识必须用零重力阶跃或台架（`testbench_sim2real.py`）**，不能用站立姿态。
4. 因此现在**没有证据**要求改 `kp_fw`。质量表填完后按顺序重跑 `home` → `step --zero-gravity`；`kp_fw` 是否要动，等台架阶跃叠图再决定。

复现：

```bash
uv run --no-sync scripts/hd1910_validate.py --stage all --onnx hd1910_walk.onnx \
    --out docs/hd1910/artifacts/validate_all.json
uv run --no-sync scripts/hd1910_validate.py --stage drift \
    --task Mjlab-Velocity-Flat-MicroDuck \
    --out docs/hd1910/artifacts/validate_drift_xl330_control.json       # 对照
uv run --no-sync scripts/hd1910_validate.py --stage step --zero-gravity  # 执行器指标
```


## 10. 禁止事项自检

| 禁止 | 本实现 |
|---|---|
| 不把 XgoDuck 质量/惯量抄到 Microduck | `mass_table.csv` 全部沿用本机 MJCF 基线并标记 `placeholder`；没有引入任何外部质量数字 |
| 不把 `q_offset` 抄进实机零位 | 原 json 里本就没有 `q_offset`；`PROVENANCE.md` 明确写了「即使存在也不能当机械零位」 |
| 不混入 kissqy 非商业权重 | 未下载、未引用、未合并任何第三方权重；ONNX 只由本仓库 smoke checkpoint 导出 |
| 不声称「已训练出可走策略」 | 全文没有此表述；walk 阶段明确 `survived=false`，并标注「sim-only，不是部署验收」 |

## 11. 你接下来要做的 5 件事

1. **称 1 颗 HD1910（含舵盘）** → 先算总质量预测。若 > +15%，停下来重新评估 kp 与步态。
2. **逐件称重** → 填 `mass_table.csv`（21 行，`status=measured`）→ `build_hd1910_mjcf.py` → 确认横幅变成 `HD1910 measured masses`。
3. **重跑 home / step --zero-gravity** → 记录新的站立高度与刚度表现。只有零重力阶跃出现明显变差时，才去动 `kp_fw`。
4. **台架阶跃**（`scripts/testbench_sim2real.py`，用 HD1910 替换 XL330）→ 叠图，必要时重拟合 `friction_viscous` 与 6 个 `load_friction_*`，另存 `1910_m6_refit.json`。
5. **4096 环境正式训练 5000 iter** → 导出 ONNX → 记录 SHA256 → 实机分阶段验收。

**「可部署的行走策略」的验收门槛（缺一不可）**：实机站立 ≥ 10 s 不摔；0.2 m/s 直行 ≥ 5 m；0.4 m/s 转向 ≥ 3 圈；被推 ±0.3 m/s 能恢复；以上都有视频与日志。在此之前，只能叫「仿真中的候选策略」。

## 12. 本机能否支撑训练？（实测）

本机：**RTX 2070 SUPER / 8 GB VRAM / 8 核 / 39 GB RAM / 233 GB 空余**。
在 HD1910 任务上逐档实测（每档跑 3 个 iteration，全程采样 `nvidia-smi`）：

| 任务 | 环境数 | 结果 | 峰值 VRAM |
|---|---:|---|---:|
| Flat | 1024 | ✅ 1.60 s/iter | 2 079 MiB |
| Flat | 2048 | ✅ 2.06 s/iter | 3 283 MiB |
| **Flat** | **4096** | ✅ **3.21 s/iter** | **5 701 MiB** |
| Rough | 512 | ✅ 2.86 s/iter | 3 312 MiB |
| **Rough** | **1024** | ✅ **3.14 s/iter** | **5 377 MiB** |
| Rough | 2048 | ❌ OOM（尝试分配 1.6 GB 时失败） | 7 720 MiB |
| Rough | 4096 | ❌ OOM | — |
| Rough 4096 + `nconmax=100, iterations=20` | 4096 | ❌ 仍 OOM | 7 701 MiB |
| Rough 2048 + 缩小地形网格 | 2048 | ❌ 仍 OOM | 7 715 MiB |

结论：

1. **Flat 4096 环境完全撑得住**，5.7 GB / 8 GB，还剩 ~1.6 GB 余量。
   5000 iteration ≈ **4.5 小时**（3.21 s × 5000），加 wandb/tensorboard 开销按 5 小时算。
2. **Rough 在 8 GB 上封顶约 1024 环境**（5.4 GB，3.14 s/iter）。
   2048 起必 OOM，且**不是 nconmax 或地形尺寸的锅**（两个旋钮都试过，仍然 OOM）——
   瓶颈在 Rough 的每环境缓冲（`nconmax=200` + 30/50 次求解迭代 + 足底射线扫描）。
   想上 4096 只能换卡（16 GB 起）或砍传感器项（未验证）。
3. 代价换算：Rough 1024 环境每轮只有 24×1024 = 24.6k 样本，是 Flat 4096 的 1/4。
   要达到同样的样本量，迭代数要 ×4（约 2 万轮 / 17 小时）。
   **所以在这台机器上：Flat 走 4096，Rough 先用 1024 出「能走」再谈泛化。**
4. 磁盘：每轮 checkpoint 4.8 MB × 200 个 ≈ 1 GB/run，233 GB 空余完全够。

复现命令（Flat 4096 与 Rough 1024 都能直接跑）：

```bash
# Flat：规格书要求的 4096 环境
uv run --no-sync train Mjlab-Velocity-Flat-MicroDuck-HD1910 \
    --env.scene.num-envs 4096 --agent.max_iterations 5000 \
    --video --video-interval 500

# Rough：本机上限
uv run --no-sync train Mjlab-Velocity-Rough-MicroDuck-HD1910 \
    --env.scene.num-envs 1024 --agent.max_iterations 20000
```

---

### 复现全部检查

```bash
uv run --no-sync --with pytest pytest tests/test_hd1910_cfg.py -q   # 26 passed
uv run --no-sync --with pytest pytest tests/ -q                     # 192 passed, 1 skipped
```
