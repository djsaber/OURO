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

## Hook

Hook 注册包含 plugin id、hook name、source、priority 和 handler。排序规则：official、third-party、
AI-generated，然后按 priority/plugin id。

非 official Hook 一次失败后在该 Run 的同一 hook 上隔离，Run 结束时清理隔离状态。Hook failure 转为
类型化记录和事件，不传播第三方异常。

## 插件注册边界

原生插件的包、安装、授权、运行时和贡献事务由[插件系统规格](plugin-system.md)定义。本规格保留共享
registry 的共同扩展边界：

- 插件贡献经 owner-aware registry 原子发布与撤销（`replace_owner`/`unregister_owner`），单项冲突导致整组
  不发布；
- 插件不得使用 `builtin.*` 压缩策略 id；
- entrypoint 只在授权后由 generation runtime 加载，注册边界本身不 import 插件代码；
- 目标实现不得重新建立一套绕过现有 ToolExecutor、Hook failure 和 durable Run 的平行执行路径。

## MCP 框架边界

MCP 已从注册占位边界演进为真实客户端。当前 transport、配置、状态机、工具转换、重连和关闭契约见
[MCP 客户端规格](mcp-client.md)。本规格只保留 MCP 与 Skills/插件 contribution 的共同扩展边界。

## 目标扩展能力

完整插件的包、来源信任、权限确认、entrypoint/worker 生命周期、贡献事务、durable generation、兼容、
更新与回滚由[插件系统规格](plugin-system.md)定义。本规格继续作为当前 Skills、Hook 和 contribution
注册边界的事实来源。

## 验收证据

- `tests/skills/test_skill_loader.py`
- `tests/skills/test_skill_manager.py`
- `tests/cli/test_skill_management_cli.py`
- `tests/core/test_hooks.py`
- `tests/plugins/test_plugin_system.py`
- `tests/mcp/test_manager.py`
