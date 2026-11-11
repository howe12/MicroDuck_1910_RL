# MicroDuck_1910_RL

MicroDuck 双足机器人在**仿真中**的强化学习训练工程，执行器从 Dynamixel XL330 换成
**Feetech HD-1910**。

- 机器人：约 782 g / 25 cm / 14 个策略关节（+1 个被动嘴关节）
- 仿真：MuJoCo Warp（mjlab 1.3.0），50 Hz 控制
- 算法：PPO（rsl_rl）
- 策略接口：**61 维观测 → 14 维动作**（与上游 Microduck 运行时契约一致）
- 产物：ONNX（归一化已烘焙）

> 本仓库只覆盖「仿真 + 训练 + 导出」。真机运行时、舵机零位标定、实机验收不在范围内。

## 快速开始

需要 CUDA GPU 与 [uv](https://docs.astral.sh/uv/)。

```bash
# 首次（ARM 机器：export UV_HTTP_TIMEOUT=600）
export UV_HTTP_TIMEOUT=600
uv sync

# 若 uv sync 卡在 better-actuator-models（GitHub git 依赖）上：
#   1) 先设置 git 缓冲再重试
git config --global http.postBuffer 524288000
uv sync
#   2) 仍失败则本地 vendor（改 pyproject.toml 的 [tool.uv.sources]）：
#   git clone https://github.com/Rhoban/bam.git third_party/bam
#   better-actuator-models = { path = "third_party/bam", editable = true }

# 1) 6 秒验证工程是活的（64 环境 × 5 轮冒烟）
uv run train Mjlab-Velocity-Flat-MicroDuck-HD1910 \
    --env.scene.num-envs 64 --agent.max_iterations 5 \
    --agent.logger tensorboard

# 2) 正式训练（4096 环境，约 4–5 小时）
uv run train Mjlab-Velocity-Flat-MicroDuck-HD1910 \
    --env.scene.num-envs 4096 --agent.max_iterations 5000 \
    --video --video-interval 500

# 3) 看效果
uv run play Mjlab-Velocity-Flat-MicroDuck-HD1910 \
    --checkpoint-file logs/rsl_rl/velocity_hd1910/<run>/model_4999.pt

# 4) 导出 ONNX（归一化烘进图里，必须走这条）
uv run scripts/export.py Mjlab-Velocity-Flat-MicroDuck-HD1910 \
    --checkpoint-file logs/rsl_rl/velocity_hd1910/<run>/model_4999.pt \
    --onnx-file policies/hd1910_walk.onnx
```

本机实测容量（RTX 2070 SUPER 8 GB）：Flat 4096 可跑（5.7 GB / 3.2 s·iter⁻¹）；
Rough 上限约 1024（2048 起 OOM）。详见 `docs/hd1910/README.md` §12。

## 任务

| Task id | 说明 |
|---|---|
| `Mjlab-Velocity-Flat-MicroDuck-HD1910` | 平地行走（主任务） |
| `Mjlab-Velocity-Rough-MicroDuck-HD1910` | 粗糙地形（台阶 ≤1.5 cm、缓坡） |

`uv run list-envs` 可看全部 35 个任务（含上游的 XL330 版与花式动作）。

## 目录

```
src/mjlab_microduck/
├── robot/
│   ├── microduck/                  # MJCF、网格、场景
│   │   ├── robot_*_hd1910.xml      # ← 由质量表生成，勿手改
│   │   ├── robot_*.xml             # 上游 XL330 模型
│   │   └── hd1910/
│   │       ├── mass_table.csv      # 21 行质量表（唯一人工维护的数据）
│   │       ├── build_hd1910_mjcf.py# 质量表 → MJCF 生成器
│   │       ├── 1910_datasheet.json # 当前使用的执行器参数（规格书推导）
│   │       ├── 1910_m6.json        # LuwuDynamics 原始参数（保留）
│   │       ├── 1910_m6_full.json   # 同一份拟合的完整版
│   │       └── PROVENANCE.md       # 参数来源与使用边界
│   └── microduck_constants.py      # robot cfg / HOME / 执行器 cfg
├── actuator/friction_dr_bam.py     # BAM + 摩擦 DR + 回差 + Feetech 修复
└── tasks/
    ├── mdp.py                      # 全部奖励/事件/观测/命令
    ├── microduck_velocity_env_cfg.py        # 上游主配方
    ├── microduck_velocity_hd1910_env_cfg.py # ← HD1910 薄封装（只换机器人+执行器）
    └── __init__.py                 # 任务注册

tests/         195 个 CPU 测试（11 秒跑完）
scripts/       export / play_latest / hd1910_validate / infer_policy …
docs/hd1910/   质量表说明、参数溯源、BOM 与电池口径、验证记录、artifacts
```

## 当前状态（重要）

| 项 | 状态 |
|---|---|
| 机器人模型 | **质量按 HD-1910 规格书推算**：15 颗 × 21 g（XL330 为 18 g）→ 782.2 g，+6.1% |
| 惯量 | 按质量比缩放 XL330 张量（没有 HD-1910 连杆惯量数据） |
| 执行器 | 规格书推导：kt=0.7355 N·m/A、R=3.7 Ω → 堵转 **1.471 N·m** @7.4 V，刚度 1.21 N·m/rad |
| 测试 | 195 passed, 1 skipped |
| 训练 | 冒烟通过；**正式 5000 轮尚未跑** |
| 电池 | NP-F550 2200 mAh，**104 g**（实测）；是否已含在 `trunk_base` 里待一次称重确认，见 [`docs/hd1910/BOM.md`](docs/hd1910/BOM.md) |

质量表里 `status=estimated` 的行是规格书推算，`baseline` 是无舵机保持原值。
**实称后**改 `mass_table.csv` 再跑一次生成器即可，代码不用动：

```bash
cd src/mjlab_microduck/robot/microduck/hd1910
python build_hd1910_mjcf.py --audit   # 看现状与总质量预测
python build_hd1910_mjcf.py           # 重新生成 3 个 MJCF
python build_hd1910_mjcf.py --check   # 验证提交物与表一致
```

## 测试

```bash
uv run --with pytest pytest tests/ -q
```

## 与上游的关系

本仓库派生自 [pollen-robotics/microduck_rl](https://github.com/pollen-robotics/microduck_rl)
（Apache-2.0）：训练配方、MDP 函数库、XL330 模型、脚本均来自上游。

HD-1910 相关的新增内容：
- `robot/microduck/hd1910/`（质量表、生成器、三份参数、溯源）
- `robot/microduck/robot_*_hd1910.xml`（生成物）
- `tasks/microduck_velocity_hd1910_env_cfg.py`（任务入口）
- `actuator/` 中的 `FeetechSts3215BamActuator`（修复上游 BAM 在 STS3215 上崩溃）
- `tests/test_hd1910_cfg.py`、`scripts/hd1910_validate.py`、`docs/hd1910/`

执行器参数来自 [LuwuDynamics/xgoduck_rl](https://github.com/LuwuDynamics/xgoduck_rl)
（Apache-2.0）的公开辨识结果，电气参数（kt/R/堵转）按飞特 HD-1910-C001 规格书 A/0 重新推导。

## 许可

代码：**Apache-2.0**（见 `LICENSE`、`NOTICE.md`）。
**3D 模型文件（`assets/*.stl` 等）为 CC BY-SA-NC**，不可商用。公开发布时请排除或替换。
