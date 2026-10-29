# 数据导入导出

> 文档类型：使用指南

CLI 统一使用 `ouro data`。TUI 命令 `/export` 仅导出当前会话，不接受参数；未打开会话时提示先打开会话。
其他操作通过 Actions → Manage data 分页菜单进入；导出选择数据类型，导入选择归档后自动识别类型。
菜单提供文件浏览、全部或指定会话/技能选择，以及适用的覆盖选项；导出提供默认文件名，无需填写命令参数或会话 ID。
执行时菜单保留，使用加载动画显示后台传输状态，成功或失败都在菜单内展示，不向对话栏插入消息。
传输期间可以继续操作或关闭菜单，重新打开可查看状态；同一时间只提交一项传输。
操作结果使用同一文本格式；CLI 的 `--json` 额外提供结构化结果与诊断。

```bash
ouro data export --kind providers --path providers.zip
ouro data import --kind providers --path providers.zip --replace
ouro data export --kind provider-templates --path templates.zip
ouro data export --kind mcp --path mcp.zip
ouro data export --kind agents --path agents.zip
ouro data export --kind prompts --path prompts.zip
ouro data import --kind prompts --path prompts.zip --replace
ouro data export --kind skills --path skills.zip

# 不指定 --id 时，导出全部会话；导入归档内全部会话
ouro data export --kind sessions --path all-sessions.zip
ouro data import --kind sessions --path all-sessions.zip --home ./restored

# 同 ID 会话默认跳过；显式选择跳过或覆盖完整会话树
ouro data import --kind sessions --path all-sessions.zip --skip-existing
ouro data import --kind sessions --path all-sessions.zip --replace

# --id 可重复指定；导入时也可以从全量归档中筛选
ouro data export --kind sessions --id ses_one --id ses_two --path selected.zip
ouro data import --kind sessions --id ses_one --path all-sessions.zip --home ./restored
ouro data import --kind sessions --path all-sessions.zip --home ./restored-sqlite --session-backend sqlite
```

- 每次操作处理一种数据。Provider、Provider 模板、MCP、Agent 和个性化提示词配置按整个领域导入；
  已有非空配置需要 `--replace`，不会混合两份配置。Agent 与提示词导入均在目标当前 revision 上递增，
  不采用来源端的并发控制 token；提示词配置在下一次模型请求时读取，不要求重启正在运行的 Host。
- 会话可指定一个、多个或全量。选择子会话会包含其所属主会话、所有子会话、Delegation 和附件；普通 fork 的历史来源不要求同时导入。
  会话保留 ID，目标 ID 冲突时默认跳过整棵树，也可显式使用 `--skip-existing`；`--replace` 替换完整目标树，
  同时移除归档中不存在的旧子会话。跨树身份冲突或运行中的目标树拒绝覆盖；其他独立会话不受影响。
  TUI 的 `Existing sessions` 提供相同的 `Skip` / `Replace` 选项，结果列出跳过和覆盖数量。
  文件与 SQLite 后端可以互相传输；覆盖后应按结果提示重启已运行的 Host，重新加载会话和订阅。
- 会话选择列表使用本地目录索引或归档内独立的轻量 `index.json`，不加载完整会话和附件。
  缺少归档索引时从 manifest 投影 ID 与标题，不读取附件；实际导入仍校验完整数据及索引一致性。
- 导出要求会话树没有运行中的任务或未完成的 Run。归档包含消息、运行记录、摘要、Harness 检查点、输入与调用事实、
  插件状态、最近请求上下文及资源内容；不包含外部工作区文件、插件安装包、自动任务或独立计费账本。
- 未知插件、工具、模型以及插件私有状态版本不会阻止导入历史。引用及 opaque 状态原样保留，结果提供外部引用诊断；
  实际继续会话时仍由 Runtime 校验所需契约。导入不安装扩展、不执行工具、不自动恢复任务。
- `skills` 导入导出 `OURO_HOME/skills` 下完整原生技能目录，包括脚本和资源；导出可用 `--id <技能名>` 筛选。
  不复制发现开关、第三方 Host 技能目录或插件贡献技能。导入按同名技能冲突检查，`--replace` 替换同名目录；
  被替换技能的旧安装来源记录会清除，其他发现策略不变。
- 归档采用带类型和版本的 ZIP，校验不通过时明确拒绝；拒绝路径穿越、链接、重复路径、超限归档和损坏附件。
  上限为 10,000 个归档条目和 512 MiB 解压后数据。导出目的地不能覆盖 OURO 管理的数据目录。
- 配置归档包含配置中原有的字面量值，可能包含内联密钥、请求头或环境配置；按私密备份保管。
  `credentials/` 中的托管凭据不导出，跨机器后需重新登录或补齐环境变量。会话归档同样可能包含私密对话。
- 导入的技能在下一次目录查询或模型请求中可用，个性化提示词在下一次模型请求中生效；其他配置按结果提示重启 Host 后加载。

失败语义和存储边界见[配置与数据传输规格](../specs/data-transfer.md)。
