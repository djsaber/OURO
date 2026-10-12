# OURO 插件系统审查报告

> 文档类型：审查、修正与验收记录
> 审查日期：2026-10-12
> 状态：R1–R5 已修正；Windows 全量回归、覆盖率及静态／构建门禁通过。
> 审查对象：当前工作区中的框架与插件实现，包含尚未提交的重构改动。
> 对照基线：v0.1.0，`96ebb7e387aedfcfe9bb93906916af5ada9c726e`（当前 HEAD）。

插件系统的整体架构方向成立：框架拥有执行事实、权限、预算和资源所有权，插件通过公开端口提供业务与算法。
本轮确认服务依赖寿命、生命周期锁回入、Driver 输入交付、排队执行所有权和重复启动五项问题。
五项均已完成修正及组合回归，并通过本轮 Windows 全量门禁。
修正围绕调用寿命、生命周期准入、输入交付、执行所有权和启动规划，在现有责任边界内统一规则，
没有整体替换架构或限制插件的正常组合能力。R3 是公开输入契约的设计缺口；R4、R5 是基线遗留问题，
不能将五项全部归因为本次重构引入的回归。
本轮通过不代表长期生态支持政策已经完整确定；按需成本与长期演进的证据及边界分别记录如下。

本报告记录代码事实、定向复现结果、修正方向与验收条件，不替代[插件规格](../../specs/plugins.md)、
[运行时规格](../../specs/runtime.md)和[自动任务规格](../../specs/automated-tasks.md)。本报告按
[文档维护规范](../../development/documentation.md)归档，当前有效契约由相应规格维护。

## 审查原则与整体判断

以下六项设计哲学是本报告的判断基准，既约束问题识别，也约束修正方案。现有代码、规格和归档计划用于核对事实及
解释取舍，不能仅因某种限制已经写入文档就认定它符合产品目标。第一性原理、清晰边界、高内聚低耦合、健壮高效及
避免过度工程化，是落实这些目标的方法；简化不能以削弱必要扩展能力为代价。

| 设计哲学 | 当前判断与证据 | 必须保持的约束 |
|---|---|---|
| 1. 可组合的独立拓展单元 | 包、服务、实现契约与 generation 的责任划分合理，服务和 Driver 样例展示了组合能力；R1、R2 修正后，精确依赖调用和生命周期模型组合分别有进程内／worker 回归 | 插件通过公开服务与显式依赖协作，不读取其他插件的私有存储；新业务组合不要求给 Runtime 增加该业务的专属分支 |
| 2. 强大、灵活且可演进的拓展能力 | Driver、具名服务、动态工具、Provider、进程与调度端口已超出基础贡献清单；R3 已将输入交付纳入共同原生端口，算法可主动接收输入，未绑定默认循环 | 原生贡献清单不是能力上限；允许插件改变算法、执行和协作方式。新的原生语义边界可以按实际需求增加，不冻结现有接口，也不开放整个 Runtime/Store |
| 3. 随需接入并完整退出 | gate、调用租约、受管理资源、停用维护与包保留具有明确所有者；R1、R2、R5 已补齐升级结算、后台服务交接和单次初始化规则 | 停止接纳、结算在途工作、释放资源和保留用户数据分别验收；清理失败不得报告为已完全退出，保留数据与恢复引用不得自行激活插件或持续执行 |
| 4. 独立发展建立在统一契约上 | SDK、Core 和 Host 分层及原生执行复用成立；R1、R3、R4 分别统一调用闭包、输入交付和排队至执行的真实所有权 | 框架统一拥有身份、准入、绑定、权限、预算、提交与恢复；插件拥有业务、算法和私有数据解释。进程内、worker、内置和用户插件遵循相应的同一业务契约 |
| 5. 能力按需增长，复杂性受到约束 | 局部贡献绑定、有界流式输出与管理冷路径的方向合理；R5 已消除重复启动和逐根状态读取，局部调用不保活无关插件；worker 启动的固有成本仍有历史测量边界 | 成本跟随已选择能力及必要依赖；未启用插件不驻留执行，局部调用不保活无关插件。共享一致性责任留在框架，插件特有业务复杂性留在插件，不用插件重试或幂等补偿框架缺陷 |
| 6. 插件系统与生态长期共同演进 | 版本声明、不可变包、精确执行绑定、显式数据维护和回收提供了基础；当前单一 SDK major 与严格格式拒绝不能单独证明生态兼容与淘汰安排完整 | 区分 API、协议、包、数据及待完成执行契约；在稳定发布承诺下明确可兼容变化、破坏性升级、维护失败及退役路径。按真实需求建设有限支持，不永久排除兼容，也不预建通用迁移平台 |

固定贡献类型清单承担原生领域的适配职责；具名服务及动态注册允许插件继续提供自己的业务协作接口。
应保留这两层的清晰分工。可卸载能力则依赖真实资源所有权：关闭入口、结算在途调用和释放资源各有不同完成条件。

### 架构核查的依据与取舍

