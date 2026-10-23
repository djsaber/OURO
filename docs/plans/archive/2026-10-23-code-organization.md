# 代码组织重构实施与验证记录

> 文档类型：工程实施记录
> 状态：P0–P6 已完成；按使用者要求仅执行定向回归，全量测试待使用者手动执行
> 审查基线：2026-10-23，提交 `9ea9e74`
> 范围：代码组织、模块依赖、导入兼容与配套工程调整；不改变功能实现
> 本次执行约束：保持现状布局，不迁移到 src；仅执行定向回归，全量测试由使用者在重构完成后手动执行

> 后续决定：使用者要求移除全部历史兼容引用中转。下文保留首轮设计与验证证据；
> 旧路径兼容承诺已由文末[移除历史兼容中转](#移除历史兼容中转)取代，当前代码不再保留这些入口。

## 目标与设计决策

保留 OURO 的模块化单体、现有分层和状态所有权，优先整理 Application、TUI 与插件宿主内部的职责聚集。
通过完整定义迁移、内聚模块分组和明确的契约入口，让维护者能够定位一项能力、理解它依赖什么、判断修改会影响谁。
未来扩展以既有端口和真实变化方向为依据，不预建通用框架。

本计划的完成标准是定位成本降低、修改范围可预测、依赖和兼容性可验证，不以目录数量、文件平均长度或抽象数量评价架构。
保留合理的小模块，也允许暂时保留有明确状态所有权的长类。不能为了达到行数目标引入 mixin 链、通用 context 对象或转发层。

现行行为与不变量仍以[架构总览](../../architecture/README.md)、[领域规格](../../specs/README.md)为准。
实现遵循[编码准则](../../development/coding-guidelines.zh-CN.md)，验证门禁由[贡献指南](../../../CONTRIBUTING.md)维护。
本文保留经复审的实施设计与迁移归属；实际交付、验证证据和未验证范围见文末。当前组织以现行架构说明为准。

## 实施范围

| 类别 | 本计划中的处理 |
|---|---|
| 整模块、完整类、完整函数、配套常量迁移 | 实施；保持定义内容、签名、装饰器、校验和执行顺序 |
| 多职责文件拆分、同一能力文件聚合 | 实施；先核对实际调用和模块全局引用，再确定迁移单元 |
| 导入、显式导出、测试路径、文档链接、打包包含规则 | 随迁移同步调整；公开兼容入口保留同一对象身份 |
| 新子包的依赖约束与迁移兼容检查 | 实施；检查真实边界，不用行数阈值或快照锁定偶然布局 |
| 巨型类拆成协作对象、替换工作流继承链 | 不在本次实现；需另行审查状态所有权和调用方式 |
| 改变 eager/lazy 导入策略 | 不与搬迁混做；属于可观察的初始化行为调整，另立专项 |
| 算法、SQL、调度、事务、锁、取消、资源释放、重试和缓存变化 | 不实施；发现问题另列任务 |
| 新功能、协议版本、数据迁移、新依赖、新发行包 | 不实施 |
| 全仓 `src/` 迁移、统一 `bootstrap/` 或 `platform/` | 不作为本计划交付项；确有独立收益时另立工程任务 |

“行为保持型结构重构”与“仅移动定义”不是同一风险等级。发现某项拆分必须修改方法职责、传递整个服务对象或改变资源生命周期时，
将该项移出本计划，而不是借组织调整扩大实现范围。

## 审查证据与适用范围

基线扫描覆盖主包、升级包、主题包和测试的 Python 文件；行数包含注释、空行。主包统计如下：

| 范围 | Python 文件数 | 物理行数 | 组织特征 |
|---|---:|---:|---|
| `ouro` | 458 | 129,596 | 31 个文件超过 800 行，16 个超过 1,500 行 |
| Application | 67 | 33,200 | 67 个文件直接位于包根目录 |
| TUI | 60 | 30,585 | 54 个文件直接位于包根目录 |
| 插件宿主 | 56 | 15,875 | 安装、代次、贡献适配与 worker 并列 |
| Core | 64 | 8,687 | 主要为较小的记录、协议和语义模块 |
| 测试 | 396 | 131,209 | 部分场景集合高度集中 |

主要证据：

- [RuntimeService](../../../ouro/application/runtime/service.py) 所在文件 4,903 行，主类 132 个方法；它已经使用 RunLifecycle、RunResources、输入队列等明确协作者，不应另造一套运行框架。
- [TUI App](../../../ouro/tui/app.py) 6,155 行，[Controller](../../../ouro/tui/controller.py) 2,681 行；界面功能通过七层 OURO 自有类继承串联。
- [Widgets](../../../ouro/tui/widgets) 3,790 行，包含可独立搬迁的 Composer、ConversationView、审批和问答面板等完整类。
- [插件 API](../../../ouro/plugins/inprocess) 混合模块加载、上下文构造和贡献转换；[worker](../../../ouro/plugins/worker) 混合 RPC 客户端、实例生命周期与编解码。
- [工具批次执行](../../../ouro/application/tool_calls.py) 中 `execute_batch` 为 610 行，闭包共享检查点、序号和锁；本计划不拆开该事务协作单元。
- [架构测试](../../../tests/architecture/test_boundaries.py) 已保护顶层依赖；部分专项检查使用固定文件名或非递归扫描，需要随迁移更新。

首次审查在同一提交运行了架构与 Core 测试，结果为 **316 passed**。独立解释器中，导入 `ouro.application.dto` 加载了 250 个 OURO 模块，
并连带加载 Runtime 与两种 Session 存储；导入 `ouro.sessions.base` 也加载两种后端。原因包括父包的集中重导出。
这些结果说明加载耦合，不构成启动耗时或内存改善的证据；本次文档复审未重复运行功能测试。

显式顶层 import 图未检出模块循环；加入函数内 import 后，插件中出现包含 12 个模块的强连通分量，以及 `package` 与 `mcp` 的依赖环。
纯类型引用、包初始化产生的隐式加载和动态导入需要分别分析，不能把它们混成一个循环数量指标。

首轮候选分组曾通过显式导入图的静态检查，但这不足以证明分组有价值，也不能证明新包初始化安全。
本计划采用更小的迁移范围；实施前必须对实际迁移映射重新检查，不沿用候选图作为验收结果。

## 从核心约束推导组织方式

### 维护单元由变化原因与所有权决定

一个模块应能回答：承载什么规则或操作、拥有何种状态、依赖哪些契约、允许哪些调用者。
同一次变化通常共同修改的定义可以邻近放置；只是名称相似或都叫 model/service 的定义，不构成聚合依据。
运行事务、插件代次和 UI 订阅各有自己的生命周期，不能为缩短文件而切断这些所有权。

创建子包须有现存证据：多个协同模块，或一个文件内存在可独立理解的完整定义集合。一个内聚文件不需要额外包裹一层目录。
不创建空包，不强制每个能力拥有相同的 `models/services/ports/adapters` 层级，不增加通用 `utils`、`manager` 或 `common` 容器。

### 契约与实现分离，但契约仍有具体语义

Core 的记录和端口保持原有语义归属；Application 的 Host 视图契约与内部运行对象分开；SDK 保持版本化公开接口。
不把三类契约合并，也不复制一套字段相同的 DTO 来制造隔离。

Application 契约可以继续引用既有 Core 记录，以及配置、认证、插件的声明类型；不引用具体存储、RuntimeService、UI 或 worker 客户端。
引用声明类型不等于拥有该领域。父包可能产生的加载副作用单独记录，不能把定义位置调整描述为已经实现轻量导入。

### 依赖约束作用于实际模块

保持现有顶层依赖矩阵，按新子系统补充约束。区分声明、具体实现与装配模块，允许真正的协调者依赖必要协作者。
组合根的高依赖数量符合其职责；不能为了降低出度，把显式装配藏进注册表或服务定位器。

包级聚合图用于发现问题，不能代替模块分析。两个能力包分别引用对方的底层声明，不自动意味着运行期循环；
但具体实现相互导入、通过父包导出间接加载自身、或下层回调上层的隐藏状态，都需要明确处理。

### 泛化依靠可组合能力

未来 Host 复用应用服务，未来执行算法复用 Driver 端口，未来 Provider 复用协议边界，业务插件复用原生 Task/Run/Agent。
新模块应能沿既有边界增加。没有现实消费者时，不提前设计远程 RPC 门面、通用 Workflow DSL、依赖注入容器或全局插件发现机制。

## 首轮方案复审结论

| 首轮建议 | 最终决策与理由 |
|---|---|
| Core 全面拆为 foundation/contracts/execution 等八组 | 取消：多数小模块已有明确语义；当前证据不足以支持基础类型或策略家族的路径迁移，保持原有组织 |
| Application 每个职责都建立子包 | 收紧：多模块能力聚合，单一状态所有者仍可保留单文件；不预建 supervision/tools 等单文件包装层 |
| 将 DTO 集中为 contracts | 有限保留：只组织现有 Application 视图、请求和结果定义，不吸收运行状态对象、SDK 或基础设施实现，不扩大公开支持承诺 |
| 将所有本地代码迁入 bootstrap/platform | 本次取消：装配与 OS 原语的现有归属清楚，全局路径改名收益不足 |
| 将 src 布局作为最终必达结构 | 改为独立工程选择：已有安装产物 smoke 检查，本次优先解决包内职责；flat 布局本身不是不规范 |
| 通过轻量入口解决导入加载耦合 | 分开处理：迁移保留现有公开导出与延迟加载要求；改变初始化策略不能悄然并入搬迁 |
| 缩小 Runtime、App 等巨型类 | 本次仅移出独立定义；方法级拆解另列，不用 mixin、函数挂载或完整 self 参数模拟拆分 |
| 用无环分组图证明设计合理 | 改为必要但不充分的检查；补充所有权、包初始化、动态入口和扩展场景审查 |

## 目标组织与迁移归属

本节的包内路径省略共同前缀 `ouro/`，仓库顶层路径另行注明；下表保留迁移前来源与最终实施归属。
每批实施时列出旧路径、新路径、完整定义、实际消费者与保留入口。
原则是同一个定义只有一个实现位置；显式兼容导出不产生第二份实现。未列入迁移的领域维持当前布局。

### 仓库与顶层边界

保留 `ouro/`、`ouro_upgrade/`、`packages/tui-themes/`、`tests/`、`examples/`、`dev/` 和文档分类。
保留 `local_application.py`、`local_providers.py`、`deployment.py` 与本地 I/O、锁、进程、权限模块的位置。
`LocalApplication` 继续选择实现和拥有共享资源关闭顺序；未来增加部署方式时才为实际第二种组合根调整组织。

不创建统一的 infrastructure 大包。`providers`、`sessions`、`auth`、`plugins` 等现有边界具有独立变化原因，继续保持可见。

### Application：聚合协作模块，保留明确的协调者

| 目标 | 当前来源与迁移规则 |
|---|---|
| `application/contracts/` | `dto.py → base.py`、`event_models.py → events.py`、`agent_models.py → agents.py`、`session_models.py → sessions.py`、`supervisor_models.py → execution.py`；configuration、plugin、plugin_configuration、provider_auth、resource 的 `*_models.py` 分别迁为 configuration、plugins、plugin_configuration、provider_auth、resources 模块 |
| `application/contracts/models.py` | 从现有 `models.py` 搬迁完整 `AvailableModel`、`ModelReasoningState` 定义；不搬入 Catalog 实现 |
| `application/agents/` | `agents.py`、`agent_definitions.py`、`agent_execution.py`、`agent_recovery.py`、`agent_tree.py`；分别承载 service、definitions、execution、recovery、tree |
| `application/sessions/` | `sessions.py`、`session_history.py`、`session_editing.py`、`session_recovery.py`；分别承载 service、history、editing、recovery |
| `application/runs/` | `runs.py`、`run_continuations.py`、`run_ownership.py`、`run_drivers.py`、`run_turns.py`、`recovery.py`；保留现有 RunLifecycle、续接、所有权、Driver/Turn 和恢复职责 |
| `application/runtime/` | `runtime.py`、`runtime_environment.py`、`runtime_configuration.py`、`run_resources.py`；分别为 service、environment、configuration、resources |
| `application/inputs/` | `queued_inputs.py`、`user_inputs.py`、`file_inputs.py`、`skill_activations.py`；保持接收、准备与队列语义 |
| `application/context/` | `compaction.py`、`summary_compaction.py`、`media_context.py`、`prompt_context.py`；保持请求上下文与压缩协作 |
| `application/models/` | `models.py` 的 Catalog 与辅助函数、`model_generation.py`、`model_requests.py`；分别为 catalog、generation、requests |
| `application/observations/` | `session_observation.py`、`session_event_stream.py`、`session_state_subscription.py`、`session_content.py`、`runtime_activity.py` |
| `application/plugins/` | `plugins.py`、`plugin_commands.py`、`plugin_configuration.py`；分别为 service、commands、configuration |
| `application/configuration/` | `configuration.py`、`configuration_views.py`；保留 Facade 与投影职责 |
| `application/provider_auth/` | `provider_auth.py`、`provider_auth_bindings.py`；与原生 auth 包的端口和凭据实现区分 |
| `application/tasks/` | `tasks.py`、`task_execution.py`、`task_scheduler.py`；不新增调度状态 |
| `application/transfer/` | `data_transfer.py`、`configuration_transfer.py`、`session_transfer.py` |
| 保留独立模块 | `supervisor.py`、`tool_calls.py`、`session_controls.py`、`events.py`、`observers.py`、`resources.py`、`harness.py`、`usage.py` |

Application 契约包只容纳已有定义；内部候选记录被迁入不意味着它自动成为长期公共 API。公开范围依据文档、显式导出与已支持消费者确认。
跨模块只为类型需要依赖的记录，应从定义模块导入，避免通过服务门面获取。

必须保持：

- `runs` 不反向依赖 RuntimeService 或 RuntimeSupervisor。`run_resources.py` 属于 Runtime 的进程内资源所有权，迁入 `runtime/resources.py`。
- 输入队列不取得 RuntimeService 或 Supervisor 的控制权；模型请求执行不取得完整 Runtime 对象。
- Session 观察读取权威状态并维护订阅；不接管事务、执行许可或 Run 终结。
- Supervisor 持有任务和容量，RunLifecycle 持有持久转换；Agent 与自动任务继续复用现有执行链。
- 全部子包避免无必要的全量重导出；兼容入口不能被迁移后的内部实现当作依赖捷径。

### TUI：先拆完整 UI 单元，不重写 App 继承和状态

`app.py`、`controller.py`、`entry.py` 保持当前角色。先从聚合文件迁出完整定义，再聚合相关界面模块：

| 目标 | 迁移内容 |
|---|---|
| `tui/view_models.py` | Controller 中完整的 UI 状态、配置、Bootstrap 和视图结果记录；不包含 TuiController 和应用服务调用 |
| `tui/status.py` | 现有系统、用量、MCP 和插件状态投影函数；不启动运行或后台任务 |
| `tui/conversation/` | ConversationView、正文/Reasoning/工具呈现控件、Transcript 投影，以及 conversation_history 中的记录、渲染函数和完整历史控件 |
| `tui/composer/` | Composer、CommandPopup、QueuedInputPanel、输入草稿、附件编辑与选择、Skill 引用选择；保持控件消息和交互顺序 |
| `tui/interactions/` | ApprovalPanel、InteractionBar、AskUserPanel、PlanConfirmationPanel 及既有表单投影 |
| `tui/menus/` | Choice 记录、ChoiceScreen、EditableChoiceScreen、MultiChoiceScreen、ConfirmationScreen 与 menu_* 模块；完整类先迁移 |
| `tui/widgets/` | BrandHeader、ActivityIndicator、StatusLine 等真正跨界面使用的小控件；不再承载整套会话和输入业务 |
| `tui/features/` | 按 providers、plugins、tasks、configuration、prompts、transfer 聚合现有 screen/workflow；每个 feature 只放自己拥有的界面内容 |
| 保持现有位置 | `welcome/`、`theme.py`、偏好、渲染基础和终端/平台模块；本次不再增加 rendering/platform 外壳 |

欢迎输入表面的 WelcomePanel 与 Composer 的生命周期保持不变，实施时归入 `composer/surface.py`，不归入可选欢迎页主题实现。
`welcome` 通过 view_models 取得状态定义，不依赖 TuiController；现有 Controller 类型入口在兼容清单要求时保留显式别名。

迁移 conversation_history 时先分离完整记录、纯呈现函数与控件类；不移动类内持有异步渲染任务、索引或滚动锚点的方法。
各 feature 的工作流类保持当前继承关系、方法名和装饰器。此次聚合不能被表述为已经消除了七层继承债务。

保持 Textual 消息类型身份、MRO、action 名称、selector/ID、焦点行为、CSS 文本以及 worker group/cancellation 行为。
不借搬迁将内联 CSS 改为外部加载文件。`ouro.tui.welcome` 与主题扩展使用的公开接口保持可用。

### 插件宿主：分开基础定义、具体适配与装配

| 目标 | 迁移内容与约束 |
|---|---|
| `plugins/inprocess/` | 从 api.py 分出模块加载、上下文构造、具体 SDK 适配和贡献转换入口；加载器不依赖贡献类型清单 |
| `plugins/worker/` | JsonRpcWorkerClient、WorkerPluginInstance、编解码、贡献适配与现有 worker_* 能力处理；宿主端与 SDK worker 端保持分离 |
| `plugins/contributions/` | PluginContributions、PreparedContributionSet 等记录；贡献准备、投影、契约计算及 contribution_types 汇总；各自分模块 |
| `plugins/catalog/` | generation gate/协议、快照与租约、绑定包装、构建函数；同一快照与租约的所有权不拆开 |
| `plugins/lifecycle/` | runtime、generation、generation_resources、readiness、candidates；Runtime 主类完整迁移，不分散 active/retired 状态 |
| `plugins/installation/` | package、installer、store、sources、scaffold 等包管理模块；保留当前文件发布与包保留语义 |
| `plugins/configuration/` | configuration、configuration_schema、configuration_values；凭据与公开声明按现有依赖和兼容入口处理 |
| 保持明确的根模块 | manifest.py、builtin.py、dependencies.py，以及没有足够聚合收益的原生能力绑定模块；不建立泛化 bindings 大包 |

贡献类型清单保持现有封闭清单和注册顺序，不改成自动发现或导入副作用注册。具体适配器只依赖自己的数据、端口和所需转换；
汇总清单依赖具体适配器，转换/构建入口使用清单。先用完整定义迁移消除“只为拿一个类型而依赖整套实现”的边。

ContributionCoordinator 或 WorkerPluginInstance 的方法如果仍需原有清单，保留其行为。无法通过完整定义迁移消除的环，
以具体模块、原因和后续拆解条件记录；不新增局部 import 掩盖环，也不在本批注入一个通用注册中心。
`package ↔ mcp` 的校验/路径依赖同样先尝试将既有完整错误、路径解析和静态描述校验定义放到低层模块，不能改写安装时的完整校验流程。

### 协议、持久化、工具与 CLI

| 范围 | 目标组织与保护点 |
|---|---|
| Providers | 按现有线协议建立 openai_responses、openai_chat_completions、anthropic_messages 子包，收纳 adapter、codec 和已有 stream/items/errors；不再套一层 protocols，不移动已内聚的共享 transport/retrying/factory 模块 |
| Session 存储 | base、portable、catalog/index、layout 和锁保持共享；`backends/files/` 收纳 LocalSessionStore、文件提交与文件索引/日志；`backends/sqlite/` 收纳 SQLiteSessionStore 与 SQLite 提交；保持后端中立契约与单一事务所有者 |
| Session 传输 | 保持共享归档校验与后端专属导入的差别；不统一 SQL 和文件提交流程，不顺便重写迁移工具 |
| Tools | `builtin/` 收纳现有 builtin_*；schema、registry、executor、batch_executor 等保持独立；Agent 控制工具仍按当前能力绑定，不能加入无条件 builtin_tools 注册 |
| CLI | 新建 `parsers/`，按命令族迁移完整 `_add_*_parser` 函数及专属参数解析；build_parser 保持显式注册顺序；命令处理模块不进行无关改名 |
| Config | `config/providers/` 收纳现有 providers.py、provider_codec.py、provider_runtime.py、provider_template_store.py，分别为 manager、codec、runtime、template_store；provider_models.py、provider_headers.py 等共享声明和已内聚的 provider_templates/ 保持位置；保持公开入口、配置格式与 mutation 锁范围 |
| Auth、MCP、Automation、Billing、Resources、Skills、Transfer | 保留现有领域包；没有明确多职责证据的模块不迁移 |
| 升级包、SDK、主题扩展、内置插件 | 保留现有发行/导入边界；不修改 SDK 版本、worker 协议、主题依赖关系或升级格式 |

协议包按线协议划分而非账户产品划分。兼容供应商优先复用既有协议，认证驱动仍留在 auth；不为每个供应商品牌复制 codec。
SessionStore 的完整实现类先整体放入所属后端；不能为了缩短 1,600 行以上的存储类拆散线程内事务状态。

### Core：保留现有语义模块

保留 base、errors、messages、state、provider、tools、资源、任务、Agent 与请求等现有语义模块。
不创建全局 foundation/contracts 容器，不为了消除文件名中的前缀而搬迁所有类型。

prompt_policies、tool_policies、turn_policies、continuations 目前分别表达不同阶段的策略语义，且已有独立记录和校验。
将它们统一放进 policies 目录主要改变导航与导入路径，尚未显示出足够的维护收益；本次不迁移，也不提取共同 Policy 基类。
保持 Observer、Context Provider 与策略的现有职责；保留“语义策略不依赖观察者”的架构测试。

Core 模块只有在后续具体变化证明存在边界问题时再调整；64 个小模块本身不足以支持全面路径迁移。

## 兼容性与导入纪律

### 先建立有依据的兼容清单

逐项记录已文档化接口、显式 `__all__` 导出、SDK 示例与插件使用入口、主题扩展接口、CLI entry point、字符串动态入口、包资源和旧测试 patch 位置。
不把仓库内部所有 import 都升级为永久公共 API，也不把未在仓库发现消费者视为公开入口可以随意删除的证据。

内部调用统一更新到新的定义模块。需要保留的旧入口只显式导出同一个函数/类，不包装调用、不复制实现、不维护两份注册表。
将普通模块替换为同名包时，移除旧实现文件，检查安装产物中不存在同名文件/目录残留。
内部兼容路径随迁移清理；外部公开路径只能按既有兼容政策处理，不能为追求“零兼容层”删除。

检查 Pydantic schema、模型身份、装饰器与注册副作用、模块全局依赖和可观察日志来源。
`__module__`、`__qualname__` 或文件路径是否需要保留，由真实的序列化、协议、动态加载和支持契约决定，不机械冻结所有 Python 内省属性。

### 包初始化与延迟导入

新子包保持最小初始化，不从下层导入整个服务实现，不用星号导出；已有公开重导出根据兼容清单保留。
读取 `application/contracts` 仍可能执行现有 application 包初始化，本次不能宣称已经解决全部传递加载。

保留当前 CLI、MCP、Pillow、可选主题和平台模块的延迟加载边界。不要新增通用 `__getattr__` 分发器、sys.modules 别名系统或运行时扫描注册。
如果轻量入口必须改变既有 eager 导出，则另列初始化专项，覆盖导入失败时点、对象身份、循环依赖与公开属性访问。

### 内置插件源码身份

[内置插件契约计算](../../../ouro/plugins/builtin.py)将插件目录内 Python 文件的相对路径和字节摘要计入契约。
[Driver 契约解析](../../../ouro/plugins/driver_contracts.py)会核对持久化实现身份。只修改 import 也会改变内置插件契约。

本计划保持各内置插件根目录内源码、相对布局和声明运行资源的字节内容不变，保持 SDK 路径以及 `ouro.plugins.builtin`、`ouro.plugins.manifest` 所需入口。
全仓自动 import 替换必须排除这些文件；迁移前后分别计算源码摘要、builtin contract hash 和相关执行契约进行对比。
不通过改哈希算法、更新保存的身份或放宽恢复检查来使测试通过。

### 行为保护边界

不得改动事件名称与顺序、CLI 参数/default/退出码、配置/数据库格式、权限和预算策略、请求编码、UI 交互、锁顺序、事务范围和资源关闭顺序。
定义搬迁后若原有 monkeypatch 失效，先核对被测定义的实际查找位置；只修正测试的定位，不移除断言或掩盖回归。
AST 比较可用于确认没有夹带逻辑改写，但不是行为等价证明；模块全局查找、父包初始化和动态加载仍需运行验证。

## 未来扩展的设计验收

依据[路线图](../roadmap.md)、[Desktop Host 计划](../desktop-host.md)和[高级智能体能力](../advanced-agents.md)进行设计走查，不在此次实现这些功能。

| 扩展场景 | 预期主要修改位置 | 不应扩散到 |
|---|---|---|
| 新增 Desktop Host | 新 Host、所需 Application 用例与 DTO、组合根 | TuiController、Core 状态机、存储格式 |
| 新增兼容供应商 | 模板、认证与路由装配 | 同线协议的复制实现 |
| 新增线协议 | 独立 Provider adapter/codec、能力声明、工厂接入 | Core 对私有 HTTP/SSE 或账户协议的解释 |
| 新增存储后端 | 后端实现、组合根、共同契约测试 | Host 中的后端判断和另一套恢复规则 |
| 新增执行算法 | Driver 或插件实现，已有模型/工具端口 | Runtime 的权限、预算、提交和取消语义 |
| 新增插件贡献类型 | 明确的声明、适配、协议与装配位置 | 全局动态发现机制或隐式跨插件状态 |
| 长期记忆 | 独立业务插件、私有存储、服务与上下文贡献 | 核心 Runtime 和通用 Session 业务字段 |
| 依赖工作流 | 插件业务图与原生 Task/Run/Delegation 引用 | 第二套调度账本、将 AgentTree/Goal 改成万能状态机 |
| 新增 TUI 功能 | 对应 feature 和必要应用用例 | 更长的工作流继承链 |

扩展边界要求修改点明确，不要求所有新贡献都能零修改框架。新的后端或 Host 仍需满足真实的事务、资源和交互契约，
目录可容纳扩展不等于当前已经支持远程执行或任意存储。若扩展场景要求改变接口，单独设计该能力，而不是提前加入空端口。

## 实施批次与进入条件

批次按下表顺序推进。每批只处理一个可验证的组织单元；大型批次继续按协议、feature 或应用能力拆成小提交。
迁移清单、代码、原测试的路径调整、对应架构规则和链接在同一提交内闭合，不能留一段不可导入的中间状态。

| 批次 | 实施内容 | 退出条件 |
|---|---|---|
| P0：基线与保护 | 列出公开入口、符号迁移映射、动态路径、包资源、内置插件摘要；让相关架构扫描递归覆盖；记录现有循环与导入边界 | 基线可重复，现有失败与本次变化可区分；没有靠扩大 allowlist 放过新依赖 |
| P1：独立定义 | TUI 视图记录、状态投影、完整控件与菜单；CLI parser 函数 | 无类内状态重分配；UI 消息、动作、参数和原场景断言保持 |
| P2：适配器内聚 | Provider 协议、工具 builtin、Session 后端、Provider 配置模块 | 共同契约、协议输出、配置 mutation 与后端事务不变；基础安装正常 |
| P3：插件内部组织 | 先 loading/codec/数据定义，再具体适配与贡献清单，再 Catalog、生命周期和安装模块 | 公共 SDK、manifest、worker 协议及 builtin hash 不变；不存在新引入的隐式注册或未解释循环 |
| P4：应用能力聚合 | 先 contracts 和输入/查询等低层定义，再运行协作、服务与 Runtime；同步现有公开导出 | Core 与 Application 无新增运行期循环；协调者和所有权保持；兼容入口未被内部反向依赖 |
| P5：测试与文档收口 | 按已迁能力拆分超大测试文件、整理就地 fixture 与文档路径；检查未迁移领域的依赖保持 | 测试未因目录、fixture 范围或收集方式丢失场景；没有遗留失效入口与现行架构描述 |
| P6：交付核验 | 定向回归、静态检查、安装验证与残余边界审查；全量回归交由使用者手动执行 | 组织迁移完成，明确记录定向结果、未验证平台、保留的巨型类与导入耦合；不宣称全量回归通过 |

P1 的完整定义提取与 feature 聚合分开审查；P3 的底层定义先行，不能先移动汇总清单再用局部 import 补洞。
P4 不重写 RuntimeService 构造方式；只因迁移后发现已有重复代码，不足以在本批合并它。

每个提交提供：迁移原因、旧/新路径及保留入口、依赖变化、实际验证结果和未验证范围。
当结构变更需要额外状态、通用抽象或协议改动才能继续时，保留当前可运行边界，将该部分转入后续专项。
回退以独立提交为单位；此次迁移不应产生需要逆向迁移的用户数据。

## 验证矩阵

优先复用现有行为测试；只有迁移新增的兼容与依赖风险缺少保护时才添加检查。
不新建重复的全量快照体系，不将机器耗时阈值当作模块化验收条件。

| 范围 | 必须保留或验证的证据 |
|---|---|
| 架构 | 相对导入解析、递归子包覆盖、Core/SDK 隔离、Host 不越过应用边界、codec 不依赖认证/传输；扫描覆盖 ouro_upgrade 和主题扩展各自相关边界 |
| 导入与安装 | 新进程执行已有 startup/import 检查；旧公开入口与新定义对象一致；动态字符串入口有效；wheel/sdist 包含完整资源且无旧实现残留 |
| CLI | 现有 help、entry、参数默认值、错误码与配置/会话命令测试；参数注册顺序不变 |
| Session 后端 | 共同 Store contract、原子提交、undo、索引查询、传输、恢复、并发和 File/SQLite 双后端回归 |
| Runtime/Agent/Task | 输入受理和去重、排队所有权、工具等待、续接、取消、Agent 结果交付、RunResources 释放、Supervisor shutdown |
| 插件 | 启停与失败清理、generation/lease、依赖启动图、worker 双向请求、Driver 恢复、SDK 示例、builtin 源码与契约身份 |
| TUI | 控件组件与 Host 集成测试；输入/草稿、审批、历史分页、切换、观察、主题和退出；真实终端外观按既有测试策略核验 |
| 测试拆分 | 迁移前后场景与参数化用例对应；fixture 发现、共享范围、子进程入口和 patch 查找位置正确 |

定向验证引用实际受影响测试。例如插件使用现有 startup_graph、service_lifetimes、driver_contracts；运行侧使用 user_input_admission、run_ownership、run_resources；
UI 使用现有组件、session/input/history、live_session_attachment 等场景。测试文件也可能迁移，执行时以更新后的测试位置为准。

完整命令与 coverage 门槛统一遵循[贡献指南](../../../CONTRIBUTING.md)，不在本计划复制另一套可能失效的命令。
base 与 themes 安装均执行脱离源码目录的发行包 smoke；实际 Windows、Linux、macOS 验证分别记录，不能用一次本机成功代替。
本计划没有授权使用真实个人 home 作为测试数据，迁移验证使用隔离夹具与临时目录。

## 完成标准与保留事项

全部满足后，才能将本计划标记完成：

- [x] 每个实际迁移单元有明确职责、唯一实现位置、依赖和兼容入口；未建立空目录或无用途的通用抽象。
- [x] 已列入 P1–P5 的组织迁移与必要文档/测试调整完成；未实施项逐条说明原因，不通过改指标掩盖遗漏。
- [x] 内置插件字节摘要与契约身份不变，公开 SDK/CLI/主题/数据与协议契约保持兼容。
- [x] 显式模块图没有新增顶层循环，导入与安装检查通过；既有初始化耦合和局部导入循环有具体归属与后续条件。
- [x] 方法体、事务、锁、取消、调度和资源所有权没有夹带改写；导入变化的可观察影响已经验证。
- [x] 未来扩展场景走查能指出明确落点，不要求新增 Host/算法复用 TUI 或复制运行事实。
- [x] 定向回归、静态检查、安装验证和平台结果有实际证据；依照使用者指示不执行全量回归，未验证范围明确列出。
- [x] 代码与现行架构说明一致，所有路径和索引更新；完成后按文档规范归档计划。

以下问题保留为后续独立专项，不以当前计划完成来宣称解决：

| 专项 | 进入条件 | 设计约束 |
|---|---|---|
| RuntimeService / AgentService 方法级分解 | 完成组织迁移后，明确哪些状态和操作能归同一独立所有者，并有行为回归保护 | 不创建第二套 Run/Delegation 状态；不传整个 Runtime 作为万能依赖 |
| TUI 工作流组合化与 Controller 收敛 | 逐项列出工作流使用的 UI 状态、Textual 装饰器与 Host 操作，再验证可替代边界 | 不继续加继承层；不建立仅转发到完整 App 的伪服务 |
| 工具批次与存储巨型类拆解 | 找到完整事务/资源边界，可以同时说明共享锁、检查点与异常收尾 | 不按行数切方法，不拆散闭包和线程内事务状态 |
| 轻量导入入口 | 明确需要支持的轻量消费者与现有公开导出兼容要求 | 单独验证初始化时点；不把符号迁移误当成传递加载已经消除 |
| src 布局或本地装配目录调整 | 有独立打包、部署或开发体验收益及迁移成本说明 | 不改变 import 包名、命令和资源位置契约，不与本次源码分组捆绑 |



## 实施结果

P0–P6 的组织迁移、导入调整、兼容入口、测试拆分、现行文档与交付核验已完成。
仓库继续使用根目录 `ouro/` 布局，`ouro_upgrade/` 与主题扩展的发行边界保持不变。
未修改依赖版本、CLI entry point、配置或数据库格式、SDK 协议及内置插件内容。

| 范围 | 最终结果 |
|---|---|
| Application | 按 contracts、agents、sessions、runs、runtime、inputs、context、models、observations 等能力聚合；保留原有状态与资源所有者 |
| TUI | 迁出 Controller 视图记录和状态投影；完整控件按 conversation/composer/interactions/menus/widgets 分组；screen/workflow 按 feature 聚合 |
| 插件宿主 | 拆分加载、上下文、贡献适配、worker RPC/codec、准备/投影/契约、Catalog、代次和安装；注册清单与顺序保持 |
| 适配器与 CLI | Provider 按线协议、Session 按后端、builtin tools 按实现聚合；CLI 参数函数按命令族拆分，显式注册顺序保持 |
| 导入 | 内部引用使用定义位置；首轮曾为 89 个符号保留旧路径别名，后续按使用者要求移除中转文件和包入口兼容导出 |
| 测试 | 原 `tests/tui/test_app.py` 的 234 个测试函数按场景分为 22 个文件；共用构造与 Provider 单列；既有 fixture 生命周期保持 |

当前职责与维护边界见[代码组织与扩展边界](../../architecture/code-organization.md)。
文件数增加来自职责拆分、包入口和必要兼容导出，不以文件数量减少或平均行数下降作为成功指标。

### 依赖与内容核对

- 主包 2,567 个完整顶层函数/类定义的 AST 多重集一致；比较时只排除导入语句。
  817 个模块级赋值/类型别名的 AST 多重集一致，兼容用的 `__all__` 单独验证。
  这证明定义未夹带逻辑编辑，不等于模块初始化及全局查找的形式化行为等价证明。
- 内置插件的 18 个受保护文件字节摘要一致；三个实际 builtin contract hash 与迁移前一致。
  Core、SDK、依赖锁文件和发行配置无变化。
- 147 个迁移的 Pydantic 模型均可生成 schema，迁移前后结果完全一致。
- 显式 import 图（解析相对路径、排除 `TYPE_CHECKING`）迁移前后都没有顶层强连通分量。
  加入函数内 import 后，原安装包与 MCP 的二模块环消除；插件贡献路径保留一个九模块强连通分量。
  该图不模拟 Python 隐式父包初始化和任意动态导入，不能用于声称所有加载路径均无耦合。
- 保留的分量包括 `contributions/{contracts,inventory,preparation,preparation_adapters}`、
  `inprocess/contributions`、`tool_groups`、`worker/{adapters,instance,tool_groups}`。
  原因是协调者通过原有局部 import 访问封闭清单，清单又选择具体适配器。
  本次没有新增局部 import 规避循环；后续需在保持生命周期和注册契约的前提下独立设计协作边界。
- 内部类的 `__module__`、traceback 路径和使用 `__name__` 的 logger 名称随定义位置变化；
  公开别名保持对象身份，未为元数据伪造旧模块或引入 pickle/持久数据格式迁移。

### 实际验证

环境为本机 Windows、Python 3.14.6。以下为实际通过的定向批次，存在重叠，不累加为独立用例数。
执行使用现有虚拟环境、临时 home 和四进程定向调度；没有执行全量 pytest 回归或全量 coverage 门槛。

| 验证范围 | 结果 |
|---|---|
| Application：输入受理、所有权、资源释放、Agent 恢复/交付、观察、模型、插件配置、任务、传输、Supervisor、装配与 Runtime | 408 passed |
| 架构与会话历史组件 | 75 passed |
| Provider 工厂与架构 | 67 passed |
| worker 双向 RPC 的进程查找位置修复 | 6 passed |
| 显式公开导出的同一对象身份 | 89 passed |
| 架构、会话输入、Provider 表单、运行资源、宿主插件命令、Responses/模板/认证与 worker results | 385 passed |
| 架构、输入受理及导航、跨 Host 观察、菜单、插件工作流、会话控制与任务界面 | 262 passed |
| CLI help/entry/错误码与最后调整的会话输入、历史、worker 测试 | 54 passed |
| 全仓仅收集测试并比较参数化场景 | 6,316 → 6,408；原场景无缺失，新增 89 项兼容检查和 3 项边界检查；未执行全仓测试 |
| Ruff lint / format | 通过；1,019 个 Python 文件格式检查通过 |
| mypy `--no-incremental --platform win32`（主包与主题扩展） | 597 个源码文件通过 |
| compileall 与 `git diff --check` | 通过 |
| 主包、主题扩展 wheel 与 sdist | 构建通过 |
| 构建产物与首次导入 | wheel/sdist 中 615 个源码与资源文件逐字节对应工作区，无旧模块/同名包冲突；15 个关键模块各自新进程首次导入通过 |
| 发行包隔离安装 smoke，base 与 themes 分别运行 | 两种安装均通过，覆盖 metadata、CLI、实际安装路径、TUI 启动、主题选择和偏好保持 |

其他迁移批次还执行了协议、双 Session 后端、配置、工具及插件生命周期相关定向场景。
发现的失败均先定位迁移后的查找位置：包括工厂动态字符串、monkeypatch 所在模块和拆分测试的间接 helper 导入；
修复保留原断言与业务实现，相关失败用例已在后续批次通过。

本次调整的文档路径与索引已核对，没有新增失效本地链接。旧的 2026-10-12 插件平台归档仍保留
迁移基线中已有的 SDK v3 源码链接；该历史版本入口不属于本次代码组织迁移，未将其改指当前 v5 协议。

### 未验证范围与保留事项

- **全量测试与 coverage 门槛没有执行，按使用者要求留待其手动运行。** 测试收集检查不是全量行为回归。
- 未在真实 Linux/macOS 环境运行，也未进行真实终端视觉检查或真实供应商账户验证；Windows 定向结果不代表跨平台验收。
- RuntimeService、AgentService、OuroTuiApp、完整存储类及工具批次内部方法保持原实现；七层 TUI 工作流继承和父包 eager 导出仍保留。
  这些均是方案事先排除的方法/初始化专项，不以本次目录重构宣称已经解决。
- 已修改文件仍留在工作区供审查，没有创建提交、修改用户数据或执行发布。

## 移除历史兼容中转

使用者在首轮组织迁移完成后明确要求移除全部历史兼容引用中转。此次收口取消旧路径兼容承诺，
实际定义、函数和类实现不变；项目继续使用根目录布局，只执行定向回归。

| 范围 | 处理 |
|---|---|
| Application | 删除 agent_definitions、agent_models、agent_recovery、agent_tree、data_transfer、run_continuations、session_event_stream、skill_activations 共 8 个中转文件 |
| 插件宿主 | 删除 adapters、candidates、contracts、contribution_types、generation_resources、scaffold 共 6 个中转文件 |
| Tools | 删除 builtin_agents、builtin_ask_user、builtin_glob、builtin_grep、builtin_ignore、builtin_todolist 共 6 个中转文件 |
| TUI | 删除 input_drafts、plan_confirmation 共 2 个中转文件 |
| 子包入口 | 清除 application.agents、plugins.catalog、三个 Provider 协议包及 tools.builtin 共 6 处兼容重导出；保留子包标识与职责说明 |
| 测试 | 删除仅验证旧入口别名的 test_public_exports.py，共 89 个参数化用例；这些用例对应的兼容承诺已取消，原业务测试保留 |

移除前扫描源码、测试、示例与开发脚本，没有调用方仍经上述中转模块导入，因此无需再次批量改写业务导入。
领域包的正常公开 API 保留。`plugins/compatibility.py` 是社区插件格式检查的实际实现，也继续保留。

验证结果如下，均在本机 Windows 环境完成：

| 验证 | 结果 |
|---|---|
| 架构、CLI 入口/help、Provider 工厂、Catalog 绑定/启动图、内置工具、Agent 定义、草稿、计划确认与 TUI 生命周期 | 188 passed |
| 全仓仅收集测试 | 6,408 → 6,319；减少的 89 项全部来自被删除的旧入口兼容测试，原业务场景无缺失；未执行全仓测试 |
| Ruff lint / format | 通过；996 个 Python 文件格式检查通过 |
| mypy `--no-incremental --platform win32` | 575 个源码文件通过 |
| 源码与导入检查 | 无旧中转模块引用；22 个旧模块不可导入，6 处子包旧符号别名不可访问；内置插件文件摘要不变 |
| wheel/sdist | 构建通过，580 个源码及资源文件与工作区一致；不包含已删除的模块或包入口兼容导出 |
| 发行包隔离安装 smoke | base、themes 均通过 |
| 文档及差异检查 | 无指向被删除兼容文件的本地链接，`git diff --check` 通过 |

没有执行全量回归或 coverage 门槛；继续由使用者手动执行。此次不新增与目录快照绑定的替代测试，
现有架构规则、行为测试和安装检查保护实际使用路径。改动仍留在工作区，未创建提交。

## 全量结果回收与定向修复

使用者自行运行 `uv run --frozen --extra themes pytest -n 16 --dist worksteal --durations=20`，
提供的结果为 **6 failed、6,307 passed、6 skipped**。本轮仅根据该结果定位并修复受影响测试，未再次运行全量测试。

| 根因 | 修复与保护的契约 |
|---|---|
| 5 个渲染场景仍在 `conversation.history_rendering` 替换函数，但 `ConversationHistory` 从自己的模块全局引用调用该函数；测试门闩因此没有生效 | 三个测试文件改在 `conversation.history` 的实际查找位置注入；保持完成时折叠与摘要同时发布、首帧为完整最新窗口、正文游标独立于后台渲染等原有断言 |
| 导航场景为准备一个已完成会话，将真实文件存储上的模型启动限制为 5 秒，且启动等待失败时缺少前置任务的明确收拢 | 以 TaskGroup 共同拥有运行任务与 Provider 完成握手；收到启动通知后结束流，等待真实 Run 完成。失败和取消会收拢任务，60 秒整体期限仅作挂起保护，不作为启动性能验收 |

修复前对附件列出的六个场景定向执行，结果为 **5 failed、1 passed**：五个错误注入位置可稳定复现，
导航用例的 5 秒超时未在该次较低并发的定向环境重现。修复没有调整生产逻辑、延长渲染用例期限、
删除业务断言、恢复兼容中转，或改变异步渲染、缓存、分页、持久化与观察游标的设计。

修复涉及 `tests/tui/test_execution_traces.py`、`test_history_windows.py`、`test_async_observation.py`、
`test_background_navigation.py` 四个文件。验证结果：

- 上述四个文件使用 `-n 4 --dist worksteal`：**97 passed**。
- 同一范围使用与附件一致的 `-n 16 --dist worksteal`：**97 passed**。
- 四个文件的 Ruff lint / format 检查通过，原业务断言保留。
- 修复前后主包 Python 源码摘要一致；本轮没有修改生产代码。

两次运行都是受影响文件的定向回归，不代表全量测试或全量 coverage 已通过。未创建提交。
