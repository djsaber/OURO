# 安装与使用插件

> 文档类型：使用指南

原生插件使用 `.ouro-plugin/plugin.json`。插件是系统级扩展，只分 builtin 与 user；安装、state、CLI 和 DTO
均不定义 workspace/project scope。安装只做静态校验和不可变发布，不 import entrypoint、启动 worker 或 MCP。
用户通过 `enable` 显式接受并运行当前 selected digest；`update` 只并排安装候选，只有显式 `select`、`reload` 或
`rollback` 才切换版本。激活前 Host 重新校验不可变包内容、配置、依赖和 Manifest 契约。

```bash
uv run --frozen ouro plugin scaffold ./my-plugin --plugin-id dev.example.plugin --name "Example plugin"
uv run --frozen ouro plugin validate ./my-plugin --home ./.ouro-dev
uv run --frozen ouro plugin install ./my-plugin --home ./.ouro-dev
uv run --frozen ouro plugin enable dev.example.plugin --home ./.ouro-dev

uv run --frozen ouro plugin list --home ./.ouro-dev
uv run --frozen ouro plugin show dev.example.plugin --home ./.ouro-dev
uv run --frozen ouro plugin commands --home ./.ouro-dev
uv run --frozen ouro plugin run-command dev.example.plugin:command "argument" --home ./.ouro-dev

uv run --frozen ouro plugin update dev.example.plugin ./new-version --home ./.ouro-dev
uv run --frozen ouro plugin select dev.example.plugin <candidate_digest> --home ./.ouro-dev
uv run --frozen ouro plugin reload dev.example.plugin ./my-plugin --home ./.ouro-dev
uv run --frozen ouro plugin rollback dev.example.plugin <package_digest> --home ./.ouro-dev
uv run --frozen ouro plugin disable dev.example.plugin --home ./.ouro-dev
uv run --frozen ouro plugin remove dev.example.plugin --home ./.ouro-dev
```

## 权限与版本生命周期

`python-inprocess` 执行与 OURO 同进程的受信代码；worker 提供进程和协议隔离，但不是 OS 沙箱。启用、选择、重载或回滚精确的包 digest 才执行相应代码；`update` 只安装候选版本，不自动切换。

候选准备失败时保留旧目录；已发出的模型请求及其调用保持原绑定，后续决策采用当前插件能力。插件凭据只接受已声明槽位的环境变量引用。社区包先用 `plugin import-report` 查看逐项兼容报告，导入不会执行未知 Hook 或安装脚本。

完整权限、Catalog、取消和跨进程恢复规则见[插件规格](../specs/plugins.md)；开发新插件见[开发指南](../development/plugin-development.md)。

## Goal 与 Plan

内置 `dev.ouro.goal` Goal 插件提供跨多个有界 Run 的持久目标模式，并随其他 OURO 内置插件
默认启用。可直接使用：

```text
/goal [--token-budget N] <request>
/goal status
/goal pause
/goal resume [--token-budget N]
/goal replace [--token-budget N] <request>
/goal cancel
```

省略 `--token-budget` 即不设预算。一次 Run 的自然结束不会完成 Goal；模型必须通过 `UpdateGoal` 明确报告
`complete` 或 `blocked`。用户保留
pause、resume、replace、cancel 与预算权限。Goal 与 Plan 竞争同一个 primary behavior claim：退出 Plan 使用
`/plan off`；Goal 完成或 `/goal cancel` 后才释放该控制权，`/goal pause` 仍保留占用。完整设计与验收边界见
[Goal 插件](../specs/goal-plugin.md)。

Plan 模式通过 `/plan on` 进入、`/plan status` 查询、`/plan off` 退出。模式允许只读调查及修改指定计划文件，其他写入工具受限；模型通过 `ExitPlanMode` 提交评审，用户接受、修改或拒绝。AFK 按已配置的无人值守策略处理评审。详见[Plan 模块说明](../../ouro/builtin_plugins/plan/README.md)与[AFK 契约](../specs/afk.md)。
