# 工具调用、Run 停驻与有序回填

2026-11-27 归档，实施与独立复审均已完成。

基线为 `dev@f14f4e42`。开始时 HEAD 与审查版本一致，工作树干净。默认模型循环和插件 Driver 已共用协调器、调用事实和发布事务。实施从冷恢复回归开始。

## 四个时机

| 时机 | 用例与约束 |
|---|---|
| 启动 handler | 批次通过组合契约，调用就绪且取得宿主分配的名额。按实际参数、工具绑定、当前权限复核，先记录该次 Start。 |
| 单项落盘 | 收到问题或确定结果即记录到其调用与 attempt。问题状态和问题一起提交，普通结果的状态提案留给结果帧。 |
| Run 交权 | 执行者处理就绪调用并等待在途 handler 交回控制。仅剩外部等待时，持久化停驻状态，交权后按目标调用处理交互。 |
| 发布并交还 Driver | 每项都有反馈，按源序原子提交消息、状态提案、发布引用及 checkpoint。恢复复用已提交事实。 |

## 里程碑与验收

1. **停驻和冷恢复。** 从 `RuntimeService.run`、答复入口和两种存储的冷重开开始。修改协调器的执行中检查点、会话恢复和批次转换。问题即时可见，答复生效可等到当前执行者交权。兼容旧的 `waiting_user/executing` 记录。以外部执行标记仅一次、待答问题保留、在途未知反馈和正确回填验收。覆盖确定结果已落盘和帧已提交两个中断点。
2. **宿主模式与独占。** 从 TUI 使用的宿主配置、模型及程序化 Driver 的批次创建入口、工具定义及摘要兼容读取入手。两个入口均保存宿主当时的模式和并发上限，已接受批次保留其记录。直接使用原生工具级串行偏好的调用方需自行协调资源。用新旧配置下的实际 handler 重叠、旧 Shell 与工具组摘要、AskUser 混批、Plan 和 Goal 混批零启动验收。
3. **逐项等待与继续。** 修改预检、执行器、审批和答复入口，以及 Supervisor 和子结果交接。其他就绪调用可在审批或答复前产生外部效果。升级前的停驻批次首次正常唤醒后也按就绪情况继续。以两问一读、问题版本与逆序答复、两项待批加一项自动获准、修改参数和撤权、混合子等待及取消验收。复用 Run 所有权、调用身份和交互投影。

各阶段运行对应的应用和存储回归。交付前更新[工具与交互规格](../../specs/tools-and-interactions.md)，按[贡献指南](../../../CONTRIBUTING.md)执行完整门禁，分别记录本地、离线协议与平台证据。最后以普通可恢复插件接入、调整 Run 交权条件两条维护路径复核变更范围。

## 执行记录

| 里程碑 | 实际结果与证据 |
|---|---|
| 停驻与冷恢复 | 首个子进程回归在文件、SQLite 上均重现答复后缺少兄弟反馈的失败。修复后，当前记录与旧式 `waiting_user/executing` 都能回答原问题、按序发布未知反馈，外部标记始终只有一次。另测最后一项确定结果落盘、整帧落盘，以及同批已知观察带未提交提案的中断。见[进程中断](../../../tests/application/test_tool_process_boundaries.py)和[Driver checkpoint](../../../tests/application/test_run_drivers.py)。 |
| 模式与组合 | 两种来源均从 TUI 使用的配置入口设置模式和上限，以四个 handler 实测串行峰值 1、并行峰值 2。用相反设置重启后，旧批次仍以保存的上限 2 执行。AskUser 与 Read 同批。Plan 或 Goal 的混合批次在两个入口都是零 Start、零外部写入。旧 Shell 和动态工具组使用历史摘要夹具校验原变体。见[工具批次](../../../tests/application/test_tool_call_batches.py)、[程序化调用](../../../tests/application/test_run_drivers.py)、[Shell](../../../tests/tools/test_builtin_shell.py)和[动态工具组](../../../tests/plugins/test_tool_groups.py)。 |
| 逐项调度与提交 | 两个 SDK 提问让出执行名额，独立读取先完成。逆序答复、追问及旧版本拒绝后，只发布一帧。审批与同步子任务可按任一顺序结算。批准第一项后，TUI 仍显示第二项审批时，第一项文件已经写入。内置、SDK 和真实本地 stdio MCP 共用协调器。见[审批面板](../../../tests/tui/app/test_interactions.py)、[父子交互](../../../tests/application/test_driver_agents.py)和[MCP 协议集成](../../../tests/mcp/test_sdk_integration.py)。 |