| 核查面 | 当前实现与判断 |
|---|---|
| 扩展能力是否仅限于固定清单 | [服务端口](../../../ouro/sdk/plugins/services.py)支持 JSON 调用及可撤销 handler 注册；[Driver 端口](../../../ouro/sdk/plugins/run_drivers.py)允许普通异步代码组合模型、工具和 Agent；[动态工具组](../../../ouro/plugins/tool_groups.py)允许运行期整组更新。保留原生贡献类型清单不等于限制业务扩展面 |
| 原生执行是否被另建一套 | 默认和自定义 Driver 共用 [NativeRunDriverContext](../../../ouro/application/run_drivers.py)、[模型请求](../../../ouro/application/model_requests.py)与原生工具批次；独立生成复用模型执行和用量账本，不伪造 Session/Run。该责任划分应保留 |
| worker 是否只有被动回调 | [双向请求分派](../../../ouro/plugins/worker.py)继承 Host 记录的父调用上下文，业务任务不占用 reader；模型、服务、进程、调度和 Driver 均有公开 worker 适配。R1、R2 已补齐 worker 升级、取消、初始化及失败交接组合验证 |
| 升级与恢复是否混淆 | [精确 Driver 装载](../../../ouro/plugins/driver_contracts.py)、调用租约、持久 checkpoint 与 [Task 领取绑定](../../../ouro/plugins/tasks.py)分别负责实现选择、资源寿命和恢复。不能为减少类型数量而删掉这些独立事实；R1 已将原代次闭包租约贯通服务及注册回调入口 |
| 关闭与数据是否形成永久运行负担 | [代次释放](../../../ouro/plugins/generation.py)、受管理进程、home 调度所有权和私有数据共享／独占锁均有明确职责。保留用户数据及待完成引用不等于继续驻留执行；停止失败保留资源与失败状态是必要事实。进程内任意 Python 的强制终止仍不属于承诺 |
| 维护成本是否合理 | SDK 独立、Core 不依赖插件安装和 worker 实现、内置业务使用公开 SDK，由架构检查约束。调用闭包、生命周期准入和启动规划在原有责任层内收敛；仅凭模块数量或文件长度，不能推导应引入通用 DI、事件总线、Scope 层级或工作流 DSL |

本轮没有证据要求整体替换 Catalog、全面惰性加载、建立持久引用索引或通用数据迁移平台。
类型化领域端口、显式包依赖和当前两级资源所有权能够支撑已展示的 Mod 式能力；应先修正已证明的不变量缺口。
这不是冻结扩展面的结论：新增共同执行语义时可以增加窄领域契约，插件特有的协作协议由具名服务承载。

### 按需成本的审查边界

用户选择的能力包括其必要依赖，以及本次决策实际暴露的工具与上下文；成本随这些内容增长是合理的。
已安装但未启用的包可以产生安装元数据、磁盘存储和显式管理查询成本，不能因此常驻 worker、监听器、连接或业务轮询。
已启用插件的初始化和必要后台服务可以有成本，但这些成本必须有归属、可观测并能随停用释放。

局部命令、服务和注册回调应只持有实际 owner 与依赖；不能以修复 R1 为由保活全目录。
R5 应消除重复构建和重复初始化，而不是先用并发启动掩盖工作量。不能要求任意数量的已选能力都保持恒定成本，
也不能只凭关闭变快就忽略启动退化。全面惰性加载、缓存或索引是否必要，取决于可重复的实际瓶颈及维护成本。

### 长期演进的审查边界

[版本规则](../../specs/plugins.md)已规定显式版本和破坏性变化检查；
[归档设计](2026-10-12-plugin-platform-refactoring.md)区分 SDK major、包与数据演进。
当前拒绝旧格式和旧 SDK 是开发期约束，不能误报为本次回归；但“明确拒绝旧版本”只证明不会误读，
并未完整回答既有插件如何持续使用、迁移或有序退出。当前不提供 deprecated API 周期不能直接成为长期生态政策。

长期演进按以下责任审查，不以现在实现通用兼容层为前提：

- **公开契约**：稳定 major 内的新增能力应保持已承诺语义；破坏性变化明确版本边界和影响。在作出稳定生态承诺前，
  说明支持范围、升级要求与旧能力退役安排；需要过渡支持时采用有范围、有退出条件的方案。
- **插件数据**：由数据所有者定义格式及必要转换，框架提供停用、独占维护、结果记录和重新预检边界。
  转换失败保留数据与可解释状态；代码选回旧版不等于数据可以回滚。
- **待完成执行**：升级不把已接受的调用、审批或 checkpoint 交给同名新实现。无法继续原契约时明确阻断并提供终结或
  释放路径；不以强行重放副作用、永久运行旧插件或永久占有旧包维持兼容。
- **淘汰与回收**：区分停止运行、卸载安装引用、保留用户数据及回收无引用代码。版本支持和保留引用必须能解释其原因与
  解除条件，旧机制退出后不继续产生后台负担。

这些是架构与发布政策的验收约束，不是已经复现的第六项运行故障，也不是对旧版本永久兼容的承诺。
当前证据支持有限的升级、维护和回收能力；稳定生态的支持及退役政策仍需在相应发布前明确，不能标为已经完整验收。

## 对原审查报告的判定

原报告的五项核心观察均成立，但证据范围与归因应按下表解释。

