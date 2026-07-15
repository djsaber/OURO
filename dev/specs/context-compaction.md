# 上下文与压缩规格

状态：当前

## 目标与范围

上下文系统从 canonical messages 构建一次 Provider 请求视图，在不改写历史的前提下应用系统提示词、
Skills 指令、恢复投影、摘要压缩和模型 token 预算。

## ContextView

`ContextView` 包含：system prompt、messages、tools、token estimate、budget、overflow 状态和 metadata。
`ContextBuilder` 是纯构建器，不执行 Provider、文件或 SessionStore I/O。

系统提示词保持原始内容；有 Skills 指令时使用稳定空行拼接。恢复投影只在模型上下文中标记 incomplete
assistant 尝试，不把它升级为 canonical 完成消息。

## Token 预算

- 输入预算来自 Endpoint context window 减去 max output tokens。
- 估算覆盖 system prompt、message content、typed tool fields 和 tool definitions。
- 默认 estimator 是 Provider 无关的保守近似。
- 上下文超预算时不能静默截断 message 或拆开工具交换。

## 原子上下文帧

- 同一 `turn_id` 的 assistant 工具请求与结果属于一个模型回合帧。
- 其前的用户消息加入随后的 identified frame。
- 无 `turn_id` 的普通消息按用户请求分组。
- 含未完成工具请求的后缀整体受保护。

压缩计划只能覆盖 canonical 前缀，并至少保留最新完整帧。

## 压缩配置

`CompactionConfig` 包含：primary/fallback strategy、触发与目标比例、summary 输出预算，以及可选专用
Provider/Endpoint 和请求参数。专用 endpoint 解析失败时可回退到当前 Session runtime endpoint，并在
metadata 中记录原因；这是当前运行策略，不是配置格式兼容。

## 摘要执行

内置 `builtin.llm_summary` 策略：

1. 验证已有 active compaction lineage；
2. 计算可覆盖前缀和保留帧；
3. 将已有摘要与新增 messages 序列化为独立摘要请求；
4. 必要时按输入容量分块滚动摘要；
5. 累加 usage；
6. 生成 `CompactionArtifact`；
7. application 写入 `CompactionRecord` 并激活；
8. 重新构建 ContextView。

摘要请求不携带普通工具定义，不把摘要文本写回 canonical messages。

## CompactionRecord

记录至少包含：

- compaction/session/previous identity；
- strategy id/version 和 prompt version；
- trigger；
- summary；
- covered message ids；
- source digest；
- usage、model route 和 metadata。

激活时 message id 前缀和 digest 必须同时匹配当前 canonical history，否则抛出 `CompactionStateError`。

## 自动与手动压缩

- 自动压缩在上下文达到 trigger ratio 时运行。
- 手动 `session compact` 强制尝试覆盖至少一个旧帧。
- 无可压缩前缀时产生 skipped 结果，不创建空记录。
- 压缩 Run 使用同一 session lock、RunLifecycle、事件和 abort signal。
- Interactive Host 把 `started/progress/completed/failed/skipped` 事件投影为单行动态状态和稳定结果；压缩核心逻辑不依赖终端实现。

## 失败语义

- 摘要 Provider、格式或持久化失败产生 `compaction_failed`，不得激活部分记录。
- 失败后 canonical history 保持不变。
- 压缩后仍超预算时抛出 `ContextOverflowError` 或将 Run 以 `context_blocked` 暂停。
- 配置的 primary/fallback strategy 都失败时聚合明确错误。

## 验收证据

- `tests/core/test_context.py`
- `tests/core/test_compaction.py`
- `tests/application/test_compaction.py`
- `tests/cli/test_compaction_config_commands.py`
- `tests/cli/test_session_commands.py`