两个边界在实施中得到回归验证。问题本身可能依赖 handler 读取的会话状态，即便没有写入提案，提问时也需要校验捕获的 Harness 版本。没有状态竞争的多项提问可独立应答。同一 owner 或 controls 的竞争提案在发布前失败。子结果的观察、对应回执及 Run 下一状态必须在父扩展回调前落盘。此时撤权会阻止回调，回调失败后仍可恢复原观察。已发布的未知反馈和已消费的 checkpoint 不随迟到结果变化。见[问题版本](../../../tests/application/test_harness_tools.py)、[状态冲突](../../../tests/application/test_parallel_tool_observations.py)、[子回执归属与取消](../../../tests/application/test_agent_call_identity.py)和[迟到结果](../../../tests/application/test_late_tool_results.py)。

## 维护边界与兼容性

- 新增可恢复插件只需提供 SDK 的 `ToolContribution`、handler 和 resume handler，通过现有贡献注册。两问一读回归使用同一个贡献的两个调用。管线以 Call 身份、问题版本和结果推进。
- 调整活跃 Run 的交权条件改 `tool_batch_suspension`。预检、交互结算及在途 handler 收尾均使用该规则。`RunLifecycle.checkpoint_tool_dispatch` 负责提交相应状态。冷恢复先处理失去执行者的事实，再复用主要等待原因的投影。
- 基线的生产定义中，只有 AskUser 明确指定工具级串行，原独占声明已将其合法批次限定为单项。用户可观察到的变化来自放开混批，例如 Read 能在 AskUser 获得答复前完成。SDK v7 的工具贡献和 MCP 保留各自的协议。第三方直接构造原生 `ToolDefinition` 的代码若曾依赖 `execution_mode`，新批次将遵循宿主设置，直接读取旧字段的代码需更新。未调查仓外调用方。

## 首轮实施的交付验证

在 Windows、Python 3.14.6 下按[贡献指南](../../../CONTRIBUTING.md)执行。文件与 SQLite 的中断用例启动独立子进程，MCP 集成使用本地 stdio 服务，TUI 使用 Textual 的交互测试入口。

| 门禁 | 结果 |
|---|---|
| 锁定依赖与架构、核心测试 | `uv sync --frozen --all-groups --extra themes` 成功。架构与核心 410 项通过。 |
| 带 themes 的全量测试 | 四个 worker 按 worksteal 运行，8457 项通过、9 项按环境跳过，覆盖率 89%。8 项需要本机未获准的符号链接，1 项需要 POSIX 权限。 |
| 静态检查 | Ruff 检查与格式、win32 mypy 和 compileall 通过。mypy 检查 644 个源文件。 |
| 分发 | 主包与主题包的 sdist、wheel 构建成功。无可选依赖的隔离测试 427 项通过，基础与 themes 两种 wheel 安装检查通过。 |

本机未执行 Linux、macOS、真实模型账户或远程 MCP 服务验收。

## 独立复审

复审开始时，`dev`、HEAD 和 `origin/dev` 均为 `f14f4e42`。索引没有改动，工作树有首轮修改的 38 个已跟踪文件和本计划。对照包含完整工作树。复审另外检查了 Provider 组装、两个 Driver 入口、Agent 回执、控制器、观察投影、配置入口、SDK、MCP、示例及现行规格。

| 严重度 | 最小反例与用户后果 | 修复与证据 |
|---|---|---|
| 高 | 问题已投影，兄弟 handler 已写外部标记。宿主 B 提交答案后第一次取锁失败，此时让宿主 A 退出。两种后端均在 B 的同一次答复中报 `run is not parked`。 | 交互取得原 Run 锁后重读会话和目标 Run。遇到失去执行者的批次，仅对已取得所有权的 Run 应用冷恢复规则，再匹配问题版本。旧问题得到答案，兄弟项发布未知结果，外部标记保持一次。[交互入口](../../../ouro/application/runs/interactions.py)、[恢复事务](../../../ouro/application/sessions/recovery.py)、[双进程回归](../../../tests/application/test_tool_process_boundaries.py)。 |
| 中 | 并行上限为 2，一个调用的执行前策略在等外部决定。另一个独立读取即使已获准，也无法取得 Start，直到第一项的权限回调返回。 | 执行前策略在批次检查点锁外运行，各自按当前参数和权限产生决定。独立读取在策略返回前已启动，最终被拒的调用从未启动。共享检查点写入仍串行。[执行边界](../../../ouro/application/tool_calls.py)、[并行权限回归](../../../tests/application/test_tool_call_batches.py)。 |
| 中（验收缺口） | 原 `after_frame` 在 SQLite 的内层 `commit_session` 返回时立即退出。冷读实际得到 0 条结果消息，旧断言在补帧后才检查结果，回滚也能通过。 | 把“帧后”退出点移到 `publish_tool_batch` 返回之后。两后端在任何恢复操作之前断言结果消息数量，恢复后核对同一个消息 ID 和 Outcome 引用。性能探针也按最外层成功事务计时。[中断断言](../../../tests/application/test_tool_process_boundaries.py)、[事务计时](../../../dev/benchmarks/tool_call_pipeline.py)。 |

