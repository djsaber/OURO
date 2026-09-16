# Provider 认证用例规格

> 文档类型：现行规格
> 代码基线：2026-09-16
> 范围：可注入驱动的 Application 登录与凭据管理，已接入轻量本地组合与 CLI；现有模型协议和 Runtime 共享认证已接线；生产驱动与 TUI 尚未接入。

`application/provider_auth.py` 编排认证用例，`provider_auth_models.py` 定义公开 DTO，`provider_auth_bindings.py` 拥有配置绑定快照与提交检查。持久化及 revision 以[凭据核心规格](OURO_凭据核心规格_2026-09-16.md)为准；网络授权以[认证驱动与授权规格](OURO_认证驱动与授权规格_2026-09-16.md)为准。剩余接入工作见[实施计划](../plans/OURO_模型订阅认证实施计划_2026-09-16.md)。

## 依赖与所有权

ProviderAuthService 接收 ProviderConfigManager、CredentialService、具有唯一 id 的 AuthDriver 集合及可替换时钟。不自行加载 driver/store，不启动 Runtime、Agent、MCP 或模型请求；构造时不读取凭据，未登录或凭据损坏不影响构造。

Application 依赖 auth 的公开契约和通用 CredentialService，不导入具体驱动、文件 store、权限实现或 providers。config 不依赖 auth；auth 不解释 Provider 配置。static 不伪装成可交互 driver，生产产品注册仍不包含未验证的订阅组合。

