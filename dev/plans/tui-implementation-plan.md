# OURO TUI 实施计划

状态：P0 输入、实时流与问题交互闭环完成，P1 待实施
更新日期：2026-08-03

## 目标

在不复制 Application/Core 业务规则的前提下，将 `ouro tui` 建设为稳定、流畅、可持续扩展的终端 Host。
TUI 只负责输入意图、焦点、视觉状态和 Runtime 事实渲染；Session、Run、工具、审批、AskUser、队列和模型
选择继续由现有 Application 服务拥有。

## 已建立的基线

- Textual 8.2 与 Rich 15 已作为直接依赖；
- `ouro.tui` 已分为状态投影、Application 控制器、视图控件和入口组合；
- 已接入流式 Markdown、Runtime activity、工具状态、审批、AskUser、Session/Model/Action 选择器和 MCP 启动摘要；
- 旧 stdin/stdout Interactive 暂时仍是无子命令默认入口，TUI 使用显式 `ouro tui`；
- 全仓测试和覆盖率门槛已通过，后续变更不得降低现有分层约束。

## 当前最高优先级：输入闭环修复

### P0. 提交消息后必须立即产生可观察反馈

目标行为：

1. Enter 提交非空草稿后，用户消息立即进入时间线；
2. Composer 立即恢复为可继续输入状态，顶栏切换为 preparing/waiting/streaming 等真实状态；
3. 新 Session 的首个 Runtime 事件到达前显示有界的提交状态，不允许出现静默空窗；
4. Runtime/Provider/配置错误必须落在时间线错误块中，不能只存在于 Textual Worker 异常；
5. 首个事件早于 `runtime.run()` 返回时不丢失 Session identity 或文本增量；
6. 同一 Session 运行中再次提交时沿用 Runtime follow-up 语义，不取消前一个 Worker。

验证：正常流、Provider 失败、启动失败、新 Session 首事件竞态、已有 Session follow-up、abort 各有测试。

### P0. `/` 自动命令视窗

目标行为：

1. Composer 从普通文本进入以 `/` 开头的草稿时自动打开命令 popup；
2. 继续输入时实时过滤，空结果给出明确提示；
3. Up/Down 移动选项，Enter 选择并执行无参数命令或补全需要参数的命令，Escape 关闭；
4. 删除开头 `/`、移动到非命令上下文、提交或失焦时关闭；
5. popup 与 Session/Model/Action modal 使用同一命令描述源，不能维护两份名称与帮助；
6. popup 打开时 Enter 不得被 Composer 抢先提交原始文本。

验证：打开、过滤、键盘选择、Escape、删除 `/`、未知命令和焦点返回均有 Pilot 测试。

### P0. 实时流式 Markdown

目标行为：

1. Runtime event sink 是低延迟主链路，模型文本分片到达后立即进入同一 Session 投影；
2. Markdown 控件使用 `Markdown.get_stream()` 增量写入，不为每个分片重建完整时间线；
3. `RunResult.events` 只作为完成边界上的确定性回放，通过 event sequence 幂等去重，不能成为首屏渲染来源；
4. 提交、审批和用户回答恢复都经过同一个结果收敛入口，observer 隔离失败时仍能得到最终规范状态；
5. 测试 Provider 在首个文本分片后保持阻塞时，页面必须已经出现首个分片且 Run 尚未完成。
6. 高速 reasoning/tool/text 事件不得挤满 Runtime 的有界 sink 队列；事件发布必须给 sink 调度机会，TUI
   ingress 必须保持异步且只做入队；
7. Textual 每次只处理一个已发布事件前缀，同一前缀的文本合并写入 Markdown，工具、activity、交互状态只渲染
   最新规范快照，不能为每个 token 重复布局、滚动和状态栏刷新。

### P0. 通用工具提问视窗

目标行为：

1. 内置 `AskUser` 渲染独立问题视窗，支持 1–3 个问题、2–4 个选项、单选、多选和 `Other` 自由输入；
2. Up/Down、数字键、Space、Enter 完成全键盘交互，视窗与 Composer 按是否等待自由输入仲裁焦点；
3. 内置回答严格编码为 `{"answers": {question_id: [labels...]}}`，不再把结构化请求降成裸字符串；
4. Widget 只消费 Host `QuestionForm`，内置协议解析与插件常见 JSON 形态适配集中在
   `ouro/tui/question_forms.py`；
5. 插件单问题/问题列表可复用同一视窗，未知 JSON 请求安全降级为有界预览和自由输入，不把插件业务协议写入控件；
6. 自由回答期间 `/` 是答案内容而非 slash command，回答失败时保留问题视窗并恢复可操作焦点。

## 后续迭代

### P1. 输入与运行控制

- 显式 follow-up/steer 选择和队列可视化；
- 资源附件、`@` 引用 popup、附件能力校验及草稿保留；
- 每 Session 草稿、输入历史搜索和外部编辑器；
- Ctrl+C/Escape 的中断、清空和退出层级统一。

### P1. 过程可视化

