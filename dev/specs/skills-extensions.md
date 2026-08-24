# Skills 与扩展规格

状态：当前

## Agent Skills

每个 Skill 是包含 `SKILL.md` 的目录。Frontmatter 当前支持：

- `name`、`description`；
- `license`、`compatibility`、`metadata`；
- `allowed-tools`；
- `user-invocable`、`disable-model-invocation`、`argument-hint`；
- 未识别社区字段保存在 `extensions`。

`name` 和 `description` 必填。YAML、字段类型和 `allowed-tools` 格式不合法时产生 diagnostic；parser 不修复
无效 YAML、不从目录名补 name、不把列表转换为字符串。

## Skills 发现

用户级 root：

- `OURO_HOME/skills`；
- 用户 profile 下 `.agents`、`.claude`、`.copilot`、`.codex` 的 skills 目录。

工作区 root：

- `.ouro/skills`；
- `.agents`、`.claude`、`.github`、`.codex` 的 skills 目录；
- 当前配置中显式 paths。

固定目录来源同时记录 Host origin 与 scope，并使用 `<origin>.<scope>` 稳定 id。当前 id 为：

- OURO：`ouro.user`、`ouro.workspace`；
- 开放/兼容 Host：`agents.user`、`agents.workspace`、`claude.user`、`claude.workspace`、
  `copilot.user`、`github.workspace`、`codex.user`、`codex.workspace`。

所有固定来源默认启用；`skills.disabled_sources` 只保存关闭项，因此可分别关闭 OURO 原生目录、其他 Host
目录以及 user/workspace scope。`skills.enabled=false` 是 Host 目录发现总开关。显式 `skills.paths` 各自使用
`enabled`，默认同样为 true。CLI 与 TUI Actions 复用同一应用层策略；TUI 切换后同步刷新当前 Runtime。
旧的 `scan_*` 粗粒度字段不再接受，避免一个开关同时改变多个 scope。

插件 Skill 不属于目录发现源。它是启用插件 Generation 的 snapshot-bound contribution，由插件授权、启停、
依赖和 execution contract 共同管理；若插件被关闭，其 Skill 与其他贡献在同一 Catalog 事务中撤销。Skill
目录策略不得只过滤插件包的一部分贡献，否则会制造与插件生命周期不一致的第二控制平面。

后扫描 root 以同名覆盖前项并产生 shadow diagnostic。扫描受 depth 和 directory count 限制，忽略常见依赖
和缓存目录。catalog 阶段只读取 metadata；完整正文和资源在激活时加载。

## Skills 管理

- install 支持本地目录和 Git URL；单 Skill 自动选择，集合要求 `--skill` 或 `--all`。
- 安装前校验所有候选，不复制符号链接、忽略 VCS/缓存目录，并限制文件数量和总大小。
- 部署通过同文件系统 staging/backup 原子替换。
- 来源、scope、target、selector 和 ref 记录在中央配置，不修改 `SKILL.md`。
- remove 只能删除 OURO 原生管理 root 内的目标；外部社区 root 不受管理。
- enable/disable 是中央策略，不改写第三方内容。

## Prompt 注入

可供模型调用且未禁用的 Skills 被渲染为原生系统指令目录，包含 name、description 和 location。完整
`SKILL.md` 不在每次请求中展开；模型需要使用时再读取内容。

## Observer 与 Prompt Policy

当前 Observer 包含 `runtime_event`、`context_prepared`、`tool_call_requested`、`tool_call_finished` 和
`run_finished` 五类类型化事实。handler 只能返回 `None`；它不改写 Context、工具参数、工具结果或 Run 状态。

Application 为每个 registration 建立独立有界 FIFO mailbox。生产点只做非阻塞 enqueue，不等待插件；慢、超时、异常或
满队列只隔离对应 observer/scope，并通过脱敏 `plugin_observer_failed` 诊断暴露。队列项持有精确 Catalog lease，直到
处理、丢弃或关闭时恰好释放一次。可靠事实仍以 canonical event store 为准，Observer 是 at-most-once 进程内通知。

`prompt_policies` 是独立的 fail-closed guard，只检查 Host 路由后的直接用户提示并返回 `allow | deny`，不能改写提示。User plugin
必须声明 `sessions.prompt.inspect`；拒绝或失败发生在提示进入 canonical message、durable queue 或 Run 前。允许后，直接
Run 或 queued prospective Run 都继续使用评估时固定的同一 Catalog execution contract。完整契约见
[插件系统规格](plugin-system.md)和
[插件观察器与语义扩展点设计](../../docs/OURO_插件观察器与语义扩展点设计_2026-07-29.md)。

## 插件注册边界

原生插件的包、安装、授权、运行时和贡献事务由[插件系统规格](plugin-system.md)定义。插件是系统级模块，
只存在 builtin 与 user 来源；本规格中的 workspace roots 仅用于发现 Skills，不构成 workspace/project 插件。
本规格保留共享 registry 的共同扩展边界：

- 插件贡献经 owner-aware registry 原子发布与撤销（`replace_owner`/`unregister_owner`），单项冲突导致整组
  不发布；
- 插件不得使用 `builtin.*` 压缩策略 id；
- entrypoint 只在授权后由 generation runtime 加载，注册边界本身不 import 插件代码；
- 目标实现不得重新建立一套绕过现有 ToolExecutor、类型化 Policy、Observer failure 和 durable Run 的平行执行路径。

## MCP 框架边界

MCP 已从注册占位边界演进为真实客户端。当前 transport、配置、状态机、工具转换、重连和关闭契约见
[MCP 客户端规格](mcp-client.md)。本规格只保留 MCP 与 Skills/插件 contribution 的共同扩展边界。

## 目标扩展能力

插件的目标包模型、来源信任、权限确认、entrypoint/worker 生命周期、贡献事务、durable execution contract、
更新与回滚由[插件系统规格](plugin-system.md)定义。本规格继续作为当前 Skills、Observer、Prompt Policy 和
contribution 注册边界的事实来源。

## 验收证据

- `tests/skills/test_skill_loader.py`
- `tests/skills/test_skill_manager.py`
- `tests/cli/test_skill_management_cli.py`
- `tests/core/test_observers.py`
- `tests/core/test_prompt_policies.py`
- `tests/plugins/test_plugin_system.py`
- `tests/mcp/test_manager.py`