首轮的静态说明中，运行规格把继续调度与 Driver 接手写在同一个时机，持久化说明中的“等待状态”也可能被读成整次 Run 停驻。现行[运行规格](../../specs/runtime.md)、[工具规格](../../specs/tools-and-interactions.md)和[性能规格](../../specs/performance.md)均已标明事务或所有权边界。

### 决策归属与兼容性复核

| 决定 | 代码与实际调用方 | 保留理由 |
|---|---|---|
| 接受并准入调用 | Provider 增量按索引组装。默认 `InnerAgentLoop` 使用模型响应身份，SDK Driver 可以主动提交 Calls。两个执行端口都刷新宿主设置，经协调器保存同样的模式、并发上限及组合契约。[模型组装](../../../ouro/core/model_execution.py)、[默认循环](../../../ouro/core/inner_loop.py)、[两个端口](../../../ouro/application/runs/execution.py)。 | 模型批次保留 assistant 消息关系，主动调用保留 Driver 来源。混合独占批次在任何 Start 前逐项返回拒绝，避免 Plan/Goal 提案提交前已有外部写入。基线生产定义中只有 AskUser 指定强制串行。插件示例也使用显式独占。 |
| 启动与逐项记录 | 执行器按就绪项和批次已保存的上限放行。协调器执行准入，保存 Start 后进入 handler。结果按 Call、尝试和绑定保存。审批、问题和子结果分别按原身份接收。[执行器](../../../ouro/tools/batch_executor.py)、[协调器](../../../ouro/application/tool_calls.py)、[持久化用例](../../../ouro/application/runs/lifecycle.py)。 | 预检到实际启动、提问到恢复之间，权限、参数和绑定都可能变化。慢策略只占本项名额。取消或失去执行者后，Start 说明外部效果可能已经发生，重试会有重复执行的风险。 |
| 交出 Run | 预检、交互结算、执行器收尾均调用 `tool_batch_suspension`。协调器按已提交的 Run 状态通知子任务服务。冷恢复先处置失去执行者的项，再按有效等待恢复交接点。[交权规则](../../../ouro/core/tool_batch_transitions.py)、[交互结算](../../../ouro/application/runs/interactions.py)。 | 一个等待有自己的 Call 身份和问题版本。Run 的主要等待原因用于展示。已有执行者负责正在运行的 handler，交接只能在取得同一 Run 所有权后生效。冷检查本身不启动外部操作，唤醒旧批次后再按保存的模式调度。 |
| 发布与下一轮 | `publish_tool_batch` 接受完整反馈，在同一提交中按源序固定消息及 Outcome 引用、会话状态和 Driver checkpoint。随后由原 Driver 消费结果或继续默认循环。[发布事务](../../../ouro/application/runs/lifecycle.py)、[Driver 接手](../../../ouro/application/runs/drivers.py)。 | 问题随状态变更提交时检查捕获版本。最终提案按 owner 和 controls 检查冲突。迟到观察只能补充原 attempt 的执行记录，已发布引用与已消费 checkpoint 不会被改写或重放。 |

可恢复插件的新增路径仍是 SDK 贡献注册、handler 和 resume handler，两次提问和 MCP 测试走现成的适配及装配。改变活跃 Run 的交权条件只需调整 `tool_batch_suspension`，其他入口提交或读取这项决定。Shell 和动态工具组的历史摘要仍由执行绑定读取逻辑处理，恢复旧批次使用批次头。仓内原生工具级覆盖仅见旧 AskUser，仓外直接构造 `ToolDefinition` 的代码没有实测。

### 本轮实跑的门禁与中断检查

