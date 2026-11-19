# 实机部署记录（2026-11-18）

## 环境

| 项 | 值 |
|---|---|
| 主机 | Radxa ZERO 3W，`radxa@192.168.0.13` |
| 系统 | Armbian 26.2 / Linux 6.1.115 aarch64，Python 3.13.5 |
| 运行时 | 官方 `robotd 0.15.1` + `robotctl`，策略槽位机制 |
| 舵机 | Feetech HD-1910，15 个，总线 `/dev/ttyS2` |
| 控制频率 | 50 Hz（实测 50.0–50.1，0 missed ticks）|
| 电池 | 7.5 V |

## 部署机制（关键发现）

不是"拷一个 onnx 进去"，官方有一套策略槽位系统：

```bash
robotctl policy load <slot> <file>
```

- 槽位：`walk` / `stand` / `sitstand` / `ground_pick` / `kick_left` / `kick_right` / `roulade`
- **热切换**、**形状校验**（非 `obs[1,61]→actions[1,14]` 直接拒绝）、**失败保留原策略**
- 写入 `/etc/robot/robotd.toml` → **重启后依然生效**（`robotd.service` 已 enabled），
  即"导入完成 = 开机自启完成"

## 两个必须先解决的坑

### 坑一：commissioning 模式拒绝一切运动

`robotd` 带 `--commissioning` 时是**只读校准模式**，`policy load` 会报：

```
error: robotd is read-only: motor commands are disabled
```

标定完成后必须切模式：

```bash
sudo python3 /opt/robot/feetech-ft5-r5/runtime.py configure-startup motion
```

### 坑二：ONNX 必须放持久路径

`/tmp` 重启即清空，而 `policy load` 记的是路径 → 策略会加载失败。
本仓库存放到 `/opt/robot/policies/local/`（root:root 644）。

## 兼容性验证（部署前做的）

用官方 `velstand.onnx` 与我们的 `hd1910_walk_all.onnx` 逐项对比：

| 项 | 官方 | 我们 |
|---|---|---|
| 输入 / 输出 | `obs[1,61]` → `actions[1,14]` | 一致 |
| 节点数 | 9 | 9 |
| 归一化 | `Sub`+`Div` **在模型内** | 一致 |
| metadata | joint_names / default_joint_pos / action_scale 等 8 项 | 一致 |

**归一化烘焙在 ONNX 里，运行时喂原始观测** —— 我们导出时的处理正好正确。

## 已部署的槽位（5/7 是我们的）

| 槽位 | 文件 | 来源 |
|---|---|---|
| `walk` | `hd1910_walk_all.onnx` | 我们的（走/转/摔/爬起）|
| `sitstand` | `hd1910_sitstand.onnx` | 我们的 |
| `ground_pick` | `hd1910_groundpick.onnx` | 我们的 |
| `kick_right` | `hd1910_ballkick.onnx` | 我们的（右脚踢）|
| `roulade` | `hd1910_roulade.onnx` | 我们的 |
| `stand` | — | 关闭（walk_all 零指令即站住）|
| `kick_left` | 原厂 | 未替换（左脚踢需对应训练）|

## 回滚

```bash
sudo robotctl policy reset <slot>     # 单个槽位
sudo robotctl policy reset            # 全部
```

## 尚未验证（必须记住）

1. **实机行为完全没有验收** —— 只确认了 `robotctl health` healthy、无总线错误。
2. 零位是"物理零位"，仿真 HOME 是标称姿态，两者差异未量化。
3. 质量表 0/21 实称，惯量按质量比缩放 —— 策略是在这个近似模型上训的。
4. `kick_left` 仍是原厂策略，左右脚不一致。

## 更新（2026-11-19）：固件要求的是"模型目录"，不是单个 onnx

上一版部署只把裸 `.onnx` 放进 `/opt/robot/policies/local/`，固件启动第 3/4 步报：

```
/opt/robot/policies/local/deployment-contract.json: No such file or directory
```

固件读的是 **onnx 所在目录**的配套文件，缺 `deployment-contract.json` 直接失败。

### 契约（来自 `model_packages.validate_export`，逐条实测）

| 项 | 要求 |
|---|---|
| `schema` | `microduck-deployment-contract/v1` |
| `task_adapter` | `hd1910-official0151`（或 v1/v2/v8 限定场景）|
| `task` | 必须在白名单里；**walk 槽位只认 `Mjlab-VelStand-Flat-MicroDuck`** |
| 维度 | 61 / 14 |
| `obs_normalizer` | `baked_in_onnx` |
| 哈希 | `sha256(policy.onnx) == manifest.onnx_sha256`，且 `manifest.task == contract.task` |
| ONNX 元数据 | 必须与 `contract.onnx_metadata` **完全相等**（8 个键逐字节）|
| 关节顺序 | 固定 14 个：left 5 + 头 4 + right 5 |
| 动作 | `use_default_offset=true`、`offset=0`、`clip=null`、`0<scale≤2` |
| 观测布局 | 正好 8 项，`joint_pos`/`joint_vel` 用 `_rel`（齿隙版用 `_rel_backlash`），不得带 clip/scale/history |
| BAM | `parameter_file.sha256` 必须等于 `ef2d51ad…`，`kp_fw` 为 1..32 整数 |
| HOME | 14 个 `home_rad` 与 metadata `default_joint_pos` 逐项差 ≤ 0.000501 rad |
| ONNX 算子 | 只允许 Sub/Div/Gemm/Elu/Tanh/Relu/Add/Mul/Clip/Identity/Constant/MatMul/LeakyRelu/Sigmoid/Softplus |

### 解决办法

`scripts/pack_deployment.py`：拿随包模型目录当模板（actuator/BAM/joint 表/观测布局直接沿用），
只重写属于我们自己训练的部分——task 名、ONNX 元数据、训练 HOME、哈希。

```bash
uv run --no-sync scripts/pack_deployment.py \
    --onnx policies/hd1910_walk_all.onnx --template /tmp/tpl/m --out /tmp/pkg_walk
```

校验（板上）：

```python
import model_packages as mp; mp.verify_directory(Path("/tmp/pkg_walk"))
→ OK id=9f4774d02832 slot=walk task=Mjlab-VelStand-Flat-MicroDuck
```

安装：目录必须**含 `policy.onnx`**，然后把 `[policy] walk` 指到 `<目录>/policy.onnx`。

重启后 `robot.busStatus.loaded_models.walk` 出现 `task=Mjlab-VelStand-Flat-MicroDuck sha=9f4774d028`，
即固件已接受该模型。

### 仍未完成

`sitstand` / `ground_pick` / `kick_right` / `roulade` 仍是裸 onnx，`loaded_models` 里为 `null`，
需要各自生成模型目录（模板与 task 名不同，roulade/kick 还有额外的 manifest 字段要求）。
