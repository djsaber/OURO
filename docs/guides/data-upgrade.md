# 升级已有用户数据

> 文档类型：使用指南

`ouro-upgrade` 提供有限的离线核心数据升级，在指定 home 的原位置操作。它随当前 `ouro-agent` 安装，
不会升级 OURO 程序。支持的历史格式、保留范围与故障边界见[数据升级规格](../specs/data-upgrade.md)。

## TUI 自动升级与初始化兜底

TUI 启动遇到数据版本错误或已识别的旧提供方能力配置时，会关闭本次启动的服务，自动对当前 home 和所选 Session / Task 后端执行升级。
界面在终端中央显示检查、备份、写回和验证阶段。文件后端检查显示已完成的会话或任务数，源数据复核、备份和写回显示路径或文件数，
并使用当前主题的加载动画提示仍在处理。计数表示当前阶段，不能换算为整体进度；较大的单个会话仍可能需要较长检查时间。
升级成功后重新加载主页；原来的 `--session` 选择不会自动打开。正常启动不执行全目录升级检查。

升级失败会显示原因并提供初始化或退出选项。写回中断且有恢复标记时，还可选择恢复备份并重新升级。
界面沿用当前主题和菜单键盘操作，居中显示，无边框、无遮罩。一次启动只自动尝试升级一次，避免失败后循环重启。

**初始化会永久清空指定 home 中的全部数据，包括会话、任务、文件、配置、凭据、插件和升级备份，无法撤销。**
选择初始化后必须再次确认，确认界面默认选中取消。初始化成功后重新加载主页，由正常启动建立全新的用户数据。
只有维护锁文件会保留；home 外的工作目录不会被清理。初始化中断后，重新启动 TUI 并再次确认初始化才能继续清理。

操作前关闭使用同一 home 的其他进程。当前标准 CLI/TUI 会通过共享/独占锁拒绝与维护操作同时运行；
旧版 OURO、直接使用 Store 的脚本及嵌入式调用仍需自行停写。处理中不能通过菜单取消，避免后台写入与重新启动重叠。

## 命令行升级

先更新 OURO 程序并关闭所有使用此 home 的 OURO 进程及外部写入者。保留独立备份，确认有足够空间存放
受影响文件的旧副本、新产物和数据库临时文件。升级只备份实际修改集合，独立完整备份由用户自行管理。

以下为 PowerShell 示例，请替换实际 home：

```powershell
# 检查受支持的核心格式与所需修改
ouro-upgrade --home 'C:\data\ouro-home' --check

# 在该目录内升级
ouro-upgrade --home 'C:\data\ouro-home'

# 按原来的后端启动同一目录
ouro --home 'C:\data\ouro-home'
```

源码环境使用 `uv run --frozen ouro-upgrade ...`，或在已安装依赖的环境中使用
`python -m ouro_upgrade ...`。`--home` 必填，工具不会选择目标目录或搬运整个 home。

Session / Task 后端都默认 files，可分别由 `OURO_SESSION_BACKEND`、`OURO_TASK_BACKEND` 或命令行覆盖。
若原启动选择了 SQLite，应使用相同选择，例如：

```powershell
ouro-upgrade --home 'C:\data\ouro-home' --session-backend sqlite --task-backend sqlite --check
ouro-upgrade --home 'C:\data\ouro-home' --session-backend sqlite --task-backend sqlite
```

升级包含 `config/providers.json` 中旧端点能力字段的转换，保留连接设置、凭据引用及其他配置内容。
不会合并或转换未选后端。回收站、旧维护备份、其他配置（包括提供方模板）、凭据及插件私有数据保留原样；成功不代表这些内容
或全部历史执行可用。未知核心格式会明确拒绝，不会通过跳过坏记录、重置任务或清空数据完成升级。

## 结果和恢复

命令输出 JSON：`upgrade_required` 表示检查通过且需要升级，`current` 表示无需改写，`upgraded` 表示
本次受支持的核心数据升级完成。成功报告位于 `.upgrade/receipts/`，其中列明修改文件和备份位置。
确认使用正常后，可自行清理报告所指的备份目录；有未完成升级时必须保留恢复材料。

如果写回中断，`.upgrade/in-progress.json` 会阻止 Runtime 使用此 home，TUI 会进入恢复界面。
保持其他写入者停止，也可以执行：

```powershell
ouro-upgrade --home 'C:\data\ouro-home' --restore
```

`restored` 表示整个修改集合已恢复为升级前内容。排除失败原因后重新执行升级。不要手工删除升级中标记
绕过保护；若清单或备份损坏，工具停止恢复，需要使用独立备份处理。恢复不支持对已经成功升级且继续使用的数据自动降级。
