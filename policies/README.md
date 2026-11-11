# 策略产物

| 文件 | 来源 | 说明 |
|---|---|---|
| `hd1910_stand_v1.onnx` | run 1 `2026-11-11_10-11-14_hd1910_v1` @ model_4000 | **站立策略**：能稳定站住（episode 990/1000），但**不走**（速度误差 0.40 m/s）。留作基线对照，不是可部署的行走策略。 |

所有 ONNX 均由 `scripts/export.py` 导出（观测归一化已烘焙进图），输入 `[1,61]`、输出 `[1,14]`。

校验：

```bash
python -c "import onnxruntime as ort; s=ort.InferenceSession('policies/hd1910_stand_v1.onnx'); print(s.get_inputs()[0].shape, s.get_outputs()[0].shape)"
```
