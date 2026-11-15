# 代码组织与扩展边界

> 文档类型：现行架构
> 配套约束：[架构总览](README.md)、[编码准则](../development/coding-guidelines.zh-CN.md)、[依赖测试](../../tests/architecture/test_boundaries.py)。

OURO 的主包与升级包位于仓库根目录下的 `ouro/` 与 `ouro_upgrade/`；主题扩展具有独立的
`packages/tui-themes/` 发行边界。主包按领域分层，各领域内部按实际能力和状态所有权组织。
目录不规定统一的服务模板，也不承担运行时注册或自动发现职责。

## 应用用例与运行所有权

`application` 聚合应用能力，Host 从这里取得用例和视图。`local_application.py` 选择具体实现，
装配共享服务并拥有整体关闭顺序；Core 的领域记录、端口和运行语义保持独立。

| 位置 | 职责与边界 |
|---|---|
| `application/contracts/` | Host 使用的请求、结果与视图；引用既有领域声明，不引用应用服务、Host 或具体存储后端 |
| `application/agents/`、`tasks/` | Agent 派遣及结果交付、自动任务领取及执行；复用原生 Session/Run |
| `application/sessions/` | 会话用例、历史、编辑和恢复 |
| `application/runs/` | 持久 Run 转换、执行所有权、续接、Driver/Turn 协作及恢复 |
| `application/runtime/` | Runtime 编排、环境和 RunResources；`extensions.py` 定义执行所需的能力、调用作用域及租约契约 |
| `application/inputs/` | 输入准备、策略评估、受理回执和队列；`admission.py` 拥有初始输入发布与受理事实查询 |
| `application/context/` | `preparation.py` 提供上下文构造与记录，`compaction.py` 组合默认压缩流程，`summary.py` 实现摘要生成 |
| `application/models/`、`observations/` | 模型查询和执行、会话快照与事件订阅 |
| `application/configuration/`、`plugins/`、`provider_auth/`、`transfer/` | 对应能力的应用用例及投影 |
| `application/supervisor.py`、`tool_calls.py` 等独立模块 | 任务调度、工具批次事务和各自的资源协作单元 |

RunLifecycle 拥有持久转换，Supervisor 拥有会话调度、执行队列和执行许可，Runtime 拥有单次运行协作。
Supervisor 的每个任务持有一种类型明确的执行操作；输入、继续、预留 Run、审批、回答和子任务恢复各自携带
所需参数，任务对象只维护共有的调度、完成与取消状态。Core 的运行转换函数拥有续接时的状态准备规则。
模型请求派发记录、Run 预算写入和已请求中止的状态结算归 RunLifecycle；Runtime 负责调用时机、
资源收尾与提交后的事件发布，避免两个服务分别维护同一类持久转换。
Runtime 通过装配的窄回调报告会话推进机会，不引用具体调度器或 Host。调度按当前队列事实安排执行，
TUI、AgentService 和任务协调器保留自己的业务身份、授权与结算。
`agents/budgets.py` 从 SessionStore 与用量账本读取共享预算事实；AgentService 拥有派遣、等待与结果交付的事务状态。
账本按有界批次逐 Run 核对派发计数与报告覆盖，只返回预算所需的原始消耗与完整性。
输入、观察和模型请求协作者不取得完整 Runtime 的控制权。`application` 与 `sessions` 包入口不加载服务或后端；
契约和实现从定义模块显式导入，会话契约可在不加载存储及运行服务的情况下使用。

`application/models/catalog.py` 定义模型目录契约、固定模型目录和原生模型投影。
`local_configuration.py` 装配 ConfigurationFacade 的本地配置协作者，持有文件配置解析与
ConfiguredModelCatalog，负责配置路由、adapter fingerprint、并发构造及关闭；Runtime 通过目录协议和
配置、提示词、技能查询回调取得当前能力。
AgentDefinitionService 显式接收默认模型路由查询，在调用时取得当前路由；Agent 配置不提供 Provider 配置的装配信息。
Provider 构造参数统一使用 `config/provider_models.py` 的 ProviderRuntimeOptions。

`application/configuration/live.py` 拥有配置保存、运行生效和失败补偿的完整流程。LocalApplication
将 MCP 协调、Skills 刷新和当前技能查询绑定到该用例。TUI 转换表单并呈现结果；离线 CLI 使用
ConfigurationFacade 修改配置，不启动运行环境。配置管理器拥有原子写入与锁边界。
压缩配置的在线保存由 LiveConfigurationService 解析所选模型的运行适配器并校验推理选项，再调用 ConfigurationFacade
保存持久配置。运行目录可以由嵌入宿主注入，其模型能力与磁盘配置各自保留约束；TUI 只转换表单和路由格式。

