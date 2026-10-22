# 配置工具、指令与 Skills

> 文档类型：使用指南

个性化基础 / 追加提示词见[配置文件指南](configuration.md)。下例中的 Provider、Endpoint 和 Skill 标识须替换为实际目录中的值。

```bash
# Shell 工具名取决于宿主检测结果；Windows 通常为 PowerShell，POSIX 通常为 Bash
uv run --frozen ouro config permissions --home ./.ouro-dev --tool PowerShell --mode approval
uv run --frozen ouro config permissions --home ./.ouro-dev --tool Write --mode deny
uv run --frozen ouro config permissions --home ./.ouro-dev --plugin dev.example.plugin --mode approval
uv run --frozen ouro config tool-execution --home ./.ouro-dev --mode parallel --max-concurrency 4 \
  --workspace-only --max-output-chars 100000
uv run --frozen ouro config queued-inputs --home ./.ouro-dev --consumption-mode all-at-once
uv run --frozen ouro config run-budget --home ./.ouro-dev --max-model-rounds 50
uv run --frozen ouro config compaction --home ./.ouro-dev --provider <provider_id> --endpoint <endpoint_id>
uv run --frozen ouro config skills --home ./.ouro-dev --disable-source codex.user

uv run --frozen ouro skills list --home ./.ouro-dev
uv run --frozen ouro skills show pdf --home ./.ouro-dev
uv run --frozen ouro skills roots --home ./.ouro-dev
uv run --frozen ouro skills validate ./path/to/skill --json
uv run --frozen ouro skills install ./path/to/skill --home ./.ouro-dev
uv run --frozen ouro skills install github:owner/repo --skill skill-name --ref v1.0.0 --home ./.ouro-dev
uv run --frozen ouro skills disable skill-name --home ./.ouro-dev
uv run --frozen ouro skills update skill-name --home ./.ouro-dev
uv run --frozen ouro skills remove skill-name --home ./.ouro-dev
```

默认启用的 `dev.ouro.agent_context` 内置插件会在每次模型请求时加载开放规范的 `AGENTS.md`：依次读取
`<user-home>/.agents/AGENTS.md`、`OURO_HOME/AGENTS.md`，再从 Git 项目根目录到当前 Session workspace
逐层拼接 `AGENTS.md`。越靠后的项目规则越具体并在冲突时优先；合计上限为 32 KiB。没有 Git 根时不会向
workspace 外继续搜索。该机制只接受 `AGENTS.md`，不会把产品私有的 `CLAUDE.md`、`GEMINI.md`，或具有不同
生命周期的 `MEMORY.md`、`SYSTEM.md`、`SKILL.md` 当作别名。它追加规则而不替换基础 system prompt，并保留
原有请求级系统环境信息注入。

Skills 使用标准 `SKILL.md` YAML frontmatter。用户级原生目录是 `OURO_HOME/skills`，工作区原生目录是
`./.ouro/skills`；也可扫描 `.agents`、`.claude`、`.github`、`.copilot` 和 `.codex` 的 Skills 目录。
目录来源按 Host 与作用域使用稳定 id，例如 `ouro.user`、`ouro.workspace`、`codex.user` 和
`codex.workspace`。所有来源默认启用，可通过 `ouro config skills --disable-source <id>` 独立关闭；
TUI 的 `Actions > Skill sources` 提供相同的总开关与逐源开关，并即时刷新当前 Runtime；
自定义 `skills.paths` 条目使用自身的 `enabled` 字段。插件 Skill 属于插件贡献契约，继续通过所属插件启停，
不受目录发现源开关影响。格式错误会产生明确诊断，OURO 不会猜测或修复无效 frontmatter。

在 TUI 输入框首位输入 `@skill ` 选择技能：↑/↓ 移动，Space 多选，Enter 将所选技能加入输入框，Esc 取消。菜单顶部常驻技能标题，右侧以低亮度 `[x]` 显示已选数量。技能显示为独立的 `@名称` 元素，可与 `@file` 文件引用一起编辑、删除和撤销。随后输入任务并发送，所有引用随同一条输入提交。

会话中由 Agent、CLI 或其他工具安装的技能，无需重启 TUI：再次打开 `@skill` 即读取当前可用列表，发送时读取
所选技能的当前文件。直接发送自然语言任务同样会在下一次模型请求中刷新技能目录，Agent 可自行选择新安装的技能。
更新或删除技能也在下一次使用时生效。已经接受并排队的输入保留提交时的指令。
