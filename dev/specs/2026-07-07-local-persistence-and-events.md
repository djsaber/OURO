# OURO Agent 本地持久化与事件流规格

- Date: 2026-07-07
- Status: Accepted / Phase 1 implemented
- Related: ../../docs/OURO_AGENT_项目概述_V0707.md

## 1. Motivation

OURO 默认是本地优先的个人 Agent。用户应该能够理解、迁移和备份自己的会话数据。

第一阶段默认持久化使用本地可阅读文件：JSON、YAML、JSONL。SQLite 可以作为未来同等职责后端，但不作为第一阶段默认依赖。

## 2. Goals

- 定义默认数据目录结构。
- 定义消息、事件、会话状态和运行状态的文件职责。
- 支持完整事件流默认长期保存。
- 支持崩溃后恢复到可解释状态。
- 支持按会话获取历史快照、运行状态和事件流。
- 支持会话列表展示所需的基础索引。
- 支持资源引用和生成文件归属记录。
- 支持基础数据维护：导出、归档、清理、迁移提示。
- 定义持久化协议边界，使核心运行层不直接依赖具体文件后端。

## 3. Non-goals

- 不实现云同步。
- 不实现团队共享数据仓库。
- 不实现企业级审计或脱敏策略。
- 不要求第一阶段实现 SQLite。
- 不要求第一阶段实现全文搜索引擎。
- 不要求自动后台清理数据。

## 4. Storage Roots

建议数据根分为三类：

1. 用户数据根

   保存全局配置、凭据引用、插件启用状态、会话数据。

   可通过环境变量 `OURO_HOME` 覆盖。默认路径由实现根据系统选择，但必须可被用户定位和打开。

2. 工作区配置

   保存当前项目相关的 OURO 配置，例如项目提示、技能启用、工作区信任状态。

   建议路径：工作区内 `.ouro/`。

3. 临时运行目录

   保存短期缓存、临时下载、未归档中间文件。

   临时目录可以清理，但必须避免误删持久化会话数据。

## 5. Default Layout

建议用户数据根结构：

```
ouro-home/
  config/
    user.yaml
    models.yaml
    credentials.yaml
    plugins.yaml
    mcp.yaml
    skills.yaml
  sessions/
    index.json
    <session_id>/
      session.json
      messages.jsonl
      events.jsonl
      runs/
        <run_id>.json
      resources/
        index.json
  logs/
    ouro.log.jsonl
  migrations/
    state.json
```

说明：

- `messages.jsonl` 只追加保存会话消息。
- `events.jsonl` 只追加保存运行事件。
- `session.json` 保存会话元数据和当前会话状态。
- `runs/<run_id>.json` 保存运行最终状态或可恢复状态。
- `resources/index.json` 保存附件、生成文件、外部资源引用。
- `sessions/index.json` 保存会话列表展示和基础搜索所需元数据。
- 配置文件默认使用 YAML 或 JSON，具体格式后续实现决定，但必须可阅读。

## 6. Write Semantics

JSONL 文件：

- 采用追加写入。
- 每行一个完整 JSON 对象。
- 每个对象必须包含 schema_version。
- 写入后尽量 flush，降低崩溃损失。

JSON/YAML 状态文件：

- 采用临时文件写入后原子替换。
- 不应在写入中途留下半截 JSON/YAML。
- 文件损坏时应保留原文件，并生成可读错误。

索引文件：

- 可以从 session.json 和 messages.jsonl 重建。
- 若索引损坏，不应导致会话不可打开。

## 7. Data Records

### SessionRecord

最小字段：

- schema_version。
- session_id。
- title。
- created_at。
- updated_at。
- archived。
- workspace。
- current_model。
- status。
- last_message_id。
- last_run_id。
- message_count。
- tags。
- metadata。

### MessageRecord

最小字段：