单条输入以完整的 QueuedInput 交给队列，队列负责受理事务、顺序和替换；
用户、任务、Agent 及插件入口拥有各自的身份和调度规则。Runtime 为待入队用户输入构造一次意图，
完成策略检查后附上检查凭据并提交；检查所用的扩展租约覆盖受理提交，在提交结束时释放。
插件命令的输入与 Harness 变更在同一事务中提交，保证整批操作的原子性。

用户输入约束由 `UserInputRequest` 表达。`RuntimeService.run` 与 Supervisor 受理共用该请求和新会话输入准备；
`run` 等待本次执行结果，Supervisor 受理返回持久回执并拥有后续调度。已受理输入通过 `run_pending_inputs`，
预留 Run 通过 `run_prepared` 进入内部执行边界。
`InputSessionSettings` 表达随输入提交的会话选项；队列受理和直接 Run 启动在各自的原子提交内应用同一规则。
策略检查在持久提交前完成；输入拒绝或受理提交失败不发布这些选项。
执行边界统一处理锁竞争时的排队、锁内重读、输入消费和 Run 收尾，执行入口分别管理用户请求、已受理输入和锁预留。

Runtime 通过 `RuntimeExtensions` 获取当前能力，通过 `RuntimeCapabilities` 持有一次决策的租约。
RunResources 拥有单次 Run 的工具选择：控制器可见范围、派遣能力上限与 Agent 控制工具在此组合。
控制器查询仍随 Run 所有权动态读取，释放运行资源后不保留控制器授权。
`PluginRuntimeResource` 与 `ExtensionCatalogLease` 分别实现这两个契约，插件层拥有代次资源及安装、激活和候选发布状态。
默认 Driver 通过 RuntimeExtensions 的原生 `driver_scope` 进入扩展调用作用域，插件宿主负责 SDK 转换、
身份、授权和退出失效；插件 Driver 的适配 handler 拥有自己的作用域。Runtime 不引用插件实现或 SDK 类型。
Provider 绑定由本地装配直接注入协议适配器，插件运行资源在请求时提供对应代次的绑定。
`LocalApplication.build` 装配完整执行环境；`from_runtime` 接收调用方已装配的 Runtime 及模型能力。
`build_local_runtime` 提供独立本地 Runtime，插件协议通过显式 Provider binder 接入。
未绑定的协议可保留模型路由与会话管理能力，实际模型请求报告不可用；连接检查由
`open_local_provider_runtime` 持有所需的 Runtime 和协议插件生命周期。
Core 的 `SessionControllerRegistry` 表达控制器契约、调用规则与可用性，插件适配器负责 SDK 转换及代次归属绑定。
`RunDriverRegistration` 归 Core；执行者使用其原生 handler 和持久绑定，不依赖插件注册表实现。

工具首次执行与恢复共用 `tools/executor.py` 的回调执行边界，统一取消收尾、迟到结果保留、异常转换和
结果归类；恢复入口负责恢复前的契约判断。Executor 返回结果、待输入或控制意图，
ToolCallCoordinator 与 RunLifecycle 负责结果事实、控制变更和批次发布的提交顺序。

控制状态快照与续接检查点显式接收调用方取得的 SessionControlState，读取时机与事务边界由调用方控制。
Core 的 `SessionControlState.run_snapshot` 定义控制快照表示，`RunRecord` 提供控制归属和预算的按需读取。
预算的持久表示由领域统一解释，运行配置、子任务约束及查询展示各自选择所需策略。
审批、问答和子任务等待通过 RunExecutionOwnership 的停驻执行作用域取得锁、重读 Session 和目标 Run，
并在离开作用域时释放执行权；完整工具批次只在锁内读取。Run 选择与恢复使用 RuntimeSessionSnapshot。
交互结果与其下一执行边界由 RunLifecycle 一次提交，包含回答或审批事实、批次等待阶段及对应事件。
Runtime 消费该提交结果继续执行或保持等待，释放锁后再推进会话调度。

