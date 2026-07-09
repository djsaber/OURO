# OURO Agent 工具、权限与人机协同规格

- Date: 2026-07-07
- Status: Accepted / Phase 1 implemented
- Related: ../../docs/OURO_AGENT_项目概述_V0707.md

## 1. Motivation

Agent 的价值来自工具执行，但个人本地 Agent 的体验也最容易在工具执行上失控。

OURO 的权限哲学是不做企业级复杂隔离，而是给个人用户提供可理解、可调整、可中止的执行路径。第一阶段需要定义基础工具、工具权限、审批、AskUser、取消和恢复的共同模型。

## 2. Goals

- 提供统一工具注册和执行入口。
- 支持基础工具：Read、Write、Edit、Bash、AskUser。
- 支持工具来源标识。
- 支持工具权限：自动执行或用户批准。
- 支持插件权限：读写能力和网络访问能力。
- 支持审批、拒绝、修改参数。
- 支持 AskUser 挂起与恢复。
- 支持工具执行中止。
- 工具结果进入消息和事件流。

## 3. Non-goals

- 不实现沙箱。
- 不实现企业级路径策略语言。
- 不实现复杂网络域名白名单。
- 不实现项目插件权限模型。
- 不实现长期后台任务调度。
- 不要求所有命令都可强制中止，只要求尽力响应中止信号并明确状态。

## 4. Tool Model

### ToolDefinition

工具定义描述模型可以调用什么。

最小字段：

- name。
- display_name。
- description。
- parameters_schema。
- source。
- permission_mode。
- result_schema。
- can_abort。
- metadata。

permission_mode：

- auto。
- approval。

source：

- builtin。
- official_plugin。
- third_party_plugin。
- ai_generated_plugin。
- mcp。

### ToolInvocation

工具调用请求。

最小字段：

- tool_call_id。
- session_id。
- run_id。
- turn_id。
- tool_name。
- source。
- arguments。
- requested_at。
- permission_mode。
- approval_id。

### ToolResult

工具执行结果。

最小字段：

- tool_call_id。
- tool_name。
- is_error。
- content。
- structured。
- resource_refs。
- started_at。
- ended_at。
- exit_code。
- metadata。

工具结果至少支持文本和结构化数据。图片、文件引用等作为 resource_refs 记录。

## 5. Built-in Tools

### Read

用途：

- 读取文本文件。
- 列出目录结构。
- 读取必要的二进制或图片资源引用。

默认权限：

- auto。

注意：

- 第一阶段不做复杂路径隔离。
- 首次访问明显敏感路径时可以提示，但不作为硬性安全系统。

### Write

用途：

- 创建或覆盖文件。

默认权限：

- approval。

审批展示：

- 目标路径。
- 是否覆盖。
- 写入摘要或预览。

### Edit

用途：

- 对文本文件做局部编辑。

默认权限：

- approval。

审批展示：

- 目标路径。
- diff 或变更摘要。

### Bash

用途：

- 执行本地命令。
- 捕获 stdout、stderr、exit_code。

默认权限：

- approval。

审批展示：

- 命令文本。
- 工作目录。
- 环境变量摘要。
- 超时设置。

### AskUser

用途：

- 向用户提问。
- 请求用户补充信息或选择。

默认权限：

- auto。

AskUser 会让运行进入 waiting_user 状态。用户回答后从挂起点恢复。

## 6. Approval Lifecycle

审批状态：

- requested。
- approved。
- rejected。
- modified。
- expired。
- aborted。

流程：

1. 工具调用生成。
2. ToolExecutor 判断 permission_mode。
3. 若为 auto，直接执行。
4. 若为 approval，创建 approval_id。
5. 发布 approval_requested 事件。
6. 运行进入 waiting_approval。
7. 用户可以批准、拒绝或修改参数。
8. 用户响应写入事件流和运行状态。
9. 批准后执行工具。
10. 拒绝后生成 tool_result error，让模型理解拒绝原因。

拒绝不是系统错误。拒绝结果应回灌给模型。

## 7. Permission Policy

第一阶段策略：

- 基础读取类工具默认 auto。
- 写文件、编辑文件、执行命令、网络访问默认 approval。
- 用户可按工具或来源调整默认策略。
- 插件启用时授予读写能力和网络访问能力。
- AI 自主生成插件默认需要用户确认启用。

策略持久化位置：

- 全局配置。
- 会话配置。
- 工作区配置。

优先级遵循配置规格。

## 8. Cancellation

用户可中止：

- 当前运行。
- 正在等待的审批。
- 正在等待的 AskUser。
- 正在执行且支持中止的工具。

工具执行器必须：

- 在开始执行前检查 abort_requested。
- 对支持中止的工具传递取消信号。
- 对无法中止的命令展示“正在等待进程结束”或等价状态。
- 中止后发布 tool_call_completed 或 run_aborted 事件，不能让 UI 卡在执行中。

## 9. Event Sequence

工具调用事件建议顺序：

1. tool_call_requested。
2. approval_requested，若需要审批。
3. approval_resolved，若用户响应。
4. tool_call_started。
5. tool_call_completed。
6. message_added，写入 tool_result 消息。

审批拒绝时：

1. tool_call_requested。
2. approval_requested。
3. approval_resolved rejected。
4. message_added tool_result is_error=true。

## 10. Persistence

必须持久化：

- 工具调用请求。
- 审批请求和结果。
- 工具执行结果。
- AskUser 请求和回答。
- 中止状态。

挂起恢复时，如果存在 pending approval 或 pending AskUser，宿主应能重新展示待处理请求。

## 11. Acceptance

完成本规格后，应满足：

- 模型可以调用 Read，自动读取文件并返回结果。
- 模型调用 Write/Edit/Bash 时触发审批。
- 用户可以批准、拒绝、修改参数。
- 拒绝结果作为 tool_result 回灌模型。
- AskUser 可以挂起运行并等待用户回答。
- 中止运行后会话保持可继续。
- 工具事件和结果都写入事件流。
- 插件工具带来源标识，并受相同审批机制控制。

## 12. Resolved Decisions

- 第一阶段 Bash 默认超时时间为 30 秒，可通过工具参数覆盖。
- 第一阶段 Edit 使用保守 exact replace 策略。
- 第一阶段审批支持 approve、reject、modify，不提供“本次会话总是允许此工具”快捷授权。