- schema_version。
- message_id。
- session_id。
- run_id。
- turn_id。
- role。
- created_at。
- sequence。
- content。
- synthetic。
- resource_refs。
- metadata。

role 可选值：

- user。
- assistant。
- tool_result。
- system_synthetic。

### EventRecord

最小字段：

- schema_version。
- event_id。
- session_id。
- run_id。
- turn_id。
- event_type。
- created_at。
- sequence。
- payload。

事件流默认保存完整 payload。

### RunRecord

最小字段：

- schema_version。
- run_id。
- session_id。
- status。
- trigger。
- started_at。
- ended_at。
- model。
- pending_approval_id。
- abort_requested。
- error。
- usage。
- metadata。

### ResourceRecord

最小字段：

- schema_version。
- resource_id。
- session_id。
- run_id。
- source。
- kind。
- uri_or_path。
- display_name。
- media_type。
- size。
- created_at。
- related_message_id。
- related_tool_call_id。
- metadata。

资源记录不要求第一阶段复制文件。若只保存路径引用，必须明确这是引用而不是快照。

## 8. Event Types

第一阶段建议事件类型：

- session_loaded。
- run_started。
- run_status_changed。
- user_message_added。
- model_request_started。
- model_text_delta。
- model_tool_call_delta。
- model_message_completed。
- tool_call_requested。
- approval_requested。
- approval_resolved。
- tool_call_started。
- tool_call_completed。
- ask_user_requested。
- ask_user_resolved。
- run_retry_scheduled。
- run_completed。
- run_aborted。
- run_failed。
- persistence_warning。

事件命名可以在实现中调整，但必须覆盖这些语义。

## 9. Recovery

打开会话时：

1. 读取 session.json。
2. 读取 messages.jsonl，构建用户可见历史。
3. 读取最后一个 run record。
4. 若 run 状态为 waiting_approval 或 waiting_user，恢复待处理状态。
5. 若 run 状态为 running 但进程已退出，标记为 recoverable 或 failed。
6. 读取 resources/index.json，恢复资源引用。
7. 修复或重建 sessions/index.json。

恢复不要求自动继续模型调用。第一阶段只要求恢复到可解释状态，并允许用户继续或重新运行。

## 10. Multi-session Support

持久化层必须支持：

- 列出所有会话。
- 按 session_id 打开会话快照。
- 按 session_id 获取当前运行状态。
- 按 session_id 和 run_id 读取事件。
- 在不同会话之间隔离消息、事件和运行状态。

未来 Desktop 需要多个会话后台生成，因此持久化接口不能假设“当前只有一个会话存在”。

## 11. Data Maintenance

第一阶段最低能力：

- 查看数据根目录。
- 导出单个会话为可阅读目录或压缩包。
- 归档会话。
- 删除会话前二次确认。
- 手动清理指定会话的历史事件。
- 检测 schema_version 不兼容并给出迁移提示。

不做后台静默清理。用户没有明确请求时，不删除完整事件流。

## 12. Acceptance

完成本规格后，应满足：

- 创建会话后生成 session.json、messages.jsonl、events.jsonl。
- 用户消息、助手消息、工具结果都写入 messages.jsonl。
- 每次运行产生的事件写入 events.jsonl。
- 崩溃后重新打开会话能看到历史消息。
- 未完成运行能被标记为 recoverable 或 failed。
- 会话列表能展示标题、更新时间、状态、工作区。
- 资源引用能关联到消息、运行或工具调用。
- 索引损坏时可以重建，不导致会话丢失。
- Runtime 依赖持久化协议，默认实现为本地可阅读文件。

## 13. Resolved Decisions

- 默认用户数据根使用 `OURO_HOME` 覆盖；未设置时使用 `~/.ouro`。
- 第一阶段会话标题使用用户首条消息截断生成，后续可加入标题生成能力。
- 第一阶段不为 JSONL 引入校验和或每行 hash；索引可重建，损坏定位增强后置。