会话观察为当前执行提供连续的输出、工具状态和待处理交互。任务清单与统计通过查询取得当前快照，
Host 在页面进入、相关操作完成或显式刷新时读取。MCPManager 在进程内运行记录变化时通知消费者，
Host 查询当前记录并投影连接状态和工具数量；各消费者独立合并通知。仓库负责持久化与查询，不承担界面变化检测。
框架不依赖 Host 的组件、消息泵或绘制周期；页面选择、显示优先级、渲染缓存与刷新时机归各 Host 管理。

## 终端呈现与功能界面

| 位置 | 职责与边界 |
|---|---|
| `tui/view_models.py`、`status.py` | UI 记录和状态投影；欢迎页不依赖 TuiController 实现 |
| `tui/conversation/` | Transcript 投影、历史记录、Markdown/行渲染、完整会话控件 |
| `tui/composer/` | 输入框、命令提示、队列、草稿、附件、技能选择及欢迎输入表面 |
| `tui/interactions/` | 审批、问答、计划确认与表单投影 |
| `tui/menus/` | Choice 记录、选择/编辑/确认菜单及共用行呈现 |
| `tui/menus/sessions.py` | 会话菜单的领域筛选、标题与分组投影，不持有 App 或 Controller |
| `tui/host_screen.py`、`styles.py` | 宿主屏幕的剪贴板和页面生命周期交互；应用布局样式 |
| `tui/features/` | providers、plugins、configuration、tasks、prompts、transfer 的屏幕与工作流 |
| `tui/widgets/` | 跨界面共用的品牌、活动和状态小控件 |

`app.py` 拥有界面任务和视图协调，`controller.py` 衔接应用服务。完整 Widget 类持有自己的
渲染任务、滚动锚点和交互状态。providers、plugins、tasks、prompts 与配置菜单各自是独立工作流对象。
`menus/workflows.py` 集中表达它们共用的菜单操作：当前 Controller 和主题查询、页面展示、通知及
任务启动。App 绑定这些操作并拥有 Textual 事件、导航 worker 和关闭顺序；工作流不持有 App。
跨功能导航通过显式回调完成。Controller 与主题按使用时查询，启动恢复及主题切换无需重建工作流。
`features/transfer/workflows.py` 的 DataTransferWorkflow 独立持有进度和屏幕弱引用，通过窄操作契约与
导航、通知、任务启动回调协作。App 拥有传输 worker，关闭菜单不会取消已接受的文件操作；
菜单自己的检查与选择读取仍由 Screen 管理。操作入口取得当前 Controller，以支持启动恢复后的依赖替换。
`composer/draft_persistence.py` 独立拥有草稿保存任务、修订号、防抖等待与提交前刷盘，
App 保留草稿内容、偏好、错误通知及关闭顺序。保存器在写入时取得当前快照，不依赖 App、
Controller 或 Textual；提交取消时保护已有保存任务，关闭时等待其完成。

`theme.py`、`welcome/`、终端平台适配和渲染基础各自承担独立职责。欢迎输入表面的 `WelcomePanel`
属于 `composer/surface.py`，可选主题包通过欢迎页接口提供页面。

## 插件宿主

| 位置 | 职责与边界 |
|---|---|
| `plugins/inprocess/` | 私有模块加载、SDK 上下文、工具和策略适配、贡献转换 |
| `plugins/worker/` | 双向 RPC 客户端、实例生命周期、wire codec 及具体能力适配 |
| `plugins/contributions/` | 声明记录、准备、绑定、投影、契约与显式贡献类型清单 |
| `plugins/catalog/` | generation gate、绑定包装、Catalog 构建、快照和租约 |
| `plugins/lifecycle/` | 激活、代次、候选就绪与资源释放 |
| `plugins/installation/` | 包校验、安装、发布、来源、支架及声明路径解析 |
| `plugins/configuration/` | 配置服务、schema 与配置值 |

加载器和 wire codec 不依赖贡献清单、宿主生命周期或应用服务。路径校验与错误定义位于
`installation/paths.py`，包校验和 MCP 声明处理共用它。`contributions/inventory.py` 保留显式、
封闭的贡献类型清单与注册顺序；增加贡献类型需要同时接入声明、准备、投影和协议适配。