| 原条目 | 复核判定 | 结论边界 |
|---|---|---|
| R1 | 保留；普通服务调用及注册 handler 两个入口分别复现 | 已有持有完整 `runtime.snapshot()` 的测试证明外部租约能保活，不能证明服务入口自身持有依赖；独立审查故障探针覆盖进程内；后续 R1 修正回归已补齐 worker 组合 |
| R2 | 保留；本地 Provider 下启用返回 `degraded`，启用完成后相同命令成功 | 循环等待需要同步路径进入 mutation lock，例如状态戳尚未被观察。不能写成所有后台模型请求都会死锁，也不能把请求超时当作 Provider 性能问题 |
| R3 | 保留为输入契约缺口；进程内和 worker 都复现 | 修正前的运行时规格将 turn input 限于默认 Driver，却仍将 `steer` 定义为同一 Run 的后续决策输入，SDK 当时没有替代消费端口。修正后的[运行时规格](../../specs/runtime.md)已闭合两项契约，并保留算法独立性 |
| R4 | 保留；文件与 SQLite 后端都复现，且排队任务随后执行失败 | 已核对 HEAD 中相同的恢复／本地所有权检查顺序，属于保留链路；不仅是界面状态不同步 |
| R5 | 保留；三个依赖插件的构建数为 `1、2、3` | 已核对 HEAD 中同一启动算法。依赖链计数不能用来解释无依赖 worker 基准的全部启动差异 |

归档计划记录的是此前实施与验收证据，不证明后来发现的组合缺陷不存在。本报告保留问题和修正出口，
不把已有通过测试改写成失败，也不把此前的全量、Linux 或性能记录算作本轮重新验证。

## 问题清单

P1 表示影响执行正确性或阻断有效能力组合，应优先修正；P2 表示需要修正的工作量与生命周期重复问题。
“保留链路”表示相关实现已存在于 v0.1.0 基线，不能据此归因于本次新增代码，但仍属于当前架构需要处理的问题。

| 编号 | 优先级 | 问题 | 涉及范围 | 状态 |
|---|---|---|---|---|
| R1 | P1 | 服务调用未完整保留依赖代次 | 新增服务调用与热更新的组合 | 已修正，定向及全量回归通过 |
| R2 | P1 | 后台服务初始化与模型绑定循环等待 | 生命周期与新增模型端口的组合 | 已修正，定向及全量回归通过 |
| R3 | P1 | 自定义 Driver 缺少运行中 steering 的交付契约 | 新增 Driver 与原生输入链的设计缺口 | 已修正，定向及全量回归通过 |
| R4 | P1 | 恢复误判已有本地所有者的排队 Run | 保留的 Supervisor 与恢复链路 | 已修正，定向及全量回归通过 |
| R5 | P2 | 启动重复构建依赖方代次 | 保留的插件启动算法 | 已修正，定向及全量回归通过 |

## R1 服务调用未完整保留依赖代次

### 修正前代码事实与复现

[`ServiceEndpoint._invoke`](../../../ouro/plugins/services.py)在调用期间仅通过 `target.acquire()`
租用目标插件，没有同时租用其传递依赖。注册回调入口
[`_BoundHandler.__call__`](../../../ouro/plugins/services.py)也只租用回调 owner。
构造端点时传入的是 [`active.acquire`](../../../ouro/plugins/runtime.py)，因此这里并没有隐藏的依赖闭包租约。
相比之下，[Catalog 贡献绑定](../../../ouro/plugins/catalog.py)和[计划任务绑定](../../../ouro/plugins/tasks.py)
会保留依赖闭包。

复现使用三个进程内插件：B 依赖 A，C 依赖 B。C 通过公开服务端口调用 B，B 等待一个受控事件后调用 A。
在 B 等待时重建 A，重建会连带发布新的 B、C。此时观察到旧 A、B、C 的租约数分别为 `0、1、0`；
旧 A 已停止，释放事件后，原调用失败：

```text
KeyError: 'service method is unavailable: dev.probe.a/api/read'
```

复现没有额外持有完整 Catalog 租约。现有
[`test_service_dependency_stays_bound_to_its_generation_during_upgrade`](../../../tests/plugins/test_services.py)
主动持有 `runtime.snapshot()`，因此不能证明服务调用本身保留了完整依赖。

注册回调另行复现：过滤器插件依赖记忆服务及数据服务，向记忆服务注册 handler；另一个只依赖记忆服务的插件发起查询。
handler 在等待后读取数据服务；等待期间重建数据服务及过滤器插件，原数据代次停止，handler 随后报
`KeyError: 'service method is unavailable: dev.review.dependency/api/read'`。该场景同样没有额外 Catalog 租约。

### 影响与修正方向

正常升级可以使已经接受的服务调用失去原依赖，破坏“按原绑定完成”的承诺。服务调用、注册回调和其他贡献绑定
应复用一致的依赖闭包租约规则，持有调用实际需要的 owner 与依赖，并在调用结算后释放。显式停用仍应撤销入口，
不能因保留旧代次而继续接受被禁止的新工作。

验收要求：

- [x] 无额外 Catalog 租约时，服务调用和注册回调跨依赖重建仍使用原依赖完成；共享传递依赖不会漏持有或重复释放。
- [x] 覆盖进程内与 worker 调用，并验证取消、异常、显式停用和正常返回后的租约与资源释放。

