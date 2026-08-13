# Plan Mode 内置插件

`dev.ouro.plan_mode` 用插件贡献实现 Session 级计划模式，不在 Runtime 中建立专用执行分支。插件默认启用，
仍可通过标准 builtin 管理入口显式禁用。

## 职责

- `EnterPlanMode` 原子创建当前 Session 的计划状态并取得 `ouro.mode` controller claim；
- Context Provider 在计划模式有效时注入计划约束；
- Tool Policy 依据冻结的阶段和 revision 限制工具 effect；
- Turn Policy 在计划尚未完成时阻止 Run 以普通文本提前结束；
- `ExitPlanMode` 通过 resumable tool 协议保存退出请求、接收用户选择或 AFK resolution，并释放 controller；
- `/plan` Command 通过标准 Session input 进入同一生命周期。

## 状态与执行边界

计划记录保存在 Session extension state 的 `dev.ouro.plan_mode` namespace 中，并使用版本化 envelope 与原子更新。
状态包含模式阶段、计划正文、可选选项、revision 和待处理退出请求。插件私有目录不保存 canonical Session 状态。

每个 Run 冻结自身的 Catalog execution contract；上下文、策略和工具恢复都按该快照筛选对应 activation。
同一轮已开始的工具调用使用冻结 revision，状态变化从下一模型轮次生效。普通退出、拒绝、AFK 和进程恢复均通过
同一 resumable checkpoint 收敛，不伪造用户回答。

Plan Mode 只拥有计划生命周期。权限、审批、工具执行、Run 调度、持久化和 Agent Loop 仍由框架公共边界负责。

## 源码入口

- `definition.py`：builtin definition 与 manifest；
- `plan_mode_plugin.py`：贡献声明与 handler；
- `state.py`：版本化状态及转换；
- `interaction.py`：退出选择和 resolution 解析。

精确工具描述与 JSON Schema 以 `plan_mode_plugin.py` 和 `definition.py` 为单一事实源。

## 验证

```bash
uv run pytest tests/plugins/test_builtin_plan_mode.py -q
```

框架级 Session claim、工具恢复和 AFK 组合契约由对应 application/runtime 测试覆盖。
