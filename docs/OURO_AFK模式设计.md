# OURO AFK 模式设计

## 1. 目标

AFK（Away From Keyboard）模式用于用户暂时离开但希望 Agent 继续自主工作的场景。它保证：

- 所有可批准的工具调用由 Host 自动批准，不进入等待审批状态；
- 工具请求用户输入时不让 Run 停在 `waiting_user`，而是进入类型化的无人值守解决路径；
- 自动行为保持真实 provenance，不伪造“用户批准”或“用户回答”；
- 硬拒绝、参数校验、执行契约、路径边界、工具阶段策略和取消语义仍然有效；
- AFK、Plan Mode 和其他插件阶段可以组合，不建立第二套 Agent Loop。

AFK 不承诺任何工具必然成功。它承诺的是：能够自动批准的调用自动批准，需要用户语义输入的调用由工具按无人值守契约处理，无法无人值守完成的调用产生明确结果而不是永久挂起。

## 2. 第一性原理

工具权限和用户输入是两个不同问题：

- 工具权限回答“Host 是否允许执行这个调用”，发生在执行前；
- 工具输入回答“工具继续业务流程需要什么信息”，发生在执行中。

因此 AFK 不能成为新的工具权限值，也不能把 `ToolInputRequest` 塞进 `resolve_tool_permission()`。Session 保存两个正交策略：

```text
approval_mode: inherit | auto | approval | deny
interaction_mode: interactive | unattended
```

AFK 是应用层命名预设，而不是第三个重复状态字段：

```text
AFK = approval_mode:auto + interaction_mode:unattended
```

Host 和 UI 可以把精确匹配该组合的 Session 展示为 AFK；canonical Session 数据只保存真正决定行为的策略值。

## 3. 权限边界

“AFK 自动放行工具”的规范含义是“自动批准所有可批准调用”。以下行为不受 AFK 覆盖：

- `ToolPolicyDecision(action="deny")`；
- tool-controlled `permission_mode="deny"`；
- 参数或 JSON Schema 校验失败；
- execution contract、插件 Generation 或工具身份不匹配；
- workspace、artifact、批次独占、取消和未知副作用约束。

若最终权限仍为 `approval`，`interaction_mode="unattended"` 立即生成并持久化一个由 Host policy 解决的批准记录，然后继续执行。该记录必须标明 `resolved_by="host_policy"` 和稳定的 `resolver_id`，不能记为用户批准。

## 4. 用户输入解决协议

`ToolInputRequest.data` 继续是 Core 不解释的任意 JSON。回答内容和解决控制信息不得混在同一个任意对象中。恢复调用使用判别式 resolution：

```text
AnswerResolution
  kind: answer
  value: 任意 JSON
  resolved_by: user | host_policy
  resolver_id: 可选

UnattendedResolution
  kind: unattended
  resolved_by: host_policy
  resolver_id: ouro.afk
  reason: 稳定、可诊断文本
```

用户实际回答产生 `AnswerResolution(resolved_by="user")`。AFK 不为未知请求猜测布尔值、空字符串、第一选项或 `null`，而是把 `UnattendedResolution` 交回原工具的 resume handler。工具拥有请求 schema 和业务语义，因此也拥有无人值守处理方式。

工具定义必须显式声明 `supports_unattended`。只有同时 resumable 且声明支持的工具会收到 `UnattendedResolution`；其他工具产生标准 `UserInputUnavailable` ToolResult。该能力进入 manifest、SDK、worker descriptor 和 execution contract hash。

收到无人值守 resolution 的 handler 必须返回终态 ToolOutput。再次请求用户输入属于 `UnattendedInteractionLoop` 协议错误，避免 AFK 快速自循环。

## 5. Run 与持久化语义

正常 AFK 路径不对 Host 返回 `waiting_approval` 或 `waiting_user`：

```text
tool preflight
  -> approval required
  -> durable policy approval
  -> execute
  -> ToolInputRequest
  -> durable input checkpoint
  -> unattended resume / standard unavailable result
  -> commit ToolResult
  -> continue the same batch and model loop
```

原始 request、interaction revision、resolution provenance、精确 execution contract 和最终结果都保存在 active ToolBatch 中。恢复和重放继续绑定原 Run Catalog。进程在工具或 resume handler 内中断时，沿用 OURO 对未知副作用的 recoverable 语义，不猜测调用结果。

事件至少区分：

- `approval_requested` / `approval_resolved`，后者记录 resolver provenance；
- `ask_user_requested`，保留现有 Host 兼容投影；
- `ask_user_resolved`，记录 resolution 类型和 provenance；
- 自动解决不得产生伪造的用户消息。

AFK 的 `ask_user_requested` 事件显式携带 `interaction_mode=unattended` 与 `will_suspend=false`。它记录工具确实
产生过输入请求，但 Runtime activity 必须投影为 `resolving_input`，不能短暂显示成等待用户；最终结果 checkpoint
持久化后才写入 `ask_user_resolved`。

## 6. Plan Mode 组合语义

Plan Mode 的阶段 Tool Policy 仍然有效，AFK 不穿透其 `deny`。`ExitPlanMode` 声明支持无人值守恢复：

- 用户回答：继续解析 `accept | revise | reject`；
- AFK resolution：把 `awaiting_approval` 原子迁移为 `inactive`，返回成功 ToolOutput；
- 输出明确声明批准门由 AFK policy 解决，不使用 “approved by user”；
- 无选项时直接继续；一个选项由策略选择并记录 provenance；多个选项不默认选择第一项，交由 Agent 根据计划自主选择。

拒绝不能作为 AFK fallback，因为拒绝会保持 Plan Mode active 并返回错误，容易形成 Exit 重试循环。

## 7. 分层职责

- Core：策略类型、resolution 类型、Approval provenance 和 Tool contract 字段；
- Application：Session 策略变更、自动批准、无人值守协调、事件、持久化和恢复；
- ToolExecutor：执行与 resume 隔离，不读取 Session AFK 状态；
- Plugin/工具：解释自己的请求与无人值守业务语义；
- CLI/Desktop：提供 AFK 预设、状态展示和交互入口；
- Plugin SDK/worker：传递 resolution 与 capability，不解释业务 payload。

不增加通用 Interaction Resolver Registry。首版复用已经存在的 resumable tool 边界；只有未来出现多个独立、可配置的外部回答器时，才基于真实用例增加新的 contribution。

## 8. 验收不变量

1. Interactive Session 的审批和用户输入行为保持不变。
2. AFK Run 不以 `waiting_approval` 或 `waiting_user` 返回。
3. AFK 自动批准具有 `host_policy` provenance，不能冒充用户。
4. 任意 JSON request 不由 Core 猜测答案。
5. 不支持无人值守的工具得到明确、可回注模型的错误结果。
6. Plan Mode 在 AFK 下正常退出且不产生用户拒绝语义。
7. Tool Policy `deny` 和所有执行安全边界仍不可被 AFK 降低。
8. in-process 与 worker 工具使用同一 resolution 契约，相关字段进入 execution contract。
9. 顺序和并行批次在自动解决后都能继续未执行调用。
10. 自动解决不能产生无限输入请求循环。
