# 本地性能测量

基准默认使用临时目录、合成会话和 Provider，不访问网络模型或个人 OURO 数据；数据升级基准可显式指定已有 home 的只读输入副本。
使用锁定依赖，独立运行，不要与 pytest 或其他基准争用资源。输出为 JSON Lines，包含环境、原始样本和统计量；需要保存时写入本
地忽略目录。基准结果只适用于测量环境，不能替代真实终端和网络 Provider 验证，也不作为 CI 的固定时间断言。

## 历史数据升级

```powershell
uv run --frozen python -m dev.benchmarks.data_upgrade --operations 1000 --repeat 3
uv run --frozen python -m dev.benchmarks.data_upgrade --backend sqlite --operations 1000 --repeat 3
uv run --frozen python -m dev.benchmarks.data_upgrade --check --operations 1000 --repeat 3
```

使用固定历史夹具，增加指定数量的 Session 提交记录，保留夹具中的消息、任务和用量数据。
每个样本重新准备临时 home，输出总时间、阶段时间、变更文件数，以及 Python `os.fsync` 的调用次数和耗时；
同步计数不包括 SQLite 内部调用或物理磁盘写入。计时包含升级工作区初始化及收尾，不包含输入构造、复制和 Python 导入，
不清除 OS 缓存。`--profile PATH --repeat 1` 可保存 cProfile 数据，采样开销计入该次测量。

排查实际数据时可使用 `--source-home 'C:\data\ouro-home' --repeat 1`；请替换路径并停止该 home 的写入者。
该选项将输入复制到权限受限的临时目录后再测试，排除 `.upgrade`，不调用原 home 的升级或数据库连接。
Session 和 Task 使用同一个 `--backend` 选择，必须与样本对应；输出只包含统计，不输出记录内容。
临时副本在结束时清理，需要足够的输入副本、转换产物和 SQLite 临时文件空间。
数据域阶段包含产物写入与同步；`prepare` 是发布清单准备，`verify_source` 是修改集合复核与原文件同步，
`publish` 包含原文件移入备份和新文件安装，`finalize` 是报告与清理。`--check` 不进入这些发布阶段。

## 导入与首屏就绪

```powershell
uv run --frozen python -m dev.benchmarks.startup --repeat 5
```

每次使用新 Python 进程与临时 home，显式导入 CLI 和完整 TUI App，在 120×40 headless 窗口等待启动 worker
完成并确认 Composer 可用。输出导入、从导入开始到首屏就绪、进程与关闭总耗时；`--root` 可指定待测 checkout。
这是进程冷启动，不清空操作系统文件缓存，不包括真实终端 paint 或首次 MCP 连接。

## 会话管线与 TUI

```powershell
uv run --frozen python -m dev.benchmarks.file_menu C:\Windows\System32 --keys 60
```

文件菜单基准只读枚举指定目录，在 100×32 的 Textual headless 窗口加载完整候选集；分别输出目录读取、候选项构造、
打开菜单到完成绘制，以及连续移动到完成绘制的中位数和 P95。计时包含 Textual 布局和刷新调度，
不包含操作系统终端输出或键盘输入链路；不清除文件系统缓存。目录不会被修改。

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
`--mode runtime|tui|activity|all` 选择场景，`--backend files|sqlite|all` 选择后端。`--deltas-per-round` 控制流长度，
`--delta-interval-ms` 控制分片间隔；设为 0 可测量连续突发。
TUI 使用 Textual `run_test(size=(120, 40))`，记录 1 ms ticker 的 loop lag、按键到 Composer 更新、动画 tick 间隔、
GC、慢回调和 Store 线程归属。超过 16ms 的 GC 附带代次、回收对象数与 gen2 触发栈。
Store 计数包含嵌套公共调用，不等于物理 IO；注入延迟不代表特定硬件。
探针在退出时恢复，不修改生产 GC、动画频率、流式窗口和背压。

## 会话观察

```powershell
uv run --frozen python dev/benchmarks/session_observation.py --messages 120 1000 10000 --repeat 3
uv run --frozen python dev/benchmarks/session_observation.py --root C:\checkouts\baseline --repeat 3
```

观察基准用临时 home、合成历史和可保持运行的 Provider 测量冷进入、重新进入和流式会话切换。
合成模型使用 2,000,000 token 上下文容量，避免历史规模触发自动压缩；探针断言请求 purpose 为 chat，
并在重进后检查未提交正文，确保测量正文流阶段。
`--root` 指向待测源码，使用同一探针和当前锁定依赖；每个样本使用独立事件循环，线程收尾后才删除临时数据。
输出内容提交、首个正文帧与已加载窗口就绪延迟，以及排版项数、排版耗时、Store 公共查询次数、正文快照/窗口读取消息数、
归约事件数、跟随器、订阅、任务和缓存数量。5 ms ticker 记录选择期间的 loop lag；首帧探针被动等待新正文出现，
随后等待一次原生刷新，不在准备期间持续请求刷新。阶段耗时可相互包含，查询计数包含嵌套调用；排版计数包含主题和
尺寸变化的重复工作。消息计数不包含执行恢复或完整 context 内嵌消息；总读取成本需结合进程 IO 判断。
窗口就绪要求其历史行已生成且主题刷新结束，不要求未请求的旧页就绪。
Windows 输出当前进程 working set 和本次选择的读取传输字节，包含缓存 IO，不等于物理磁盘流量；其他平台不提供这两项。
这些指标均不包含实际终端 paint 或操作系统键盘链路。不同版本必须使用同一探针比较，不混用测量口径。