Windows 11 x64、CPython 3.14.6、uv 0.11.28。以下是独立复审重新执行的结果。

| 验证 | 实测结果 |
|---|---|
| 运行、工具、权限、SDK Driver、子任务、MCP、TUI、旧摘要定向测试 | 365 项通过。修正帧中断点和进程清理后，两后端的完整进程边界文件另跑 18 项通过。 |
| 真实程序化 Driver 退出 | 文件与 SQLite 各在帧提交前后强制退出。重启时 checkpoint 分别为 pending 和 ready。两次 reconcile 的结果引用相同，原 attempt 为 1。继续原 Run 后 SDK resume 与外部写入均为 1 次，checkpoint 为 consumed，模型轮数为 0。 |
| 锁定依赖、架构与核心 | `uv sync --frozen --all-groups --extra themes` 成功。架构与核心 410 项通过。 |
| 带 themes 的全量测试 | 四个 worker，8460 项通过、9 项跳过，覆盖率 89%。8 项受 Windows 符号链接权限限制，1 项需要 POSIX 权限。 |
| 静态与分发 | Ruff、格式、win32 mypy（644 个源文件）、compileall 通过。主包与主题包的 sdist 和 wheel 构建成功，两种 wheel 安装检查通过，无 themes 的隔离用例 427 项通过。 |

完整门禁命令见[贡献指南](../../../CONTRIBUTING.md)。三处回归的正常入口可单独运行。

```powershell
uv run --frozen pytest -q tests/application/test_tool_process_boundaries.py
uv run --frozen pytest -q tests/application/test_tool_call_batches.py -k parallel_policy_wait
```

### 性能对照方法

既有 `session_pipeline` 测量会话与界面延迟，`runtime_boundaries` 测量单独的运行边界。本次增加基准入口以使用同一套事务计时比较两种 Driver，复用仓内 FauxProvider、示例插件和实际存储。三版在同一台 Windows 主机上使用同一解释器和同一磁盘临时目录。两种存储和两种工具来源交叉测量。每次使用独立会话，以可控 Provider 或实际安装的进程内 SDK Driver 运行。对每个组合轮换三个版本的顺序，每组一次预热、三次计时，共三组。批次为 1、4、12 项，并发上限为 2，handler 负载与策略负载分别固定为 35 ms。串行和单工具的 handler 立即返回。等待场景立即提交答案，通过真实 resume handler 再执行一次，Driver 接手前逐项核对结束状态和轮数。

版本标识为 `ouro/**/*.py` 按路径排序，以 UTF-8 相对路径、NUL、LF 正文、NUL 依次计算 SHA-256。基线 `f14f4e4282b498d3f6acea42d7683aed42dca834` 的源码摘要前缀为 `8b6041f73d9b`，复审入场工作树为 `36124c7de20a`，修复后工作树为 `c1726492614b`。复审入场的完整 Git 差异另存供复跑。运行时输出的 `code` 标明实际导入的仓库。

```powershell
# 在最终工作树启动。将 PYTHONPATH 依次设为基线、入场快照和最终源码根。
$env:PYTHONHASHSEED = '0'
$env:PYTHONPATH = (Get-Location).Path
.\.venv\Scripts\python.exe dev/benchmarks/tool_call_pipeline.py --repeat 3 --warmup 1 --sizes 4 12 --limit 2 --delay-ms 35 --cases single serial parallel wait policy
```

计时采用最外层会话事务成功退出的时刻。首启从接受到首个 handler，发布从末项 Outcome 落盘到整帧落盘，吞吐从接受到整帧落盘。工具注册、插件安装、建库和最终模型响应均在计时区间外。仓内没有这三个指标的绝对毫秒预算。最初取内层 `commit_session` 返回的探索数据已排除。AskUser 混批属于新增执行行为，用独立读取先于答复的功能断言验证。[可复跑基准](../../../dev/benchmarks/tool_call_pipeline.py)

### 性能实测

每行每版 9 个有效样本，共 864 个。首启和发布均为中位数，单位 ms，顺序为基线 / 最终。吞吐单位为批/秒，顺序为基线 / 复审前 / 最终。末列为整批用时的 P25–P75，单位 ms。Model 使用默认 Agent 循环，Driver 主动提交原生工具批次。

