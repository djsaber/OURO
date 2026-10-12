# 本地性能测量

基准使用临时目录、合成会话和 Provider，不访问网络模型或个人 OURO 数据。使用锁定依赖，独立运行，
不要与 pytest 或其他基准争用资源。输出为 JSON Lines，包含环境、原始样本和统计量；需要保存时写入本地忽略目录。
基准结果只适用于测量环境，不能替代真实终端和网络 Provider 验证，也不作为 CI 的固定时间断言。

## 导入与首屏就绪

```powershell
uv run --frozen python -m dev.benchmarks.startup --repeat 5
```

每次使用新 Python 进程与临时 home，显式导入 CLI 和完整 TUI App，在 120×40 headless 窗口等待启动 worker
完成并确认 Composer 可用。输出导入、从导入开始到首屏就绪、进程与关闭总耗时；`--root` 可指定待测 checkout。
这是进程冷启动，不清空操作系统文件缓存，不包括真实终端 paint 或首次 MCP 连接。

## 会话管线与 TUI

```powershell
uv run --frozen python -m dev.benchmarks.text_wrapping --repeat 15
```

断行基准在 80 列 headless 窗口使用 1,024、4,096、16,384 字符的 ASCII 与中英混排文本，测量 Rich
折行、编辑器连续插入和每批 32 字符的流式追加。断行偏移的临时分配峰值独立测量；输出原始样本、
中位数与 P95。编辑和追加计时包含同步文档/strip 更新，不包含后续布局、终端 paint 或 IME 处理。

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
uv run --frozen python -m dev.benchmarks.run_resources --label current-environment
uv run --frozen python -m dev.benchmarks.session_harness_reads
```

资源基准覆盖正常终结、等待/恢复、显式租约转交和关闭；每场景使用 100/1,000 个 Run。计时与 tracemalloc 分开，
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
uv run --frozen python -m dev.benchmarks.plugin_scale --counts 0 10 100 --repeat 5
```

规模基准分别安装带 Skill 的声明式插件、带命令的进程内插件和真实 stdio worker。每个样本使用新进程，
测量插件 Host 启动、首次使用、20 次稳态使用、列表、单插件关闭／启用和 Host 关闭。安装及启用意图准备
不计时，Python 导入不计入 Host 启动；进程冷启动的完整导入成本由 `startup` 单独测量。声明式场景读取
Skill 目录，可执行场景调用一个插件的 echo 命令，其余插件保持空闲。输出明确记录插件数、实际 worker 数、
Host 句柄、进程 I/O 和原始样本；100 个 worker 的结果只说明此场景，不代表 100 个模型请求并发执行。
worker 夹具逐条处理标准输入上的 JSON-RPC，不导入业务 SDK，也不调用反向端口，便于单独观察 Host 与 stdio 成本。
重复样本复用临时 home，不清除 OS 缓存；I/O 含缓存命中和管道传输，不等于物理磁盘流量。

```powershell
uv run --frozen python -m dev.benchmarks.plugin_lifecycle --cycles 100
```

插件生命周期基准在临时 home 安装进程内命令与真实 stdio worker 示例，预热后各自反复启用和关闭。
计时与 tracemalloc 分开执行，输出原始耗时、P50/P95、进程句柄或文件描述符数，以及 GC 后仍保留的
generation、worker client、私有模块数量。句柄观测支持 Windows 和 Linux，其他平台输出 null；
只统计 Host 进程自身，不代表全系统 IO、RSS 或跨平台性能结论。

```powershell
uv run --frozen python -m dev.benchmarks.plugin_startup_graph --counts 3 10 30 --repeat 3
```

启动图基准复用离线回归的内置插件夹具，覆盖链、共享依赖和独立插件。对照同一当前运行时下旧的逐根
`activate` 启动循环与统一启动图，记录代次构建、后台服务启动、状态读取及原始耗时。每次新建临时 home，
不计导入、夹具构造和关闭，不清除 OS 缓存；不启动 worker，不代表网络或整个旧版本的性能。

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
首个模型 delta 不等于终端 paint，TUI 帧、按键和网络性能须按[性能规划](../../docs/plans/performance.md)
单独验证。
