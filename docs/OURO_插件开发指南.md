# OURO 插件开发指南

本文面向本地 user plugin 作者，描述当前代码实际支持的契约。规范性约束以
[插件系统目标规格](../dev/specs/plugin-system.md)为准。

## 1. 先选择正确的扩展形态

OURO 插件是系统级扩展模块，只有两类：

- `builtin` 由 OURO composition root 提供，user package 不能安装、更新、移除或覆盖其 identity；
- `user` 安装到一个 `OURO_HOME`，不随项目或 workspace 自动激活。

workspace 可以作为一次 Tool/Command 调用的 request context。项目级说明应使用 workspace Skill 或提示词，而不是
创建项目插件。

按能力选择 runtime：

| Runtime | 用途 | 信任与资源边界 |
|---|---|---|
| `declarative` | Agent Skill、MCP descriptor 等静态资源 | 不 import 插件代码 |
| `python-inprocess` | 深度 Python 扩展、低延迟回调、Host request context | 与 OURO 同进程、full-trust、exact-digest code trust |
| `worker` | 独立进程、多语言或进程故障隔离 | 严格 stdio JSON-RPC 协议；不是 OS sandbox |

不要为了简单 Skill 建 worker，也不要把需要独立第三方依赖环境的代码塞进 OURO 主 Python 环境。

## 2. 最小本地闭环

创建一个可运行的 in-process 插件：

```bash
uv run ouro plugin scaffold ./my-plugin \
  --plugin-id dev.example.my_plugin \
  --name "My plugin" \
  --json
```

scaffold 原子创建新目录，目标已存在时拒绝覆盖，并在发布目录前运行完整 package validation。随后显式完成安装与
授权：

```bash
uv run ouro plugin validate ./my-plugin --json
uv run ouro plugin install ./my-plugin --home ./.ouro-dev --json
uv run ouro plugin authorize dev.example.my_plugin --home ./.ouro-dev --json
uv run ouro plugin trust-code dev.example.my_plugin --home ./.ouro-dev --json
uv run ouro plugin enable dev.example.my_plugin --home ./.ouro-dev --json
uv run ouro plugin run-command dev.example.my_plugin:hello world --home ./.ouro-dev
```

安装只保存不可变 bytes，不 import、不执行、不给权限也不启用。`authorize` 接受 manifest 声明的 Host capability；
`trust-code` 接受 executable package 的 exact digest。两者不是同一个授权。

本地修改后可使用开发 reload：

```bash
uv run ouro plugin reload dev.example.my_plugin ./my-plugin \
  --authorize \
  --trust-code \
  --home ./.ouro-dev \
  --json
```

reload 先静态校验并并排安装新 digest；只有候选配置、能力授权和代码信任完整时才原子 select。省略显式授权选项时，
active 插件的候选保持并排安装并返回 `needs_authorization` 或 `needs_code_trust`，旧 Catalog 继续可用。相同 digest 的
reload 执行显式 restart，不制造新 package。

## 3. Manifest 与 package

Manifest 固定在 `.ouro-plugin/plugin.json`，关键 identity 是：

- `plugin_id`：反向域名风格的稳定 identity；
- `version`：作者发布的 SemVer；
- `package_digest`：OURO 对规范化文件树计算的 SHA-256，不由作者填写；
- `namespace`：Tool 暴露名和 Python owned module 的命名边界；
- `engines.ouro` / `engines.plugin_api`：显式版本范围；
- `runtime`、`contributes`、`permissions`、`dependencies`：可执行契约。

相同 version、不同 bytes 会形成不同 digest 并并排保存，不静默覆盖。当前开发阶段不兼容旧 manifest/state/SDK 或
worker protocol，schema 错误会直接拒绝。

Git source 使用 shallow、blob-filtered fetch；指定 `--subdirectory` 时在 checkout 前按 Git tree 校验文件数与大小，
再做 sparse checkout。Git LFS 自动下载被禁用。archive/local/Git 最终都经过同一 package path、symlink、类型、大小、
manifest 与 digest 校验。

## 4. SDK contribution

in-process 插件只从 `ouro.sdk.plugins.v1` 使用公开类型。factory 接收 `PluginContext` 并返回实现以下生命周期的对象：

```python
class Plugin:
    async def prepare(self) -> ContributionSet: ...
    async def start(self) -> None: ...
    async def stop(self, reason, deadline) -> None: ...
```

- `prepare` 声明 contribution，不发布全局对象，不启动长期副作用；
- `start` 在候选 contribution 与 required resource 准备完成后调用；
- `stop` 必须幂等，并在 deadline 内关闭 owner 资源。

当前稳定 contribution 包括 Tool、Command、Hook、Context Provider、Compaction Strategy、Skill、MCP Server、
Background Service、Continuation Policy 和 Tool Policy。插件不能动态注册新的 extension point。