- [完成] reasoning 独立 Markdown stream、live 状态与 canonical history 恢复；
- reasoning 长内容有界预览、完成后折叠与展开控制；
- tool 参数有界摘要、结果详情和并行批次分组；
- compaction、subagent、重试和后台任务使用统一 activity 视觉语法；
- MCP 运行期 reconnect、失败详情和重试入口。

### P2. 长会话与终端适配

- cursor 分页和慢消费者重同步；
- 自动滚动锁、跳转最新消息、复制模式和链接处理；
- 60/80/120 列响应式布局，ASCII/低色深/NO_COLOR/animations-off 降级；
- SVG 视觉基线、长 Markdown/多工具性能门槛和跨平台按键测试。

## 模块边界

- `ouro/tui/state.py`：只将规范 Runtime 事实投影为可渲染状态；
- `ouro/tui/controller.py`：只把 Host intent 翻译为 `LocalApplication` 公开调用；
- `ouro/tui/widgets.py`：Composer、时间线、activity、交互条等可复用视图；
- `ouro/tui/question_forms.py`：将内置及插件工具输入请求投影成工具无关问题表单，并编码回答；
- `ouro/tui/screens.py`：选择器、帮助和命令 popup；
- `ouro/tui/app.py`：Textual 生命周期、消息路由、焦点仲裁和组件组合；
- `tests/tui`：纯投影测试、Pilot 交互测试和真实 Runtime 纵向测试。

禁止把 Provider 调用、Session 持久化、工具权限判断、审批合法性或队列规则放进 Textual Widget。

## 每阶段完成门槛

- 用户动作在 100 ms 内有本地视觉反馈；
- 高频模型增量继续使用 `Markdown.get_stream()` 合并，输入不因渲染阻塞；
- 所有 Worker 失败都有可见错误和可恢复焦点；
- App、Markdown stream、LocalApplication 和后台 Worker 有明确关闭路径；
- `ruff check ouro tests`、`mypy ouro`、TUI 定向测试、架构测试和全仓覆盖率门槛通过；
- README、CLI Host 规格、Changelog 与实际行为同步。

## 当前执行顺序

1. [完成] 复现并定位消息提交静默链路；
2. [完成] 修复提交反馈、Worker 错误和首事件竞态；
3. [完成] 建立单一命令目录与自动 popup；
4. [完成] 补齐 Pilot/Runtime 回归测试；
5. [完成] 更新规格与变更记录，运行分层质量门槛。
6. [完成] 用阻塞 Provider 证明首个 Markdown 分片在 Run 返回前完成渲染；
7. [完成] 建立工具无关问题表单投影，接通内置 AskUser、插件常见协议和自由输入降级；
8. [完成] 覆盖结构化回答、插件标量回答、焦点与 slash 前缀自由回答。
9. [完成] 以超过 sink 容量的 reasoning burst 复现实时事件丢失，并修复 eager task 下的 sink 生命周期；
10. [完成] TUI 按 UI 帧合并 Runtime 事件前缀，并直接断言 Run 完成前的 Markdown 控件 source。
11. [完成] 将工具审批升级为焦点独占的方向键选择面板，并阻止审批按键进入 Composer。
12. [完成] 将并行审批投影为有序队列并显式提交 approval ID；按事件序列分段渲染模型输出与工具活动。

本轮修复将 live event sink 与 `RunResult.events` 归入同一个幂等投影入口，并移除了对 Textual 私有
`_closing` 字段的覆盖。Slash popup 由 Composer change 事件自动驱动，命令目录同时供 popup、dispatch、帮助和
动作面板使用。

实时流主链路现在由阻塞式端到端用例保护：Provider 在首片后不返回，TUI 仍已更新 Markdown 和 Runtime
activity。提问交互不再复用单行审批条；`AskUserPanel` 只依赖 Host 表单，内置 `AskUser` 与插件输入请求由独立
适配层投影，回答继续交给 Runtime 校验和恢复原 Run。

后续实测显示短回答仍在 Run 完成后一次性回放，长回答则稳定停在第 257 个事件。Textual 的 production
`run_async` 会在 Python 3.14 安装 `asyncio.eager_task_factory`；原实现先 `create_task()`，再把 sink 状态加入
registry，导致消费协程在任务创建调用返回前反查空 registry 并退出。短回答未填满无人消费的 256 容量 mailbox，
所以最终可由 `RunResult.events` 回放；长回答在下一个事件到达容量边界后失败或卡住。

最终修复保留框架原有的独立有界 mailbox：先建立并注册完整 sink 状态，再创建消费任务，并把该状态直接传入任务
入口，不依赖启动时反查。上一轮引入的生产者背压与显式发布调度已撤销；它们只会把已退出的消费者表现从丢流改成
死锁。TUI sink 在事件进入 Textual message pump 后主动让出一次调度，单一 ready message 继续批量投影、合并
Markdown 文本和刷新最终 activity。回归测试使用 production `run_async` 的 eager task 语义，覆盖超过队列容量的
reasoning burst，并在 Provider 仍阻塞时验证可见文本。