| 存储 | 来源 | 负载与项数 | 首启 ms | 发布 ms | 吞吐 批/s | 整批 P25–P75 ms |
|---|---|---|---:|---:|---:|---|
| files | model | single ×1 | 11.1 / 10.7 | 59.9 / 60.2 | 13.2 / 12.2 / 13.2 | 73.2–77.7 / 73.9–79.8 |
| files | model | serial ×4 | 16.8 / 15.3 | 67.8 / 65.1 | 8.5 / 8.6 / 8.8 | 113.4–130.2 / 112.6–123.0 |
| files | model | serial ×12 | 27.1 / 27.4 | 70.0 / 67.4 | 4.6 / 4.6 / 4.9 | 204.8–235.9 / 202.1–210.4 |
| files | model | parallel ×4 | 16.3 / 15.6 | 65.7 / 65.0 | 5.0 / 5.0 / 5.2 | 194.1–201.6 / 188.8–192.2 |
| files | model | parallel ×12 | 28.6 / 28.4 | 73.3 / 71.6 | 2.1 / 2.1 / 2.2 | 461.2–475.0 / 462.1–466.4 |
| files | model | wait ×1 | 10.9 / 10.7 | 58.9 / 52.6 | 5.1 / 5.9 / 6.1 | 185.1–203.1 / 160.3–173.5 |
| files | model | policy ×4 | 114.5 / 62.2 | 67.2 / 66.5 | 3.3 / 3.4 / 4.9 | 299.8–312.4 / 200.8–206.1 |
| files | model | policy ×12 | 134.2 / 73.3 | 73.2 / 75.8 | 1.4 / 1.4 / 2.1 | 688.6–699.2 / 467.0–476.0 |
| files | driver | single ×1 | 9.7 / 10.1 | 47.4 / 49.4 | 16.2 / 15.6 / 15.4 | 60.1–69.3 / 62.2–66.2 |
| files | driver | serial ×4 | 15.3 / 14.4 | 53.7 / 50.4 | 9.6 / 10.1 / 10.1 | 99.0–109.5 / 97.6–100.9 |
| files | driver | serial ×12 | 27.3 / 30.3 | 54.8 / 54.4 | 5.1 / 5.0 / 4.7 | 193.6–201.3 / 196.7–215.8 |
| files | driver | parallel ×4 | 15.9 / 16.4 | 53.9 / 50.8 | 5.4 / 5.4 / 5.4 | 183.8–187.4 / 179.1–188.7 |
| files | driver | parallel ×12 | 28.9 / 29.7 | 58.2 / 55.5 | 2.2 / 2.2 / 2.2 | 429.6–457.7 / 455.9–462.4 |
| files | driver | wait ×1 | 10.3 / 10.1 | 58.0 / 56.6 | 5.5 / 6.6 / 7.0 | 176.5–187.4 / 139.0–155.7 |
| files | driver | policy ×4 | 105.3 / 58.4 | 55.1 / 51.7 | 3.5 / 3.5 / 5.3 | 278.9–287.7 / 184.2–193.6 |
| files | driver | policy ×12 | 131.2 / 77.3 | 56.6 / 60.3 | 1.5 / 1.5 / 2.1 | 672.6–687.3 / 459.1–471.2 |
| sqlite | model | single ×1 | 5.1 / 4.7 | 2.2 / 2.1 | 113.5 / 108.1 / 113.7 | 8.5–9.8 / 8.7–9.1 |
| sqlite | model | serial ×4 | 6.1 / 5.5 | 2.8 / 2.9 | 44.2 / 44.0 / 44.8 | 21.3–25.0 / 20.8–23.8 |
| sqlite | model | serial ×12 | 8.8 / 8.0 | 4.9 / 4.9 | 16.5 / 12.1 / 12.2 | 56.8–79.5 / 71.5–84.2 |
| sqlite | model | parallel ×4 | 6.2 / 5.8 | 3.6 / 3.4 | 7.0 / 6.9 / 8.2 | 139.5–150.2 / 120.0–134.4 |
| sqlite | model | parallel ×12 | 9.6 / 9.2 | 5.0 / 5.1 | 3.0 / 3.1 / 3.1 | 322.3–336.0 / 321.6–323.7 |
| sqlite | model | wait ×1 | 4.4 / 5.3 | 5.7 / 4.1 | 21.1 / 20.7 / 22.2 | 41.0–47.6 / 43.3–49.3 |
| sqlite | model | policy ×4 | 110.2 / 71.4 | 3.6 / 3.3 | 4.1 / 4.5 / 6.9 | 218.3–247.2 / 137.3–153.5 |
| sqlite | model | policy ×12 | 122.0 / 62.5 | 5.0 / 5.2 | 1.6 / 1.7 / 3.0 | 599.2–619.0 / 328.5–339.9 |
| sqlite | driver | single ×1 | 7.0 / 7.0 | 2.5 / 2.4 | 85.2 / 90.0 / 82.9 | 10.8–12.0 / 10.8–12.2 |
| sqlite | driver | serial ×4 | 7.2 / 7.4 | 3.5 / 3.4 | 40.6 / 39.3 / 40.4 | 24.2–24.8 / 24.5–25.7 |
| sqlite | driver | serial ×12 | 11.0 / 11.2 | 4.7 / 4.9 | 14.9 / 14.6 / 12.5 | 59.4–84.3 / 66.3–100.3 |
| sqlite | driver | parallel ×4 | 8.4 / 7.5 | 3.5 / 3.8 | 6.4 / 6.5 / 7.5 | 149.9–169.4 / 125.0–138.1 |
| sqlite | driver | parallel ×12 | 12.6 / 11.6 | 5.2 / 5.1 | 3.0 / 3.0 / 3.1 | 328.1–339.2 / 321.2–324.0 |
| sqlite | driver | wait ×1 | 5.9 / 6.4 | 6.4 / 4.2 | 20.0 / 21.1 / 21.2 | 47.7–51.3 / 45.7–48.9 |
| sqlite | driver | policy ×4 | 110.3 / 54.4 | 3.5 / 3.6 | 4.4 / 4.4 / 6.9 | 215.2–233.2 / 137.2–149.9 |
| sqlite | driver | policy ×12 | 117.5 / 67.5 | 5.1 / 5.2 | 1.6 / 1.6 / 2.9 | 610.2–637.1 / 339.4–351.0 |

