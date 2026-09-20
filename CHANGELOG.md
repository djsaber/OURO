# 版本能力摘要

## 0.1.0

OURO 是本地优先的 Agent 框架，包含：

- 可恢复的 Session/Run、files 与 SQLite 持久化、排队输入、撤销与分支。
- TUI 会话加载与模型可用性解耦：失效路由不阻断历史恢复与观察，保留历史上下文用量，并允许重新选择有效模型继续对话。
- Provider/Endpoint 配置、流式模型调用、工具权限、人工协同、资源与上下文压缩。
- CLI/TUI 共用 `openai-responses-effort` 思考预设，将端点等级与开关编码为 Responses 原生 `reasoning.effort`，沿用能力校验与 opaque 续接；配置构造函数支持端点自定义默认等级。
- Responses 思考展示支持标准 reasoning_text 与 reasoning_summary_text 的流式及完整输出；预设区分接收可见思考和显式请求摘要，允许终态省略已流出的思考文本，并保留原生续接与必要一致性校验。
- 协议实现、编解码器与思考预设按 OpenAI Chat Completions、OpenAI Responses、Anthropic Messages 明确命名；自定义接入统一为 `custom`，CLI 必须显式指定协议，TUI 提供三个协议入口，保留自定义 API 路径与已有供应商协议。
- Responses 流在完成事件前后均忽略未知扩展事件；已识别的输出与生命周期事件保留严格终态校验，兼容供应商附加元数据而不设置事件名称特例。
- 请求时的服务计费归属与全局用量账本；CLI/TUI 区分未知、零金额和部分金额，并展示未定价请求数及计费模式。
- 受管理认证基础：驱动契约、单次授权资源生命周期、受限凭据存储、跨进程刷新协调，以及 Provider/模板的认证引用与组合校验；通用 Application 登录、配置条件提交、安全状态与注销/凭据管理已接入轻量认证 CLI；现有协议已接入逐次请求认证、最终目标检查、共用重试和安全错误码；Runtime 已装配共享认证并支持默认/压缩路由绑定变化；TUI 已支持通用登录、状态、注销及本地凭据管理；首期 OpenAI 浏览器订阅已内建注册，完成持久登录、调用、重启续接与注销验收。
- Responses 协议适配、完整工具/加密 reasoning 续接、共用认证恢复与服务计费策略；订阅后端保留单一 max_output_tokens 本地预算并明确披露无请求输出硬上限。开发探针仅显式联网，源码与脱敏测试保留在 dev/probes 和 tests/probes，不随运行包发布。
- OpenAI API 与 Codex 订阅预制配置按官方目录提供 GPT-5.5、GPT-5.6 Luna/Terra/Sol 和 GPT-6 Astra，默认 Sol；API 新模板使用 Responses，按模型声明 reasoning 等级，保留各后端的预算与能力边界。
- 插件 Catalog 与代次租约、内置 Goal/Plan、Skills、MCP、自动任务和子智能体。
- 全屏 TUI，以及用于单次运行、配置、管理和脚本集成的 CLI。

使用方式见 [README](README.md)，接口与数据边界见[现行规格](dev/specs/README.md)。