修正：`PluginGeneration` 统一提供精确依赖闭包和调用绑定，服务、注册 handler 与 Task 使用相同代次事实；
Catalog 和 Task 复用具备部分获取回滚的租约获取操作。每个调用只持有必要闭包，未引入持久缓存或全目录保活。
新增 [16 项服务寿命回归](../../../tests/plugins/test_service_lifetimes.py)，覆盖菱形依赖、热更新、正常返回、异常、取消与停用。
进程内／worker 的两个入口在修正前均因缺失依赖租约失败；修正后与服务、Catalog、Task 相关测试共 **88 项通过**。

## R2 后台服务初始化与模型绑定循环等待

### 修正前代码事实与复现

[`PluginRuntimeResource._transition_state_locked`](../../../ouro/plugins/runtime.py)在持有 mutation lock
期间等待发布后的后台服务交接。Background Service 的 `start` 调用 `models.generate`，若模型使用插件 Provider，
则进入 [`RuntimeService.bind_provider`](../../../ouro/application/runtime.py)。绑定先调用
`PluginRuntimeResource.synchronize()`；当持久状态变化尚未被记录为已观察时，它再次等待同一把 mutation lock。
外层在 [`await asyncio.shield(handover)`](../../../ouro/plugins/runtime.py) 等待启动回调，形成
`生命周期持锁 → 等待 start → 等待 Provider 绑定 → 等待生命周期锁` 的闭环。

复现使用 `LocalApplication`、本地模拟 Provider 和公开 SDK。在插件 Background Service 的启动函数中执行一次
`models.generate`，为探针设置 1 秒请求期限。独立复核同样返回 `degraded`，诊断为：

```text
plugin_background_service_start_failed
TimeoutError:
```

启用返回后，通过命令执行相同模型请求得到 `provider answer`。复现没有外部模型请求或网络延迟。
此复核直接验证的是启用路径；冷启动、配置更新、home 服务交接等同类入口仍应加入修正验收，不能由一个探针宣称全部实测。

### 影响与修正方向

已经发布的后台服务无法在初始化期间正常组合原生模型端口；等待时间受请求期限控制，正常插件被误判为降级。
需要重新划定目录状态提交的临界区与插件回调的执行边界，保留旧服务停止、新服务启动及失败阻止交接的规则。
对于已经接受的调用，应明确其绑定事实，避免回入自身尚未完成的生命周期同步。

延长期限、吞掉超时或让插件自行重试不能解除循环等待。也不能简单删除同步或提前释放所有权，从而允许新旧服务
同时运行。

验收要求：

- [x] 冷启动、启用和更新期间，Background Service 可通过原生模型端口调用插件 Provider，完成初始化。
- [x] 覆盖状态变化、调用取消与旧服务停止失败；保持目录发布一致性和后台服务唯一所有权。

修正：明确区分已发布生命周期回调的嵌套 Provider 绑定与独立请求的状态同步。插件 Runtime 在回调开始时
提供本次已提交目录的短期准入上下文，回调结束即失效并清除引用；只获取所选 Provider 的依赖租约。
生命周期串行锁继续承担交接唯一所有权，Provider 绑定不再回入自身未完成的同步。没有全局跳过同步、提前标记
已观察状态或释放旧服务所有权。进程内和 worker 共用此规则，继承上下文的后续独立任务仍重新同步。
新增 [11 项后台模型回归](../../../tests/plugins/test_background_models.py)，覆盖 host/home、两种执行方式、轻量管理 Host
冷启动、配置更新、取消、旧服务停止失败、外部停用竞争及上下文到期。修正前两种执行方式均返回 `degraded`。
相关回归 **71 项通过**；补齐外部状态测试夹具后，三项外部状态／上下文到期测试另行通过。

## R3 自定义 Driver 缺少运行中 steering 的交付契约

### 修正前代码事实与复现

[`RuntimeService._continue_run_driver`](../../../ouro/application/runtime.py)仅在默认 Driver 分支中
构造 `NativeRunTurns`，接入 `_consume_turn_boundary_inputs`。自定义 Driver 使用的
[`NativeRunDriverContext`](../../../ouro/application/run_drivers.py)没有相应输入消费入口，
公开 [`RunDriverContext`](../../../ouro/sdk/plugins/run_drivers.py)也未提供可供替代算法调用的交付端口。

复现使用公开 [`review-driver`](../../../examples/plugins/review-driver/review_driver_plugin.py)示例。
该 Driver 先请求评审，再请求执行。阻塞第一次模型请求，向同一 Session 提交 `queue_mode="steer"` 的纠正指令，
随后释放模型请求。

观察结果：纠正指令提交回执为 `running`；前两次模型请求均未包含纠正指令，第三次请求才包含它。
持久记录出现两个已完成 Run，Driver 分别为 `dev.ouro.examples.review_driver/run_drivers/review` 和 `ouro.default`。
纠正指令被拖延到自定义 Driver 完成后，作为新的默认 Driver 工作执行。上述结果在进程内和 worker 两种方式均复现，
请求是否包含纠正指令的序列均为 `[False, False, True]`。

### 影响与修正方向

用户试图纠正当前执行时，自定义 Driver 的下一次决策仍可能沿用原输入。循环可替换性没有完整保留 Host 的输入语义。
规格有意隔离默认算法的 turn input／turn policy，因此这里不是遗漏调用某个默认策略的局部错误：
框架仍接受 `steer`，却既不向替代算法交付，也不向调用方表达不支持当前 Run 的纠正。

