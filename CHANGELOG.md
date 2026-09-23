# 当前能力摘要

OURO 是本地优先的 Agent 框架，提供以下能力：

- 可恢复的 Session/Run、文件与 SQLite 存储、排队输入、撤销、分支、摘要查询和附件。
- Provider/Endpoint 配置，OpenAI Chat Completions、OpenAI Responses 与 Anthropic Messages 协议，以及自定义接入。
- 流式文本、思考与工具参数展示；工具权限、人工协同、上下文预算及压缩。
- 独立认证驱动、受管理凭据、浏览器订阅登录、刷新、注销和跨进程协调；请求计费归属及全局用量账本。
- 插件 Catalog 与代次租约、内置 Goal/Plan、Skills、MCP、自动任务和子智能体。
- 全屏 TUI，以及支持单次运行、配置、管理和脚本集成的 CLI。
- 各领域配置、原生技能和完整会话树的导入导出；会话归档支持附件与 files/SQLite 跨后端往返。

TUI 的历史加载独立于模型路由可用性；模型菜单区分当前选择与系统默认值。主页用量区展示请求数、输入/输出 token 和缓存命中率。详细协议、默认配置与支持边界由对应规格负责。

使用方式见 [README](README.md)，数据与接口见[规格索引](dev/specs/README.md)，订阅限制见[订阅支持指南](docs/OURO_订阅支持使用指南_ZH_2026-09-19.md)。
