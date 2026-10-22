# 运行热路径性能规格

> 文档类型：现行规格

## 目标与原则

运行热路径按业务语义边界分配工作，而不是按传输碎片、UI 按键或可重复派生投影分配工作。异步事件循环只做
编排和轻量投影；可能等待文件锁、`fsync`、SQLite writer 或执行大对象物化的同步工作必须离开事件循环。性能改进
不能改变 canonical 顺序、原子提交、恢复、取消、插件 generation 隔离或 Host 观察语义。

真实终端的测量目标和条件性优化见[性能规划](../plans/performance.md)。

## 导入与 Host 准备

CLI 参数解析和 TUI 入口导入不加载 Textual App。`run_tui` 在启动界面时加载 App；MCP 配置、DTO 和
Manager 导入不加载外部 MCP SDK。默认 SDK factory 在 server 首次实际连接时由 worker 加载，重连复用
同一 owner 的 factory；加载期间收到 stop 不得再打开 transport。显式注入 factory 与公开 SDK factory 入口保留。

TUI 启动并发读取模型目录、默认模型和用量；插件/技能系统状态投影及提交前模型解析在 worker 中完成。
插件菜单的列表、详情和状态刷新同样在线程中读取，返回上级菜单复用页面自身的异步刷新回调。
asyncio 队列、界面状态和订阅仍由事件循环拥有。提交前跨越查询 await 后检查 Host 状态及选中的会话/草稿
模型；准备期间发生切换时拒绝该次提交，不能把旧输入发送到新的会话。

## 请求内工作量

- token 估算统一使用 ASCII/非 ASCII 字符计数；纯 ASCII 文本只读长度，混合文本在原生编码路径计数。
  Context 与摘要选块共用该函数，字符权重及取整语义一致，不引入文本缓存。
- 一个工具目录查询至多取得一次当前 active controller 集合；没有受 controller 控制的工具时不读取该状态。
  每次查询重新取值，单工具调用仍检查当时可见性，不跨查询缓存 Harness 状态。
- 工具参数校验遍历错误并保留排序键最小的一项，错误选择保持确定性，不物化完整错误排序列表。
- 文件输入消费时准备有界内容并写入骨架，后续请求复用该内容；上传地址按当前会话清单解析。
  需要即时展开的资源由实际读取路径校验字节完整性，导出在执行复制时校验；请求间不缓存资源清单。

## 流式事件

`RuntimeAdapter` 为每个 Provider stream 维护一个 reader 和一个固定容量 FIFO。reader 独占 Provider async
iterator，消费者独占 FIFO；FIFO 满是唯一无损背压点。

普通 `text_delta` 和 `reasoning_delta` 的 Provider chunk 边界不构成稳定业务语义：

- 每种普通 delta 在一个连续语义段中的首个分片不得等待未来分片；
- 后续分片以最近一次同类发射时间为锚点，在最多 8 ms 的固定窗口内合并；窗口不得因新分片到达而续期；
- 分片到达时窗口已经截止则立即发射，因此慢流不逐片承担额外等待；
- 每批最多 16 KiB，超出部分保持顺序留给下一批；
- continuation、tool、retry、done、error、usage/completion、delta 类型切换和 abort 都会立即结束当前合并边界；
- Provider 异常在先前事件后按序交付；取消或消费者关闭必须关闭 reader 和 Provider iterator。

该合并只降低事件频率，不改变拼接后的 text/reasoning 内容。

## 压缩容量规划

摘要选块必须保持 frame 原子性，并使用与 `estimate_text_tokens` 相同的精确保守估算。一次 `_next_chunk` 中：

- summarization prompt 和已有摘要包络只度量一次；
- 每个候选 frame 的模型可见文本最多序列化一次；
- 累计字符度量必须可加，选块工作量对已检查 frame/message 数为线性复杂度；
- 已有摘要已占满输入容量、单 frame 超预算时继续抛出原有 `ContextOverflowError`。

