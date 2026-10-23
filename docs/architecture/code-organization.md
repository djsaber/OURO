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
| `application/runtime/` | Runtime 编排、环境、配置和 RunResources；统一管理单次运行的进程内资源 |
| `application/inputs/`、`context/` | 输入准备与队列、上下文构造与压缩 |
| `application/models/`、`observations/` | 模型查询和执行、会话快照与事件订阅 |
| `application/configuration/`、`plugins/`、`provider_auth/`、`transfer/` | 对应能力的应用用例及投影 |
| `application/supervisor.py`、`tool_calls.py` 等独立模块 | 任务调度、工具批次事务和各自的资源协作单元 |

RunLifecycle 拥有持久转换，Supervisor 拥有任务、队列和执行许可，Runtime 拥有单次运行协作。
输入、观察和模型请求协作者不取得完整 Runtime 的控制权。契约放置在独立目录不意味着轻量导入：
父包显式导出服务，因此导入初始化成本与模块的类型依赖需要分别评估。

## 终端呈现与功能界面

| 位置 | 职责与边界 |
|---|---|
| `tui/view_models.py`、`status.py` | UI 记录和状态投影；欢迎页不依赖 TuiController 实现 |
| `tui/conversation/` | Transcript 投影、历史记录、Markdown/行渲染、完整会话控件 |
| `tui/composer/` | 输入框、命令提示、队列、草稿、附件、技能选择及欢迎输入表面 |
| `tui/interactions/` | 审批、问答、计划确认与表单投影 |
| `tui/menus/` | Choice 记录、选择/编辑/确认菜单及共用行呈现 |
| `tui/features/` | providers、plugins、configuration、tasks、prompts、transfer 的屏幕与工作流 |
| `tui/widgets/` | 跨界面共用的品牌、活动和状态小控件 |

`app.py` 拥有界面任务和视图协调，`controller.py` 衔接应用服务。完整 Widget 类持有自己的
渲染任务、滚动锚点和交互状态。功能工作流通过类继承与 App 协作，其方法访问 App 的界面状态；
维护这一边界时需要同时检查状态访问、消息分发与 Textual 装饰器契约。

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

ContributionCoordinator 与 WorkerPluginInstance 通过方法内导入访问清单，清单依赖具体适配器，
形成局部导入耦合。调用时机与代次生命周期共同约束该关系，模块依赖审查必须覆盖方法内导入。
SDK 与内置插件具有独立边界；内置插件 Python 内容、相对路径和声明的运行资源参与持久化实现身份计算。

## 协议与基础设施

| 位置 | 组织依据 |
|---|---|
| `providers/openai_responses/`、`openai_chat_completions/`、`anthropic_messages/` | 按线协议组织 adapter、codec 和协议专属流处理；认证仍归 auth，共享传输、重试和工厂保留独立模块 |
| `sessions/backends/files/`、`sqlite/` | 后端专属 store、commit 和索引；base、portable、共享查询、layout 与锁保持后端中立 |
| `tools/builtin/` | 内置工具与显式注册；Agent 控制工具按能力绑定 |
| `config/providers/` | Provider 配置管理、codec、运行解析与模板存储；共享声明位于 `config/provider_models.py`，内置模板位于 `config/provider_templates/` |
| `cli/parsers/` | 命令族的完整参数注册函数；`cli/parser.py` 保持明确的注册顺序 |

Core、Auth、MCP、Automation、Billing、Resources、Skills 和 Transfer 各自组织领域规则与实现。
不建立统一 infrastructure、foundation 或 utils 容器；新分组应对应多个实际协作者或完整、可独立理解的定义集合。

## 导入、测试与演进

内部代码从定义模块导入；包入口只公开该领域的受支持 API，例如 `ouro.application`、`ouro.providers`、
`ouro.tools` 的显式导出。目录不承担路径代理或隐式注册职责。
递归架构测试保护顶层依赖及契约、协议和 Host 边界。源码定位、类的 `__module__` 与诊断 logger 名称
属于 Python 模块元数据，不作为业务数据格式或插件协议。

TUI 应用集成场景位于 `tests/tui/app/`，共用构造函数和受控 Provider 分别位于 `support.py`、`providers.py`。
通用异步等待使用 `tests/tui/helpers.py`。组件测试与跨 Host、恢复、资源和并发测试具有不同的验证边界；
monkeypatch 应指向被测代码实际查找依赖的位置，而非机械地指向依赖定义模块。

新增 Host 复用应用用例与契约；新增协议、存储后端或业务插件在各自边界接入并满足既有契约。
只有第二个真实消费者或明确的状态所有权变化，才构成提取新端口或协作对象的理由。
RuntimeService、AgentService、OuroTuiApp 及存储类各自持有事务或资源协作状态；模块大小必须服从这些所有权边界。