常规调用的持久化工作量在上述批量内随项数线性增长。两版的 1、4、12 项分别有 5、11、27 次会话提交，Call 状态点读为 4、16、48 次。真实的提问/恢复由基线的 10 次提交、11 次状态点读、3 次 Outcome 点读降至 9、9、2。Start 和确定 Outcome 都已落盘。既有 Start 检查点仍复制内存 Call 列表并生成批次头，大批量的 CPU 成本未外推。此次变更没有引入跨调用缓存或单独的交互队列。

并行 handler 的固定等待与执行前策略的固定等待分开测量。前者的实际 handler 峰值为 2，后者的策略回调峰值从 1 到 2。在每个慢策略样本中，三版均对每项执行两次策略检查和一次 handler。模型入口均完成两个模型轮次，程序化入口均消费一次 checkpoint。

### 波动复核与验收限制

主对照的两个发布时间差异在每组各版追加 15 次后均未复现。文件后端 Driver 的 12 项慢策略批次，基线与最终的发布中位数分别为 58.28、57.71 ms。SQLite Driver 的 4 项并行批次分别为 3.70、3.54 ms。复测也覆盖了较大串行批次与并行批次，共 360 个样本。SQLite 模型入口的 12 项串行批次在主对照中看似变慢，复测的整批中位数为 83.28、65.34 ms。

对波动较大的 SQLite Driver 12 项串行路径，再轮换三版各采 33 次，共 99 个样本。基线、复审前和最终的整批中位数为 82.52、82.71、84.90 ms。基线 P25–P75 为 61.62–86.86 ms，最终为 65.79–93.07 ms。首启中位数为 10.79、11.33、11.24 ms，发布为 4.70、4.74、4.86 ms，吞吐为 12.12、12.09、11.78 批/秒。这条路径的最终中位数略慢，差异落在基线的实测波动内。常规调用的提交及点读次数一致。三组对照共保留 1323 个有效样本，未观察到可复现且超出基线波动的框架退化。

本地原始样本、各指标分位数、复审入场补丁、真实进程验收及门禁日志在仓库的忽略目录 `.ouro-dev/tool-call-review-20261126/`。从仓库根运行下列命令可在新的临时目录重建三版主对照。脚本创建基线与入场快照，用当前工作树作为最终版。

```powershell
& .\.ouro-dev\tool-call-review-20261126\replay.ps1
```

本轮仅验证 Windows 本地文件与 SQLite、可控 Provider、进程内 SDK Driver 的性能。远程模型或 MCP、worker 传输、Linux/macOS、冷盘与物理掉电的性能未验证。实际进程退出、SDK worker、MCP 本地 stdio 另有正确性证据。已获准写操作可以早于兄弟审批发生，这项产品变化通过行为用例单独验收。