不得通过放宽 token 预算或拆开工具交换获得更低计算成本。

## Runtime Event Observer

canonical Event 始终持久化；Host event sink 按注册的同步 filter 选择入队事件，未设 filter 时接收全部事件。
filter 不执行 I/O，拒绝投递不影响持久化或其他消费者。Plugin Observer 是独立可选消费者：

- Registry 可用 event name、runtime event type 和 active controller 集合判断是否存在 enabled 匹配项；
- 已知不存在匹配项时，不构造脱敏/冻结的 `RuntimeEventObservation`，不获取额外 Catalog lease，也不进入 dispatcher；
- 当前 Catalog 或 Run snapshot 不可确定时，必须保守走完整观察路径；
- 存在匹配项时继续在事件发布时保留对应不可变 Registry/Catalog 视图。

## 会话状态观察

正文与 Agent 状态观察共享每 Session 的有序事件跟随器。状态观察只合并活动或关系变化，不缓存历史树快照；
无关文本片段不会触发树查询。每个观察者按需执行 canonical projector，同一观察者最多一个查询在途。
存储读取在线程中执行，读取边界在投影和排版前释放；关闭最后一个正文或状态观察者后回收跟随器。
根节点的 Session、选中 Run、关系与事件水位共用一个短只读边界，复用数据库连接；读取 child 前释放根边界，
不嵌套不同 Session 的文件锁。活动状态仍使用 Supervisor 的现有投影，不增加常驻树缓存。

交互式历史使用 Application 的近期窗口与 before 游标，读取成本由请求的完整帧决定；Run/compaction 元数据与
活动 Run 事件独立保留。TUI 初始窗口为 200 条消息的软限制，向前加载复用既有 Strip，不重新排版全部已加载正文。
选择仍有执行者的会话时，恢复入口先查询执行归属；已有 owner 不进入完整执行恢复读取。显示窗口和完整导出互不替代。
上下文状态读取持久化 header 索引，避免为请求身份、时间与 token metrics 反序列化完整输入和媒体正文。

## TUI 输入草稿

内存 `TuiInputDrafts` 始终包含最新修订。磁盘保存采用 300 ms 尾沿防抖：持续编辑只延后保存，静默后保存
最新完整快照。保存期间出现新修订时必须再次保存最新值。Host 关闭必须等待最终修订完成原子保存；保存失败沿用
现有 warning 语义，不能阻塞编辑或 Runtime。

## 工具结果消息

同一 `ToolCallBatchRecord` 的完成结果按 `source_index` 构造消息，并作为一个有序 `append_messages` 批次提交。
所有资源引用必须在任何新消息提交前验证。提交成功后才发布 `ToolCallFinishedObservation` 并推进批次状态。

幂等恢复时，已有 `(tool_result, tool_call_id)` 消息直接复用，只把缺失消息放入新批次；sequence 和
`result_message_id` 必须保持确定性。单结果接口与批接口共享同一构造和校验规则。

## Runtime 阻塞边界

同步 Store port 保持同步接口；application/core 的异步编排通过一个受所有权保护的 worker 调用边界执行它们：

- 一个 transaction/mutation 闭包从取得锁、读取、状态转换、写入到释放锁必须完整留在同一个 worker；不得把闭包内
  的 Store 调用逐个切换线程；
- model round begin/commit、上下文投影和快照保存、usage、工具批检查点、Run complete/fail/pause，以及 Run 启动所需
  的 durable mutation 不得在事件循环线程执行；
- 不使用全局单线程 executor。不同 Session 可以由默认 worker pool 并行，顺序与排他性仍由 Session/SQLite 的既有锁
  和事务定义；“同一 worker”只约束一次完整操作；
- caller cancellation 不能遗弃仍持锁或正在修改 durable state 的线程。worker 必须先 settle，随后再向 caller 传播
  cancellation；
- canonical event 仍在 durable boundary 成功后由事件循环按序发布。