将原生输入交付与默认循环完成策略分开，定义算法可使用的安全决策或操作边界；存在模型工具批次时须先完整结算。
Driver 应能够使用已经接受的输入，并保持消息提交、Prompt Policy、队列次序与取消语义。默认 Turn Policy 仍可保留
自身轮次规则，不要求所有算法实现相同循环，也不能在待结算的模型工具调用与结果之间插入消息。
仅禁止自定义 Driver 接受 steering 会收窄必要能力，不能作为本项修正完成的依据。修正时同步闭合运行时规格和 SDK 契约。

验收要求：

- [x] 自定义 Driver 多次决策期间，steering 在约定边界进入当前 Run；默认 Driver 的既有行为保持不变。
- [x] 覆盖输入顺序、`follow_up` 与 `steer` 的区别、Prompt Policy 拒绝、等待恢复和取消；不通过额外 Run 掩盖未交付输入。

修正：输入交付归属共享 `NativeRunDriverContext`，在模型决策前自动执行；SDK 增加 `inputs.consume()`，
允许插件算法在没有模型请求的情况下主动声明决策边界。返回消息 ID、完整内容、来源 metadata、scaffold、
semantic tag 和资源引用的脱离持久对象的副本。worker 经有效父调用进入同一个原生端口。
显式消费后的紧接模型请求不额外消费一个队列单元；后续显式消费、模型或工具完成后的决策进入新边界。
待结算模型工具响应、原生操作忙碌和已暂停／结束状态不能插入输入。默认完成策略仍属于默认算法。
消费只读取最新 Session 元数据，不为每次模型决策额外读取完整历史。

[输入回归](../../../tests/application/test_driver_inputs.py)与 [SDK 回归](../../../tests/plugins/test_run_driver_sdk.py)
共 **42 项通过**，覆盖进程内／worker、文件／SQLite、自动／显式消费、FIFO 与消费策略、follow-up 区分、
审批期间暂停后重新打开 Host、Prompt Policy 拒绝、取消和无有效父调用的拒绝。
默认循环、原生批次、Driver 和架构相关定向集成另有 **183 项通过**（早于最后一轮输入用例补齐）。

## R4 恢复误判已有本地所有者的排队 Run

### 修正前代码事实与复现

[`RuntimeSupervisor.submit`](../../../ouro/application/supervisor.py)先调用 `reconcile_session`，
随后才检查 `_jobs_by_session`。而 [`SessionRecoveryService._recover_snapshot`](../../../ouro/application/session_recovery.py)
主要根据 Session 执行锁识别活跃执行，尚未取得执行锁的本地排队任务可能被当成中断任务。

复现步骤：

1. 将 Supervisor 并发数设为 1，用阻塞模型请求占用执行 worker。
2. 为另一个 Session 创建持久 queued Run，通过 `submit_prepared` 接受进入本地队列。
3. 再次对该 Session 调用 `Supervisor.submit`。

第二次提交返回 `SessionBusyError`，但原 Run 的持久状态已从 `queued` 变成 `recoverable`，metadata 出现
`recovered_from_status="queued"`；按原 handle 查询的 Supervisor 本地状态仍为 `queued`。
释放占用 worker 的第一个请求后，原排队任务最终返回 `failed`，诊断为
`supervised_run_failed: runtime task failed (ValueError)`。文件与 SQLite 两个 Session 后端结果相同。

### 影响与修正方向

一次被拒绝的重复提交修改了已有工作的执行事实，造成本地所有权与持久状态不一致，并使原任务无法正常执行。相关调用顺序已存在于
v0.1.0 基线；此处应作为当前框架缺陷修正，而非作为新增插件 API 的回归归因。

恢复入口应统一判断已经接受工作的所有权。“未取得执行锁”不能单独证明任务失去所有者。
检查 Supervisor、普通 Runtime 输入、插件输入及 Host 输入消费等入口，复用原生所有权边界，避免逐个添加例外判断。
不应为修复本地接纳与执行之间的空隙而引入全局 Host 名册或心跳平台。

验收要求：

- [x] 已被本地 Supervisor 接受的 queued Run 不因重复提交或恢复查询改变状态；拒绝提交不修改其事实。
- [x] 文件与 SQLite 后端均验证队列、执行和终结交接；真正失去所有者的任务仍可按契约恢复，外部持锁执行不会被误恢复。

修正：持久 Run 被 Supervisor 接纳排队前，经 `RunExecutionOwnership.reserve` 取得真实 Session 执行锁，
持锁复核当前 queued 身份。`RunReservation` 只允许向匹配 Session/Run 一次性移交原锁；执行接管后沿用原生
释放规则。排队终止在所属 reservation 授权下结算；接纳取消、关闭、执行前失败均释放未移交锁。
恢复继续只判断同一原生锁，无须引入全局 Host 名册、第二套活跃 Run 注册表或各入口的恢复例外。
已删除 Supervisor 原有的本地恢复特判。尚无持久 Run 的普通调度回执和仅保存意图的 queued input 不新增磁盘锁。

