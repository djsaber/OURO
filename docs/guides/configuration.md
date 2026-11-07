# 配置文件与个性化提示词

> 文档类型：使用指南

配置按职责保存在 `OURO_HOME/config/`，每个分区只有一个权威文件：

| 文件 | 内容 |
|---|---|
| `config.json` | 权限、工具执行、运行预算、排队输入、压缩和 Skills 发现策略 |
| `providers.json` | `providers.default`、`providers.items` 及各 Provider 的 Endpoint |
| `mcp.json` | `mcp.servers` |
| `prompts.json` | `prompts.revision`、`prompts.replacements`、`prompts.additions` |
| `agents.json` | `agents.revision`、`agents.definitions`、`agents.runtime` |
| `provider-templates.json` | Provider 模板及本地覆盖元数据 |

各文件独立读取、加锁并原子写入。查看或修改一个领域的配置，不受无关文件中的错误阻断；需要组合配置的
运行与跨域引用检查仍会读取并校验所需文件，失败时明确报错，不静默使用默认配置。
当前实现只接受现行结构：读取主文件时发现 `providers`、`mcp`、`agents` 或 `prompts` 会明确报错，
不自动迁移；使用新 home 或显式修复失效配置。托管凭据继续保存在独立的 `credentials/`。

## 配置生效边界

TUI 保存、CLI 命令和同一 `OURO_HOME` 下其他进程的配置写入都按以下边界生效，无需重启 TUI 或重建 Session。
保存设置不会自行发起模型请求，也不会中断正在生成的响应或已经执行的工具。

| 配置 | 生效边界 |
|---|---|
| 压缩开关、触发比例、目标比例、策略及摘要模型参数 | 下一次上下文准备；手动压缩在操作开始时读取 |
| 工具与插件权限、工具并发模式/数量、workspace 限制、输出截断长度 | 下一批工具调用；已经开始的批次保留执行快照 |
| 运行软预算 | 下一次模型请求的预算检查，沿用当前 Run 的累计用量和轮数 |
| 排队输入消费模式 | 下一次消费队列，保持 FIFO 与输入批次的原子性 |
| 模型端点参数、默认模型与认证配置 | 下一次模型路由解析；显式选择的会话模型保持其路由 |
| 个性化提示词、Skills 发现配置 | 下一次模型请求准备 |
| Agent definition | 下一次父级模型决策 |

压缩阈值调低后，若下一次准备的估算输入已达到新阈值且存在可压缩旧帧，就触发压缩；调高或关闭后按新设置判断。
同一次压缩的触发比例、目标、策略、fallback 和摘要请求参数使用同一快照，生成过程中修改设置留待下一次操作。
运行预算调高后可以继续原先暂停的 Run；配置更新不清零累计消耗，也不自动恢复暂停的 Run。
已经接受的 Delegation 保持其预算和能力上限，根 Run 的共享软预算随父级请求边界更新。

`config.json` 的运行设置共用读取、校验和发布边界，按文件标识缓存；未变化时不重复读取和解析内容。
空队列不刷新配置；预算检查先于能力绑定，预算记录仅在值变化时写入；工具执行器按 Run 在使用时更新。
读取或校验失败会明确阻止该次执行，不静默沿用旧配置。修复配置后可重新发起操作。
MCP 连接、插件启停和 Agent 平台派遣上限按各自生命周期操作生效；Agent 平台派遣上限仍需重启。

输出上限 `max_output_tokens` 可以省略或设为 `null`，均表示 OURO 不限制输出；正整数设置有限上限。TUI 的端点与摘要设置使用英文标签 `Max output tokens`，空值标为 `Optional`，清空输入即可恢复不限制。CLI 清空示例：

```bash
ouro provider endpoint update <provider_id> <endpoint_id> --max-output-tokens none
ouro config compaction --max-output-tokens none
```

摘要上限是独立配置，默认不限制，选择当前会话模型也不会继承主请求或端点的输出数值。Messages 协议要求显式有限值，需要分别为使用它的主请求和摘要请求配置。请求与输入预算见[Provider 规格](../specs/provider-configuration.md)，摘要采用条件见[上下文与压缩](../specs/context-and-compaction.md)。

个性化提示词通过 `ouro config prompt` 或 TUI `Actions > Personal prompts` 管理。替换项最多启用一项；
启用另一项会原子关闭原项，全部关闭时使用默认系统提示词。追加项可独立启停，按数组顺序在基础提示词之后、
Skills 和请求级上下文之前追加。描述可省略，仅用于列表展示和用户标注，模型只接收正文。

命令操作示例：

```bash
uv run --frozen ouro config prompt add replacement --home ./.ouro-dev --text "Custom system prompt" --enabled
uv run --frozen ouro config prompt add addition --home ./.ouro-dev --description "表达风格" --text "回答简洁清晰" --enabled
uv run --frozen ouro config prompt move addition 1 1 --home ./.ouro-dev
uv run --frozen ouro config prompt list --home ./.ouro-dev --json
```

`prompts.json` 持久化结果示例（ID 由框架生成，无需用户填写）：

