# 版本能力摘要

## 0.1.0

OURO 是本地优先的 Agent 框架，包含：

- 可恢复的 Session/Run、files 与 SQLite 持久化、排队输入、撤销与分支。
- Provider/Endpoint 配置、流式模型调用、工具权限、人工协同、资源与上下文压缩。
- 独立受管理凭据核心：受限文件存储、跨进程刷新协调、条件提交与注销；尚未接入订阅登录或模型调用。
- 插件 Catalog 与代次租约、内置 Goal/Plan、Skills、MCP、自动任务和子智能体。
- 全屏 TUI，以及用于单次运行、配置、管理和脚本集成的 CLI。

使用方式见 [README](README.md)，接口与数据边界见[现行规格](dev/specs/README.md)。