[18 项排队所有权回归](../../../tests/application/test_queued_ownership.py)通过，覆盖两种后端、独立 Store/Runtime、
重复提交、直接恢复、Session 打开、普通／插件／Host 输入、同一 Run 执行完成、排队取消、关闭、执行前异常及
接纳取消后的孤儿恢复。现有 Supervisor 的 **32 项回归**通过；跨 Host、Agent、continuation 及 Host 输入扩大回归
中 198 项通过。一项测试包装器未转发新增 reservation 参数，修正后该测试的两种收尾边界均通过，整体门禁以最终版本重跑。

## R5 启动重复构建依赖方代次

### 修正前代码事实与复现

[`PluginRuntimeResource.start`](../../../ouro/plugins/runtime.py)遍历所有 desired roots，逐项调用
[`activate`](../../../ouro/plugins/runtime.py)。每次激活计算依赖方闭包，并将其中已经活跃的插件再次纳入重建。
启动和显式重启因此共用了不适合批量启动的工作规划。

三个默认启用的插件中，B 依赖 A，C 依赖 B。一次 `start()` 创建的 A、B、C 实例数量分别为 `1、2、3`。
后台服务记录如下，最后三个插件状态均为 `active`：

```text
a:start, b:start, c:start,
c:stop, b:stop, b:start, c:start,
c:stop, c:start
```

上述算法存在于 v0.1.0 基线。对按依赖顺序进入该循环的长度为 n 的链，每项均纳入依赖方重建时，
代次构建次数可达到 `n(n+1)/2`；该工作量推导不等同于所有依赖图或实际耗时均严格服从同一公式。

### 影响与修正方向

重复构建带来包校验、状态读取、配置解析、worker 创建和目录发布成本，也会重复触发真实后台服务初始化。
服务能否幂等不应成为框架重复启动的补偿手段。

以操作内的一致状态快照规划启动图，按依赖顺序各构建一次所需代次；显式重启和依赖契约变化才执行对应重建。
保留独立根失败隔离与状态冲突检查，避免因合并启动工作而让一个坏插件阻断所有无关插件。

验收要求：

- [x] 无并发配置变更的正常启动中，每个所需代次只创建一次，后台服务只启动一次；覆盖链式、共享依赖及无依赖场景。
- [x] 失败依赖只阻断其依赖方，无关插件仍可启动；比较构建次数、读取量与启动时间，确认没有把工作转移为常驻扫描或缓存负担。

修正：启动按一次状态快照规划依赖图，按拓扑顺序构建未激活代次，统一核对共享状态后发布目录并启动后台服务。
依赖解析在操作内复用已经解析的节点；执行指纹沿精确代次依赖计算，不为每个节点再次解析包与版本。
prepare/start 失败跳过依赖方，健康候选保持；贡献投影冲突带回 owner 身份，丢弃该候选及其依赖方后重新投影，
不会重建健康实例。共享状态冲突仍丢弃未发布候选，下一同步边界重新规划。显式 restart 保持原重建语义。
没有持久图缓存、后台扫描或并发启动补偿。

[启动图回归](../../../tests/plugins/test_startup_graph.py)、执行契约与 Catalog 绑定共 **20 项通过**；插件系统、
同步、生命周期和后台模型相关 **126 项通过**。新增验证覆盖链、菱形共享依赖、独立插件、候选失败、贡献冲突、
依赖环、边版本不匹配及构建期间共享状态变更。

PowerShell 执行 `uv run --frozen python -m dev.benchmarks.plugin_startup_graph --counts 3 10 30 --repeat 3`。
30 个内置插件、每组 3 次测量的结果如下；构造、导入和关闭不计时，每次新建临时 home，不清除 OS 缓存。
对照为同一当前运行时中的旧逐根激活算法，不是整个 v0.1.0 版本对照；未启动 worker。测量期间同机有离线回归
运行，耗时仅作为诊断参考；构建、服务启动和状态读取次数是本场景的确定工作量，不能解释为物理磁盘读次数。

| 依赖图 | 代次构建／后台启动：旧 → 新 | 状态读取：旧 → 新 | 启动 P50：旧 → 新 |
|---|---|---|---|
| 链 | 465 → 30 | 121 → 3 | 1355.59 ms → 87.50 ms |
| 共享依赖 | 59 → 30 | 121 → 3 | 374.43 ms → 89.38 ms |
| 独立插件 | 30 → 30 | 121 → 3 | 244.21 ms → 82.65 ms |

## 性能证据与适用范围

已有[性能验收记录](2026-10-12-plugin-platform-refactoring.md)包含 Windows 下相同依赖环境的规模对照。
100 个 worker 的 Host 启动 P50 从约 14.325 秒变为 20.184 秒，增加约 41%；关闭 P50 从约 23.230 秒降至
0.168 秒。每组测量 5 个新进程，启动不包含安装准备和 Python 导入，没有清空 OS 缓存。

这些结果说明启动与关闭的收益不同。规模基准中的插件没有构成 R5 的依赖链，因此不能把上述启动差异直接归因于
重复依赖方构建，也不能仅凭总耗时将差异全部归因于 Windows 进程管理。
应先消除已证明的重复工作，再根据分项测量决定是否需要进一步优化。

## 独立审查阶段的验证记录

