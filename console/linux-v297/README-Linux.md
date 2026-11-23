# Microduck Console Studio —— Linux 运行版

这是 **V297 BATE** 中控的 Linux 启动封装。  
原始中控是“本地 Python HTTP 服务 + 浏览器 UI”，本目录只增加 Linux 启动脚本并修复两个兼容问题，不改动机器人固件。

## 1. 前置条件

- Linux x86_64 / aarch64
- **Python 3.11 或 3.12**
- OpenSSH 客户端（`ssh`）
- 浏览器（Chrome / Firefox / Edge）

Ubuntu / Debian：

```bash
sudo apt update
sudo apt install python3.11 python3.11-venv openssh-client
```

如果系统有 Python 3.12，优先用 3.12。

## 2. 启动

```bash
cd Microduck-Console-Linux-V297-BETA
./start.sh
```

浏览器会打开：

```text
http://127.0.0.1:8090
```

如果 8090 被占用：

```bash
MICRODUCK_PORT=8091 ./start.sh
```

不自动打开浏览器：

```bash
MICRODUCK_NO_OPEN=1 ./start.sh
```

## 3. 连接机器人

在页面填写：

```text
SSH 地址：radxa@<机器人IP>
SSH / sudo 密码：配网时设置的系统密码
```

例如：

```text
radxa@192.168.0.13
```

> 中控只监听 `127.0.0.1`，不要把它暴露到局域网。

## 4. 安装桌面图标

```bash
./install.sh
```

然后从应用菜单打开 **Microduck Console / Microduck 中控**。

## 5. 本版修复的 Linux 问题

### 5.1 Python 3.11 语法兼容

原始 `service_channel.py` 第 54 行使用了只有 Python 3.12 才支持的嵌套 f-string：

```python
f'... {self.target or '本机'} ...'
```

已改为：

```python
target_label = self.target or '本机'
f'... {target_label} ...'
```

因此 Python 3.11 也能运行。

### 5.2 `ssh-askpass` 执行权限

ZIP 解压后 `ssh-askpass` 是 `644`，Linux 密码登录会失败。  
`start.sh` 和 `install.sh` 会自动 `chmod +x`；你也可以手动：

```bash
chmod +x ssh-askpass
```

## 6. 常见问题

### 提示 `No module named tomllib`

Python 版本低于 3.11。安装 3.11/3.12：

```bash
sudo apt install python3.11
```

### 提示 `SyntaxError: f-string: expecting '}'`

你运行的是未打补丁的原包，或系统用了 Python 3.10。  
用本目录的 `start.sh`，它会选择 Python 3.11+；或安装 3.12。

### 提示 `ssh: command not found`

```bash
sudo apt install openssh-client
```

### 页面能开，但连不上机器人

```bash
ping <机器人IP>
ssh radxa@<机器人IP>
```

确认电脑和机器人同一网络，SSH 22 端口可达。

### SSH host key 变化

如果重刷过系统：

```bash
ssh-keygen -R <机器人IP>
```

然后重新连接。

## 7. 目录说明

```text
console.py             中控主程序
service_rpc.py         SSH 远端中继
service_channel.py     持久 SSH 通道（已修复 3.11 语法）
ssh-askpass            Linux 密码回调
start.sh               Linux 启动脚本
start.command          macOS/Linux 兼容入口
install.sh             桌面图标安装
docs/                  版本说明
licenses/              第三方许可
```

## 8. 许可

本封装保留原包的全部许可文件：

- `LICENSE-SCOPE.txt`
- `Personal-NonCommercial.txt`
- `THIRD_PARTY_NOTICES.md`
- `licenses/`

原项目原创部分为个人学习、研究、非商业使用；商业使用需作者书面授权。  
本封装不改变原许可，也不授予额外商业权利。

## 9. 停住 / 卡死诊断

中控本身有实时曲线、日志和约 120 秒录制，但“走几步突然停住”最好持续落盘。  
`tools/duck-monitor.sh` 会轮询中控的 `/api/recording`，把每一帧写到 JSONL，并自动标记异常。

启动中控并连接机器人后，另开一个终端：

```bash
cd Microduck-Console-Linux-V297-BETA
./tools/duck-monitor.sh
```

或：

```bash
python3 tools/duck_monitor.py --interval 0.5
```

输出目录：

```text
~/.local/share/MicroduckConsole/monitor/<时间戳>/
├── telemetry.jsonl   每一帧关节、IMU、总线、健康数据
├── events.jsonl      异常事件
└── summary.json      本次监控汇总
```

事件含义：

| 事件 | 可能原因 |
|---|---|
| `channel_not_live` | robotd / 总线 / IMU / SSH 数据断了 |
| `no_new_frames` | 中控收不到新帧，控制循环可能停了 |
| `joint_error` | 某个关节跟随误差 > 15°，机械卡住/舵机没力 |
| `high_current` | 电流 > 1500 mA，堵转/卡死/负载过大 |
| `high_temperature` | 舵机 > 70°C，热保护风险 |
| `low_battery` | 电池 < 6.8 V，走几步掉压 |

复现“走几步停住”后按 `Ctrl+C`，先看：

```bash
cat ~/.local/share/MicroduckConsole/monitor/<时间戳>/summary.json
cat ~/.local/share/MicroduckConsole/monitor/<时间戳>/events.jsonl
```

同时在中控里点一次 **导出诊断**，并把以下命令的输出保存：

```bash
robotctl logs robotd > /tmp/robotd.log
journalctl -u robotd -b --no-pager > /tmp/robotd-journal.log
```

> 监控工具只读，不会发运动命令。
