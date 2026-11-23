# 观测/动作契约：给回放分析者的确定答案

> 针对 15:22:51 黑匣子回放暴露的"第一帧正常、随后闭环发散"问题。
> 本文所有结论都来自代码，可复核；路径指本仓库或板上的固件 Python。

## 1. `previous_action` 到底存什么

**答案：raw policy output，不乘 scale、不加 HOME、不做滤波。**

证据链（mjlab 1.3.0）：

```python
# mjlab/envs/mdp/actions/actions.py:135
@property
def raw_action(self) -> torch.Tensor:
    """Raw actions (before scale/offset)."""
    return self._raw_actions

# mjlab/envs/mdp/observations.py:80
def last_action(env, action_name=None):
    if action_name is None:
        return env.action_manager.action      # 原始动作向量
    return env.action_manager.get_term(action_name).raw_action
```

而 `process_action` 才做 `scale * raw + offset`（offset = `default_joint_pos`）。

**所以运行时的 `previous_action` 必须是网络的 14 维裸输出。**
若喂的是"缩放后"或"缩放后 + HOME"的值，策略会低估自己已发出的动作 →
下一步补偿更大 → 形成几何级发散。这与黑匣子看到的逐帧爬升（10% / 步量级）一致。

## 2. `joint_vel` 的单位与滤波

**答案：rad/s，相对关节速度，不滤波、不缩放。**

- 观测项 `joint_vel` → `joint_vel_rel`，obs 项 `scale=None`（无缩放）
- 无 history、无 clip
- 不是舵机 steps/s；若喂 steps/s，实测输出会直接爆到 2000° 量级（见 §4）

## 3. 关于 `imu_misaligned` 的误解（请勿追这条线）

训练用的观测函数名是 `base_ang_vel_imu_misaligned` / `projected_gravity_imu_misaligned`，
但它**不是固定的安装修正**，而是域随机化：

```python
q = _imu_misalignment_quat(env, math.radians(max_angle_deg))   # max_angle_deg = 1.0
return quat_apply(q, asset.data.projected_gravity_b)
```

即每个环境一个 **≤1° 的随机旋转**。运行时喂**未旋转**的原始值，
等价于随机化取到 0°，**仍在训练分布内**。不需要额外修正。

## 4. 敏感度探针（判断"观测是否被喂错"的定量标尺）

对 `hd1910_walk_all.onnx` 注入不同输入，看输出的绝对值上限：

| 输入 | 输出幅度 |
|---|---:|
| 理想站立、零指令（正确） | **9.8°** |
| 前进 0.3 m/s | 41° |
| 躺倒（重力在机体 +X） | 53° |
| 躺倒（重力 +Z，仰面） | 44° |
| 观测全零 | 19° |
| **双重归一化** | **495°** |
| **头部指令用度数（20 而非 0.35）** | **326°** |
| **身体指令用毫米（30 而非 0.03）** | **1430°** |
| **关节速度用舵机单位（±1000）** | **2158°** |
| **指令用百分比（±100）** | **2162°** |

用途：若某帧的观测重建后喂进去输出 >100°，说明该帧观测本身已离训练分布很远，
不必再怀疑策略。

注：`body_command` 的归一化 std 仅 0.013、`head_command` 第 4 维 std 仅 0.013，
这两路是最脆弱通道——单位从 m 变 mm、从 rad 变 deg 就会爆。

## 5. 黄金向量（回放基准）

文件：`docs/hd1910/artifacts/onnx_golden_vectors.json`
（每个用例含完整 61 维 obs、14 维 raw action、加 HOME 后的 target，全精度）

| 用例 | 61 维 obs 特征 | raw action 最大值 | target 最大值 |
|---|---|---:|---:|
| `stand_zero_cmd` | 重力 (0,0,−1)，其余全 0 | **0.1712 rad** | 0.472 rad |
| `stand_forward_0p3` | 同上 + command (0.3,0,0) | 0.7179 rad | 0.495 rad |
| `lie_face_up` | 重力 (0,0,+1) | 0.7635 rad | 0.948 rad |
| `tilt_45_pitch` | 重力 (0.7071,0,−0.7071) | 1.7323 rad | 1.383 rad |

`stand_zero_cmd` 的 14 维输出：

```
[-0.0039, -0.0633, 0.0049, -0.0039, -0.1064, -0.0890, -0.0066, 0.0380,
 -0.0816,  0.0071,  0.0667,  0.0142,  0.0569,  0.1712]
```

**只要喂进这组 61 维，任何实现都必须得到上面这组 14 维。** 这能一次性验证
归一化次数、维度布局、以及 14 个通道的对应关系。

## 6. 下一步该怎样做实验（一次只改一个变量）

黑匣子里 `实际 ≈ 预测 × 0.75` 的比例在各帧都成立（14.4→10.4、164→109、216→149），
说明**运行时的输出缩放就是配置的 0.75，这部分是对的**，发散发生在闭环内部。

两个都能解释"逐帧放大"的机制，需要区分：

| 假设 | 机制 | 判别方法 |
|---|---|---|
| **A. 动作历史被缩放** | 策略低估自身动作 → 过度补偿 | 离线回放：把 `previous_action` 分别按 raw / ×0.75 送入，看哪条曲线复现实测 |
| **B. 被控对象欠交付** | 指令了但舵机没到位（摩擦/欠压/延迟）→ 策略不断加码 | 对比 target 与 achieved 的滞后与幅值比；或临时降 `action_scale` |

**建议顺序**：先只修历史（保持 `action_scale=0.75`），验证是否收敛；
若仍发散，再单独把 `action_scale` 提到 1.0。**两项一起改会失去归因能力。**

⚠️ 注意：把 `action_scale` 从 0.75 提到 1.0 会**提高环路增益 33%**。
若发散有一部分来自增益/延迟，这一步会让它更糟，而不是更好。
