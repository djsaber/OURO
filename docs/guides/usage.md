# 查询 Token 与费用

> 文档类型：使用指南

全局账本按每次完成的供应商模型请求记录 usage，覆盖普通 Agent 回合和上下文压缩请求。这里的 token
只来自 Provider 响应；`ContextBuilder` 对上下文长度的本地估算仅用于预算和触发压缩，绝不会作为账本
缺失值的回退来源。`estimated_cost` 的框架契约固定为 USD；只有 adapter 能保证该语义时才可自动填入，
否则保持未定价状态，可按账单手动修订。

计费模式随请求记入账本，不从登录方式或当前 Provider 配置推断。CLI/TUI 显示未知金额请求数，混合汇总的金额
标记为部分金额；订阅调用没有单次金额时保持未知，不记为零。可使用 `usage summary --group-by billing_mode`
按 `usage_based`、`subscription`、`unknown` 汇总，具体行为见[用量与计费规格](../specs/usage-and-billing.md)。

```bash
# 全局汇总；裸日期按 UTC 自然日处理，--until 裸日期包含当天
uv run --frozen ouro usage summary --home ./.ouro-dev
uv run --frozen ouro usage summary --home ./.ouro-dev \
  --since 2026-07-01 --until 2026-07-15 \
  --provider <provider_id> --model claude-sonnet-5-5 --group-by date --json

# 独立插件模型请求按实际插件身份记账
uv run --frozen ouro usage summary --home ./.ouro-dev --purpose plugin --group-by plugin

# 请求级明细及更多过滤维度
uv run --frozen ouro usage list --home ./.ouro-dev --purpose compaction --limit 100
uv run --frozen ouro usage show <usage_entry_id> --home ./.ouro-dev --json

# 修改和重置都是追加式审计操作，不会悄悄覆写或删除历史事件
uv run --frozen ouro usage modify <usage_entry_id> --home ./.ouro-dev \
  --input-tokens 1234 --estimated-cost 0.42 --note "provider invoice correction"
uv run --frozen ouro usage reset --home ./.ouro-dev --model old-model \
  --note "start a new accounting period" --force
uv run --frozen ouro usage audit --home ./.ouro-dev --limit 100 --json
```

`summary`、`list` 和 `reset` 共享 `--since`、`--until`、`--provider`、`--endpoint`、`--model`、
`--session`、`--run`、`--plugin`、`--purpose` 过滤器，重复同一参数表示匹配其中任意值。Provider adapter 会把
`input_tokens` 统一为包含缓存读写的总输入，cache read/write token 保留为其中的独立明细；
`total_tokens` 只计算 input + output，避免重复计入缓存明细。
全局账本不从 `run.usage` 猜测或回填缺少请求路由与请求边界的记录；统计从账本实际记录的供应商调用开始。
人工修改或重置只改变可审计的全局费用视图，不改写
Session 中保存的原始 Run usage。
