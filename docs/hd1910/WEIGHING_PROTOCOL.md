# HD1910 实机称重协议（deliverable A）

> 这一份是**给人做的**：我没有任何硬件，无法代替你上秤。
> 代码侧已就绪——你只要填 `mass_table.csv`，一条命令重新生成 3 个 MJCF。

## 0. 秤与误差预算

| 项 | 要求 | 理由 |
|---|---|---|
| 量程/分度 | 0.01 g 分度，量程 ≥ 500 g | 最小件 `neck_pitch` 只有 5.72 g，0.1 g 分度会让它 ±2% |
| 校准 | 每次开始前用 100 g / 200 g 标准砝码单点校准 | 廉价天平在 1/10 量程处线性度最差 |
| 环境 | 无风、水平台面、室温稳定 | 气流会让 5 g 级读数跳 0.05 g |
| 次数 | 每件称 3 次取中位数，极差 > 0.05 g 重称 | 记录极差，作为不确定度 |
| 记录 | 单位统一 **g**，保留两位小数 | CSV 里 `mass_g` |

## 1. 称什么：先整体，后逐件

**顺序很重要**：先称整机，再拆。整机质量是唯一能校验「逐件求和」的基准，
拆完就再也拿不到了。

1. **整机（装电池、不插线）**：`M_total`
2. **整机（含全部线束、上电状态）**：`M_total_wired`
3. **电池单独**：`M_batt`
4. 逐件拆称（见下）

逐件求和与整机之差应 ≤ 1%（螺丝、胶、扎带、焊点都在里面）。
若差得多，说明有件没归到任何 body 上——**不要用比例缩放去凑总质量**，
那正是任务书禁止的「只改总质量」。

## 2. 逐件清单（对应 21 个 body）

MJCF 的 body 是**装配体**，不是零件。每个 body 的质量 = 它挂着的所有零件：

| body | 需要上秤的零件 |
|---|---|
| `trunk_base` | 躯干壳（上下）、电池、主控 PCB、扩展板/RPi hat、电源支架、线束（含扎带） |
| `yaw2roll` / `bearing_roll` | 髋 yaw 舵机 + 输出舵盘 + yaw→roll 支架 + 螺丝 |
| `hip_l` / `hip_l_2` | 髋 roll 支架 + 轴承（22×16×4）+ 轴 |
| `upper_leg_left` / `upper_leg_right` | 大腿壳 + 舵机 + 舵盘 + 刚性加强板 |
| `leg` / `leg_2` | 小腿壳 + 舵机 |
| `ankle_left` / `ankle_right` | 踝舵机 + 脚壳 + 脚垫（左右各一） |
| `ankle_l_v1` / `ankle_r_v1` | 轮滑版脚部（舵机 + 刀架） |
| `tire` … `tire_4` | 单个滚轮（含轮轴） |
| `neck` | 颈座 + 舵机 |
| `neck_pitch` | 颈俯仰摇臂 + 舵机 |
| `yaw_roll_motion` | 头部 yaw/roll 联动件 + 舵机 |
| `jaw_soft` | 头部上下壳 + 喙 + 镜头 + 头内线束 |

> 左右件必须**分别称**，不要「左边 ×2」。原 MJCF 里左右就已经不同
> （`hip_l` 6.18934 g vs `hip_l_2` 6.18930 g，`ankle_left` 30.0246 vs `ankle_right` 30.0251）。

## 3. 舵机替换的关键数字（先称这一个）

**HD1910 单颗质量必须先称。** 它是整个质量预算的主导项：

```
ΔM ≈ 14 × (m_HD1910 + m_舵盘 − m_XL330 − m_舵盘)
```

XL330 约 18 g 量级；若 HD1910 是 STS3215 一类的 55–60 g 舵机，
14 颗就会让整机从 737 g 涨到 **1.2 kg 量级**——那不是「换个舵机」，
而是换了一台机器人：站立高度、CoM、kp、步态全都要重来。

所以流程是：

1. 称 1 颗 HD1910（含舵盘）→ 填入下表，先算一次总质量预测；
2. 若总质量变化 > 15%，**先不要跑长训练**，先做 `home` 阶段验证与 kp 扫参；
3. 逐件称完 → 生成模型 → 重新做 `home` / `step` 验证。

## 4. 填写格式

`src/mjlab_microduck/robot/microduck/hd1910/mass_table.csv`：

```csv
body,part_label,mass_g,com_mm,inertia_mode,inertia_kgm2,status,source,notes
trunk_base,躯干...,215.40,"-21.0 -0.1 3.2",scale,,measured,kitchen_scale_0.01g,3次中位数 极差0.03
hip_l,...,7.85,,scale,,measured,...,
```

- `mass_g`：实称，g
- `com_mm`：**可选**。没有三坐标/悬线法就留空 → 沿用基线 CoM，并在 `notes` 写 `com=baseline`
- `inertia_mode`：
  - `scale`（默认，推荐先这样）：按质量比等比缩放基线惯量张量
  - `explicit`：在 `inertia_kgm2` 填 6 个数 `ixx iyy izz ixy ixz iyz`
  - `keep`：只改质量，惯量完全不动（不推荐，会质量/惯量不自洽）
- `status`：只有写 `measured` 才会被写进模型；其余一律视为占位
- `source`：秤的编号 / 谁称的 / 日期，便于追溯

## 5. 生成与校验

```bash
cd src/mjlab_microduck/robot/microduck/hd1910
python build_hd1910_mjcf.py --audit     # 先看：哪些还缺、总质量变成多少
python build_hd1910_mjcf.py             # 生成 3 个 XML（没有 PLACEHOLDER 横幅才算完）
python build_hd1910_mjcf.py --check     # CI/测试用：XML 与表是否一致
```

全部 21 行都是 `measured` 后，横幅会从
`PLACEHOLDER MASSES — NOT HD1910 DYNAMICS` 变成 `HD1910 measured masses`。
**没有这行变化，就不要对外说「HD1910 动力学已就绪」。**

## 6. 惯量的诚实边界

用厨房秤只能得到质量，得不到惯量。三者按可信度排序：

1. **CAD 重算**（最好）：把 HD1910 的三维模型换进装配，直接导出 `fullinertia`；
2. **等比缩放**（默认，可接受）：`I_new = I_base × m_new/m_base`，假设质量分布在
   同一几何上缩放。舵机变重但外形不变时会高估惯量；
3. **只改质量、不动惯量**（不推荐）：总质量对了，转动惯量还是 XL330 的，
   摆动动力学系统性偏快。

请在报告的「不确定度」一节写明用的是哪一种，以及 ±多少。
