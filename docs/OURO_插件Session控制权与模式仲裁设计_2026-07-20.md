# OURO 插件 Session 控制权与模式仲裁设计

> 文档性质：历史设计记录（2026-07-20）。当前规范以
> [`dev/specs/plugin-system.md`](../dev/specs/plugin-system.md) 为准。

本文定义插件如何在不让 Host 理解具体业务模式的前提下，安全地激活、组合、切换和关闭会话级控制行为。
Plan、Goal 仅作为验证示例，不进入 Host 的分支、枚举或冲突表。

规范性约束最终同步到 [`dev/specs/plugin-system.md`](../dev/specs/plugin-system.md)。

## 1. 问题

当前插件系统已经为每类 contribution 定义了独立组合规则：Command 使用 canonical identity，Context Provider
追加带来源的片段，Tool Policy 只能收紧权限，Turn Policy 在同一 Run 内续轮，Continuation Policy 在 Run 终态
请求跨 Run 动作。

但“某个插件在当前 Session 已进入一种长期控制状态”仍由插件私有数据表达。Command 可以直接修改该数据，
Context/Policy/Continuation 随后再动态读取它。这留下四个缺口：

1. Host 无法在激活时发现两个插件争用同一控制权，只能等到下游 contribution 产生冲突；
2. 命令回调可先修改插件状态，Application 无法把冲突检查、状态提交和后续输入组成一个事务；
3. 当前模型轮次与动态插件状态之间没有 activation revision，模式切换可能在生成提示词后改变工具闸门；
4. 关闭插件状态后，旧的 pending interaction、queued continuation 或恢复请求仍可能引用旧状态。

命令重名治理不能解决这些问题。`<plugin_id>:<command>` 只保证路由确定，不保证命令产生的长期行为可以组合。

## 2. 设计原则

1. **Host 只理解控制协议，不理解插件业务。** Host 不出现 `plan`、`goal` 或任何第三方插件 id 的特殊分支。
2. **冲突来自声明的资源控制权。** Host 不分析提示词、命令文本、工具名称或插件私有 JSON 来猜测冲突。
3. **命令和工具返回意图，不先提交控制状态。** 只有 Application 可以持久化 Session control activation。
4. **激活与 contribution contract 绑定。** activation 引用 stable canonical controller id、release identity 和
   execution contract；随机 generation id 只用于当前进程路由。
5. **每个 Run/模型轮次冻结有效 activation revision。** 同一轮的 Context、Tool Policy、Turn Policy 和
   Continuation 观察同一个控制快照。
6. **默认拒绝隐式替换。** 不使用加载顺序、priority 或 last-command-wins 决定谁取得排他控制权。
7. **用户始终有 Host-owned 逃生口。** 即使 owner 插件失效，Host 仍能撤销 activation 并取消其后续工作。

## 3. 通用领域模型

### 3.1 Session Controller

`SessionControllerContribution` 是一种 snapshot-bound contribution。它描述一个可在单个 Session 内激活的控制器：

```text
canonical_controller_id = <plugin_id>/session_controllers/<local_id>
```

声明包含：

- local id 和描述；
- 一组 `ControlClaim`；
- activate/deactivate handler；
- callback timeout；
- 可选的、仅由 owner 解释的 payload schema。

它不是新的 Agent 循环，也不是 workflow engine。它只为现有 Context、Policy、Continuation 和 Command 提供一致的
会话级激活边界。

### 3.2 Control Claim

```text
ControlClaim {
    domain: string
    access: exclusive | shared
}
```

`domain` 表示被控制的 Host 资源维度，而不是插件名称。例如某个长期自主控制器可以声明一个排他控制域，
另一个只收紧工具权限的控制器可以声明不同的域。两个 controller 是否冲突完全由 claim 集合决定：

- 同一 domain 上存在两个不同 activation 的 `exclusive` claim：冲突；
- `exclusive` 与 `shared`：冲突；
- 全部为 `shared`：允许共存；
- domain 不同：控制权层允许共存，下游 contribution 仍按各自规则组合。

首版 domain 使用受校验的 canonical 字符串，不建立插件对插件冲突表。OURO 保留 `ouro.*` 前缀用于标准控制域；
第三方可以使用其反向域名命名私有协作域。插件若要与其他插件协调，应依赖公开稳定的 domain，而不是检测 owner id。

### 3.3 Activation

Session 持久化：

```text
SessionControlActivation {
    activation_id
    controller_id
    plugin_id
    controller_local_id
    plugin_class
    release_id
    execution_contract_hash
    claims[]
    revision
    phase: activating | active | deactivating | failed
    owner_state_revision?
    created_at
    updated_at
}
```

