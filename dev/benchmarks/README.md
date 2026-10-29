# 本地性能测量

基准默认使用临时目录、合成会话和 Provider，不访问网络模型或个人 OURO 数据；数据升级基准可显式指定已有 home 的只读输入副本。
使用锁定依赖，独立运行，不要与 pytest 或其他基准争用资源。输出为 JSON Lines，包含环境、原始样本和统计量；需要保存时写入本
地忽略目录。基准结果只适用于测量环境，不能替代真实终端和网络 Provider 验证，也不作为 CI 的固定时间断言。

## 会话重构前后对照

```powershell
uv run --frozen --extra themes python -B -X utf8 -m dev.benchmarks.session_refactor_comparison
uv run --frozen --extra themes python -B -X utf8 -m dev.benchmarks.session_refactor_comparison --scenario history --messages 100 1000 10000 --repeat 3
uv run --frozen --extra themes python -B -X utf8 -m dev.benchmarks.session_refactor_comparison --scenario agent_io --rounds 64 --repeat 3
uv run --frozen --extra themes python -B -X utf8 -m dev.benchmarks.session_refactor_comparison --scenario stream_io --messages 10000 --stream-chunks 131072 --repeat 3
uv run --frozen --extra themes python -B -X utf8 -m dev.benchmarks.session_refactor_comparison --scenario interaction --messages 1000 10000 --repeat 3
```

默认基线为重构前 `44724d8378632f3c5e63b6861e5e873493e71dce`，可用 `--baseline` 指定其他兼容基线。
`--backend files|sqlite` 可只测一个后端；省略时测量两者。
脚本将该提交和当前工作区的 `ouro/` 分别快照到临时目录，用同一锁定解释器串行启动独立子进程，
逐次交换先后顺序。当前源码标识、环境和每次原始样本输出到 stdout；源码快照与合成数据在退出时清理。
基线和当前版本使用各自格式构造数据，不调用升级模块，不访问用户 home。

默认包含两种后端、三次重复和以下工作负载：

- `long_run`：8/32/64 轮真实 Runtime、工具及自动摘要；每轮 4 KiB 输出与 4 KiB 工具结果。
  测总耗时、末尾轮次、进程读写字节及次数、RSS 边界样本、RunResult 体积、持久文件及 SQLite 已用页、空闲页和结束时 WAL。
- `history`：100/1,000/10,000 条各 1 KiB 的消息，固定八条活动消息及一条摘要。
  测闲置会话恢复、Python 分配峰值、完整一轮、会话头/末尾 16 条/全量观察读取。
  另测 32 个逐次确认的 256 B 分片写入与独立观察延迟，以及连续 128 个分片的 32 KiB 有效吞吐。
  独立观察使用公开 `observe_session` 接口，不注入本地推送，不含 Host 呈现；连续输出允许框架原有的分片合并，
  写入吞吐计至最后正文被本地事件 sink 收到，独立读取计至完整正文被观察者收到。

`long_run` 和 `agent_io` 支持 `--structured-bytes 65536` 增加每个工具结果的独立结构化正文；
`--harness-owner-bytes 65536` 在计时前保存八个 owner 的私有状态，各 64 KiB，并在执行期间保持其业务值不变。
两项默认均为零。用零、小、大状态分别测量，区分固定成本与重复读取活动私有状态的增长成本。
`--argument-bytes 65536` 另外给每个原始工具调用加入 64 KiB 字符串参数，默认零。
该参数也将合成长循环的 context_window 从 5,000 调整为 `5,000 + argument_bytes`，容纳原工具帧；
不同参数规模的摘要次数可能不同，前后性能必须在相同规模、相同摘要次数下比较，不能用跨规模总耗时推断参数增长成本。
完整观察保留原参数；结果窗口可另行衡量框架只读取展示所需数据的成本。

