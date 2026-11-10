# NOTICE

本仓库包含并派生自以下项目的成果，按其各自许可使用：

## Microduck 训练工程（上游）

- 项目：**pollen-robotics/microduck_rl**
- 地址：https://github.com/pollen-robotics/microduck_rl
- 版本：`develop` 分支，commit `5946fd9`（2026-09-01）
- 许可：Apache License 2.0
- 使用范围：训练框架、MDP 函数库（`tasks/mdp.py`）、速度任务配方、XL330 MJCF 模型、
  导出/预演脚本、测试。本仓库对其的修改与增补见 `README.md`「与上游的关系」。

## Microduck 机器人（几何与运行时契约）

- 项目：**pollen-robotics/microduck**
- 地址：https://github.com/pollen-robotics/microduck
- 许可：代码 Apache-2.0；**3D 模型文件为 CC BY-SA-NC（署名-相同方式共享-非商业）**

> ⚠️ `src/mjlab_microduck/robot/microduck/assets/*.stl` 等网格文件属于 CC BY-SA-NC。
> 若本仓库要公开或用于商业用途，**必须排除这些文件**或替换为自制模型。

## HD-1910 执行器参数

- 项目：**LuwuDynamics/xgoduck_rl**
- 地址：https://github.com/LuwuDynamics/xgoduck_rl
- 参数文件：`src/mjlab_microduck/robot/xgoduck/params/1910_m6.json`
- 许可：Apache License 2.0
- 使用范围：`robot/microduck/hd1910/1910_m6.json`（原样保留）与 `1910_m6_full.json`

电气参数（kt、R、堵转力矩）另按 **飞特 HD-1910-C001 串型规格书 A/0** 重新推导，
见 `1910_datasheet.json` 的 `_provenance` 字段与 `PROVENANCE.md`。

## BAM（执行器辨识框架）

- 项目：**Rhoban/bam**
- 地址：https://github.com/Rhoban/bam
- 许可：Apache License 2.0

## mjlab

- 项目：**mujocolab/mjlab**
- 地址：https://github.com/mujocolab/mjlab
- 许可：Apache License 2.0

## 本项目新增内容

`robot/microduck/hd1910/`（质量表、MJCF 生成器、参数溯源）、`robot_*_hd1910.xml`、
`microduck_velocity_hd1910_env_cfg.py`、`FeetechSts3215BamActuator`、`tests/test_hd1910_cfg.py`、
`scripts/hd1910_validate.py`、`docs/hd1910/` —— 原样以 Apache-2.0 发布。