`activation_id` 区分同一 controller 的多次启用。所有自动输入、interaction 和恢复引用必须携带该 identity 或对应的
Run control snapshot；旧 activation 不能凭同名 controller 复活。

### 3.4 Transition Intent

插件通过 `SessionControlTransition` 请求：

```text
acquire(controller_id, payload?, expected_session_revision?)
release(activation_id | controller_id, reason?, expected_revision?)
replace(controller_id, replaced_activation_ids[], payload?, expected_session_revision?)
```

`acquire` 和 `release` 可以由 Plugin Command 或 Plugin Tool 产生。Command/Tool handler 不直接写 Host activation，
也不得在返回成功前把自己的业务状态变为 active。Application 完成仲裁后再调用 controller lifecycle handler。

`replace` 只能来自明确的用户动作或 Host policy；普通插件不能静默抢占其他 owner。

## 4. Application 仲裁事务

每个 transition 在 Session mutation lock 内执行：

1. 从执行该 Command/Tool 的 Catalog lease 解析 exact controller contribution；
2. 校验 session id、generation gate、execution contract、payload 和 expected revision；
3. 对 acquire 计算 claim 冲突；同 controller 的已激活请求按幂等规则返回原 activation；
4. 写入 `activating` activation；
5. 调用有界 activate handler，传入 Host 生成的 activation id 和当前 Session context；
6. handler 成功后提交 `active`；失败则删除或标记 failed，并返回类型化错误；
7. release 先写 `deactivating`，阻止新 Run 取得该 activation，再调用 deactivate handler；
8. 清理该 activation 产生但尚未执行的 queued continuation，并取消或终结关联 pending interaction；
9. 提交删除/终态和递增的 Session control revision。

生命周期回调必须幂等。进程在中间状态退出后，由 reconciler 根据持久化 phase 继续 deactivate 或把不完整 activate
回滚为 failed；不能仅凭插件私有数据推导 Host activation。

多存储后端必须通过同一个 SessionStore mutation 契约完成 compare-and-set。文件后端继续使用 Session lock 和原子
replace，SQLite 后端使用事务；不能在 Plugin Command registry 内写 Session。

## 5. Run 与模型轮次绑定

Run 创建时冻结 active controller refs，记录 canonical controller id、activation id、activation revision 和
execution contract。每个模型轮次再从该 Run 控制快照派生本轮有效 contribution scope。

控制切换默认从下一模型轮次生效：

- 已经在旧控制上下文中生成的工具调用继续按旧 snapshot 的 Tool Policy 审核；
- release 会阻止旧 activation 创建新的跨 Run continuation；
- 当前 in-flight callback 可完成，但其输出在提交前重新校验 activation revision；
- 显式 abort 仍由 Host 的 Run cancellation 处理，不伪装成插件 transition。

这样避免“模型按受限模式生成调用，用户恰好关闭模式后该调用突然获得更宽权限”的 TOCTOU。

## 6. Activation-bound Contributions

Manifest 中的以下 contribution 可以选择声明 `active_when=<session-controller-local-id>`：

- Tool；
- Context Provider；
- Tool Policy；
- Turn Policy；
- Continuation Policy；
- Observer。

Host 在分发前检查本轮 Run control snapshot，只调用对应 activation 有效且 contract 匹配的贡献。插件回调仍可读取自己的
业务数据，但不再负责判断 Host activation 是否存在。

未声明 `active_when` 的 contribution 保持现有全局启用语义。Tools 和 Commands 默认始终在 Catalog 中可见，因为它们
通常正是进入、查询和退出 controller 的入口；Tool 只有显式声明 `active_when` 时才按轮次隐藏。Command 不做激活隐藏，
确保 status/off 控制面始终可达。

工具可见性不承担 pending interaction 的生命周期归属。常驻可见的 resumable Tool 可独立声明
`interaction_controller=<session-controller-local-id>`；Host 将其等待交互绑定到该 controller，在 release 时明确取消，
但不会据此裁剪模型工具列表。只有真正不应在非激活阶段出现的工具才使用 `active_when`。

## 7. 组合与冲突

Session Controller 不取代现有 contribution 规则：

| 层 | 组合规则 |
|---|---|
| Command identity | canonical identity；短别名仅在唯一时提供 |
| Session control claims | 激活时按 domain/access 仲裁 |
| Context Provider | 对有效 activation 按 priority/canonical identity 稳定追加 |
| Tool Policy | 取最严格结果；fail-closed |
| Turn Policy | 任一 continue 则同 Run 续轮；失败暂停 |
| Continuation Policy | 多个非 ignore 控制决策则暂停，不自动选优先级 |

