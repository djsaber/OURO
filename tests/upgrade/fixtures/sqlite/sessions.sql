BEGIN TRANSACTION;
CREATE TABLE compactions (
                        row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        compaction_id TEXT NOT NULL UNIQUE,
                        payload TEXT NOT NULL,
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
CREATE TABLE delegations (
                        delegation_id TEXT PRIMARY KEY,
                        root_session_id TEXT NOT NULL,
                        parent_session_id TEXT NOT NULL,
                        child_session_id TEXT,
                        idempotency_key TEXT NOT NULL,
                        spawn_state TEXT NOT NULL,
                        delivery_status TEXT,
                        revision INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        UNIQUE(root_session_id, idempotency_key),
                        FOREIGN KEY(root_session_id)
                            REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
CREATE TABLE events (
                        row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        event_id TEXT NOT NULL UNIQUE,
                        run_id TEXT,
                        sequence INTEGER NOT NULL,
                        payload TEXT NOT NULL,
                        UNIQUE(session_id, sequence),
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
CREATE TABLE harness_versions (
                        session_id TEXT NOT NULL,
                        version_id TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        PRIMARY KEY(session_id, version_id),
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
INSERT INTO "harness_versions" VALUES('ses_fixture','harness_fixture','{"schema_version":1,"session_id":"ses_fixture","version_id":"harness_fixture","state":{"controls":{"revision":0,"activations":[]},"states":[]}}');
CREATE TABLE latest_contexts (
                        session_id TEXT PRIMARY KEY,
                        context_id TEXT NOT NULL UNIQUE,
                        payload TEXT NOT NULL,
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
INSERT INTO "latest_contexts" VALUES('ses_fixture','ctx_fixture','{"schema_version":4,"context_id":"ctx_fixture","session_id":"ses_fixture","run_id":"run_fixture","turn_id":"turn_fixture","model_round":1,"provider_id":"fixture","endpoint_id":null,"model_id":"fixture-model","supports_image_input":false,"supports_audio_input":false,"supports_video_input":false,"prepared_at":"2026-09-01T00:00:00Z","completed_at":null,"system_prompt":"","injected_messages":[{"role":"tool_result","content":[{"type":"text","text":"fixture tool output"}],"semantic_tag":null,"tool_calls":[],"tool_result":{"tool_call_id":"tool_fixture","tool_name":"example","is_error":false},"provider_continuation":null}],"source_messages":[{"schema_version":3,"message_id":"msg_user","before_harness_version":"harness_fixture","session_id":"ses_fixture","run_id":null,"turn_id":null,"role":"user","created_at":"2026-09-01T00:00:00Z","sequence":1,"content":[{"type":"text","text":"fixture question"}],"scaffold":false,"semantic_tag":null,"resource_refs":[],"input_attachments":[],"tool_calls":[],"tool_result":null,"reasoning_content":"","model_response":null,"tool_execution":null,"provider_continuation":null,"metadata":{}},{"schema_version":3,"message_id":"msg_assistant","before_harness_version":"harness_fixture","session_id":"ses_fixture","run_id":"run_fixture","turn_id":null,"role":"assistant","created_at":"2026-09-01T00:00:00Z","sequence":2,"content":[{"type":"text","text":"fixture tool call"}],"scaffold":false,"semantic_tag":null,"resource_refs":[],"input_attachments":[],"tool_calls":[{"id":"tool_add549a9490642a4ba5d111b35f9ee07","name":"example","arguments":{"schema_version":999,"private":{"message_id":"unchanged","payload":["safe-example",1,null]}},"source_index":0}],"tool_result":null,"reasoning_content":"","model_response":null,"tool_execution":null,"provider_continuation":null,"metadata":{}},{"schema_version":3,"message_id":"msg_result","before_harness_version":"harness_fixture","session_id":"ses_fixture","run_id":"run_fixture","turn_id":null,"role":"tool_result","created_at":"2026-09-01T00:00:00Z","sequence":3,"content":[{"type":"text","text":"fixture tool output"}],"scaffold":false,"semantic_tag":null,"resource_refs":[],"input_attachments":[],"tool_calls":[],"tool_result":{"tool_call_id":"tool_fixture","tool_name":"example","is_error":false},"reasoning_content":"","model_response":null,"tool_execution":null,"provider_continuation":null,"metadata":{"schema_version":999,"private":{"message_id":"unchanged","payload":["safe-example",1,null]}}}],"tool_definitions":[],"metrics":{"estimated_input_tokens":20,"estimated_fixed_tokens":1,"context_window_tokens":null,"reserved_output_tokens":0,"input_budget_tokens":null,"estimated_remaining_tokens":null,"estimated_utilization_ratio":null,"over_budget":false,"estimator_id":"builtin.conservative","estimator_version":"2"},"context_metadata":{},"context_digest":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","provider_reported_input_tokens":null}');
CREATE TABLE messages (
                        row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        message_id TEXT NOT NULL UNIQUE,
                        sequence INTEGER NOT NULL,
                        payload TEXT NOT NULL,
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
INSERT INTO "messages" VALUES(1,'ses_fixture','msg_user',1,'{"schema_version":3,"message_id":"msg_user","before_harness_version":"harness_fixture","session_id":"ses_fixture","run_id":null,"turn_id":null,"role":"user","created_at":"2026-09-01T00:00:00Z","sequence":1,"content":[{"type":"text","text":"fixture question"}],"scaffold":false,"semantic_tag":null,"resource_refs":[],"input_attachments":[],"tool_calls":[],"tool_result":null,"reasoning_content":"","model_response":null,"tool_execution":null,"provider_continuation":null,"metadata":{}}');
INSERT INTO "messages" VALUES(2,'ses_fixture','msg_assistant',2,'{"schema_version":3,"message_id":"msg_assistant","before_harness_version":"harness_fixture","session_id":"ses_fixture","run_id":"run_fixture","turn_id":null,"role":"assistant","created_at":"2026-09-01T00:00:00Z","sequence":2,"content":[{"type":"text","text":"fixture tool call"}],"scaffold":false,"semantic_tag":null,"resource_refs":[],"input_attachments":[],"tool_calls":[{"id":"tool_add549a9490642a4ba5d111b35f9ee07","name":"example","arguments":{"schema_version":999,"private":{"message_id":"unchanged","payload":["safe-example",1,null]}},"source_index":0}],"tool_result":null,"reasoning_content":"","model_response":null,"tool_execution":null,"provider_continuation":null,"metadata":{}}');
INSERT INTO "messages" VALUES(3,'ses_fixture','msg_result',3,'{"schema_version":3,"message_id":"msg_result","before_harness_version":"harness_fixture","session_id":"ses_fixture","run_id":"run_fixture","turn_id":null,"role":"tool_result","created_at":"2026-09-01T00:00:00Z","sequence":3,"content":[{"type":"text","text":"fixture tool output"}],"scaffold":false,"semantic_tag":null,"resource_refs":[],"input_attachments":[],"tool_calls":[],"tool_result":{"tool_call_id":"tool_fixture","tool_name":"example","is_error":false},"reasoning_content":"","model_response":null,"tool_execution":null,"provider_continuation":null,"metadata":{"schema_version":999,"private":{"message_id":"unchanged","payload":["safe-example",1,null]}}}');
CREATE TABLE metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
INSERT INTO "metadata" VALUES('sqlite_schema_version','7');
CREATE TABLE resource_references (
                        session_id TEXT NOT NULL,
                        resource_id TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        PRIMARY KEY(session_id, resource_id),
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
CREATE TABLE runs (
                        session_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        PRIMARY KEY(session_id, run_id),
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
INSERT INTO "runs" VALUES('ses_fixture','run_fixture','2026-09-01 00:00:00+00:00','{"schema_version":2,"history_epoch":0,"run_id":"run_fixture","session_id":"ses_fixture","kind":"agent","origin":"user","parent_run_id":null,"initiator_id":null,"status":"completed","phase":"model_streaming","trigger":"user","started_at":"2026-09-01T00:00:00Z","ended_at":"2026-09-01T00:00:00Z","model":null,"model_endpoint_id":null,"pending_approval_id":null,"active_tool_batch":{"batch_id":"batch_fixture","turn_id":"turn_fixture","assistant_message_id":"msg_assistant","phase":"planned","execution_mode":"sequential","max_concurrency":4,"calls":[{"request":{"id":"tool_add549a9490642a4ba5d111b35f9ee07","name":"example","arguments":{"schema_version":999,"private":{"message_id":"unchanged","payload":["safe-example",1,null]}},"source_index":0},"status":"completed","invocation":{"tool_call_id":"tool_fixture","session_id":"ses_fixture","run_id":"run_fixture","turn_id":null,"tool_name":"example","canonical_tool_id":null,"owner_id":null,"source":"builtin","effect":"unknown","batch_behavior":"compatible","arguments":{"schema_version":999,"private":{"message_id":"unchanged","payload":["safe-example",1,null]}},"requested_at":"2026-09-01T00:00:00Z","permission_mode":"auto","permission_control":"host","resumable":null,"definition_contract_hash":null,"approval_id":null,"plugin_id":null,"plugin_version":null,"plugin_class":null,"release_id":null,"package_digest":null,"builtin_contract_hash":null,"generation_id":null,"contract_hash":null,"execution_contract_hash":null,"active_when_controller_id":null,"interaction_controller_id":null,"interaction_activation_id":null,"tool_policy_ids":[],"tool_policy_reason":null},"approval":null,"input_request":null,"input_resolution":null,"input_revision":0,"agent_wait":null,"result":null,"result_message_id":null,"metadata":{}}],"created_at":"2026-09-01T00:00:00Z","updated_at":"2026-09-01T00:00:00Z","metadata":{}},"abort_requested":false,"error":null,"usage":{},"usage_entry_ids":[],"model_round_count":0,"continuation":null,"agent_catalog":null,"metadata":{}}');
CREATE TABLE session_catalog (
                        session_id TEXT PRIMARY KEY,
                        payload TEXT NOT NULL,
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
CREATE TABLE session_commits (
                        session_id TEXT NOT NULL,
                        operation_id TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        PRIMARY KEY(session_id, operation_id),
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
INSERT INTO "session_commits" VALUES('ses_fixture','op_fixture','{"schema_version":1,"operation_id":"op_fixture","request_digest":"a350d2a8a1edd846f0cc419e5eac147d86a5c3fdf361e68dbd25b952dc6c42dc","session":{"schema_version":2,"session_id":"ses_fixture","harness_version":"harness_fixture","history_epoch":0,"namespace":"default","root_session_id":"ses_fixture","parent_session_id":null,"session_kind":"primary","delegation_id":null,"forked_from_message_id":null,"forked_from_run_id":null,"title":"New conversation","title_source":"fallback","title_locked":false,"created_at":"2026-09-01T00:00:00Z","updated_at":"2026-09-01T00:00:00Z","last_activity_at":"2026-09-01T00:00:00Z","archived":false,"pinned":false,"workspace":null,"workspace_trusted":false,"current_model":null,"reasoning_request":{"mode":"default","effort":null},"approval_mode":"inherit","interaction_mode":"interactive","controls":{"revision":0,"activations":[]},"active_compaction_id":null,"status":"idle","active_run_id":null,"last_message_id":"msg_result","last_run_id":"run_fixture","message_count":3,"tags":[],"metadata":{"schema_version":999,"private":{"message_id":"unchanged","payload":["safe-example",1,null]}}},"messages":[{"schema_version":3,"message_id":"msg_user","before_harness_version":"harness_fixture","session_id":"ses_fixture","run_id":null,"turn_id":null,"role":"user","created_at":"2026-09-01T00:00:00Z","sequence":1,"content":[{"type":"text","text":"fixture question"}],"scaffold":false,"semantic_tag":null,"resource_refs":[],"input_attachments":[],"tool_calls":[],"tool_result":null,"reasoning_content":"","model_response":null,"tool_execution":null,"provider_continuation":null,"metadata":{}},{"schema_version":3,"message_id":"msg_assistant","before_harness_version":"harness_fixture","session_id":"ses_fixture","run_id":"run_fixture","turn_id":null,"role":"assistant","created_at":"2026-09-01T00:00:00Z","sequence":2,"content":[{"type":"text","text":"fixture tool call"}],"scaffold":false,"semantic_tag":null,"resource_refs":[],"input_attachments":[],"tool_calls":[{"id":"tool_add549a9490642a4ba5d111b35f9ee07","name":"example","arguments":{"schema_version":999,"private":{"message_id":"unchanged","payload":["safe-example",1,null]}},"source_index":0}],"tool_result":null,"reasoning_content":"","model_response":null,"tool_execution":null,"provider_continuation":null,"metadata":{}},{"schema_version":3,"message_id":"msg_result","before_harness_version":"harness_fixture","session_id":"ses_fixture","run_id":"run_fixture","turn_id":null,"role":"tool_result","created_at":"2026-09-01T00:00:00Z","sequence":3,"content":[{"type":"text","text":"fixture tool output"}],"scaffold":false,"semantic_tag":null,"resource_refs":[],"input_attachments":[],"tool_calls":[],"tool_result":{"tool_call_id":"tool_fixture","tool_name":"example","is_error":false},"reasoning_content":"","model_response":null,"tool_execution":null,"provider_continuation":null,"metadata":{"schema_version":999,"private":{"message_id":"unchanged","payload":["safe-example",1,null]}}}],"events":[],"output":{"schema_version":999,"private":{"message_id":"unchanged","payload":["safe-example",1,null]}},"truncation":null}');
CREATE TABLE sessions (
                        session_id TEXT PRIMARY KEY,
                        payload TEXT NOT NULL
                    );
INSERT INTO "sessions" VALUES('ses_fixture','{"schema_version":2,"session_id":"ses_fixture","harness_version":"harness_fixture","history_epoch":0,"namespace":"default","root_session_id":"ses_fixture","parent_session_id":null,"session_kind":"primary","delegation_id":null,"forked_from_message_id":null,"forked_from_run_id":null,"title":"New conversation","title_source":"fallback","title_locked":false,"created_at":"2026-09-01T00:00:00Z","updated_at":"2026-09-01T00:00:00Z","last_activity_at":"2026-09-01T00:00:00Z","archived":false,"pinned":false,"workspace":null,"workspace_trusted":false,"current_model":null,"reasoning_request":{"mode":"default","effort":null},"approval_mode":"inherit","interaction_mode":"interactive","active_compaction_id":null,"status":"idle","active_run_id":null,"last_message_id":"msg_result","last_run_id":"run_fixture","message_count":3,"tags":[],"metadata":{"schema_version":999,"private":{"message_id":"unchanged","payload":["safe-example",1,null]}}}');
CREATE TABLE trashed_sessions (
                        trash_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        trashed_at TEXT NOT NULL,
                        payload TEXT NOT NULL
                    );
CREATE INDEX messages_session_order
                        ON messages(session_id, row_id);
CREATE INDEX messages_session_run
                        ON messages(session_id, json_extract(payload, '$.run_id'), row_id);
CREATE INDEX messages_session_queued_input
                        ON messages(
                            session_id, json_extract(payload, '$.metadata.queued_input_id'));
CREATE INDEX events_session_order
                        ON events(session_id, row_id);
CREATE INDEX events_session_run_order
                        ON events(session_id, run_id, row_id);
CREATE INDEX resource_reference_identity
                        ON resource_references(resource_id);
CREATE INDEX delegations_root_parent
                        ON delegations(root_session_id, parent_session_id, created_at);
CREATE UNIQUE INDEX delegations_child_session
                        ON delegations(child_session_id) WHERE child_session_id IS NOT NULL;
CREATE INDEX delegations_spawn_state
                        ON delegations(spawn_state);
CREATE INDEX delegations_delivery_status
                        ON delegations(delivery_status);
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('messages',3);
COMMIT;
PRAGMA application_id=0;
PRAGMA user_version=0;
