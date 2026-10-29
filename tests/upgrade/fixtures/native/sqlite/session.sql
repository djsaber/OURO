BEGIN TRANSACTION;
CREATE TABLE compactions (
                        row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        compaction_id TEXT NOT NULL UNIQUE,
                        previous_compaction_id TEXT,
                        covered_end_message_id TEXT NOT NULL,
                        history_boundary_message_id TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
CREATE TABLE delegations (
                        delegation_id TEXT PRIMARY KEY,
                        root_session_id TEXT NOT NULL,
                        parent_session_id TEXT NOT NULL,
                        parent_run_id TEXT NOT NULL,
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
    payload TEXT NOT NULL,
    session_id TEXT GENERATED ALWAYS AS (json_extract(payload, '$.session_id')) VIRTUAL NOT NULL,
    event_id TEXT GENERATED ALWAYS AS (json_extract(payload, '$.event_id')) VIRTUAL NOT NULL UNIQUE,
    run_id TEXT GENERATED ALWAYS AS (json_extract(payload, '$.run_id')) VIRTUAL,
    sequence INTEGER GENERATED ALWAYS AS (json_extract(payload, '$.sequence')) VIRTUAL NOT NULL,
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
CREATE TABLE input_states (
                        session_id TEXT NOT NULL,
                        input_id TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        PRIMARY KEY(session_id, input_id),
                        FOREIGN KEY(session_id, input_id) REFERENCES inputs(session_id, input_id)
                            ON DELETE CASCADE
                    );
INSERT INTO "input_states" VALUES('ses_fixture','input_consumed','{"schema_version":1,"session_id":"ses_fixture","input_id":"input_consumed","history_epoch":0,"history_bound":false,"mode":"follow_up","source":"user","role":"user","scaffold":false,"semantic_tag":null,"coalesce_key":null,"plugin_id":null,"contribution_id":null,"session_control_controller_id":null,"session_control_activation_id":null,"status":"consumed","message_ids":["msg_input"]}');
INSERT INTO "input_states" VALUES('ses_fixture','input_pending','{"schema_version":1,"session_id":"ses_fixture","input_id":"input_pending","history_epoch":0,"history_bound":false,"mode":"follow_up","source":"user","role":"user","scaffold":false,"semantic_tag":null,"coalesce_key":null,"plugin_id":null,"contribution_id":null,"session_control_controller_id":null,"session_control_activation_id":null,"status":"pending","message_ids":[]}');
CREATE TABLE inputs (
                        session_id TEXT NOT NULL,
                        input_id TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        PRIMARY KEY(session_id, input_id),
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
INSERT INTO "inputs" VALUES('ses_fixture','input_consumed','{"schema_version":1,"session_id":"ses_fixture","item":{"history_epoch":0,"history_bound":false,"queue_id":"input_consumed","prompt":"Accepted input input_consumed","skill_activations":[],"mode":"follow_up","driver":null,"title":null,"resource_refs":[],"attachments":[],"queued_at":"2026-09-01T00:00:00Z","source":"user","role":"user","scaffold":false,"semantic_tag":null,"batch_id":null,"batch_index":null,"batch_size":null,"coalesce_key":null,"host_source_id":null,"delegation_id":null,"plugin_id":null,"contribution_id":null,"generation_id":null,"execution_contract_hash":null,"caused_by_run_id":null,"continuation_decision_id":null,"session_control_controller_id":null,"session_control_activation_id":null,"prompt_policy_checked":false,"prospective_run_id":null,"extension_catalog_id":null,"extension_generations":[]}}');
INSERT INTO "inputs" VALUES('ses_fixture','input_pending','{"schema_version":1,"session_id":"ses_fixture","item":{"history_epoch":0,"history_bound":false,"queue_id":"input_pending","prompt":"Accepted input input_pending","skill_activations":[],"mode":"follow_up","driver":null,"title":null,"resource_refs":[],"attachments":[],"queued_at":"2026-09-01T00:00:00Z","source":"user","role":"user","scaffold":false,"semantic_tag":null,"batch_id":null,"batch_index":null,"batch_size":null,"coalesce_key":null,"host_source_id":null,"delegation_id":null,"plugin_id":null,"contribution_id":null,"generation_id":null,"execution_contract_hash":null,"caused_by_run_id":null,"continuation_decision_id":null,"session_control_controller_id":null,"session_control_activation_id":null,"prompt_policy_checked":false,"prospective_run_id":null,"extension_catalog_id":null,"extension_generations":[]}}');
CREATE TABLE latest_contexts (
                        session_id TEXT PRIMARY KEY,
                        context_id TEXT NOT NULL UNIQUE,
                        payload TEXT NOT NULL,
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
INSERT INTO "latest_contexts" VALUES('ses_fixture','ctx_fixture','{"schema_version":10,"context_id":"ctx_fixture","supports_image_input":false,"supports_audio_input":false,"supports_video_input":false,"manifest":{"system_prompt":"","messages":[{"role":"tool_result","content":[{"type":"text","text":"fixture tool output"}],"semantic_tag":null,"tool_calls":[],"tool_result":{"tool_call_id":"tool_fixture","tool_name":"example","is_error":false,"producer":{"kind":"model","driver_id":null}},"provider_continuation":null},{"role":"user","content":[{"type":"text","text":"fixture question"}],"semantic_tag":null,"tool_calls":[],"tool_result":null,"provider_continuation":null},{"role":"assistant","content":[{"type":"text","text":"fixture tool call"}],"semantic_tag":null,"tool_calls":[{"id":"tool_add549a9490642a4ba5d111b35f9ee07","name":"example","arguments":{"schema_version":999,"private":{"message_id":"unchanged","payload":["safe-example",1,null]}},"source_index":0}],"tool_result":null,"provider_continuation":null},{"role":"tool_result","content":[{"type":"text","text":"fixture tool output"}],"semantic_tag":null,"tool_calls":[],"tool_result":{"tool_call_id":"tool_fixture","tool_name":"example","is_error":false,"producer":{"kind":"model","driver_id":null}},"provider_continuation":null}],"tools":[]}}');
CREATE TABLE message_call_references (
                        message_row INTEGER NOT NULL,
                        session_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        call_key TEXT NOT NULL,
                        PRIMARY KEY(message_row, call_key),
                        FOREIGN KEY(message_row) REFERENCES messages(row_id) ON DELETE CASCADE
                    );
INSERT INTO "message_call_references" VALUES(2,'ses_fixture','run_fixture','["run_fixture",null,"tool_add549a9490642a4ba5d111b35f9ee07"]');
INSERT INTO "message_call_references" VALUES(3,'ses_fixture','run_fixture','["run_fixture",null,"tool_fixture"]');
CREATE TABLE messages (
                        row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        message_id TEXT NOT NULL UNIQUE,
                        sequence INTEGER NOT NULL,
                        run_id TEXT,
                        input_origin TEXT,
                        role TEXT NOT NULL,
                        delegation_input_id TEXT,
                        resource_refs TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
INSERT INTO "messages" VALUES(1,'ses_fixture','msg_user',1,NULL,NULL,'user',NULL,'[]','{"schema_version":8,"message_id":"msg_user","before_harness_version":"harness_fixture","session_id":"ses_fixture","role":"user","created_at":"2026-09-01T00:00:00Z","sequence":1,"content":[{"type":"text","text":"fixture question"}],"scaffold":false}');
INSERT INTO "messages" VALUES(2,'ses_fixture','msg_assistant',2,'run_fixture',NULL,'assistant',NULL,'[]','{"schema_version":8,"message_id":"msg_assistant","before_harness_version":"harness_fixture","session_id":"ses_fixture","run_id":"run_fixture","role":"assistant","created_at":"2026-09-01T00:00:00Z","sequence":2,"content":[{"type":"text","text":"fixture tool call"}],"scaffold":false,"call_refs":[{"run_id":"run_fixture","batch_id":"batch_fixture","tool_call_id":"tool_add549a9490642a4ba5d111b35f9ee07"}]}');
INSERT INTO "messages" VALUES(3,'ses_fixture','msg_result',3,'run_fixture',NULL,'tool_result',NULL,'[]','{"schema_version":8,"message_id":"msg_result","before_harness_version":"harness_fixture","session_id":"ses_fixture","run_id":"run_fixture","role":"tool_result","created_at":"2026-09-01T00:00:00Z","sequence":3,"content":[{"type":"text","text":"fixture tool output"}],"scaffold":false,"outcome_ref":{"identity":{"run_id":"run_fixture","batch_id":"msg_result","tool_call_id":"tool_fixture"},"feedback_is_error":false},"metadata":{"schema_version":999,"private":{"message_id":"unchanged","payload":["safe-example",1,null]}}}');
INSERT INTO "messages" VALUES(4,'ses_fixture','msg_input',4,NULL,NULL,'user',NULL,'[]','{"schema_version":8,"message_id":"msg_input","before_harness_version":"harness_fixture","session_id":"ses_fixture","role":"user","created_at":"2026-09-01T00:00:00Z","sequence":4,"scaffold":false,"input_ref":{"input_id":"input_consumed","part":"body","attachments_from_input":true}}');
CREATE TABLE metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
INSERT INTO "metadata" VALUES('sqlite_schema_version','26');
CREATE TABLE request_headers (
                        session_id TEXT NOT NULL,
                        context_id TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        PRIMARY KEY(session_id, context_id),
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
INSERT INTO "request_headers" VALUES('ses_fixture','ctx_fixture','{"schema_version":11,"context_id":"ctx_fixture","session_id":"ses_fixture","run_id":"run_fixture","purpose":"agent","turn_id":"turn_fixture","model_round":1,"provider_id":"fixture","endpoint_id":null,"model_id":"fixture-model","prepared_at":"2026-09-01T00:00:00Z","started_at":"2026-09-01T00:00:00Z","provider_done_at":null,"interrupted_at":null,"response_message_id":null,"response_compaction_id":null,"response_closed_at":null,"usage_entry_id":null,"metrics":{"estimated_input_tokens":20,"estimated_fixed_tokens":1,"budget":null,"estimator_id":"builtin.conservative","estimator_version":"2"},"context_metadata":{}}');
CREATE TABLE resource_references (
    session_id TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY(session_id, resource_id),
    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);
CREATE TABLE response_drafts (
                        session_id TEXT NOT NULL,
                        position INTEGER NOT NULL,
                        channel TEXT NOT NULL,
                        content TEXT NOT NULL,
                        PRIMARY KEY(session_id, position),
                        FOREIGN KEY(session_id) REFERENCES response_streams(session_id)
                            ON DELETE CASCADE
                    );
INSERT INTO "response_drafts" VALUES('ses_fixture',1,'reasoning','Draft reasoning');
INSERT INTO "response_drafts" VALUES('ses_fixture',2,'text','Unfinished answer');
CREATE TABLE response_streams (
                        session_id TEXT PRIMARY KEY,
                        context_id TEXT NOT NULL,
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
INSERT INTO "response_streams" VALUES('ses_fixture','ctx_fixture');
CREATE TABLE run_message_counts (
                        session_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        tool_call_count INTEGER NOT NULL CHECK(tool_call_count >= 0),
                        input_count INTEGER NOT NULL CHECK(input_count >= 0),
                        automatic_input_count INTEGER NOT NULL
                            CHECK(automatic_input_count >= 0
                                  AND automatic_input_count <= input_count),
                        PRIMARY KEY(session_id, run_id),
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
INSERT INTO "run_message_counts" VALUES('ses_fixture','run_fixture',2,0,0);
CREATE TABLE runs (
                        session_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        history_epoch INTEGER NOT NULL,
                        needs_recovery INTEGER NOT NULL,
                        model_round_count INTEGER NOT NULL,
                        summary_request_count INTEGER NOT NULL,
                        payload TEXT NOT NULL,
                        PRIMARY KEY(session_id, run_id),
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
INSERT INTO "runs" VALUES('ses_fixture','run_fixture','2026-09-01 00:00:00+00:00',0,0,0,0,'{"schema_version":10,"history_epoch":0,"run_id":"run_fixture","session_id":"ses_fixture","kind":"agent","origin":"user","parent_run_id":null,"initiator_id":null,"status":"completed","phase":"model_streaming","trigger":"user","started_at":"2026-09-01T00:00:00Z","ended_at":"2026-09-01T00:00:00Z","model":null,"model_endpoint_id":null,"pending_approval_id":null,"active_tool_batch":{"batch_id":"batch_fixture","turn_id":"turn_fixture","assistant_message_id":"msg_assistant","producer":{"kind":"model","driver_id":null},"phase":"planned","execution_mode":"sequential","max_concurrency":4,"call_ids":["tool_add549a9490642a4ba5d111b35f9ee07"],"created_at":"2026-09-01T00:00:00Z","updated_at":"2026-09-01T00:00:00Z","metadata":{}},"abort_requested":false,"error":null,"model_round_count":0,"summary_request_count":0,"current_request_id":null,"continuation":null,"driver":{"driver_id":"ouro.default","contract_hash":"ouro.default/1","implementation":null,"dependencies":[]},"driver_checkpoint":null,"agent_catalog":null,"metadata":{}}');
CREATE TABLE session_catalog (
                        session_id TEXT PRIMARY KEY,
                        payload TEXT NOT NULL,
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
INSERT INTO "session_catalog" VALUES('ses_fixture','{"session_id":"ses_fixture","namespace":"default","session_kind":"primary","root_session_id":"ses_fixture","parent_session_id":null,"title":"New conversation","title_source":"fallback","title_locked":false,"created_at":"2026-09-01T00:00:00Z","updated_at":"2026-09-01T00:00:00Z","last_activity_at":"2026-09-01T00:00:00Z","status":"completed","active_run_id":null,"pending_interaction":null,"current_provider":null,"current_endpoint":null,"current_model":null,"approval_mode":"inherit","interaction_mode":"interactive","workspace":null,"workspace_trusted":false,"archived":false,"pinned":false,"tags":[],"message_count":3,"run_count":1,"resource_count":0,"last_message_preview":"","health":"ok","error_detail":null,"catalog_error":null}');
CREATE TABLE sessions (
                        session_id TEXT PRIMARY KEY,
                        payload TEXT NOT NULL
                    );
INSERT INTO "sessions" VALUES('ses_fixture','{"schema_version":5,"session_id":"ses_fixture","harness_version":"harness_fixture","history_epoch":0,"namespace":"default","root_session_id":"ses_fixture","parent_session_id":null,"session_kind":"primary","delegation_id":null,"forked_from_message_id":null,"forked_from_run_id":null,"title":"New conversation","title_source":"fallback","title_locked":false,"created_at":"2026-09-01T00:00:00Z","updated_at":"2026-10-29T17:51:04.383462Z","last_activity_at":"2026-09-01T00:00:00Z","archived":false,"pinned":false,"workspace":null,"workspace_trusted":false,"current_model":null,"reasoning_request":{"mode":"default","effort":null},"approval_mode":"inherit","interaction_mode":"interactive","controls":{"revision":0,"activations":[]},"context_selection":{"compaction_id":null,"media_elided_through":null},"status":"idle","active_run_id":null,"continuation_request":null,"last_message_id":"msg_input","last_commit_id":"op_e0b519844d654c86b6ee09a63f0ed768","last_run_id":"run_fixture","message_count":4,"run_count":1,"resource_count":0,"pending_input_ids":["input_pending"],"tags":[],"metadata":{"schema_version":999,"private":{"message_id":"unchanged","payload":["safe-example",1,null]}}}');
CREATE TABLE tool_call_starts (
                        session_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        batch_id TEXT NOT NULL,
                        tool_call_id TEXT NOT NULL,
                        attempt INTEGER NOT NULL,
                        payload TEXT NOT NULL,
                        PRIMARY KEY(session_id, run_id, batch_id, tool_call_id, attempt),
                        FOREIGN KEY(session_id, run_id, batch_id, tool_call_id)
                            REFERENCES tool_calls(session_id, run_id, batch_id, tool_call_id)
                            ON DELETE CASCADE
                    );
CREATE TABLE tool_call_states (
                        session_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        batch_id TEXT NOT NULL,
                        tool_call_id TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        PRIMARY KEY(session_id, run_id, batch_id, tool_call_id),
                        FOREIGN KEY(session_id, run_id, batch_id, tool_call_id)
                            REFERENCES tool_calls(session_id, run_id, batch_id, tool_call_id)
                            ON DELETE CASCADE
                    );
INSERT INTO "tool_call_states" VALUES('ses_fixture','run_fixture','batch_fixture','tool_add549a9490642a4ba5d111b35f9ee07','{"schema_version":1,"session_id":"ses_fixture","identity":{"run_id":"run_fixture","batch_id":"batch_fixture","tool_call_id":"tool_add549a9490642a4ba5d111b35f9ee07"},"status":"completed","attempt":0,"prepared_binding":{"producer":{"kind":"model","driver_id":null},"tool_call_id":"tool_add549a9490642a4ba5d111b35f9ee07","session_id":"ses_fixture","run_id":"run_fixture","turn_id":"turn_fixture","tool_name":"example","canonical_tool_id":null,"owner_id":null,"source":"builtin","effect":"unknown","batch_behavior":"compatible","requested_at":"2026-09-01T00:00:00Z","permission_mode":"auto","permission_control":"host","resumable":null,"definition_contract_hash":null,"approval_id":null,"plugin_id":null,"plugin_version":null,"plugin_class":null,"release_id":null,"package_digest":null,"builtin_contract_hash":null,"generation_id":null,"contract_hash":null,"execution_contract_hash":null,"active_when_controller_id":null,"interaction_controller_id":null,"interaction_activation_id":null,"tool_policy_ids":[],"tool_policy_reason":null},"arguments_override":null,"approval":null,"input_request":null,"input_resolution":null,"input_revision":0,"interaction_activation_id":null,"agent_wait":null,"outcome_attempt":null,"proposals":null,"feedback":null,"result_message_id":null,"metadata":{}}');
CREATE TABLE tool_calls (
                        session_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        batch_id TEXT NOT NULL,
                        tool_call_id TEXT NOT NULL,
                        projection TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        PRIMARY KEY(session_id, run_id, batch_id, tool_call_id),
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
INSERT INTO "tool_calls" VALUES('ses_fixture','run_fixture','batch_fixture','tool_add549a9490642a4ba5d111b35f9ee07','{"schema_version":1,"session_id":"ses_fixture","identity":{"run_id":"run_fixture","batch_id":"batch_fixture","tool_call_id":"tool_add549a9490642a4ba5d111b35f9ee07"},"origin_session_id":"ses_fixture","history_epoch":0,"turn_id":"turn_fixture","producer":{"kind":"model","driver_id":null},"request":{"id":"tool_add549a9490642a4ba5d111b35f9ee07","name":"example","source_index":0},"accepted_at":"2026-09-01T00:00:00Z"}','{"schema_version":1,"session_id":"ses_fixture","identity":{"run_id":"run_fixture","batch_id":"batch_fixture","tool_call_id":"tool_add549a9490642a4ba5d111b35f9ee07"},"origin_session_id":"ses_fixture","history_epoch":0,"turn_id":"turn_fixture","producer":{"kind":"model","driver_id":null},"request":{"id":"tool_add549a9490642a4ba5d111b35f9ee07","name":"example","arguments":{"schema_version":999,"private":{"message_id":"unchanged","payload":["safe-example",1,null]}},"source_index":0},"accepted_at":"2026-09-01T00:00:00Z"}');
INSERT INTO "tool_calls" VALUES('ses_fixture','run_fixture','msg_result','tool_fixture','{"schema_version":1,"session_id":"ses_fixture","identity":{"run_id":"run_fixture","batch_id":"msg_result","tool_call_id":"tool_fixture"},"origin_session_id":"ses_fixture","history_epoch":0,"turn_id":"msg_result","producer":{"kind":"model","driver_id":null},"request":{"id":"tool_fixture","name":"example","source_index":0},"accepted_at":"2026-09-01T00:00:00Z"}','{"schema_version":1,"session_id":"ses_fixture","identity":{"run_id":"run_fixture","batch_id":"msg_result","tool_call_id":"tool_fixture"},"origin_session_id":"ses_fixture","history_epoch":0,"turn_id":"msg_result","producer":{"kind":"model","driver_id":null},"request":{"id":"tool_fixture","name":"example","arguments":{},"source_index":0},"accepted_at":"2026-09-01T00:00:00Z"}');
CREATE TABLE tool_outcomes (
                        session_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        batch_id TEXT NOT NULL,
                        tool_call_id TEXT NOT NULL,
                        attempt INTEGER NOT NULL,
                        payload TEXT NOT NULL,
                        structured TEXT NOT NULL,
                        PRIMARY KEY(session_id, run_id, batch_id, tool_call_id, attempt),
                        FOREIGN KEY(session_id, run_id, batch_id, tool_call_id)
                            REFERENCES tool_calls(session_id, run_id, batch_id, tool_call_id)
                            ON DELETE CASCADE
                    );
CREATE TABLE trashed_sessions (
                        trash_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        trashed_at TEXT NOT NULL,
                        payload TEXT NOT NULL
                    );
CREATE INDEX message_call_identity
                        ON message_call_references(session_id, call_key);
CREATE INDEX message_calls_suffix
                        ON message_call_references(session_id, message_row);
CREATE INDEX messages_run_result_references
                        ON messages(session_id, run_id, row_id, message_id, role,
                                    delegation_input_id, resource_refs);
CREATE INDEX messages_session_order
                        ON messages(session_id, row_id);
CREATE INDEX messages_session_run
                        ON messages(session_id, run_id, row_id);
CREATE INDEX messages_session_queued_input
                        ON messages(
                            session_id, json_extract(payload, '$.input_ref.input_id'));
CREATE INDEX runs_request_counts
                        ON runs(session_id, run_id, model_round_count, summary_request_count);
CREATE INDEX runs_pending
                        ON runs(session_id, history_epoch, started_at, run_id)
                        WHERE needs_recovery = 1;
CREATE INDEX compaction_references
                        ON compactions(session_id, row_id, compaction_id,
                            previous_compaction_id, covered_end_message_id,
                            history_boundary_message_id);
CREATE INDEX delegations_root_parent
                        ON delegations(root_session_id, parent_session_id, created_at);
CREATE INDEX delegations_root_run
                        ON delegations(root_session_id, parent_run_id, created_at, delegation_id);
CREATE UNIQUE INDEX delegations_child_session
                        ON delegations(child_session_id) WHERE child_session_id IS NOT NULL;
CREATE INDEX delegations_spawn_state
                        ON delegations(spawn_state);
CREATE INDEX delegations_delivery_status
                        ON delegations(delivery_status);
CREATE INDEX messages_assistant_turn
                        ON messages(session_id, run_id,
                            json_extract(payload, '$.turn_id'), row_id)
                        WHERE json_extract(payload, '$.role') = 'assistant';
CREATE INDEX messages_context_delivery
                        ON messages(session_id, run_id,
                            json_extract(payload, '$.metadata.context_provider_delivery_key'),
                            row_id)
                        WHERE json_extract(payload, '$.metadata.context_provider_delivery_key')
                            IS NOT NULL;
CREATE INDEX messages_assistant_run
                        ON messages(session_id, run_id, row_id)
                        WHERE json_extract(payload, '$.role') = 'assistant';
CREATE INDEX messages_input_history
                        ON messages(session_id, row_id)
                        WHERE json_extract(payload, '$.role') IN ('user', 'system');
CREATE INDEX events_session_order ON events(session_id, row_id);
CREATE INDEX events_session_run_order ON events(session_id, run_id, row_id);
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('messages',4);
COMMIT;
