# Linux 中控分支（console/linux-v297）

这个分支在原有 `MicroDuck_1910_RL` 训练工程之外，新增 **Microduck 中控的 Linux 运行版**，不修改 master 上的训练代码、模型或配置。

## 内容

```text
console/linux-v297/        Linux 中控完整运行目录
├── console.py             中控主程序
├── start.sh               Linux 启动脚本
├── install.sh             桌面图标安装
├── ssh-askpass            Linux SSH 密码回调
├── static/                前端页面与资源
├── docs/                  版本说明
└── README-Linux.md        Linux 使用说明
```

## 来源

基于 kissqy 的 **Microduck Console Studio V297 BATE** 发行包，保留原包许可文件与第三方声明。

## 本分支额外修复

1. `service_channel.py` 的 Python 3.12 专属 f-string 改为 Python 3.11 兼容写法；
2. `start.sh` 自动选择 Python 3.11/3.12，并检查 `tomllib`、`ssh`；
3. 自动给 `ssh-askpass` 增加执行权限；
4. 增加 Linux 桌面安装脚本和 `README-Linux.md`；
5. 增加 `tools/duck_monitor.py` 持续诊断记录器（关节误差、电流、温度、掉帧、通道断开）。

## 运行

```bash
cd console/linux-v297
./start.sh
```

浏览器打开 `http://127.0.0.1:8090`，填写机器人的 SSH 地址和密码。

安装桌面图标：

```bash
./install.sh
```

## 说明

- 本分支只增加中控，不改变 `master` 的训练/仿真内容；
- 中控只监听 `127.0.0.1`；
- 原项目原创部分仍为个人非商业许可，商业使用需作者书面授权；
- 不包含或重新授权任何第三方模型权重。