独立审查阶段的平台为 Windows、PowerShell、CPython 3.14.6，使用 `uv run --frozen` 和仓库锁定依赖。
以下记录产生于产品实现修改前；后续修正证据见 R1–R5 的验收段。验证分为已有测试和独立故障探针，
两者不能混为“缺陷已修复”。全部工作仍未提交。

### 已有测试

工作目录为仓库根目录，shell 为 PowerShell。框架、插件及扩展组合测试命令如下：

```powershell
$reviewPaths = @(
    'tests/architecture',
    'tests/core',
    'tests/plugins',
    'tests/application/test_run_drivers.py',
    'tests/application/test_driver_inputs.py',
    'tests/application/test_driver_contract_loading.py',
    'tests/application/test_dynamic_tool_groups.py',
    'tests/application/test_worker_dynamic_tools.py',
    'tests/application/test_plugin_native_runs.py',
    'tests/application/test_plugin_scheduling.py',
    'tests/application/test_plugin_tasks.py',
    'tests/application/test_model_generation.py',
    'tests/application/test_model_requests.py',
    'tests/application/test_driver_agents.py'
)
uv run --frozen pytest @reviewPaths -q --no-cov
```

结果：**918 passed in 553.60s**。其中包括全部 17 项架构边界检查，以及生命周期、数据清理、跨 Host 同步、
进程内／worker Provider、服务协作、动态工具、Driver 原契约恢复和插件调度测试。

补充原生所有权、批次、进程及模型结果投影测试：

```powershell
$nativePaths = @(
    'tests/application/test_supervisor.py',
    'tests/application/test_tool_call_batches.py',
    'tests/application/test_run_continuations.py',
    'tests/test_local_processes.py',
    'tests/test_local_file_access.py',
    'tests/tools/test_builtin_shell.py',
    'tests/providers/test_tool_result_content.py'
)
uv run --frozen pytest @nativePaths -q --no-cov
```

结果：**139 passed in 86.24s**。两组共 **1,057 项已有测试通过**，没有失败或跳过。
这说明已有测试覆盖的行为成立，不证明 R1–R5 所述组合得到覆盖。

### 独立故障探针

在仓库外的临时 pytest 文件中运行八个探针，每项使用隔离的临时 home；探针断言实际缺陷现象，
结果为 **8 passed in 12.79s**，表示八个复现断言均成立，不表示正确行为验收通过。

| 条目 | 探针数与方式 | 复用入口与关键观察 |
|---|---|---|
| R1 | 2 个，进程内服务及注册 handler | SDK `ServiceContribution`、`services.call/register`；分别在目标及回调内用事件暂停，重建后旧依赖均停止，调用均报服务不可用。注册场景复用[服务测试夹具](../../../tests/plugins/test_services.py)中的 `Memory`、`Consumer` |
| R2 | 1 个，真实 LocalApplication 与本地插件 Provider | 复用 [Provider 包夹具](../../../tests/plugins/test_providers.py)；加入 Background Service，在 start 中请求 1 秒期限的独立生成。启用降级，同一 Provider 的命令调用随后成功 |
| R3 | 2 个，进程内／worker | 复用 [review-driver Host 与包夹具](../../../tests/application/test_run_drivers.py)，模型首轮由事件暂停；接纳 steer 后请求包含标记的序列为 `[False, False, True]`，产生两个 Run |
| R4 | 2 个，文件／SQLite | 复用 [ConcurrencyProvider](../../../tests/application/test_supervisor.py)，`max_concurrency=1`；重复提交先改变 queued Run 的事实，再被拒绝，原 handle 最终失败 |
| R5 | 1 个，进程内冷启动 | 三个默认启用的 builtin 定义经同一 SDK 加载，B 依赖 A、C 依赖 B；构建与后台服务记录独立计数，实例数为 `1、2、3` |

以上为独立审查结束时的历史证据。后续修正已落盘期望正确行为的正式回归，并补齐 worker、其他生命周期入口、
依赖图与取消／失败交接场景，见 R1–R5 各节。独立审查阶段未重跑全量门禁；后续优化门禁结果另行记录，
不能与此前归档的跨平台和真实环境验收混用。

## 整体验收发现的呈现问题

首次完整回归结果为 **5,341 passed、6 skipped、3 failed，覆盖率 87.05%**，耗时 4,076.31 秒。
三个失败均在 TUI；这次结果不作为最终通过记录。后续诊断及修正如下：

- 连续审批测试只等待第二个请求进入投影，随即断言焦点；实际 UI 同步尚未结束。测试现在等待审批面板可接受
  键盘输入，保留请求顺序、文件副作用和最终完成状态的断言。
- 从任务历史进入另一个 Runtime 正在执行的 Session 时，历史记录已有 assistant 内容，但画面偶发缺失。
  通过失败快照和确定性回归确认，[`append_many_rendered`](../../../ouro/tui/conversation_history.py) 的首次追加路径会在布局／渲染前返回，随后 live 内容被
  移除。修正由历史组件复用已有初始渲染等待，完成交接后返回；未增加业务重试、固定延时或新的渲染状态层。
  任务导航测试同时等待选择动作完成，再验证画面。新增回归在修正前稳定失败。
