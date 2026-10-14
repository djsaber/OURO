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
uv run --frozen ouro plugin purge-data dev.example.plugin --home ./.ouro-dev
```

`plugin maintain <id> --home <home> --timeout 300 --execute <程序及参数>` 在停用插件的私有目录中执行明确指定的
维护程序。所有 OURO 参数放在 `--execute` 之前；脚本、解释器和备份路径建议使用绝对路径。入口与数据清理共享
独占访问检查，不启动插件或模型，不自动重新启用。备份和恢复规则由维护程序负责，参见
[Scheduled Sync 样例](../../examples/plugins/scheduled-sync/README.md)。

`remove` 移除安装记录，默认保留插件私有数据；有可恢复工作引用时，结果列出包代码的保留原因。保留代码不等于
仍然安装或启用；继续相关工作前需明确安装所需包并满足其配置契约，或结束工作后执行 `gc` 回收代码。
`purge-data` 显式删除私有数据，可在卸载后单独执行。先停用插件并等待所有
Host 的相关资源退出；仍有占用或可恢复工作的引用时，清理会拒绝。它不删除 Session 历史、Task 记录、用量、
制品或其他插件的数据，也不会为执行清理启动插件。

## 配置插件

TUI 的 Plugins 菜单中，插件操作提供 **Settings**。普通字段使用现有输入与选择控件；复杂字段以 JSON
编辑，并根据 Schema 显示示例。**Settings (JSON)** 编辑整份普通值，凭据使用独立槽位。输入不会立即写入有效配置；
**Validate** 只做静态预检，**Apply** 才提交。失败保留当前面板中的草稿；发生冲突后，
通过 **Reload** 确认丢弃草稿、读取最新值与基线后重新编辑。取消刷新会保留当前草稿，
关闭整个配置面板则放弃尚未应用的编辑。

用下面的 PowerShell 命令体验不执行业务逻辑的配置展示插件：

```powershell
uv run ouro plugin install ./examples/plugins/configuration-playground --home ./.ouro-dev
uv run ouro plugin configuration dev.ouro.configuration_playground --home ./.ouro-dev --export ./plugin-draft.json --json
```

导出的 JSON 包含插件身份、目标包 digest、编辑基线、普通 `values` 及 `credential_refs`。编辑取值时保留
身份和基线。TUI 中选中凭据项按 Enter 直接编辑，Tab 切换两种来源：

- **KEY**：直接输入密钥，保存在本地配置中；对应 `{"type":"inline","value":"actual-key"}`。
- **ENV**：输入 `env:OURO_CONFIGURATION_EXAMPLE_KEY` 等环境变量引用。停用状态保存只检查引用形式，
  启用时才要求当前 Host 的环境变量存在且非空。

Enter 保存当前输入与类型，Esc 取消本次修改；已绑定的行右侧显示遮蔽符及 `[KEY]` / `[ENV]`。

例如，导出草稿中的绑定可以写为：

```json
{
  "credential_refs": {
    "example_key": {"type": "inline", "value": "playground-test-key"},
    "optional_key": "env:OURO_OPTIONAL_KEY"
  }
}
```

展示插件不请求远程服务，必填 `example_key` 可用测试值。可选槽位不需要时从映射中省略。
列表和编辑框遮蔽凭据内容；普通查询与预检输出也脱敏。显式导出的编辑草稿包含保存的直接密钥，文件
仅允许当前用户访问。直接密钥在本地文件中按明文保存，环境变量引用不会在导出时解析为密钥。

```powershell
uv run ouro plugin configure dev.ouro.configuration_playground --home ./.ouro-dev --file ./plugin-draft.json --preview --json
uv run ouro plugin configure dev.ouro.configuration_playground --home ./.ouro-dev --file ./plugin-draft.json --json
```

`--preview` 不启动插件或更改状态。`--values` 可直接提交完整普通值对象，`--credentials` 提交完整槽位绑定映射，
省略后者表示空映射。仅凭据插件允许只提供 `--credentials`。`--digest` 固定编辑某个已安装包；
`--expected-revision` 可为内联提交指定已知基线。文件提交始终保留文件中的原始基线，过期时拒绝覆盖。

返回结果区分 `unchanged`、`saved` 和 `published`，分别表示未变化、保存但未激活，以及当前 Host 已发布。
发布后的 `status` 可以为 `degraded`，应查看诊断；其他 Host 按各自同步边界与环境落实配置。普通值和凭据
绑定均是完整替换，缺失字段按 Schema 默认规则处理，不表示保留旧值。不要直接编辑插件 state 或包内文件。

结构、默认值、凭据和运行时语义见[配置规格](../specs/plugin-configuration.md)。示例的字段与操作说明见
[Configuration Playground](../../examples/plugins/configuration-playground/README.md)。

## 权限与版本生命周期

`python-inprocess` 执行与 OURO 同进程的受信代码；worker 提供进程和协议隔离，但不是 OS 沙箱。启用、选择、重载或回滚精确的包 digest 才执行相应代码；`update` 只安装候选版本，不自动切换。

候选准备失败时保留旧目录；已发出的模型请求及其调用保持原绑定，后续决策采用当前插件能力。插件凭据只接受已声明槽位的环境变量引用。社区包先用 `plugin import-report` 查看逐项兼容报告，导入不会执行未知 Hook 或安装脚本。

完整权限、Catalog、取消和跨进程恢复规则见[插件规格](../specs/plugins.md)；开发新插件见[开发指南](../development/plugin-development.md)。

## Goal 与 Plan

内置 `dev.ouro.goal` Goal 插件提供跨多个有界 Run 的持久目标模式，并随其他 OURO 内置插件
默认启用。可直接使用：

```text
/goal [--token-budget N | --no-token-budget] <request>
/goal status
/goal pause
/goal resume [--token-budget N | --no-token-budget]
/goal replace [--token-budget N | --no-token-budget] <request>
/goal cancel
/goal clear
```

创建命令直接保存并激活目标，模型也可通过 `CreateGoal` 响应明确的自然语言创建请求。
创建时省略预算表示不设置；恢复和修改时省略表示保留，`--no-token-budget` 显式清除预算。
`clear` 是 `cancel` 的别名。停止期间仍可补充信息、讨论状态并使用只读工具，普通输入不会恢复目标。
一次 Run 的自然结束不会完成 Goal；模型必须通过 `UpdateGoal` 明确报告
`complete` 或 `blocked`。用户保留
pause、resume、replace、cancel 与预算权限。Goal 与 Plan 竞争同一个 primary behavior claim：退出 Plan 使用
`/plan off`；Goal 完成或 `/goal cancel` 后才释放该控制权，`/goal pause` 仍保留占用。完整设计与验收边界见
[Goal 插件](../specs/goal-plugin.md)。

Plan 模式通过 `/plan on` 进入、`/plan status` 查询、`/plan off` 退出。模式允许只读调查及修改指定计划文件，其他写入工具受限；模型通过 `ExitPlanMode` 提交评审，用户接受、修改或拒绝。AFK 按已配置的无人值守策略处理评审。详见[Plan 模块说明](../../ouro/builtin_plugins/plan/README.md)与[AFK 契约](../specs/afk.md)。