Claim 解决“是否允许同时激活”，各 contribution 的 typed composition 解决“允许激活后如何组合”。两者不能合并成通用
priority middleware。

## 8. 命令与用户逃生口

插件可以提供自己的友好命令，例如无参数启用、`status` 查询、`off` 释放；这些命令最终都返回通用 transition intent。

Host 另提供不依赖 owner callback 成功的管理入口：

```text
/control status
/control release <activation-id|controller-id>
/control release --all
```

正常 release 会调用 owner deactivate。插件缺失、generation 不可恢复或 callback 失败时，用户可以 force release；Host
撤销 activation gate、取消其后续工作并记录诊断，但不宣称已清理插件私有资源。

## 9. Plan 插件迁移验收

Plan 是首个迁移样例，但不获得 Host 特权：

1. 声明一个 Session Controller 和所需 control claim；
2. plan scaffold Context 与 plan Tool Policy 绑定 `active_when`；
3. `/plan` 返回 acquire intent，不直接提前激活；
4. `/plan off` 返回 release intent；重复关闭幂等；
5. `EnterPlanMode` 与 `ExitPlanMode` 始终可见，并通过相同 transition 协议进入/离开；Exit 的等待审批用
   `interaction_controller` 独立绑定 mode 生命周期；
6. `/plan off` 在 active 和 awaiting approval 阶段都可用，不要求计划文件存在或非空；
7. off 不删除 artifact、不表示批准、不创建实施 Run；
8. awaiting approval 被关闭时，pending request 得到明确 cancellation，旧 resume 不能重新激活；
9. 当前轮次继续按冻结的 Plan revision 审核工具，下一轮不再注入 scaffold 或应用 gate。

## 10. 失败与恢复

- controller contribution 不在目标 Catalog：transition 拒绝，不修改 Session；
- claim 冲突：返回双方 canonical controller id 和 domain，不调用 activate；
- activate timeout/异常：fail-closed，activation 不进入 active；
- deactivate timeout/异常：撤销 Host gate并记录 degraded cleanup；不得让失效 owner 继续自动续跑；
- execution contract 不可恢复：activation 进入 failed，Host 管理命令仍可 force release；
- stale expected revision：返回 conflict，调用者重新读取状态；
- stale continuation/interaction：以 activation superseded/cancelled 结束，不执行插件业务动作；
- Plugin disable/revoke：先撤销 generation gate，再把其 Session activation 标记 unavailable；用户可管理清理，不能静默
  迁移到同名新 contract。

## 11. 实现状态

已完成：

1. 领域模型、文件/SQLite 持久化和通用 claim 仲裁器；
2. SDK、Manifest、worker、contribution inventory、contract 和 Catalog 全链路 Session Controller；
3. Command/Tool transition commit、acquire/release/同 owner replace 与 revision/contract 校验；
4. Tool、Context、Prompt Policy、Tool Policy、Turn Policy、Continuation、Observer 的 `active_when` 和模型轮次 durable
   snapshot，以及 resumable Tool 独立的 `interaction_controller` 生命周期绑定；
5. builtin Plan 迁移、稳定工具列表、`/plan off`、审批/AFK 回归与 controller-owned pending interaction 取消；
6. Host `/control status|release|release --all [--force]` 管理面；
7. 启动期 interrupted phase fail-closed，以及未知业务插件同域冲突、异域组合、replace 和双存储测试。

Goal 或其他长期控制插件必须建立在该机制上，不新增 Host 特例。后续增强只包括更完整的跨进程 lifecycle callback
重试/诊断 UI；它不改变这里的领域协议。

## 12. 非目标

- 不建立通用 workflow/DAG engine；
- 不让 Host 解释插件 payload、目标完成条件或计划审批语义；
- 不允许插件动态注册新的 contribution type；
- 不用字符串提示词分析推断兼容性；
- 不保证受信任 in-process 插件不能绕过 SDK 直接访问操作系统；
- 不使用 controller 机制替代 Tool permission、Run lock 或 Catalog generation gate。

## 13. 完成证据

实现完成必须同时证明：

- 两个未知业务插件仅通过相同 claim 即可在激活时得到确定冲突；
- Host 源码不存在针对这些插件 id 的仲裁分支；
- 不同 claim 的 controller 可以共存，贡献仍按各自 typed 规则组合；
- Command 与 Tool 都不能在仲裁失败后留下 active 私有/Host 状态；
- Run/round snapshot 消除切换期间的权限放宽；
- release 后旧 continuation、interaction 和恢复请求不能复活；
- 文件与 SQLite Session backend 行为一致；
- Plan 的进入、审批退出、AFK 退出和 `/plan off` 全部通过回归测试。
