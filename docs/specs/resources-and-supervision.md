# 资源与后台运行监督规格

> 文档类型：现行规格

## 目标与范围

ResourceService 为本地附件提供会话范围的稳定身份、不可变文件和所有权校验；RuntimeSupervisor 为多个
Session 的 Run 提供进程内调度、订阅和 Host 生命周期。二者都是 application 边界，不改变 core 的依赖
方向，也不引入远程对象存储、分布式队列或后台 daemon。

## ResourceRecord 与会话归属

- 内部资源身份是会话范围的稳定 ID，与完整性 SHA-256 摘要解耦；模型使用真实路径。
- `SessionResourceReference` 保存完整 `ResourceRecord` 元信息：ID、原名称、安全文件名、类型、大小、摘要、时间及复制来源。
- 上传文件位于 `OURO_HOME/sessions/<session_id>/resources/files/`，不自动写入会话工作目录。
- 文件后端的 `resources/index.json`、SQLite 的资源记录分别是所属会话的元信息权威；无全局资源目录或重复元信息库。
- ResourceRecord schema 为 2；不支持的布局和记录版本明确拒绝，不自动迁移或清空用户数据。

## 导入、读取与删除

导入拒绝符号链接、特殊文件及无效媒体类型，在会话 mutation 内分配可读名称，使用会话私有 staging
流式复制、限制大小并计算摘要，校验后原子发布文件，再提交元信息。名称按跨平台安全规则处理，
大小写冲突分配编号；相同字节仍保留各自文件名和会话副本。引用提交失败撤回新文件。
导入允许活跃 Run 准备后续输入，history_epoch 限制迟到结果的归属。

show/export 和模型读取校验摘要与大小。导出使用一次分块复制，验证后原子发布目标；失败不覆盖既有目标。
模型读取字节窗口时也流式校验整个文件，仅将请求窗口保留在内存；窗口上限约束内存占用，不减少摘要校验所需的磁盘读取量。
remove 遵循执行锁，拒绝 canonical 消息或待消费队列仍使用的资源，仅删除当前会话持有的文件。
会话回收保留附件：文件后端随会话目录移动；SQLite 将文件移到对应 trash_id 目录并在数据库保留回收记录。
SQLite 普通事务失败会恢复已移动目录；跨介质发布不承诺掉电原子性，失败可保留无引用文件供诊断。
Session 查询索引更新失败通过 diagnostic 暴露，不使已提交资源失效。

## 分支、迁移与工具结果

分支校验源文件，复制所需文件并与目标消息、元信息一起发布；子会话显式接收资源时也保存自己的副本。
源会话被删除或进入回收站后，目标仍可读取与导出。导入归档校验文件名、大小、摘要和消息/队列/请求引用闭包，
提交的元信息必须具有完整文件。目标地址从目标会话清单生成；历史请求快照保留实际使用的地址。

工具结果的 `resource_refs` 在 canonical 提交前经 ResourceService 校验；内置文件工具不把普通路径伪装成资源 ID。
资源服务不提供模型专用工具。CLI 显式导出继续保留；模型通过既有文件工具读取真实路径。

## 模型文件投影

上传副本与 `@` 原路径引用共用文本预览、原生图片、音频、视频和引用降级算法，并分别标明来源。
独立 `attachments` 骨架和原用户正文属于同一输入单位；前者持有文件事实，后者不混入元信息。
骨架 content 保存已输入的文本和媒体，请求快照保存适配后的实际请求；查询历史直接读取持久记录。默认限额、路径权限与 TUI 见[附件规格](attachments.md)。

## RuntimeSupervisor 生命周期

Supervisor 管理 `session_id -> active job/handle` 的进程内 registry。单 Session 仍只有一个 active Run，
durable 状态转换仍由 `RuntimeService`、`RunLifecycle` 和 `SessionStore` 完成。

- submit 进入显式 FIFO 队列，由固定数量 worker 提供全局并发上限。
- status 返回类型化快照；terminal job 从 active Session registry 自动移除。
- task 边界异常转换为 diagnostic，不终止其他 Session 的 worker。
- Supervisor 启动只建立进程资源和 worker，不扫描 SessionStore。
- 显式执行/恢复入口只恢复目标 Session；`recover_session` 保留本 Supervisor 已接管的 job，包含尚未取得
  执行锁的排队阶段。Host 选择控制会话时仅对 running Run 请求恢复，不能把 queued 缺少执行锁视为中断。
  真正中断且无 live owner 的 Run 转为 `recoverable`，只读观察不执行恢复。
- Agent root 恢复只扩展到该 root 的 durable child Session，不读取其他 primary Session 历史。
- 内存 handle 和 registry 只用于当前进程控制，不作为运行事实。
- `wait(handle)` 等待本进程的一次执行尝试；`wait_run(session_id, run_id)` 等待指定 canonical Run 结束，跨越审批、
  子任务等待和执行端交接。可指定 `until` 提前返回某个持久化状态。二者不能混作一个完成信号。
- AgentService 通过窄 scheduler port 使用 `submit_prepared` 派发已经 durable 创建的 child Run；不会递归进入
  RuntimeService，也不建立 Agent 专用 worker pool。
- parent `waiting_child` 立即 park 并释放 permit；child terminal 后以 `resume_agent_wait` 恢复原父 Run。即使
  `max_concurrency=1`，sync join 也不会死锁。

## 订阅、detach、abort 与 shutdown

订阅按 Session 接收已经持久化的 Runtime events。每个订阅使用有界队列；慢订阅者溢出时收到 lag
diagnostic，不反向阻塞 Run。关闭 subscription 只表示 Host detach，不终止 durable Run。

Run 等待在 Session mutation 中读取精确 Run 与 event cursor，再接入同一共享事件流；状态转换或 lag 时才重读该
Run，不因文本 token 读取 checkpoint 或历史。每个观察者独立取消，最后一个订阅退出才停止该 Session 的增量 reader。

explicit abort 对仍有执行 owner 的 Run 先持久化取消请求并通知协作式取消，待 owner 结清后发布 aborted；
无执行 owner 的挂起/排队 Run 可直接终止。撤销必须等待取消结算。shutdown policy 有两种：

- `wait`：在有限时间内等待；超时后释放进程任务，未完成 durable Run 在下次激活所属 Session 时恢复；
- `abort`：显式 abort active Run，再在有限时间内等待资源释放。

Agent root subscription 组合现有 per-Session Supervisor stream。初始值来自 canonical `AgentTreeSnapshot`；新
child 动态加入，终态 child 退出 live stream 但保留历史节点，慢订阅者收到 lag diagnostic 后用 snapshot revision
重连。树只投影 bounded status/activity/usage/interaction summary；完整 child text/reasoning/tool delta 仍从 child
Session stream 按 event cursor 增量读取。关闭树或 child stream 只是 Host detach，不 abort child。

## CLI 与 Host 边界

CLI 提供 `resource import/list/show/export/remove`，并通过 `run --session ... --resource ...` 将已导入资源附加到
本次用户输入；Session export 的资源清单使用会话内文件名，归档携带对应文件。原路径引用与请求快照保留各自实际地址。
Supervisor 与 AgentService 只暴露 application API，并由非交互 Host 测试覆盖；CLI/未来 Desktop 不实现
cascade、permission、delivery 或树状态迁移。当前不提供后台 daemon 命令。

## 验收证据

- `tests/application/test_resources.py`
- `tests/application/test_runtime.py`
- `tests/application/test_supervisor.py`
- `tests/application/test_agents.py`
- `tests/cli/test_resource_commands.py`
- `tests/architecture/test_boundaries.py`
