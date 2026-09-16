# 版本能力摘要

## 0.1.0

OURO 是本地优先的 Agent 框架，包含：

- 可恢复的 Session/Run、files 与 SQLite 持久化、排队输入、撤销与分支。
- Provider/Endpoint 配置、流式模型调用、工具权限、人工协同、资源与上下文压缩。
- 受管理认证基础：驱动契约、单次授权资源生命周期、受限凭据存储、跨进程刷新协调，以及 Provider/模板的认证引用与组合校验；通用 Application 登录、配置条件提交、安全状态与注销/凭据管理已接入轻量认证 CLI；现有协议已接入逐次请求认证、最终目标检查、共用重试和安全错误码；生产订阅、Runtime 共享认证装配、Responses 与 TUI 尚未完成。
- 插件 Catalog 与代次租约、内置 Goal/Plan、Skills、MCP、自动任务和子智能体。
- 全屏 TUI，以及用于单次运行、配置、管理和脚本集成的 CLI。

使用方式见 [README](README.md)，接口与数据边界见[现行规格](dev/specs/README.md)。
