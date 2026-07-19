# 工具与人机协同规格

状态：当前

## 目标与范围

工具系统把模型请求转换为受 schema、权限、取消和持久化约束的执行。模型错误调用应形成可回灌结果，
基础设施错误应明确终止或诊断，不能产生不可解释的半状态。

## 工具契约

`ToolDefinition` 描述：name、description、参数 schema、结果 schema、权限模式、来源和 metadata。
`ToolCallRequest` 保存 Provider 调用身份、参数和原始顺序。`ToolInvocation` 增加 Session/Run 上下文。
`ToolResult` 保存文本、结构化结果、错误标志和资源引用。

注册时验证 schema；执行前使用同一 schema 验证参数。未知工具或非法参数返回 error `ToolResult`，
不进入审批或实际 handler。

## 内置工具

- Read：只读取 UTF-8 文本文件，不承担目录枚举；使用 1 基 `offset` 和 `limit` 分页，按
  `<行号>\t<内容>` 返回，单次至多 1000 行或 100,000 UTF-8 字节；
- Write：以覆盖或追加模式写入 UTF-8 文本，并自动创建缺失的父目录；
- Edit：执行精确字面文本替换，默认替换首处，可显式选择替换全部；
- 命令行工具：启动时检测宿主可执行 shell，仅暴露一个与实际执行方言同名的工具；Windows 依次选择
  PowerShell 7、Windows PowerShell、Command Prompt，POSIX 系统依次选择 Bash、POSIX sh；
- AskUser：暂停 Run 并请求用户输入。

写入、编辑和命令行工具默认可由权限配置要求审批。Read/Write/Edit 的路径和命令行工具的工作目录统一经过
`ToolExecutionContext` 解析；默认限制在 Session workspace 内，并拒绝 `..` 与符号链接逃逸。Read 在文件
IO 边界流式执行自身的行数和 UTF-8 字节上限，返回 `next_offset`、实际行数与截断信息，避免为截取一页而
全量载入文件；所有工具结果仍在 executor 边界执行宿主统一长度上限和显式截断标记。

命令行工具的模型可见 name、方言描述和执行包装必须由同一个检测结果生成，不能用 `Bash` 名称委托宿主
默认 shell。PowerShell 以无 Profile、非交互、UTF-8 文本模式运行；Bash 以无 Profile、非交互并启用
`pipefail` 的模式运行。各方言描述需明确其引用、管道、路径、可选程序探测、错误状态与非持久会话等常见
陷阱。若没有支持的 shell，则不注册命令行工具，不能暴露一个实际无法执行或会落到其他方言的定义。

## 权限

权限模式包括：

- `auto`：schema 通过后可执行；
- `approval`：生成 `ApprovalRecord`，Run 进入 `waiting_approval`；
- `deny`：硬拒绝调用，不允许会话级 `auto` 或一次审批绕过。

配置按工具名覆盖 definition 默认值。审批可批准、拒绝或替换参数；替换后的参数必须重新验证。
拒绝生成 error ToolResult 并继续模型循环，不把用户拒绝当作应用崩溃。

## 工具批次

一次完整 Provider 响应中的所有 tool calls 组成 `ToolCallBatchRecord`。批次保存：

- batch/turn/assistant message identity；
- execution mode 和 max concurrency；
- 每个调用的 request、invocation、approval、result 和 result message；
- planned、interaction、execution、commit、terminal 或 recoverable phase。

批次必须先持久化 assistant tool-call message，再执行外部工具。结果无论顺序或并行执行，都按 Provider
原始 `source_index` 顺序提交并回灌模型。

## 顺序与并行

- `sequential`：逐个执行，适合有显式顺序或共享副作用的调用。
- `parallel`：受 `max_concurrency` 限制并发执行。
- AskUser 和审批会在实际执行前完成 preflight；未解决交互时不启动批次副作用。
- 一个调用失败不取消其他已经允许执行的调用；批次结果保留每项状态。

## AskUser

AskUser 调用进入 `waiting_user`，保存活动批次和问题。用户答案转换为该调用的 ToolResult，随后继续处理
同一批次。Run id 和 tool call id 必须匹配，不能把答案写入其他 waiting Run。

## 取消与进程

`ToolExecutionContext` 提供 abort 检查、可取消 sleep 和 shell 运行。Shell：

- 在独立进程组中启动；
- timeout 或 abort 时终止完整进程树；
- 等待 stdout/stderr 管道 EOF 后返回；
- 结果标记 timed_out 或 aborted。

插件工具应使用 execution context 的协作式 API，不能屏蔽取消信号。

workspace 约束只限制命令行工具的工作目录，不是操作系统命令沙箱；命令执行仍依赖权限策略、审批和部署
环境隔离。

## 恢复

进程退出时正在执行或提交结果的调用标记为 `unknown_outcome`。恢复不得自动重放这些调用。已经 durable
提交的结果通过 call id 去重；checkpointed result 可安全完成消息提交。

## Hook 与事件

工具调用周围分发 `before_tool_call` / `after_tool_call` Hook，并产生 batch/call/approval/AskUser 事件。
Hook 失败隔离为 `plugin_hook_failed`，不改变工具的实际结果。

## 验收证据

- `tests/tools/test_tools.py`
- `tests/tools/test_executor_permissions.py`
- `tests/application/test_tool_call_batches.py`
- `tests/application/test_runtime.py`
- `tests/cli/test_approval_commands.py`
- `tests/cli/test_tool_execution_config_commands.py`