Windows 的文件/目录原子发布共享同一个有界 transient sharing-race 重试；重试发生在 worker 上，不得冻结 TUI。

## 取消观察

Provider 和工具执行中的 `AbortSignal.is_aborted()` 是高频同步检查，只能读取进程内状态：

- 本进程 abort 在 durable transition 成功后立即发布到本地 signal；
- signal 激活时先执行一次 worker-side durable read，关闭 `run_started` 与 signal 建立之间的竞态；后续 Run mutation
  必须从 latest Run 派生，不能用旧快照覆盖 `abort_requested`；
- 跨进程 abort 由后台 poller 最多每 50 ms 读取一次 durable Run，再桥接到本地 signal；
- durable poll 和 settlement 检查在 worker 上运行，signal 检查不得读 Store；
- Run 结束必须取消并等待 poller，不能留下后台任务或未观察异常。

## TUI 流式消费与结构边界

- Host 的 Runtime/Agent tree 订阅建立是异步操作：Store 查询与历史事件投影在 worker 中完成，订阅队列、任务与
  Supervisor 内存状态始终由事件循环拥有。先登记有界 live 队列，再读取 durable replay，合并时按 sequence 去重；
  初始化期间溢出继续发布 lagged，失败或取消必须撤销订阅。不得在线程中运行持有 asyncio 队列的整个 Supervisor。
- Supervisor 的提交、交互恢复和 enqueue 前查询离开事件循环；最后一次 await 后必须重新检查关闭状态和 Session
  调度所有权，再一次性登记 job。Agent tree 的持久化查询同样在 worker 执行，订阅集合的变更留在 loop。
- 历史冷加载、增量、宽度变化与主题变化共用一个后台渲染 worker。冷加载与增量在后台分块准备、整批发布；
  宽度或主题改变时，完整 render request、内容版本与几何校验通过后原子替换 strip buffer。
  只改变高度不重绘已完成历史；恢复可用几何时继续未绘制内容，已有有效 buffer 在新结果完成前保持可见。

- spinner、计时器和其他定宽内容动画只触发 paint；只有几何尺寸实际变化时才触发布局；
- 同一工具批的新行使用一次批量 mount，终态使用一次批量 history append 和一次批量 remove；复杂 Markdown/批量
  immutable transcript 渲染在 worker 上完成；
- TUI 每个 compositor frame 最多应用 64 条，暂存不超过一个 Application 观察批次；整批内容应用成功后确认。
  缓存 lag 从已应用位置分页追赶；快照确认不等待历史排版，可见活动内容的转移须等待替代行就绪；
- 帧消费由单个 Textual worker 执行，App 消息处理不等待后台解析和排版；显式排空与帧消费串行取得和应用前缀，
  包括投影前的异步读取。Host 关闭取消呈现 worker，计算线程完成后不能再发布已取消的结果；
- `batch_update()` 只用于合并可合并的刷新，不能被当作消除 `AwaitMount`、`AwaitRemove` 或结构布局成本的替代品。

## 文件搜索与技能目录

Grep 单文件与目录搜索共用匹配管线。文件名和计数模式只收集文件计数，不构造正文及前后文记录；正文模式保留
匹配收集上限和精确总数。普通匹配行号逐项消费，文件内容按 64 KiB 分块读取，保留 32 MiB 文件上限和二进制嗅探，
避免为每个小文件预分配上限大小的缓冲区。空文件没有可匹配行，跨行零宽匹配也不生成无效行号。

Skill 资源遍历在下降前剪枝忽略目录，不枚举其内部文件再过滤；结果继续按路径排序。插件声明式 Skill 准备通过
`run_blocking` 在 worker 中完成，事件循环持有 Catalog 发布权；取消先等待读取收尾，再进入 generation 清理。