额外的 `agent_io` 使用真实工具循环留下的 Input、Call、Outcome、摘要、Run 和消息，执行同样的完整/窗口查询。
它另测最后一条工具结果的消息点查，反映增量可视化取得确定结果正文的框架成本；消息身份在计时外确定。
随后分别输出 256 个 256 B 分片（每片之后等待 20 ms）和连续 8,192 个 256 B 分片（共 2 MiB），
测量本地发布及独立适配器观察的 P50/P95/最大延迟、交付次数和有效字节吞吐。
仅最后一片等待独立观察确认，不用逐片确认控制生产速率；观察者按累计字节识别完整分片，支持框架合并交付。
生产间隔是异步调度目标，并非硬实时频率；持续输出吞吐包括框架的读写、合并和轮询等待，
不是无限负载的理论上限，也不是跨进程 IPC 或网络性能。读写双方使用同一进程中的不同存储适配器，观察者不接收本地推送。
超过 32 个样本的分布输出样本数及分位数，避免测量输出随分片数膨胀。

`stream_io` 在同样的一万条历史上单独测连续输出，`--stream-chunks 131072` 对应 32 MiB。
较长输出跨越多个观察分页和轮询周期，减小 2 MiB 短流受轮询相位的影响；不更改框架轮询或分页设置。
同时报告完整 Run 耗时、两端收齐正文后到 Run 与观察处理完成的收尾时间，以及前后逻辑存储与原生空闲页。
观察计数按新增字节递增，不反复求和已收到的前缀；最终仍校验两端完整内容。

`navigation` 单独重复加载、翻页与重进，每个样本的每项操作执行 30 次；`tui` 使用 Textual 测试终端，
测量首次进入、缓存返回、键入和新增输出。调用方式与测量口径见[会话事实观察验收](../../docs/plans/archive/2026-10-30-session-observation-without-event-log.md)。

`interaction` 补充实际 Application/Supervisor 入口的交互成本：

- `SessionService.observation_snapshot(history_limit=200)`、向前翻页和新适配器重新打开；读取在线程中执行，
  记录三次原始耗时、进程 IO 和实际返回条数，校验翻页没有重叠、重新打开内容一致。
  首次调用单列，包含首次线程调度和读取，不混进“重复打开”数据；新进程不等于冷磁盘。
- `submit_user_input` 在已有历史的 home 中新建空会话，以及继续原长会话：从用户提交分别计至接受回执、
  进入 Provider、本地收到首段文本和完整 Run 返回；Supervisor 启动不在计时内。
  即时合成 Provider 输出中文、reasoning 和 Markdown 代码块，用于分离框架成本，不模拟网络首 token 等待。
- Provider 在首段后由 Event 保持运行，独立适配器恢复 200 条历史与当前输出；随后真实接受并取消一条排队输入，
  最后分别记录 abort 返回、Provider 关闭和 Run 终态。校验排队输入没有执行、取消后不再待处理、Run 确实 aborted。
  Event 只控制可操作阶段，60 秒期限仅防止挂起，不作为性能断言；取消结果与长 Run 吞吐分别解释。

