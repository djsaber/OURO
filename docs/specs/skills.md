# Skills 与扩展规格

> 文档类型：现行规格

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
`scan_*` 粗粒度字段无效；来源开关必须使用稳定 source id，避免一个配置同时改变多个 scope。

插件 Skill 不属于目录发现源。它是启用插件 Generation 的 snapshot-bound contribution，由插件授权、启停、
依赖和 execution contract 共同管理；若插件被关闭，其 Skill 与其他贡献在同一 Catalog 事务中撤销。Skill
目录策略不得只过滤插件包的一部分贡献，否则会制造与插件生命周期不一致的第二控制平面。

后扫描 root 以同名覆盖前项并产生 shadow diagnostic。扫描受 depth 和 directory count 限制，忽略常见依赖
和缓存目录。发现时只读取 `SKILL.md` 的 frontmatter，catalog 只保留 metadata 投影，不读取完整正文或枚举资源目录。
资源目录遍历的剪枝与工作线程边界见[性能规格](performance.md#文件搜索与技能目录)。

运行实例在技能目录查询、接收显式技能引用和每轮模型请求准备时重新发现目录技能，应用当前来源配置，并与启用插件的
技能贡献合并。安装、更新、删除和启停策略在下一使用边界生效；没有定时轮询、文件监听或跨边界的发现结果缓存。
目录内容相同时复用现有 Extension Catalog；目录变化通过现有 Catalog 发布边界生效，已发出的请求保持其绑定。
技能管理和发现配置的变更结果使用 `effective_from=next_request`；技能归档导入不要求重启 Host。

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

每轮模型请求使用当前技能目录，与用户是否显式 `@` 引用无关。会话中安装的技能在下一轮请求进入模型视口，
用户可直接用自然语言提出任务，由模型选择技能。

## TUI 显式引用

仅在输入框首位键入 `@` 可选择 `@skill`，也可输入 `@skill ` 进入技能列表。正文中、空白后或已有元素后的 `@` 不触发菜单。列表只提供已启用且允许用户调用的技能，
支持名称过滤、↑/↓ 导航、Space 多选和 Enter 确认；Esc 取消并恢复原草稿与光标。选择过程不发送消息或创建 Session。
与 `@file` 共用 CommandPopup、主题选择标记和原子编辑组件。已加入草稿的同名技能不重复列出。
每次打开 `@skill` 列表异步读取当前目录；本次菜单内的过滤、导航和多选只操作内存中的候选，不重复扫描。
关闭菜单或切换会话后，尚未完成的读取结果不再更新该菜单。
顶部常驻 `Skills` 标题，右侧固定以低亮度 `[x]` 显示已选数量，不占用列表条目。滚动、过滤无匹配时保留顶部行；无匹配但有已选技能时，Enter 仍添加全部所选技能。

选择后的技能以 `@名称` 独立元素显示，正文不包含显示标签。可以与多个文件引用混排，整体删除、撤销/重做，
并在草稿保存、会话切换、重启、发送失败、消息撤销和排队撤回时恢复引用身份、顺序与位置。

发送时按当前目录解析技能名称，并读取所选绝对路径上的完整 `SKILL.md`（包括 frontmatter）。Host 负责选择与读取，
不比较历史元数据、不验证技能是否变更，也不审查文档内容或要求正文非空。不存在、不可读取或不能解码的文件产生
明确加载错误。目录发现仍要求可解析的 name、description 等索引字段；启用状态和用户调用策略决定是否可选。

全部技能作为同一份直接用户输入保存指令，允许只有技能而无正文的输入，沿用 follow-up 交付限制。
现有 Prompt Policy 契约逐一检查各技能，全部通过才接受整份输入。多个技能位于同一条用户消息的类型化内容块中；
存在文件时全部文件共用一条完整附件骨架，不为每个技能或文件创建骨架，也不逐项启动 Run。
排队保存同一输入及全部冻结指令，后续技能文件变化不改变已接受内容。
激活块的 content digest 标识已接受的输入，不作为后续技能加载的校验依据。

激活块保存 `SKILL.md` 的完整绝对路径与技能基目录，并将二者提供给模型；完整分支保留已冻结的激活块。
按消息 fork 和 `/undo` 恢复结构化技能元素，与附件一同重新提交时按当前可用目录重新激活，不把 `@名称` 显示文字当作技能身份。

显式激活的指令在所属 Run 的每次模型请求中保持完整可见。会话帧被压缩后，请求准备层从 canonical 激活块
恢复 user 角色的临时指令上下文，并计入预算与请求快照；它不依赖摘要内容，也不阻止同一 Run 内压缩旧帧。
仍在会话尾部的激活只使用原消息，不重复注入。需要恢复的激活由持久化压缩边界所属 Run 决定，
在下次压缩前保持稳定；切换 Run 或重建运行实例不移除已有前缀，也不重新加载技能正文。

## Observer 与 Prompt Policy

当前 Observer 包含 `runtime_event`、`context_prepared`、`tool_call_requested`、`tool_call_finished` 和
`run_finished` 五类类型化事实。handler 只能返回 `None`；它不改写 Context、工具参数、工具结果或 Run 状态。

Application 为每个 registration 建立独立有界 FIFO mailbox。生产点只做非阻塞 enqueue，不等待插件；慢、超时、异常或
满队列只隔离对应 observer/scope，并通过脱敏 `plugin_observer_failed` 诊断暴露。队列项持有精确 Catalog lease，直到
处理、丢弃或关闭时恰好释放一次。可靠事实仍以会话业务记录为准，Observer 是 at-most-once 进程内通知。
Runtime event 在业务提交和 Host sink 投递后先执行轻量 registration/type/controller 匹配；已知没有匹配
Observer 时不得构造脱敏冻结投影、获取额外 Catalog lease 或进入 dispatcher。Catalog 归属无法确定时保守执行完整路径。

`prompt_policies` 是独立的 fail-closed guard，检查 Host 路由后的用户提示并返回 `allow | deny`，不能改写提示。User plugin
必须声明 `sessions.prompt.inspect`。接收阶段拒绝不创建消息、队列输入或 Run；消费阶段拒绝保留已接受的队列输入。
Skill 激活正文和输入来源作为接受事实保留；模型决策使用当前 Catalog。完整契约见
[插件系统规格](plugins.md)。

## 插件注册边界

原生插件的包、安装、授权、运行时和贡献事务由
[插件系统规格](plugins.md)定义。插件是系统级模块，
只存在 builtin 与 user 来源；本规格中的 workspace roots 仅用于发现 Skills，不构成 workspace/project 插件。
本规格保留共享 registry 的共同扩展边界：

- 插件贡献经 owner-aware registry 原子发布与撤销（`replace_owner`/`unregister_owner`），单项冲突导致整组
  不发布；
- 插件不得使用 `builtin.*` 压缩策略 id；
- entrypoint 只在授权后由 generation runtime 加载，注册边界本身不 import 插件代码；
- 扩展能力不得重新建立一套绕过现有 ToolExecutor、类型化 Policy、Observer failure 和 durable Run 的平行执行路径。

## MCP 框架边界

MCP 是具有真实 transport、配置、状态机、工具转换、重连与关闭生命周期的客户端，完整契约见
[MCP 客户端规格](mcp.md)。本规格只保留 MCP 与 Skills/插件 contribution 的共同扩展边界。

## 插件扩展边界

插件的目标包模型、来源信任、权限确认、entrypoint/worker 生命周期、贡献事务、durable execution contract、
更新与回滚由[插件系统规格](plugins.md)定义。本规格继续作为当前 Skills、Observer、Prompt Policy 和
contribution 注册边界的事实来源。

## 验收证据

- `tests/skills/test_skill_loader.py`
- `tests/skills/test_skill_manager.py`
- `tests/cli/test_skill_management_cli.py`
- `tests/core/test_observers.py`
- `tests/core/test_prompt_policies.py`
- `tests/plugins/test_plugin_system.py`
- `tests/mcp/test_manager.py`