技能目录发现只读 frontmatter，遇到结束分隔符即停止，不读取完整正文或遍历资源。目录查询、显式技能引用接受和模型请求
准备在工作线程读取当前来源；没有后台扫描或文件监听。目录未变化时复用已发布的 Extension Catalog。
`@skill` 每次打开只发现一次，菜单内输入过滤字符、导航和多选不再访问文件系统。发现期间 Composer 可继续输入或
取消，过期菜单的结果不会重新打开菜单。显式激活只读取所选 `SKILL.md`，不枚举用不到的资源路径。

插件包遍历在下降前排除缓存目录，校验与摘要使用同一文件集合；缓存数量和大小不增加包校验工作量。

## 当前契约验收证据

- `tests/core/test_adapter_stream.py`
- `tests/application/test_resource_io.py`
- `tests/tools/test_builtin_search_and_todolist.py`
- `tests/skills/test_skill_loader.py`
- `tests/plugins/test_plugin_system.py`
- `tests/test_local_locks.py`
- `tests/config/test_atomic_config.py`
- `tests/architecture/test_imports.py`
- `tests/core/test_context.py`
- `tests/tools/test_tools.py`
- `tests/mcp/test_manager.py`
- `tests/tui/test_controller.py`
- `tests/application/test_compaction.py`
- `tests/core/test_observers.py`
- `tests/application/test_events.py`
- `tests/tui/test_app.py`
- `tests/application/test_tool_call_batches.py`
- `tests/application/test_resources.py`
- `tests/core/test_callbacks.py`
- `tests/application/test_runtime.py`
- `tests/application/test_run_ownership.py`
- `tests/application/test_supervisor.py`
- `tests/tui/test_conversation_history.py`
- `tests/tui/test_session_input.py`

## IO 所有权与响应性验证

事件写入取消须持有 per-Session 顺序锁，直到 worker 写入和 live publication 都结束。Spawn submission 的持久化
更新使用 delegation 生命周期锁，避免与结果结算竞争。HITL 可以在原 Run 释放执行锁前呈现；审批/回答通过
`acquire_parked` 每 50 ms 重试，默认最多 5 秒，Run 离开预期等待态即停止。取得执行锁后重新验证准确交互身份。
文件锁是最终排他边界，取消 acquisition 必须收回线程已获得但尚未交给调用方的锁。

进程内 mutation 锁标识使用解析后的规范路径；Windows 普通路径与扩展路径前缀必须映射到同一个标识，
保证嵌套事务复用同一把可重入锁。配置文件复用共享原子文本 I/O，序列化错误仍为 ConfigError，写入失败保留
旧文件并清理临时文件；文件同步和 Windows 短暂共享冲突重试由同一基础设施负责。

Application 与 TUI 的投影只使用既有事实，时间、sequence 等不影响用户可见状态的变化不应触发全量重建。
Store 离开事件循环能消除同步 IO 阻塞，但不保证 GC、GIL、App 消息泵或真实终端绘制达到帧预算。
`dev/benchmarks` 测量 loop lag、按键、动画、GC 和局部 IO；真实 Windows/POSIX 终端须分别验证。
当前没有跨平台端到端时延保证，不能把 headless 结果或局部优化视为该保证。

## Harness 与撤销成本

状态变化写一份不可变快照 O(H)，消息只增加常量大小锚点。无变化回调、状态读取和原生撤销不新建状态
版本；撤销只移动版本引用。调用阶段捕获共享快照，owner 视图不逐字段读取磁盘或调用 RPC。

固定短后缀下，文件后端按索引/尾部定位并原地裁剪，不复制保留前缀；epoch 使跨 reader 的同长度/增长
重写失效。SQLite 采用索引定位和事务范围删除。延迟事件归属使用有界 Run epoch 缓存，不逐 token 读 Run。

只读和 no-op 不提交状态，持久化保留 fsync/WAL 耐久策略。测量方法见
[本地性能测量](../../dev/benchmarks/README.md)；进程 IO 与状态提交分别计数，不把辅助 IO 解释为业务写入。
