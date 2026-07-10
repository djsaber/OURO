# OURO Agent 插件、MCP 与 Skills 规格

- Date: 2026-07-07
- Status: Accepted / Phase 1 framework implemented; full lifecycle deferred
- Related: ../../docs/OURO_AGENT_项目概述_V0707.md

> Implementation boundary update (2026-07-11): 第一阶段实现 manifest 发现、显式 contributions 注册、
> hook 隔离、Skills 元数据/管理和 MCP 内存配置/状态/工具注册框架。插件 entrypoint 自动加载以及 MCP
> 真实进程/协议连接、握手、工具发现和重连属于第二阶段，详见
> `2026-07-11-phase-2-extensions-official-capabilities.md`。

## 1. Motivation

OURO 的核心必须保持极简，但产品又需要持续扩展工具、命令、上下文、记忆、MCP 和自动化能力。

插件、MCP 与 Skills 是三个不同层次：

- 插件提供可执行能力和系统扩展。
- MCP 接入外部工具服务器。
- Skills 提供可复用操作知识和流程说明。

三者需要共享来源标识、启用状态、权限和事件记录，但不能互相混淆。

## 2. Goals

- 定义插件来源、加载、启用、禁用和授权。
- 明确没有项目插件或全局插件划分。
- 支持官方插件、第三方插件、AI 自主生成插件。
- 定义插件权限：读写能力、网络访问能力。
- 定义 MCP 服务器生命周期管理。
- 定义 MCP 工具接入 OURO 工具系统的方式。
- 定义 Agent Skills 的发现、元数据加载和按需加载。
- 定义插件通过 Runtime Hooks 接入核心运行链路的方式。
- 支持插件加载失败隔离。

## 3. Non-goals

- 不实现插件市场。
- 不实现插件签名、组织策略或企业分发。
- 不实现项目插件概念。
- 不把 Skills 当作可执行插件。
- 不要求第一阶段实现复杂 MCP OAuth 流程。
- 不要求第一阶段实现子智能体插件。
- 不允许插件直接依赖 OURO 核心内部可变对象或任何外部框架类型。

## 4. Plugin Model

插件来源：

- official。
- third_party。
- ai_generated。

插件启用是 OURO 系统级配置，不按项目归属划分。工作区可以影响插件配置覆盖，但插件本身不属于项目。

插件可以贡献：

- tools。
- commands。
- context_providers。
- model_providers。
- skills。
- runtime_hooks。
- ui_extensions，未来。
- background_listeners，未来。

## 5. Plugin Manifest

建议 manifest 字段：

- plugin_id。
- name。
- version。
- source_type。
- entrypoint。
- description。
- capabilities。
- permissions。
- config_schema。
- commands。
- tools。
- mcp_servers。
- skills。
- runtime_hooks。

permissions：

- read_write。
- network。

权限表示插件声明可能使用的能力，不是企业级隔离保证。

## 6. Plugin Lifecycle

状态：

- discovered。
- enabled。
- disabled。
- failed。
- needs_authorization。
- needs_configuration。

加载流程：

1. 发现插件 manifest。
2. 读取元数据，不执行插件代码。
3. 校验 manifest。
4. 检查启用状态。
5. 检查授权和配置。
6. 加载插件入口。
7. 注册工具、命令、上下文提供器等能力。
8. 记录插件加载事件。

插件加载失败不能破坏核心启动。失败应写入事件或诊断日志，并允许用户禁用。

## 7. Runtime Hook Integration

插件通过 Runtime Hooks 接入核心运行链路。Runtime Hooks 是 OURO 的稳定扩展点，不等同于外部框架 middleware。

插件可以注册的 hook：

- before_run。
- after_run。
- before_context_build。
- after_context_build。
- before_model_call。
- after_model_response。
- before_tool_call。
- after_tool_call。
- on_approval_requested。
- on_event。
- on_error。

第一阶段建议优先实现：

- after_context_build：用于摘要压缩、长期记忆上下文注入等插件上下文扩展。Agent Skills 是 OURO
  原生能力：组合根只发现并渲染元数据，ContextBuilder 通过显式 `skill_instructions` 契约追加到
  system prompt；Skills 不通过 Runtime Hook 注入，也不伪装成普通 synthetic message。
- before_tool_call：用于工具调用观察、附加审批提示。
- after_tool_call：用于工具结果观察和诊断记录。
- on_event：用于调试、日志、回放增强。

