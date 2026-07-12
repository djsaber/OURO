# OURO Agent

OURO 是一个本地优先的个人智能体运行时。它以可恢复会话为中心，提供模型调用、工具执行、
权限审批、上下文压缩、Skills 发现和本地可读持久化，并为后续桌面宿主与扩展系统保留清晰边界。

## 当前能力

- 交互模式，以及 `run --print`、`run --json` 两种非交互模式。
- 基于 `provider/endpoint` 的模型配置、连接检查、会话级模型选择和 OpenAI-compatible 适配。
- 多轮模型/工具循环，支持顺序或并行工具批次、审批、AskUser、协作式中止，以及 run 级 follow-up / turn 级 steer 排队输入。
- 本地会话、消息、事件、运行记录和压缩记录；支持恢复、搜索、分支、回放、归档、置顶和回收站删除。
- 持久化 LLM 摘要压缩、上下文硬预算和可选的运行软预算。
- Read、Write、Edit、Bash、AskUser 内置工具，以及统一的权限和参数校验。
- 标准 `SKILL.md` 发现、提示词注入、校验、安装、更新、启停和移除。
- Hook、插件注册和 MCP 生命周期边界；完整的插件加载与 MCP 连接能力仍在路线图中。

产品边界与未来方向见 [项目概述](docs/OURO_AGENT_项目概述.md)，代码分层见
[开发架构](docs/OURO_AGENT_开发架构.md)。

## 开发环境

要求 Python 3.12 或更高版本。

```bash
uv sync --all-groups
uv run pytest
uv run ruff check ouro tests
uv run ruff format --check ouro tests
uv run mypy ouro
```

也可以激活虚拟环境后直接运行对应的 `python -m ...` 命令。

## 快速开始

```bash
# 交互模式
uv run ouro --home ./.ouro-dev

# 单次运行
uv run ouro run --print --home ./.ouro-dev "hello"
uv run ouro run --json --home ./.ouro-dev "hello"

# 继续会话
uv run ouro run --json --home ./.ouro-dev --session <session_id> "continue"
```

未指定 `--home` 时，OURO 使用 `OURO_HOME`；两者都未设置时使用 `~/.ouro`。

## Provider 与 Endpoint

Provider 保存供应商级连接默认值和凭据引用，Endpoint 保存具体模型及其能力。两者使用独立命令，
不会在一个写操作中隐式创建另一层对象。

```bash
uv run ouro provider set glm \
  --home ./.ouro-dev \
  --kind openai-compatible \
  --base-url https://example.test/v1 \
  --api-key-env GLM_API_KEY

uv run ouro provider endpoint set glm reasoning \
  --home ./.ouro-dev \
  --model model-name \
  --temperature 0.2 \
  --context-window 200000 \
  --max-output-tokens 16000 \
  --tools --streaming --reasoning \
  --thinking-levels low high \
  --thinking-level high \
  --default

uv run ouro provider use glm --home ./.ouro-dev
uv run ouro provider check glm --endpoint reasoning --home ./.ouro-dev
uv run ouro provider list --home ./.ouro-dev
uv run ouro provider endpoint list glm --home ./.ouro-dev
```

会话中的模型路由始终使用完整的 `provider/endpoint` 形式。交互模式可通过 `/models` 查看模型，
通过 `/model provider/endpoint` 切换当前会话模型。

## 会话与运行控制

```bash
uv run ouro session list --home ./.ouro-dev --search auth --no-archived
uv run ouro session show <session_id> --home ./.ouro-dev
uv run ouro session rename <session_id> "Auth refactor" --home ./.ouro-dev
uv run ouro session branch <session_id> <message_id> --home ./.ouro-dev
uv run ouro session replay <session_id> --home ./.ouro-dev --json
uv run ouro session compact <session_id> --home ./.ouro-dev
uv run ouro session archive <session_id> --home ./.ouro-dev
uv run ouro session remove <session_id> --home ./.ouro-dev --force

uv run ouro approve <session_id> --home ./.ouro-dev
uv run ouro reject <session_id> --home ./.ouro-dev --reason "unsafe"
uv run ouro abort <session_id> --home ./.ouro-dev
uv run ouro run --session <session_id> --queue-mode steer "Change direction"
```