```powershell
uv run --frozen python -m dev.benchmarks.session_collaboration --cycles 20
```

协作基准在同一进程的两个 headless TUI 中观察三个并发 Run：固定一个观察者，另一个循环切换三会话。
记录每次切换、任务/订阅/跟随器/缓存数量、进程工作集，以及 Agent 树查询次数与耗时。持续注入按键并记录 loop lag、
Composer 更新、GC 和读取/准备/安装/恢复各阶段耗时。单独记录双方均不可见的
两个 Run 的正文排版计数，退出后检查观察资源释放。持续新增正文会增加存储和内容占用，working set 增长本身不等于泄漏。
`read_transfer_bytes` 在此为进程累计传输量；两个 TUI 共用 CPU 和事件循环，不代表两个终端进程的延迟。

### 人工终端验收入口

在项目目录的两个实际终端中分别运行同一命令。只操作合成数据，不需要模型凭据；目录会保留供另一终端读取：

```powershell
uv run --frozen python -m dev.benchmarks.session_terminal --home .ouro-dev/manual-files --backend files
```

SQLite 使用另一个目录，并在两端都指定 `--home .ouro-dev/manual-sqlite --backend sqlite`。
需要长历史时仅在第一端追加 `--seed-history 10000`，然后从两端会话菜单打开 `Synthetic 10000 messages`。
种子快照写入后会刷新会话目录；另一端重新打开 Ctrl+O 菜单即可读取。每次使用 `--seed-history` 都创建一个新会话，
后续重启普通测试时省略该参数即可复用已有数据。初次打开显示最近消息，向上滚动时才加载更早历史。
每次输入会生成两轮文本和一个合成工具结果；默认每轮 400 片、间隔 25ms。可用 `--deltas` 和 `--interval-ms`
调整持续时间或突发，不会调用外部模型或系统命令。

将两个终端分别记为 A（发起运行）和 B（只观察）。首轮按下表逐项定位问题；全部通过后，按
[性能规划](../../docs/plans/performance.md)在两后端及 Windows/POSIX 实际终端分别重复各场景至少 20 次。
合成输出的每一轮从 1 开始计数，工具结果后开始下一轮；不要将合法的第二轮计数误记为重复消息。
开始下一项提交测试前，先清空上一项留下的未发送草稿。

| 场景 | 操作 | 通过条件 |
|---|---|---|
| 主页提交 | A 按 Ctrl+N，输入 `first` 并 Enter，随后立即输入 `draft`，暂不发送 | 仅切换到会话页一次；主页、命令菜单和队列不闪现；输入框无高度往返或焦点丢失，`draft` 保留 |
| 空闲会话提交 | 等待输出、工具及第二轮全部结束，再提交 `second` | 消息进入正文前不先展开队列；输入栏稳定，发送后仍可继续编辑 |
| 活动 Run 追加 | 输出过程中输入 `follow-up` 并 Enter | 队列出现在输入栏下方；输入被消费后队列消失，正式用户消息只出现一次；等待该追加运行结束再测空闲提交 |
| 多会话切换 | A 用 Ctrl+N 依次提交 `session-1`、`session-2`、`session-3`，用 Ctrl+O 选择会话，Enter 打开；在各会话保留不同的未发送草稿 | 草稿不串会话；返回后补齐不可见期间的输出，已完成会话有终态，无旧会话内容闪入 |
| 双终端观察 | B 用 Ctrl+O 打开 A 正在运行的同一会话；A 持续键入草稿，B 向上滚动并调整窗口宽度 | 两端输出均继续；草稿保持可编辑；B 阅读旧内容时不被拉回底部，滚动或缩放不会令 A 停止 |
| 分页与导航 | 打开种子长历史，向上滚动至已加载顶部；将焦点放到正文后按 PgUp/PgDn；输入 `/navigate #1`、`/navigate bottom`，再用 Alt+PageUp/Alt+PageDown 定位用户消息 | 无“加载更多”按钮；补页保留阅读位置；翻页直接定位，无滚动动画；编号、顺序正确，切换并重进后无重复内容或摘要 |
| 生命周期隔离 | A 保持运行，B 按 Ctrl+Q 退出；A 切换到其中一个运行中会话并输入 `/abort` | B 退出不停止 A；只中止选中的 Run，其余后台 Run 继续并可完成 |

Ctrl+O 菜单可输入文字筛选，↑/↓ 选择、Enter 打开、Esc 返回。需要区分测试会话时，用 Ctrl+P 打开 Actions，
选择 `Rename session`，为会话设置不同名称。默认运行较短，多会话操作来不及时可重新启动入口并追加
`--deltas 1200`，每次运行的两轮输出合计约 60 秒，实际时间还包括工具、存储与排版。

长历史首端启动示例（仅在 A 使用 `--seed-history`，B 仍使用同一 home 的普通启动命令）：

```powershell
uv run --frozen python -m dev.benchmarks.session_terminal --home .ouro-dev/manual-files --backend files --seed-history 10000
```

若一项失败，记录场景、操作顺序、期望与实际现象后即可反馈，不必先完成全部重复。仅“未见异常”不能代替
毫秒级响应性指标；人工外观验收和有时间数据的性能验收分别记录。

每份记录包含源码版本、操作系统、终端及版本、Python、后端、字符尺寸、数据规模、重复次数、结果和截图/录屏位置。
记录首个正文帧、键入回显、动画停顿、滚动跳变及异常；headless 指标不能填入实际 paint 一栏。
此入口仅提供可重现的人工环境，运行脚本或通过进程测试不表示已完成人工验收。

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