Hook 注册字段：

- hook_name。
- handler。
- priority。
- enabled。
- permissions_required。

Hook 执行必须满足：

- 输入是 OURO 受控上下文对象，不是核心内部可变对象。
- 第一阶段只合并 added_context 和 diagnostics；更强的工具结果覆盖、资源引用登记、终止运行等能力留给后续详细规格。
- 失败必须记录 plugin_id、hook_name、session_id、run_id。
- 第三方和 AI 生成插件 hook 失败后，本次运行可隔离该 hook。

插件不得通过 hook 绕过工具权限。若 hook 需要执行文件写入、命令或网络访问，必须走工具或插件权限声明。

## 8. AI Generated Plugins

AI 自主生成插件必须：

- 以 ai_generated 标记来源。
- 默认 disabled 或 needs_authorization。
- 展示将要提供的工具、命令和权限。
- 经用户确认后才能进入 enabled。

第一阶段只要求保留确认流程和来源标识，不要求实现完整生成器。

## 9. MCP Integration

MCP 是外部工具接入方式，不等同于插件系统，但 OURO 通过官方集成管理 MCP。

MCP 服务器配置字段：

- server_id。
- name。
- command。
- args。
- env。
- cwd。
- transport。
- credential_refs。
- enabled。
- auto_start。
- timeout_seconds。
- metadata。

生命周期状态：

- configured。
- starting。
- connected。
- failed。
- stopping。
- stopped。
- disabled。

OURO 负责：

- 加载配置。
- 启动服务器。
- 连接服务器。
- 认证或传递凭据。
- 发现工具。
- 将 MCP 工具注册为 OURO 工具。
- 停止或重启服务器。
- 记录状态和错误。

MCP 工具必须继承：

- 来源标识 source=mcp。
- 所属 server_id。
- 工具权限策略。
- 事件记录。
- 禁用机制。

## 10. Skills

Skills 是可复用操作知识和流程说明，不等同于插件。

Skill 字段：

- skill_id。
- name。
- description。
- source。
- path。
- triggers。
- metadata。
- enabled。

加载策略：

- 启动时只加载元数据。
- 满足触发条件或显式调用时加载完整内容。
- 技能可以引用本地文件、脚本和示例。
- 若技能要求执行脚本，仍通过工具权限系统审批。

Skills 来源：

- 用户全局技能。
- 工作区技能。
- 插件提供的技能。

工作区技能受工作区信任影响。

## 11. Events

建议事件：

- plugin_discovered。
- plugin_enabled。
- plugin_disabled。
- plugin_load_failed。
- plugin_authorization_requested。
- plugin_hook_registered。
- plugin_hook_failed。
- mcp_server_starting。
- mcp_server_connected。
- mcp_server_failed。
- mcp_server_stopped。
- mcp_tool_registered。
- skill_metadata_loaded。
- skill_loaded。

## 12. Acceptance

完成本规格后，应满足：

- 可以发现并加载一个官方插件 manifest。
- 插件加载失败不会阻止 OURO 启动。
- 插件工具带来源标识。
- 插件可以注册 after_context_build 或 after_tool_call hook。
- 插件 hook 输入输出使用 OURO 数据结构，不依赖外部框架类型。
- 插件 hook 失败会被记录并隔离，不破坏核心运行。
- 插件启用时可以展示读写能力和网络访问能力。
- AI 生成插件默认需要用户确认。
- 可以配置 MCP server，并在内存框架中验证 start/stop/restart 状态转换；真实服务器启动和协议连接
  是第二阶段验收。
- MCP 工具能进入 OURO 工具注册表。
- Skills 只加载元数据，按需加载完整内容。
- 可用 Skills 元数据通过原生 ContextBuilder 契约进入 system prompt，并可从
  `context_metadata.skills_injected` 观察是否生效。

## 13. Resolved / Deferred Decisions

- 第一阶段实现插件 manifest 发现和贡献注册框架；entrypoint 自动导入或命令进程执行后置。
- MCP 第一阶段提供服务器配置、生命周期状态和工具注册框架；完整协议连接与插件贡献 MCP 配置后置。
- Skills 第一阶段只支持元数据加载和显式按需加载完整内容；关键词匹配后置。
- 第三方插件可注册 Runtime Hooks，但第一阶段只承诺 added_context、diagnostics 和工具观察语义，并通过失败隔离降低风险。
