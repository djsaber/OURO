# 本地性能测量

基准使用临时目录、合成会话和 Provider，不访问网络模型或个人 OURO 数据。使用锁定依赖，独立运行，
不要与 pytest 或其他基准争用资源。输出为 JSON Lines，包含环境、原始样本和统计量；需要保存时写入本地忽略目录。
基准结果只适用于测量环境，不能替代真实终端和网络 Provider 验证，也不作为 CI 的固定时间断言。

## 会话管线与 TUI

```powershell
uv run --frozen python -m dev.benchmarks.session_pipeline --repeat 3
uv run --frozen python -m dev.benchmarks.session_pipeline --mode tui --store-delay-ms 5 --repeat 3
```

每个 Session 包含 120 条历史消息；两轮模型响应各含 40 个间隔 5 ms 的文本 delta，并执行一个约 32 KiB 的工具。
`--mode runtime|tui|activity|all` 选择场景，`--backend files|sqlite|all` 选择后端。`--deltas-per-round` 控制流长度。
TUI 使用 Textual `run_test(size=(120, 40))`，记录 1 ms ticker 的 loop lag、按键到 Composer 更新、动画 tick 间隔、
GC、慢回调和 Store 线程归属。Store 计数包含嵌套公共调用，不等于物理 IO；注入延迟不代表特定硬件。
探针在退出时恢复，不修改生产 GC、动画频率、流式窗口和背压。

## Run 资源与状态读取

```powershell
uv run --frozen python -m dev.benchmarks.run_resources
uv run --frozen python -m dev.benchmarks.session_harness_reads
```

资源基准覆盖正常终结、等待/恢复、排队租约转移和关闭；每场景使用 100/1,000 个 Run。计时与 tracemalloc 分开，
通过真实租约和弱引用检查资源收尾。读取基准使用 0/8/64 个控制关系，预热后测量 Session、Harness 与控制投影。
这些场景测量局部生命周期与读取成本，不覆盖插件启动、磁盘吞吐或终端绘制。

## Harness 状态与撤销

```powershell
uv run --frozen python -m dev.benchmarks.harness_lifecycle --repeat 3
```

覆盖两后端、100/10,000 条保留消息、固定短撤回后缀以及不同 owner 状态规模；记录状态版本、原生同步次数、
进程 IO、耗时和内存。夹具创建与首次索引建立不计时。Windows 通过 SQLite VFS 观察 `FlushFileBuffers`，Linux
通过 strace 观察同步调用；观测不可用时明确失败。调用原函数且保留返回值，不改变 fsync/WAL 策略。
同步探针校准见 `tests/benchmarks/test_sqlite_sync.py`；计数不等于绕过操作系统缓存的物理介质写入。

## Worker 响应与插件命令

```powershell
uv run --frozen python -m dev.benchmarks.worker_results
uv run --frozen python dev/benchmarks/plugin_commands.py --repeat 3
uv run --frozen python dev/benchmarks/plugin_commands.py --mode tui --store-delay-ms 5 --repeat 3
```

Worker 基准通过真实 handler 测量合成响应解码、构造次数及独立内存峰值；transport 不启动插件进程，
因此不代表 IPC 或插件执行成本。

命令基准临时安装真实 SDK 插件，覆盖查询、预建会话、输出后创建会话及已有 120 条历史的会话。
`--mode application|tui|all` 分别测 Application 命令用例与 headless TUI；输入批次由合成 Provider 和工具完成。
输出包含提交、首段文本、完整消费、消息摘要、Run 终态和响应性指标；安装、启动与初始历史准备不计时。
首个模型 delta 不等于终端 paint，TUI 帧、按键和网络性能须按[性能规划](../plans/OURO_性能优化规划_2026-09-02.md)
单独验证。