服务拥有内存登录任务，借用凭据服务与驱动。`local_application.open_local_provider_auth` 独立装配同一 home 的配置、受限 store、凭据服务和认证用例，先关闭 ProviderAuthService，再关闭 CredentialService，清理失败不跳过后者。CLI 使用这个轻量边界；本地 Runtime 另装配同一 home 的共享服务并拥有最终收尾职责，LocalApplication 在关闭消费者前停止登录，见[请求认证规格](OURO_请求认证规格_2026-09-17.md)。CLI 参数、输出与退出码见[CLI 与 Host 规格](OURO_CLI与Host规格_2026-09-02.md#provider-认证命令)。

## 用例与公开结果

| 用例 | 行为 |
|---|---|
| methods(provider_id) | 返回驱动的稳定方法描述；static 返回空集合 |
| begin(provider_id, method_id) | 捕获绑定与凭据 revision，启动授权，返回 operation_id 和 challenge |
| operation(operation_id) | 同步返回不可变的操作投影，不启动或推进网络 |
| wait(operation_id) | 等待同一操作并返回终态；取消 waiter 不取消登录 |
| cancel(operation_id) | 幂等取消并等待资源及提交线程收尾，返回实际终态 |
| status(provider_id) | 只读本地凭据摘要、派生状态及当前共享引用实例 |
| logout(provider_id) | 注销该实例引用的本地凭据，不调用远端 revoke，不修改配置 |
| credentials() | 列出安全凭据摘要和当前引用实例，包含无绑定记录 |
| forget(credential_id) | 显式清除指定记录的秘密并返回受影响实例；复用注销语义 |

未知 Provider/不可读取的配置返回 authorization_configuration_error；static 登录、未知方法或未注入驱动返回 authorization_unsupported。凭据 driver 不匹配返回 credential_conflict，不改用其他驱动。原始配置、网络和存储异常不会进入公开诊断。

LoginChallenge 是唯一包含交互 URL/code 的结果，只交付 begin 的调用方。LoginOperation、ProviderAuthStatus 和 CredentialView 不包含 challenge、Token 或任意供应商 payload；身份摘要不构成权益证明。登录成功不自动执行可能消耗额度的连接检查。

## 操作与资源生命周期

操作状态为 `pending → succeeded | failed | cancelled | expired`，直接从服务拥有的任务结果投影，不维护另一份可独立漂移的状态机。成功必须已完成资源清理和条件保存；获得 CredentialGrant 不代表成功。终态不再改变，重复 wait/cancel 不重启流程。

begin 阶段默认最多等待 30 秒；开始授权失败或被取消时 driver 负责释放尚未交接的资源，交接后由服务关闭 attempt。交换和清理遵循 attempt 自身时限。begin 总时限到期是 expired；供应商网络超时是 failed。压制取消后迟到返回的 attempt 也必须关闭，不得提交其材料。

取消 begin 调用会收尾已启动的操作；取消普通 wait 只停止该次等待。显式 cancel 在授权或锁等待阶段生效后不保存材料；保存已获执行权时等待实际结果，可以返回 succeeded 或保存失败，不能把已保存的登录报告为 cancelled。cancel 调用方再次被取消也必须等待清理完成。

服务默认最多保留 64 个操作。容量满时，新 begin 淘汰最早创建且已终结的操作；所有槽位均 pending 时返回 authorization_operation_limit。淘汰或重启后查询返回 authorization_operation_missing。操作不持久化，不增加恢复队列。

close 停止新登录，取消并收尾所有 pending 操作；清理某个操作失败仍继续关闭其他操作，最后返回安全的 authorization_cleanup_failed。重复 close 复用同一收尾任务。已开始的同步原子写入不可强行中断；时限不承诺任意磁盘阻塞或违规 driver 下的硬关闭时限。

## 配置绑定与条件提交

登录开始时保存以下不可变绑定：实例 id、产品类型、协议、driver、managed credential id、产品的 service policy 引用、Provider base_url/api_path，以及显式覆盖目标的 Endpoint id/base_url/api_path。无目标覆盖的模型、默认 Endpoint、超时等修改不改变绑定，不因无关配置变化废弃登录。

网络等待不持配置锁。取得材料并关闭 attempt 后，CredentialService 先取得 credential 锁，再进入 Application 提供的同步 binding_guard。guard 使用 ProviderConfigManager.mutation，重读并核对绑定；凭据服务随后核对 revision、原子保存。整个 guard、凭据重读、保存和配置锁释放在同一个工作线程完成，禁止跨 await 持有线程可重入锁。

两种锁共享一个提交截止时间。配置锁使用取得凭据锁后的剩余预算及取消信号。凭据服务通过一个线程互斥边界裁决取消/关闭与开始保存的先后：取消先行则不写；保存先行则以持久化结果为准。即使线程仍在排队或读文件，外层取消也必须等待其收尾后才释放 credential 锁，不能留下迟到写入者。

绑定变化或 Provider remove/replace 返回 credential_conflict；配置损坏返回安全配置错误；任一锁等待耗尽返回 credential_lock_timeout。刷新、注销、forget 或另一登录先修改 revision 时同样 conflict，不覆盖新材料。取消或失败的重登保留原记录。不增加 Provider revision、授权 epoch、双锁试抢或重试循环。

Provider 管理只取得配置锁；refresh/logout/forget 只取得凭据锁。配置 remove/replace 不隐式删除旧凭据。forget 保留递增 revision 的 logged_out 记录，阻止旧登录恢复秘密；重复注销不递增。缺失但仍被引用的凭据也可 forget，以阻止其 pending 登录；缺失且无引用时返回 authentication_required。

## 本地状态与共享引用

| 状态 | 本地事实 |
|---|---|
| static | 走既有静态凭据路径，本服务不重新解析 API Key |
| missing | managed 记录尚不存在 |
| ready | active 且没有到期；不等于服务端接受材料 |
| refresh_needed | active 已到期且驱动提供 refresh |
| reauth_required | 记录要求重登，或已到期且驱动不能刷新 |
| logged_out | 无秘密的注销记录 |
| unavailable | 无法读取、driver 不匹配或未装配；附安全错误码 |

查询不刷新、不调用 driver.encode，也不验证权益、模型可访问性或远端材料是否有效。列表遇到损坏记录明确失败，不静默略去；单 Provider 查询将存储问题投影为 unavailable。摘要以存储格式为准，driver 在实际使用材料时负责 payload 解码。

同一 home 内多个 Provider 引用同一 credential 时共享注销效果。DTO 的 bound_provider_ids 来自当前配置快照，只用于说明引用关系，不新增 owner ACL 或跨文件事务承诺。

## 验收证据

`tests/application/test_provider_auth.py` 将已有浏览器/Bearer 和设备码/Session header 两种测试驱动接入同一服务，覆盖登录—保存—状态—注销、共享引用和孤立凭据、脱敏、拒绝/过期/网络/清理/保存失败、取消与重复操作、容量边界、配置变化、双登录、刷新/logout/forget 竞争及关闭。

实际配置锁和凭据文件验证线程所有权、剩余预算、锁等待取消、提交先行时的真实结果及取消返回后无迟到写入。凭据核心另覆盖双进程互斥、同步读/写期间取消和关闭。架构测试限定 Application 只使用公开认证契约和通用服务。

轻量组合与 CLI 闭环证据见 CLI 与 Host 规格。这些证据不表示真实 OAuth、TUI、模型 adapter 或生产订阅产品已验收；无需真实账户即可重复执行。
