# 资源与后台运行监督规格

状态：当前

## 目标与范围

ResourceService 为本地附件提供稳定身份、不可变内容存储和 Session 可达性；RuntimeSupervisor 为多个
Session 的 Run 提供进程内调度、订阅和 Host 生命周期。二者都是 application 边界，不改变 core 的依赖
方向，也不引入远程对象存储、分布式队列或后台 daemon。

## ResourceRecord 与身份

- resource id 由完整 SHA-256 digest 构成，消息只保存 resource id。
- `ResourceRecord` 保存 digest、media type、size、安全来源名和创建时间，不保存导入绝对路径。
- blob 与 record 是 canonical 数据；全局资源 index 是可重建投影。
- Session 的 `resources/index.json` 保存 canonical 引用关系；分支引用记录可追踪来源 Session。

## 导入、读取与删除

导入先拒绝符号链接和非普通文件，再流式复制到 staging，同时计算 digest 并执行大小限制。发布使用同文件
系统原子替换；相同内容复用 canonical record/blob，并为目标 Session 幂等附加引用。失败 staging 会清理，
引用发布失败时不会留下新 record。

show/export 在返回内容前校验 digest 和 size。导出不经过符号链接，使用目标目录内临时文件原子发布。
remove 在 canonical message 仍引用资源时抛出 `ResourceInUseError`；引用移除后，仅当所有活动 Session 均
不可达时删除 record/blob。Session 进入 trash 后触发同一可达性清理，清理失败形成可重试 diagnostic，
不回滚已经发布的 Session trash 事务。

资源索引更新失败不回滚 canonical 导入或引用变更，而是写 dirty marker 并通过 application diagnostic 暴露。

## 分支与工具结果

- 分支先验证选中消息的全部 resource id 在源 Session 存在且内容完整。
- 消息、分支 Session 和复制后的资源引用在同一 staging 目录中原子发布。
- 工具结果的 `resource_refs` 在写入 canonical message 前通过 ResourceService 校验。
- 内置文件工具返回工作区操作结果，不把裸文件路径伪装成 resource id。

## RuntimeSupervisor 生命周期

Supervisor 管理 `session_id -> active job/handle` 的进程内 registry。单 Session 仍只有一个 active Run，
durable 状态转换仍由 `RuntimeService`、`RunLifecycle` 和 `SessionStore` 完成。

- submit 进入显式 FIFO 队列，由固定数量 worker 提供全局并发上限。
- status 返回类型化快照；terminal job 从 active Session registry 自动移除。
- task 边界异常转换为 diagnostic，不终止其他 Session 的 worker。
- Supervisor 启动扫描 SessionStore；无 live lock 的中断 Run 由现有恢复机制转为 `recoverable`。
- 内存 handle 和 registry 只用于当前进程控制，不作为运行事实。

## 订阅、detach、abort 与 shutdown

订阅按 Session 接收已经持久化的 Runtime events。每个订阅使用有界队列；慢订阅者溢出时收到 lag
diagnostic，不反向阻塞 Run。关闭 subscription 只表示 Host detach，不终止 durable Run。

explicit abort 先通过 RuntimeService 持久化 aborted，再通知进程内协作式取消。shutdown policy 有两种：

- `wait`：在有限时间内等待；超时后释放进程任务，未完成 durable Run 在下次启动恢复；
- `abort`：显式 abort active Run，再在有限时间内等待资源释放。

## CLI 与 Host 边界

CLI 提供 `resource import/list/show/export/remove`；Session export 包含无本地路径的资源 manifest。
Supervisor 只暴露 application API，并由非交互 Host 测试覆盖；当前不提供后台 daemon 命令。

## 验收证据

- `tests/application/test_resources.py`
- `tests/application/test_supervisor.py`
- `tests/cli/test_resource_commands.py`
- `tests/architecture/test_boundaries.py`