```json
{
  "prompts": {
    "revision": 0,
    "replacements": [
      {"id": "prompt_619d35f3e42e4019a4d42c612d7c32b5", "description": "个人助手", "text": "你是一位个人助手。", "enabled": true}
    ],
    "additions": [
      {"id": "prompt_c3d96ba1fe774216af49f225164f59d6", "description": "表达风格", "text": "回答简洁清晰。", "enabled": true},
      {"id": "prompt_57a86ed704064a40b2a5193c53e47fbe", "text": "先说明结论，再给出依据。", "enabled": false}
    ]
  }
}
```

用户只需填写非空白 `text`；`description` 可选，`enabled` 默认为 false。
框架在创建时自动生成内部 ID，编辑、启停和排序时保持不变；TUI 不展示或编辑 ID，
无描述时用正文摘要展示条目。读取持久化配置不会补建缺失的 ID，以免身份在读取时变化。
配置 revision 用于拒绝并发覆盖；CLI 可传 `--expected-revision`，TUI 自动使用打开时的版本。

`--json` 输出以 `config` 提供条目、启停状态、顺序和 revision；`effective_system_prompt` 提供按配置组合的
完整基础与追加提示词。未启用替换项时，基础部分采用默认系统提示词。

CLI 支持 `list`、`add`、`edit`、`remove`、`enable`、`disable`、`move`。新增只需指定
`<replacement|addition>`；管理已有条目使用列表中从 1 开始的序号，`move` 再接受目标位置。
序号在命令读取的配置快照中解析为内部 ID，写入时检查 revision，防止并发修改期间操作错位。
新增/编辑可用 `--text` 或
`--file <UTF-8文件>` 输入正文，`--description` 修改标注，`--enabled`/`--disabled` 指定启停。
TUI 复用分组列表及多行编辑器，支持 Ctrl+Enter 换行、编辑、启停、移动和删除确认。
保存后在下一次模型请求准备时生效，无需重启；已开始准备或正在生成的请求保持原快照，工具调用后的
下一轮请求也会读取新配置。共享同一 OURO_HOME 的 CLI、TUI 和其他进程写入均适用。
运行参数 `--system-prompt` 只覆盖基础提示词，仍保留最新启用的追加项。
替换基础提示词不会关闭 Skills、AGENTS 或运行环境信息注入，也不会重写对话历史。
配置读取或校验失败时暂停请求并报告诊断，修复后可继续；不会静默使用旧配置。
最近一次真实请求的上下文快照记录提示词 revision、实际使用的条目 ID 和基础提示词来源。

`config.json` 示例：

```json
{
  "tool_execution": {
    "mode": "parallel",
    "max_concurrency": 4,
    "max_output_chars": 100000
  },
  "queued_inputs": {
    "consumption_mode": "all_at_once"
  },
  "run_budget": {
    "max_model_rounds": 50,
    "max_elapsed_seconds": 1800
  },
  "skills": {
    "enabled": true,
    "disabled": [
      "unsafe-community-skill"
    ],
    "disabled_sources": [
      "claude.user"
    ],
    "paths": [
      {
        "path": "shared-skills",
        "source": "user",
        "enabled": true
      }
    ]
  }
}
```

`providers.json` 示例：

```json
{
  "providers": {
    "default": "provider_<generated>",
    "items": {
      "provider_<generated>": {
        "display_name": "GLM Gateway",
        "provider_type": "custom",
        "protocol": "openai-chat-completions",
        "credential_ref": "env:GLM_API_KEY",
        "base_url": "https://example.test/v1",
        "api_path": "/chat/completions",
        "retry_policy": {
          "max_attempts": 3,
          "initial_backoff_seconds": 0.5,
          "max_backoff_seconds": 8.0,
          "backoff_multiplier": 2.0
        },
        "default_endpoint": "endpoint_<generated>",
        "endpoints": {
          "endpoint_<generated>": {
            "display_name": "Reasoning",
            "model_id": "model-name",
            "temperature": 0.2,
            "context_window": 200000,
            "max_output_tokens": 16000,
            "supports_tools": true,
            "supports_streaming": true,
            "supports_structured_output": true,
            "reasoning": {
              "capability": {
                "activation": "toggle",
                "supported_efforts": [],
                "default": {
                  "mode": "enabled",
                  "effort": null
                }
              },
              "profile": {
                "name": "thinking-object-toggle",
                "effort": {
                  "enabled": {
                    "thinking": {
                      "type": "enabled"
                    }
                  },
                  "disabled": {
                    "thinking": {
                      "type": "disabled"
                    }
                  }
                }
              }
            }
          }
        },
        "timeout_seconds": 60,
        "enabled": true
      }
    }
  }
}
```

`timeout_seconds` 默认 60 秒。模型流式请求按等待下一条完整响应事件的空闲时间计时，
持续生成文本、思考或工具参数不会因总时长超过 60 秒而终止；非流式请求等待完整响应。
此规则对三种 HTTP 模型协议一致，详细边界见
[请求等待规格](../specs/provider-configuration.md#请求等待错误与资源生命周期)。

`mcp.json` 示例：

```json
{
  "mcp": {
    "servers": {}
  }
}
```

凭据优先使用 `env:VARIABLE_NAME` 引用。CLI 和公开 JSON 输出不会回显凭据值。