- Windows 原生控制台探针固定重设缓冲区尺寸，在当前隐藏控制台触发 `ERROR_INVALID_PARAMETER`。本机探针观察到
  字体宽度为 0；[Windows API 契约](https://learn.microsoft.com/en-us/windows/console/setconsolescreenbuffersize)
  还约束依赖字体和系统指标的最小尺寸。输出验证本身不要求改变窗口大小，探针已在实际分配的缓冲区内构造完整帧，
  保留超过 8,192 字符的分片对照、100 次原生写入、Unicode 完整性和光标位置不变断言。

历史渲染、跨 Runtime 会话、原生控制台、连续审批和工具呈现的带覆盖率定向回归 **80 项全部通过**。
随后完整回归在 **4,457 项通过、6 项跳过**后，因缓存子会话切换用例失败而提前停止。其缓存内容已替换，
保存的滚动位置却仍等待后续布局，形成新内容与旧位置的交接间隙。[滚动恢复](../../../ouro/tui/widgets.py)现在立即发布保存的位置，仍由原有
布局方法按目标内容的实际尺寸校正，未增加新的切换状态层。该次提前停止不作为完整通过或覆盖率记录。

[双 TUI 观察测试](../../../tests/tui/test_live_session_attachment.py)另有准备阶段期限受界面渲染负载影响的问题。已将执行中的工具准备放到两个观察界面挂载前，
保留 5 秒准备期限与双观察者、连续 Run、工具消失和摘要去重断言；没有通过增加等待时间处理功能缺陷。
缓存切换与跨 Runtime 会话的带覆盖率定向回归 **7 项全部通过**。
最终完整门禁优先执行 TUI，随后执行其余测试。已比对默认和调整顺序后的逐项收集清单：**5,351 项完全相同，
没有遗漏或重复**。本轮全部通过，结果见下节；按用户要求，这是本任务的最后一次全量测试。

## 最终完整门禁

执行环境为 Windows、PowerShell、CPython 3.14.6，使用仓库锁定的全部开发依赖。
最终完整测试收集 **5,351 项**，结果为 **5,345 passed、6 skipped，覆盖率 87.04%**，超过仓库要求的 80%。
耗时 **3,676.07 秒（1:01:16）**，退出码为 0。6 项跳过沿用原有条件：2 项文件重放专用用例的 SQLite 参数、
3 项本机不可用的符号链接用例、1 项 POSIX 权限用例。本轮没有通过新增跳过或放宽断言处理功能缺陷。
产品代码和测试在本轮开始后保持不变，结束后仅完成报告及索引整理。
此前完成的离线验收不替代本轮门禁；本轮 Windows 测试也不构成 Linux、macOS、真实模型账户或人工终端体验的新增验收。

```powershell
uv sync --frozen --all-groups
$remainingTests = @(rg --files tests -g 'test_*.py' | Where-Object { $_ -notmatch '^tests[\\/]tui[\\/]' } | Sort-Object)
$testTargets = @('tests/tui') + $remainingTests
uv run --frozen pytest @testTargets --cov=ouro --cov-report=term-missing --maxfail=1 --tb=short --durations=10 -o faulthandler_timeout=90
uv run --frozen ruff check ouro tests dev/benchmarks/plugin_startup_graph.py
uv run --frozen ruff format --check ouro tests dev/benchmarks/plugin_startup_graph.py
uv run --frozen mypy ouro
uv run --frozen python -m compileall -q ouro
uv build
```

锁定依赖同步通过；Ruff 检查及 762 个文件（包含新基准脚本）的格式检查通过；mypy 检查 429 个源文件通过；
字节码编译通过；源码包和 wheel 构建成功。额外检查本轮文档的本地链接目标、代码块闭合和 `git diff --check`。
测试与构建使用临时／本地制品，所有产品改动保持未提交状态。

## 修正顺序与交付边界

本轮依次修正 R1 的调用绑定、R2 的生命周期回入、R3 的输入交付和 R4 的执行所有权，最后收敛 R5 的启动工作量。
各项以独立复现和期望正确行为的回归约束相应责任层；阶段内完成定向验证，整合后执行
[贡献指南](../../../CONTRIBUTING.md)规定的全量门禁，首次全量暴露的呈现问题单独记录于上节。

修正保持 v0.1.0 已有功能设计，并保留新增的服务组合、热更新、后台初始化、替代 Driver 和调度能力。
没有以禁止正常依赖、关闭热更新、拒绝后台使用模型或取消自定义算法输入来换取测试通过。
框架负责的准入、绑定、输入和恢复规则在原有所有者处统一，插件不承担框架锁重试、额外 Catalog 保活或执行状态修补。
插件特有的业务流程、数据语义及转换实现仍由插件拥有，不能因修复共同责任而进入 Core。

各项验收同时检查公开 SDK 组合、停用后的资源释放、无关插件的持续成本，以及公开契约变化对现有消费者和
待完成执行的影响；具体边界见各项验收记录。
这五项修正目前不需要历史格式兼容分支；未来确有兼容、迁移或退役需求时，应按明确的版本承诺及实际消费者评审，
不能把当前不实施某种机制解释为永久禁止，也不能借长期演进预建无消费者的基础设施。
当前有效契约由领域规格维护；本报告保留审查归因、修正取舍和完成证据，长期发布政策的适用边界见前文。