`prepare_contributions` 编排准备过程，宿主注册表由 PluginRuntimeResource 持有。
`preparation_rules.py` 依据 Manifest、owner 和贡献值完成校验与绑定；静态贡献与动态工具组复用这些规则，
准备适配器只接收准备上下文和结果构建器。Worker 适配器仅在类型检查时引用实例类型；清单按需加载，轻量启动无需载入整套适配器。
循环依赖检查覆盖函数内导入。清单完整性和生命周期匹配由开发测试验证；插件输入、执行契约及资源校验在对应运行边界执行。
SDK 与内置插件具有独立边界；装配和加载边界提供插件发布身份，插件层统一准备工具身份、执行契约与依赖指纹，
调用与恢复逻辑不按插件来源区分规则。
Catalog 将续接策略的发布记录投影为领域控制器引用，Session controller registry 判断当前激活契约是否可用；
Runtime 消费这些结果并拥有检查点更新及租约寿命。
通用工具执行器只比较这些身份与契约，不解释插件包、供应类别或发布流程。插件状态在提交入口验证，
底层写入负责原子发布；跨异步候选构建的提交仍核对源状态。

## 协议与基础设施

| 位置 | 组织依据 |
|---|---|
| `providers/openai_responses/`、`openai_chat_completions/`、`anthropic_messages/` | 按线协议组织 adapter、codec 和协议专属流处理；认证仍归 auth，共享传输、重试和工厂保留独立模块 |
| `sessions/backends/files/`、`sqlite/` | 后端专属 store、commit 和索引；portable、共享查询、layout 与锁保持后端中立 |
| `tools/builtin/` | 内置工具与显式注册；Agent 控制工具按能力绑定 |
| `tools/builtin/search/` | `registration.py` 选择并装配搜索后端；`glob.py`、`grep.py`、`ripgrep.py` 各自拥有工具参数、执行和结果呈现；`files.py` 提供共享路径、遍历与忽略规则 |
| `config/providers/` | Provider 配置管理、codec、运行解析与模板存储；共享声明位于 `config/provider_models.py`，内置模板位于 `config/provider_templates/` |
| `cli/parsers/` | 命令族的完整参数注册函数；`cli/parser.py` 保持明确的注册顺序 |

Core、Auth、MCP、Automation、Billing、Resources、Skills 和 Transfer 各自组织领域规则与实现。
`core/session_ports.py` 定义会话事实、执行锁和目录读取契约，`session_queries.py` 与 `session_facts.py`
定义后端中立查询表示；`sessions/index.py` 拥有索引文档版本与物理表示。归档发布契约
SessionTransferStore 位于 `sessions/portable.py`，由本地传输装配使用，运行用例只依赖会话端口。
两个会话后端复用 `sessions/sqlite_facts.py` 的有界事实读取与提交准备，后端各自拥有事务和物理布局。
资源元信息是不可变值，在构造与替换时校验；文件读取边界负责当前文件归属、可用性和大小。
不建立统一 infrastructure、foundation 或 utils 容器；新分组应对应多个实际协作者或完整、可独立理解的定义集合。
搜索后端单向依赖共享文件搜索规则，不互相引用或反向依赖注册模块；后端在工具装配时确定。
AskUser 的数据契约位于 `tools/builtin/ask_user_contract.py`，工具实现与 Host 表单共用它。
通用 Run / ToolInputRequest 管线保留不透明 JSON，不依赖具体问答字段或业务插件；Host 负责解析呈现契约。

## 导入、测试与演进

内部代码从定义模块导入；`application` 和 `sessions` 的包入口仅标识命名空间，
`providers`、`tools` 等包保留明确的能力导出。目录不承担路径代理或隐式注册职责。
递归架构测试保护顶层依赖及契约、协议和 Host 边界。源码定位、类的 `__module__` 与诊断 logger 名称
属于 Python 模块元数据，不作为业务数据格式或插件协议。
显式运行时模块依赖（含函数内导入）由循环依赖测试约束；`TYPE_CHECKING` 中的纯类型引用不构成运行时边。

TUI 应用集成场景位于 `tests/tui/app/`，共用构造函数和受控 Provider 分别位于 `support.py`、`providers.py`。
通用异步等待使用 `tests/tui/helpers.py`。组件测试与跨 Host、恢复、资源和并发测试具有不同的验证边界；
monkeypatch 应指向被测代码实际查找依赖的位置，而非机械地指向依赖定义模块。

新增 Host 复用应用用例与契约；新增协议、存储后端或业务插件在各自边界接入并满足既有契约。
只有第二个真实消费者或明确的状态所有权变化，才构成提取新端口或协作对象的理由。
RuntimeService、AgentService、OuroTuiApp 及存储类各自持有事务或资源协作状态；模块大小必须服从这些所有权边界。
