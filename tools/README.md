# Tools

## `duck-monitor.sh`

从仓库根目录即可启动的持续诊断记录器，实际代码在：

```text
console/linux-v297/tools/duck_monitor.py
```

用法：

```bash
./tools/duck-monitor.sh
```

先启动中控并连接机器人，再运行它。  
记录输出在：

```text
~/.local/share/MicroduckConsole/monitor/<时间戳>/
```

详细说明见：

```text
console/linux-v297/README-Linux.md
```

参数：

```bash
./tools/duck-monitor.sh --help
```