历史夹具仍是每条 1 KiB 文本、固定活动摘要与尾部，并非真实用户内容分布；大参数、structured、媒体和长流
分别由其他场景度量。当前新会话测量也不代表应用冷启动。各用户操作、所需数据规模、已有证据和未测范围见
[交互覆盖矩阵](../../docs/plans/archive/2026-10-26-session-data-implementation.md#场景覆盖与资源边界)。
各场景分别报告，不以压力负载的吞吐替代短回复等待，不用统一总分掩盖某个交互退化；
三对样本只提供中位数和范围，不能据此宣称稳定的交互 p95/p99 或真实屏幕体感已验收。

构造、导入和清理不计入运行时间；计时与 tracemalloc 分开。未清空 OS 缓存；新适配器不等于冷磁盘。
文件字节是逻辑长度，不是 NTFS 分配空间；RSS 不是连续峰值；结束时 WAL 不是 WAL 峰值。
不同源码或参数的样本分别统计；进程 IO 计数与相同完整内容的交付耗时分别报告。
Files 当前为顺序消息正文与会话级原生数据库，后者保存定位索引、工具事实、调用状态、摘要、请求小头与 Draft。
SQLite 及 Files Draft 的原生连接只在执行所有者存续时复用。历史读取、
流式本地发布和独立观察的计时边界保持相同，默认轮询间隔不计作 Host 渲染开销。
全量观察是一次性完整历史读取，不是增量刷新 API；独立观察包含既有 100 ms 运行中轮询周期。
这些数据度量框架提供数据的成本，不能换算为 Host 的实际帧率。完整口径、结果与局限见
[重构前后性能对照](../../docs/plans/archive/2026-10-26-session-data-implementation.md#性能对照)。

## 长 Run、慢查询与取消

```powershell
uv run --frozen python -m dev.benchmarks.long_run_growth --rounds 8 64 --repeat 3
uv run --frozen python -m dev.benchmarks.long_run_growth --rounds 8 64 --repeat 1 --slow-reader
uv run --frozen python -m dev.benchmarks.long_run_growth --rounds 8 --repeat 1 --slow-reader --abort-at 4
```

运行真实 Runtime、工具与摘要提交，固定每轮输出和工具结果，采样活动输入、当前输出、慢 sink 队列和进程 RSS。
慢查询通过 Event 保持无关会话读取至任务结束；取消通过 Provider 握手触发。记录文件数、逻辑字节、SQLite 页与 WAL，
并在释放读者后显式 checkpoint。RSS 是边界样本而非进程峰值；不把无关会话可继续工作误称为所有操作无锁等待。

## 完整活动输入与编码

```powershell
uv run --frozen python -m dev.benchmarks.active_context --repeat 3
```

固定无关历史为空，独立增加活动文本条数和内联媒体体积。测量新的 ContextBuilder 准备，以及 Chat Completions、
Responses、Anthropic Messages codec 加 JSON UTF-8 编码；输入构造、存储和网络不计入。
计时与 tracemalloc 分开执行，后者不代表 RSS。两组实验的已执行范围见
[会话整合记录](../../docs/plans/archive/2026-10-26-session-data-implementation.md#验证记录)。

## 会话资源定向读取

```powershell
uv run --frozen python -m dev.benchmarks.session_resources --repeat 7
```

使用两后端的隔离目标格式数据，独立增加旧资源数和选中文件体积，固定读取首部 16 与尾部 32 字节。
记录每个样本、Python 文件打开与读取字节、分配峰值、实际文件数、逻辑字节及 SQLite 查询计划、页数、空闲页和测量结束时 WAL。
Python IO 探针不计 SQLite 引擎读取；tracemalloc 不代表 RSS。暖适配器和新建适配器分别运行，不清空 OS 缓存。
构造与清理不计入读取时间；结果不表示完整模型轮次、冷磁盘或掉电恢复成本。已执行范围见
[资源读取实施记录](../../docs/plans/archive/2026-10-26-session-data-implementation.md#场景覆盖与资源边界)。

## 最近请求完成

```powershell
uv run --frozen python -m dev.benchmarks.context_completion --repeat 7
uv run --frozen python dev/benchmarks/session_refactor_comparison.py --scenario request_io --repeat 3 --request-replacements 32 --request-bytes 4096 1048576 4194304
```

固定请求头，增加不可变输入大小，单独测量完成操作。每次样本准备一个新请求，准备、同步和 SQLite checkpoint
不计入计时；连接保持打开，完成后立即采样 WAL 大小。输出 Python 读取字节、分配峰值、逐次时间与中位数、
真实文件数、逻辑字节、SQLite 页数/空闲页及剩余请求数。没有清空 OS 缓存，不代表完整轮次、RSS 或掉电恢复。
结果见[请求头实施记录](../../docs/plans/archive/2026-10-26-session-data-implementation.md#上下文观察与计量)。

`request_io` 场景使用隔离的重构前/后源码进程，保留真实 Session 执行锁及其连接寿命，连续替换当前请求，不强制 checkpoint。
单独记录请求提交耗时、进程读写传输字节、每次提交后的文件总量与页数，以及执行锁释放后的真实占用。
夹具和空间采样不计入提交时间；进程传输含缓存命中，不等于物理设备流量。默认 `--request-content changing`
让每轮正文全部变化；`stable` 只改变前缀，单独观察原生页复用。它测量请求持久化，完整 Agent
循环与实时观察仍使用 `session_refactor_comparison` 的 `agent_io` 场景。

## 活动摘要读取与投影

```powershell
uv run --frozen python -m dev.benchmarks.compaction_boundaries --repeat 3
```

固定摘要正文和两条保留尾部，分别增加已覆盖消息数和保留摘要数；测量活动摘要点查与预加载历史上的纯投影。
输出逐次时间、Python 读取与分配峰值、摘要序列化大小、真实文件数和逻辑字节，以及 SQLite 页数、空闲页与 WAL。
快照准备和历史内存不计入测量，适配器已初始化且不清空 OS 缓存；Python IO 不包含 SQLite 引擎读取，分配峰值不是 RSS。
此实验不代表完整上下文准备、撤销或冷恢复。结果见
[摘要覆盖边界记录](../../docs/plans/archive/2026-10-26-session-data-implementation.md#上下文观察与计量)。

## Runtime 入口与冷适配器恢复

```powershell
uv run --frozen python -m dev.benchmarks.runtime_history --repeat 3
```

两后端每个样本重新生成隔离的目标格式数据，固定两条活动消息、一个当前请求头和一条短 Draft，
分别增加已覆盖消息（64/4,096）、旧 Run（1/512）及摘要（1/512）。分别测新适配器初始化与显式恢复、
Runtime 创建与完整一轮 FauxProvider 请求，记录耗时、Python 读取字节与打开次数、tracemalloc 峰值、
真实文件数、逻辑字节及 SQLite WAL/页数/空闲页。准备数据不计时，未清 OS 缓存，不代表冷磁盘。
FauxProvider 不含网络或真实 Provider 编码；内存不是 RSS，读取计数不含 SQLite 原生 I/O。
当前执行头与待处理 Run 的读取边界见[提交与执行](../../docs/plans/archive/2026-10-26-session-data-implementation.md#提交与执行)。
Catalog 文件计数、当前 Run 与 Draft 恢复等保留工作仍计入入口；不能用定向正文读取推断所有历史轴或整个冷恢复已收敛。

## 历史数据升级

```powershell
uv run --frozen python -m dev.benchmarks.data_upgrade --operations 1000 --repeat 3
uv run --frozen python -m dev.benchmarks.data_upgrade --backend sqlite --operations 1000 --repeat 3
uv run --frozen python -m dev.benchmarks.data_upgrade --check --operations 1000 --repeat 3
uv run --frozen python -m dev.benchmarks.data_upgrade --native-messages 10000 --repeat 3
uv run --frozen python -m dev.benchmarks.data_upgrade --backend sqlite --native-messages 10000 --repeat 3
```

使用固定历史夹具，增加指定数量的 Session 提交记录，保留夹具中的消息、任务和用量数据。
`--native-messages` 使用 layout 41 / schema 26 的固定原生夹具，另外加入指定数量的 4 KiB 消息，
检验升级是否绕过正文解码与重建。该模式保留待处理及已消费输入、调用事实和当前草稿，不叠加旧提交记录。
每个样本重新准备临时 home，输出总时间、阶段时间、变更文件数、进程 I/O，以及 Python `os.fsync` 的调用次数和耗时；
进程 I/O 包含 SQLite 原生调用和缓存命中的读写，不等同于物理设备写入；Python 同步计数不包括 SQLite 内部调用。
计时包含升级工作区初始化及收尾，不包含输入构造、复制和 Python 导入，
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
uv run --frozen --extra themes python -m dev.benchmarks.welcome --frames 60
uv run --frozen --extra themes python -m dev.benchmarks.welcome --frames 300 --size 114 20 --size 200 50 --stages
uv run --frozen --extra themes python -m dev.benchmarks.welcome_input --style clifford-torus --keys 240
uv run --frozen --extra themes python -m dev.benchmarks.welcome_input --pattern alternate --keys 240
```

点阵基准包含采样、公共数值处理与终端行编码，覆盖所有内置粒子主题和两个视口尺寸。
`--size 列数 行数` 可重复传入；`--stages` 另外独立测量采样器与包含采样的数值引擎，
各阶段结果不能直接相加。报告包含依赖版本、样本数、中位数、P95 和 P99；不使用固定耗时作为回归断言。
输入基准使用 114×30 的艺术区域、真实 Textual 合成与 ANSI 序列化，捕获输出而不接管终端。
默认先连续输入再连续删除；`alternate` 为交替输入/删除。分别测量暂停与播放状态，
输出按键处理延迟、编辑耗时、5 ms ticker 的 loop lag、帧发布间隔、GC 时长与输出量。
间隔统计只包含有 damage 的发布；发布不等于终端完成绘制。输出不包含操作系统终端渲染、IME
及完整 Application 的草稿持久化，因此须结合完整 TUI 回归和真实终端检查。

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
uv run --frozen python -m dev.benchmarks.session_harness_reads --label current-environment
```

资源基准覆盖正常终结、等待/恢复、显式租约转交和关闭；每场景使用 100/1,000 个 Run。计时与 tracemalloc 分开，
通过真实租约和弱引用检查资源收尾。读取基准使用 0/8/64 个控制关系，预热后测量 Session、Harness 与控制投影。
这些场景测量局部生命周期与读取成本，不覆盖插件启动、磁盘吞吐或终端绘制。

## Harness 状态与撤销

```powershell
uv run --frozen python -m dev.benchmarks.harness_lifecycle --label current-environment --repeat 3
```

覆盖两后端、100/10,000 条保留消息、固定短撤回后缀以及不同 owner 状态规模；记录状态版本、原生同步次数、
进程 IO、耗时和内存。夹具创建与首次索引建立不计时。Windows 通过 SQLite VFS 观察 `FlushFileBuffers`，Linux
通过 strace 观察同步调用；观测不可用时明确失败。调用原函数且保留返回值，不改变 fsync/WAL 策略。
同步探针校准见 `tests/benchmarks/test_sqlite_sync.py`；计数不等于绕过操作系统缓存的物理介质写入。

## 消息事实与定向读取

```powershell
uv run --frozen python -m dev.benchmarks.session_messages --counts 1000 10000 100000 --active 8 --repeat 7
```

两后端固定 8 条活动消息、0 个未决输入，分别测按 ID、Run、已接受输入和倒序尾部读取；每组包含同一 adapter
与新 adapter 样本。所选末条消息引用一个已消费 Input，其他消息拥有正文；输入及其状态通过同一快照 API 创建。
准备耗时独立报告，不进入查询计时。分别报告 MessageFact、InputRecord 解码数、Python
读取字节/打开次数、tracemalloc、实际文件数/逻辑字节、SQLite 页数与输入查询计划。文件计数与计划查询不计时。
这些观测不包含 SQLite 引擎 I/O、文件元数据 I/O、RSS 或物理设备流量；未清 OS 缓存，新 adapter 不代表冷磁盘。
运行数据只能说明选定窗口查询，不能证明普通轮次、整个 Run 冷恢复或完整所有权重构已达到目标。

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
uv run --frozen python -m dev.benchmarks.worker_results --label current-environment
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

```powershell
uv run --frozen python -m dev.benchmarks.response_drafts --repeat 3
```

Draft 实验固定输出字节数，分别拆成 10/1,000/100,000 片，使用隔离会话的真实追加和响应提交端口。
报告 Python 分配峰值、活动 Draft、终结后文件占用、两后端原生数据库的分配/空闲页；
Draft 内容字节按原生 content 列计数，文件总量包含数据库开销；采样 WAL 不代表峰值。
不含 Provider/Tool 时间，不清 OS 缓存；tracemalloc 耗时不能当作端到端延迟。

## TUI 会话进入与展开

```powershell
uv run --frozen --extra themes python -B -X utf8 -m dev.benchmarks.tui_sessions --messages 1000 10000 --repeat 3
uv run --frozen --extra themes python -B -X utf8 -m dev.benchmarks.tui_history --snapshot <observation.json> --repeat 3
```

`tui_sessions` 使用临时 Files home、离线 Provider 和真实 OuroTuiApp。进入前保持 Run 运行，恢复 48,070 字符思考
及开放答案尾部；分别测主页进入、补两页后经主页重进、后续实时分片。报告安装、输入响应、历史帧、草稿帧、
最大事件循环延迟、投影消息数及布局缓存命中。`--reasoning-chars` 控制草稿大小，`--workdir` 指定临时目录的父目录。
不计种子构造、应用启动和清理；按键计时从 Textual Key 入队到 Composer 内容改变，不包含物理键盘／终端传输。

`tui_history` 只读取当前格式的 SessionObservationSnapshot JSON，不访问或修改会话目录。
测冷／热历史、最大轨迹首次／再次展开、最长思考首次／再次展开及流式输出转历史；
`--strips-output <path>` 可保存首轮文字、样式和交互 metadata 以对照布局。该输出包含会话内容，应留在本地。
JSONL 保留每轮原始值；不清 OS 缓存。headless 刷新确认不等于真实终端 paint，三个样本不能证明 p95/p99。
设计与测量结果见 [TUI 性能改进计划](../../docs/plans/archive/2026-10-29-tui-performance.md)。
