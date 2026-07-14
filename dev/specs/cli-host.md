# CLI 与 Host 规格

状态：当前

## 目标与范围

CLI 是组合根和终端界面适配器。它解析参数、构建 application 服务、读取用户输入并格式化结果，不能实现
运行、会话、Provider 或工具业务规则。

## 顶层命令

```text
ouro [interactive]
ouro run
ouro session
ouro approve | reject | abort
ouro config
ouro provider
ouro skills
```

命令名只有当前权威形式，不保留同义 alias。

## 运行模式

### Interactive

无子命令时启动持续 stdin/stdout Host。支持 prompt、session 切换、新建、模型查看/切换、历史、导出、
Skills 和退出等 slash command。流式文本、reasoning、工具调用、审批和 AskUser 使用独立 I/O 适配器。
Interactive 直接消费 `model_tool_call_delta`：分片到达时更新当前工具预览，模型响应完成后按 source index
定稿；不得等到工具预检或执行事件才首次展示调用，也不得把未完成参数当作可执行 JSON。

### Print

`run --print` 输出 reasoning 渲染和最终文本，适合人工 shell 使用。未显式选择模式时 `run` 默认 Print。

### JSON

`run --json` 和各管理命令的 `--json` 只向 stdout 输出单个机器可读 JSON；诊断不得混入 stdout。

## 命令资源层级

- `provider set/show/list/use/remove/check` 管理供应商；
- `provider endpoint ...` 管理具体 Endpoint；
- `session ...` 管理持久化会话；
- `config prompt|permissions|tool-execution|queued-inputs|run-budget|compaction` 管理独立配置分区；
- `skills ...` 管理 Agent Skills；
- `approve/reject/abort` 恢复或终止 active Run。

Provider check 的 provider id 使用唯一位置参数；运行时覆盖使用 endpoint 和参数 flags。Provider 创建和
Endpoint 创建是独立命令。Skills 单项安装可省略 selector，集合必须明确选择。

## Home 与覆盖

Home 解析优先级：子命令 `--home`、顶层 `--home`、`OURO_HOME`、`~/.ouro`。System prompt 和运行 Provider
参数可按命令覆盖，但不会隐式持久化。

## Application 边界

- Session 管理命令通过 `ApplicationFacade`。
- Runtime 命令通过 `RuntimeService`。
- Provider、config 和 Skills 命令通过类型化 `ConfigurationFacade`；公开 DTO 不暴露凭据值。
- CLI 不直接编辑 JSONL、Run 文件或 index。

`session messages/events` 使用 byte cursor 和 limit 增量读取 JSONL；响应返回 `next_cursor`。`run --resource`
可重复指定当前 Session 已导入资源，资源解析与模型投影由 application 完成。

`run --queue-mode follow_up|steer` 在目标 Session 已有 active Run 时选择交付边界。Interactive 中普通 prompt
默认 follow-up，`/follow <prompt>` 与 `/steer <prompt>` 可显式选择。消费批量由配置而不是 Host 决定。

## 输出与安全

- 公开 Provider/Endpoint JSON 脱敏 credential 和敏感扩展字段。
- 错误包含稳定的类型和可操作上下文，不输出 token/key。
- 交互渲染失败与 durable Run 隔离。
- 导出到文件使用明确路径，stdout 导出不夹杂状态文本。

## 退出码

- `0`：成功；
- `2`：配置或参数错误；
- `3`：Provider connection check 失败；
- `4`：等待审批/用户输入；
- `5`：运行中止；
- `6`：工具相关失败；
- `7`：持久化失败；
- 其他 OURO error 由统一映射决定。

同一 RunResult 在 Print 和 JSON 模式下必须得到相同语义退出码。

## 验收证据

- `tests/cli/test_run_cli.py`
- `tests/cli/test_session_commands.py`
- `tests/cli/test_approval_commands.py`
- `tests/cli/test_error_codes.py`
- `tests/cli/test_permissions_commands.py`
- `tests/cli/test_tool_execution_config_commands.py`
- `tests/cli/test_run_budget_config_commands.py`
- `tests/cli/test_queued_inputs_cli.py`
