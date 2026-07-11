# OURO Agent 第一阶段完成审计

- Date: 2026-07-11
- Status: Complete
- Baseline: `356f98d`
- Coding standard: `../../docs/OURO_AGENT_编码准则_EN.md`
- Phase 2 specs: `../specs/2026-07-11-phase-2-personal-workflow.md`

## 1. 审计结论

第一阶段的分层方向、领域模型和主要调用链成立，不需要推倒重写。`core` 保持 UI、存储和具体
provider 无关；`application` 负责运行编排；持久化、工具、provider、插件和 CLI 均通过显式
边界接入。审计发现的缺口集中在失败路径、契约落实和规格状态同步，而不是总体架构错误。

本次只修复有直接证据的一阶段问题。资源管理、真实 MCP 协议连接、Desktop、多会话宿主、
摘要/记忆/计划等能力明确进入第二阶段，本次没有实现这些代码。

## 2. 审计方法与证据

审计覆盖：

- 六篇第一阶段规格、项目概述和编码准则；
- `core → application → host` 的依赖方向；
- 会话创建、运行、模型流、工具回灌、审批/AskUser 恢复、中止和 follow-up 队列；
- 本地文件原子写入、JSONL、索引恢复、进程锁和损坏文件；
- provider/endpoint、凭据、权限、提示词和 Skills 配置；
- 插件、Hook、MCP 框架边界及失败隔离；
- Interactive、Print、JSON 和退出码；
- 正常路径、异常路径、并发边界和兼容边界测试。

审计前基线为 197 个测试通过；修复后为 219 个测试通过，同时通过 Ruff、mypy 和
`compileall`。架构测试还增加了“低层模块不得反向依赖 application/CLI”的约束。

## 3. 已修复问题

| ID | 级别 | 问题 | 风险 | 处理 |
|---|---|---|---|---|
| P1-01 | 高 | `RuntimeService.run` 在取得会话锁后、进入原有 `finally` 前仍会执行多次持久化和事件回调 | 任一早期异常都会让当前进程继续持有锁，会话无法继续 | 把锁取得后的完整启动路径纳入单一 `try/finally`，并以 `SessionLock` 协议替代反射式 `getattr` 释放 |
| P1-02 | 高 | `ModelMetadata.context_window` 只被配置和展示，未参与 `ContextBuilder` | 长会话仍可能把超限请求发送给 provider，和“内置最简截断”不符 | 引入保守 token 估算、固定上下文成本和输入预算；保留最近原子工具交换，记录估算、预算和超限状态；校验模型 token 上限 |

> Follow-up (2026-07-11): P1-02 的截断修复随后被
> `2026-07-11-built-in-summary-compaction.md` 取代。当前实现使用持久化 LLM 摘要和硬预算错误，原截断路径已删除。
| P1-03 | 高 | 工具 `parameters_schema` 未统一执行；未知工具在注册表查找阶段抛异常 | 插件/MCP 工具参数行为不一致，模型虚构工具名会令整个 run 失败 | 在注册时校验 JSON Schema，在执行前统一校验参数；未知工具和参数错误均生成可回灌 `ToolResult`，不进入审批或执行 |
| P1-04 | 中 | 宿主 `event_sink` 异常会向运行主链传播 | CLI/Desktop 渲染故障可破坏已持久化的 run | 失败 sink 立即隔离，并持久化 `event_sink_failed` 诊断事件；其他运行逻辑继续 |
| P1-05 | 中 | 权限配置对错误 section、错误容器和非法 mode 静默降级为空 | 用户以为策略已生效，实际采用默认授权 | 增加严格 `PermissionConfig`，非法配置统一抛 `ConfigError`；写操作先验证现有配置 |
| P1-06 | 中 | 本地原子写入失败时可能遗留临时文件；损坏 session 暴露底层异常 | 故障难定位，CLI 无法稳定区分持久化失败 | 临时文件写入增加 flush/fsync 和清理；损坏数据转为带路径上下文的 `PersistenceError` |
| P1-07 | 中 | Print 与 JSON 对 waiting/aborted/tool/persistence 的退出码不一致 | 脚本无法可靠区分运行结果 | 统一状态到退出码的映射：审批 4、中止 5、工具相关失败 6、持久化 7 |
| P1-08 | 低 | 架构测试只保护 `core` 和 provider config 的少量边界 | 后续模块容易反向依赖 application/CLI | 增加 config/MCP/plugins/prompts/providers/sessions/skills/tools 的依赖方向测试 |
| P1-09 | 低 | 极窄的 lock 竞争窗口会抛出未分类 `RuntimeError` | Print/JSON 无法稳定输出可读错误 | 增加 `SessionBusyError`，锁竞争继续走统一 OURO error 边界 |
| P1-10 | 中 | Interactive 只在启动时处理 OURO error，运行中持久化/配置错误会退出整个交互进程 | 暂时故障破坏交互连续性，错误不可恢复 | 在命令和 prompt 边界报告错误并继续输入循环，保留程序错误向上暴露 |