Tool 可选择接收 Host-owned `ToolExecutionContext`，使用其中的 workspace、受约束路径解析、取消与输出限制。Command、
Continuation 不直接写 Session/Run，而是返回类型化意图；application 层负责 synthetic input、队列和持久化。Tool
Policy 只能把执行提升为审批或拒绝，不能降低 Host policy、批准调用或改写参数。

Tool handler 除 `ToolOutput` 或字符串外，还可返回 `ToolInputRequest(data=...)`。`data` 是 Host 透明转发和持久化的
任意 JSON 值；Host 可据此构建单选、多选、表单或卡片界面。Run 会进入 `waiting_user`，Host 调用
`answer_user` 后，回答以该次 tool call 的结果返回模型。此协议收集数据，不表示批准或拒绝工具执行。

```python
from ouro.sdk.plugins.v1 import ToolInputRequest

async def confirm_plan(call):
    return ToolInputRequest(
        data={
            "type": "plan_confirmation",
            "plan": call.arguments["plan"],
            "choices": ["accept", "revise"],
        }
    )
```

## 5. Catalog、owner 与 durable contract

一个 Run 只读取一个不可变 `ExtensionCatalogLease`。插件更新先完整构建候选 generation 与 Catalog，再切换 current
pointer；旧 Run 继续持有旧 Catalog。Background Service 是每 plugin 的 activation-singleton，不属于 Run Catalog。

每个 generation 拥有 instance、worker、MCP、task、module、gate 和 contribution。disable/revoke 关闭 gate 后不能
开始新的插件回调；lease 释放后 owner 才回收。

Run、Tool/Approval 和 Continuation 持久化稳定 `execution_contract_hash`，覆盖 package、immutable configuration
revision、传递 dependency lock、Plugin API/worker protocol 和 contribution contract。随机 `generation_id` 只用于
当前进程 owner 与诊断。重启后可以从保留的等价内容构建新的 process-local generation；同名但不同 contract 的 Tool
不能接管 pending invocation。

## 6. 权限、配置与数据

OURO 保留个人智能体所需的基础边界：

- executable user plugin 的 exact-digest code trust；
- 封闭 permission id 与 capability authorization；
- credential 只保存 `env:VARIABLE` binding reference，秘密不进入 manifest/state/worker initialize JSON；
- user plugin Tool 始终经过 Host schema、审批、Tool Policy 和 `ToolExecutor`；
- package、workspace path 与 owner 生命周期校验。

`python-inprocess` 是 full-trust，能够直接调用 Python/OS API；OURO 不把它描述为沙箱。worker 提供进程和协议隔离，
同样不承诺系统调用级沙箱。

配置是内容寻址的 immutable revision。active 插件 configure 使用候选 generation，失败不切换 active revision。
candidate 通过 Host data API 的写入先进入暂存视图，发布成功才提交；直接绕过 Host API 的 in-process OS 写入不属于
此事务保证。

## 7. Worker 最小协议

worker 从 stdin 逐行读取 JSON-RPC 2.0 request，只向 stdout 写协议 JSON；诊断写 stderr 或发送
`diagnostic.emit` notification。核心方法是：

- `initialize`：确认 manifest 的 `protocol_version`；
- `contributions.prepare`：返回 manifest 已声明 contribution 的协议描述；
- `lifecycle.start` / `lifecycle.stop` / `shutdown`；
- contribution 调用，例如 `tool.call`；
- `cancel`：取消超时或被 Host 取消的 request。

stdout 污染、未知消息、过大响应、声明不一致和错误 result shape 都会使协议调用失败。完整最小实现见
[`examples/plugins/worker-tool`](../examples/plugins/worker-tool)。

worker tool 以 `{"input_request": <任意 JSON 值>}` 返回同一种挂起请求；该字段不能与 `content`、
`structured`、`resource_refs` 或 `is_error` 同时出现。

## 8. 社区资源导入前审查

```bash
uv run ouro plugin import-report ./community-package --json
```

报告逐项给出：

- `supported`：可直接作为 Agent Skill 等资源表达；
- `converted`：语义可表达，但必须转换为 OURO descriptor/permission；
- `ignored`：源 Host 专属且不进入 OURO；
- `incompatible`：需要显式 worker/MCP port，不能直接运行。

每个可迁移项给出 `ouro_target`，报告同时列出 outcome count、lost semantics、安全差异和所需权限。该命令只读文件，
不执行社区 Hook、JavaScript runtime、安装脚本或 MCP server。

## 9. 可运行示例

- [`declarative-skill`](../examples/plugins/declarative-skill)：静态 Agent Skill；
- [`inprocess-command`](../examples/plugins/inprocess-command)：Python Command；
- [`worker-tool`](../examples/plugins/worker-tool)：stdio worker Tool。

这三个 package 由测试执行真实 validate/authorize/enable 生命周期，避免文档示例与实现漂移。