Interactive 中普通输入默认按 follow-up 提交；使用 `/follow <prompt>` 或 `/steer <prompt>` 可显式选择排队
交付模式。follow-up 在当前 Run 结束后进入后续 Run，steer 在当前完整 model turn 结束后进入同一 Run。

## 配置与 Skills

```bash
uv run ouro config prompt --home ./.ouro-dev --set "Custom system prompt"
uv run ouro config permissions --home ./.ouro-dev --tool Bash --mode approval
uv run ouro config tool-execution --home ./.ouro-dev --mode parallel --max-concurrency 4
uv run ouro config queued-inputs --home ./.ouro-dev --consumption-mode all_at_once
uv run ouro config run-budget --home ./.ouro-dev --max-model-rounds 50
uv run ouro config compaction --home ./.ouro-dev --provider glm --endpoint reasoning

uv run ouro skills list --home ./.ouro-dev
uv run ouro skills show pdf --home ./.ouro-dev
uv run ouro skills roots --home ./.ouro-dev
uv run ouro skills validate ./path/to/skill --json
uv run ouro skills install ./path/to/skill --home ./.ouro-dev
uv run ouro skills install github:owner/repo --skill skill-name --ref v1.0.0
uv run ouro skills disable skill-name --home ./.ouro-dev
uv run ouro skills update skill-name --home ./.ouro-dev
uv run ouro skills remove skill-name --home ./.ouro-dev
```

Skills 使用标准 `SKILL.md` YAML frontmatter。用户级原生目录是 `OURO_HOME/skills`，工作区原生目录是
`./.ouro/skills`；也可扫描 `.agents`、`.claude`、`.github`、`.copilot` 和 `.codex` 的 Skills 目录。
格式错误会产生明确诊断，OURO 不会猜测或修复无效 frontmatter。

## 配置示例

`OURO_HOME/config/config.json` 使用分区结构：

```json
{
  "prompts": {
    "system": "Custom system prompt"
  },
  "tool_execution": {
    "mode": "parallel",
    "max_concurrency": 4
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
    "disabled": ["unsafe-community-skill"],
    "scan_agents": true,
    "scan_claude": true
  },
  "providers": {
    "default": "glm",
    "items": {
      "glm": {
        "provider_type": "openai-compatible",
        "provider_id": "glm",
        "credential_ref": "env:GLM_API_KEY",
        "base_url": "https://example.test/v1",
        "api_path": "/chat/completions",
        "default_endpoint": "reasoning",
        "endpoints": {
          "reasoning": {
            "model_id": "model-name",
            "temperature": 0.2,
            "context_window": 200000,
            "max_output_tokens": 16000,
            "supports_tools": true,
            "supports_streaming": true,
            "supports_reasoning": true,
            "supported_thinking_levels": ["low", "high"],
            "default_thinking_level": "high"
          }
        },
        "timeout_seconds": 60,
        "enabled": true
      }
    }
  }
}
```

凭据优先使用 `env:VARIABLE_NAME` 引用。CLI 和公开 JSON 输出不会回显凭据值。

## 项目文档

- [项目概述](docs/OURO_AGENT_项目概述.md)：当前产品范围、核心概念和未来设计。
- [开发架构](docs/OURO_AGENT_开发架构.md)：模块职责、依赖方向和关键不变量。
- [开发路线图](docs/OURO_AGENT_开发路线图.md)：尚未实现能力的推进顺序和完成条件。
- [规格索引](dev/specs/README.md)：当前领域契约、失败语义和验收证据。
- [执行计划索引](dev/plans/README.md)：未来能力的依赖、里程碑和退出条件。
- [中文编码准则](docs/OURO_AGENT_编码准则_ZH.md)：实现与评审规范。
- [English coding guidelines](docs/OURO_AGENT_编码准则_EN.md)：英文版实现与评审规范。