## 4. 架构判断

### 4.1 保留的设计

- `core` 只定义 OURO 模型、Provider 协议、ContextBuilder、Hooks 和 inner loop ports。
- `RuntimeService` 是 application 编排入口，状态转换委托给 `RunLifecycle`，事件委托给
  `RuntimeEventRecorder`，follow-up 委托给 `FollowUpQueue`。
- `SessionStore`、Provider、Tool Registry 和 Hook Registry 均可替换，具体文件/HTTP 实现不进入
  `core`。
- assistant tool-call 消息先于 tool result 持久化，审批拒绝和工具错误都能返回模型。
- 运行锁使用进程持有的 OS lock，锁文件只承担诊断，不承担存活判断。

### 4.2 未做无收益重构的模块

`application/runtime.py`、`config/providers.py` 和 `core/inner_loop.py` 体积较大，但当前内部职责已经
由 lifecycle、events、follow-up、codec、endpoint 和 ports 分开；其剩余方法仍围绕单一编排或配置
聚合职责。为了缩短文件而引入 mixin、回调网或空转 service 会增加间接层，审计未发现足以证明这种
拆分有收益的耦合缺陷。因此本次不做机械拆分，第二阶段在引入多会话 supervisor 和 host facade 时
再按新职责自然拆分。

## 5. 规格与实现边界校正

以下内容曾出现在第一阶段规格的宽泛 Goals/Acceptance 中，但具体 Resolved Decisions 或实际代码只
实现了框架。本次把状态显式校正，避免“文档显示已完成、代码实际未承诺”的歧义：

- `ResourceRecord`、附件复制、资源索引恢复、归档、事件清理和迁移提示：第二阶段。
- MCP 真实进程/HTTP/SSE 连接、握手、认证、工具发现、重连和日志：第二阶段；第一阶段只有配置、
  状态机和工具注册边界。
- 插件 entrypoint 自动加载、安装确认、授权 UI 和完整生命周期：第二阶段；第一阶段只有 manifest
  发现和由组合根显式提供 contributions 的注册框架。
- `before_run`、`after_run`、`before_model_call`、`after_model_response`、
  `on_approval_requested`、`on_error` 名称是保留契约；第一阶段实际承诺的 dispatch 语义是
  `after_context_build`、`before_tool_call`、`after_tool_call` 和 `on_event`。
- 同一 provider 响应中的批量工具调用保真、声明式串行/并行调度：第二阶段；第一阶段能处理多个调用，
  但按流顺序逐个执行。
- 旧版扁平 provider 配置不兼容读取；权威格式是 provider 下的 endpoint 集合。原规格末尾的旧格式
  兼容表述与正文及实现冲突，已移除。

## 6. 遗留风险与第二阶段入口

- 当前 token 估算是 provider 无关的保守近似，不等同于精确 tokenizer。第二阶段允许 provider 插件
  提供 estimator，但必须保留无 tokenizer 时的内置降级。
- JSONL 仍没有逐行校验和；单行损坏会使会话加载失败并给出明确错误。定位、隔离和修复工具进入
  第二阶段数据维护。
- 单会话只允许一个 active run 已由锁保证；跨会话后台并发、订阅和宿主生命周期由第二阶段
  `RuntimeSupervisor` 负责。
- 插件仍是同进程可信扩展，没有沙箱。第二阶段只增加信任、授权和失败隔离，不宣称安全沙箱。
- 本地文件存储仍适合个人规模；SQLite 后端和大规模离线分析保持第三阶段范围。

## 7. 完成标准

- 所有 P1-01 至 P1-10 均有代码和回归测试；
- 第一阶段原有功能没有被第二阶段预实现污染；
- 第一阶段规格中的“框架已实现”和“完整能力后置”不再冲突；
- 第二阶段规格、子规格和实施计划能直接追踪上述遗留入口；
- 全量自动化验证通过，工作树只包含本次审计、一期修复和二期文档。
