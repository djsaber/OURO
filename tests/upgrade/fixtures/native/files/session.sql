PRAGMA auto_vacuum=1;
BEGIN TRANSACTION;
CREATE TABLE call_memberships (
    call_key TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (call_key, position)
) WITHOUT ROWID;
INSERT INTO "call_memberships" VALUES('["run_fixture",null,"tool_be80d1e31bad460ead6c96dd4705b1e6"]',2);
INSERT INTO "call_memberships" VALUES('["run_fixture",null,"tool_fixture"]',3);
CREATE TABLE compactions (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    compaction_id TEXT NOT NULL UNIQUE,
    previous_compaction_id TEXT,
    covered_end_message_id TEXT NOT NULL,
    history_boundary_message_id TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE counts (run_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
INSERT INTO "counts" VALUES('run_fixture','{"run_id":"run_fixture","tool_call_count":2,"input_count":0,"automatic_input_count":0}');
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
CREATE TABLE latest_contexts (
    session_id TEXT PRIMARY KEY,
    context_id TEXT NOT NULL
);
INSERT INTO "latest_contexts" VALUES('ses_fixture','ctx_fixture');
CREATE TABLE locations (
    position INTEGER PRIMARY KEY,
    message_id TEXT NOT NULL UNIQUE,
    offset INTEGER NOT NULL,
    role TEXT NOT NULL,
    run_id TEXT,
    queued_input_id TEXT,
    assistant_turn_id TEXT,
    context_delivery_key TEXT,
    input_origin TEXT,
    delegation_input_id TEXT,
    resource_refs TEXT NOT NULL
);
INSERT INTO "locations" VALUES(1,'msg_user',0,'user',NULL,NULL,NULL,NULL,NULL,NULL,'[]');
INSERT INTO "locations" VALUES(2,'msg_assistant',249,'assistant','run_fixture',NULL,NULL,NULL,NULL,NULL,'[]');
INSERT INTO "locations" VALUES(3,'msg_result',653,'tool_result','run_fixture',NULL,NULL,NULL,NULL,NULL,'[]');
INSERT INTO "locations" VALUES(4,'msg_input',1174,'user',NULL,'input_consumed',NULL,NULL,NULL,NULL,'[]');
CREATE TABLE request_headers (
    session_id TEXT NOT NULL,
    context_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY(session_id, context_id)
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
    FOREIGN KEY(session_id) REFERENCES response_streams(session_id) ON DELETE CASCADE
);
INSERT INTO "response_drafts" VALUES('ses_fixture',1,'reasoning','Draft reasoning');
INSERT INTO "response_drafts" VALUES('ses_fixture',2,'text','Unfinished answer');
CREATE TABLE response_streams (
    session_id TEXT PRIMARY KEY,
    context_id TEXT NOT NULL
);
INSERT INTO "response_streams" VALUES('ses_fixture','ctx_fixture');
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
INSERT INTO "runs" VALUES('ses_fixture','run_fixture','2026-09-01 00:00:00+00:00',0,0,0,0,'{"schema_version":10,"history_epoch":0,"run_id":"run_fixture","session_id":"ses_fixture","kind":"agent","origin":"user","parent_run_id":null,"initiator_id":null,"status":"completed","phase":"model_streaming","trigger":"user","started_at":"2026-09-01T00:00:00Z","ended_at":"2026-09-01T00:00:00Z","model":null,"model_endpoint_id":null,"pending_approval_id":null,"active_tool_batch":{"batch_id":"batch_fixture","turn_id":"turn_fixture","assistant_message_id":"msg_assistant","producer":{"kind":"model","driver_id":null},"phase":"planned","execution_mode":"sequential","max_concurrency":4,"call_ids":["tool_be80d1e31bad460ead6c96dd4705b1e6"],"created_at":"2026-09-01T00:00:00Z","updated_at":"2026-09-01T00:00:00Z","metadata":{}},"abort_requested":false,"error":null,"model_round_count":0,"summary_request_count":0,"current_request_id":null,"continuation":null,"driver":{"driver_id":"ouro.default","contract_hash":"ouro.default/1","implementation":null,"dependencies":[]},"driver_checkpoint":null,"agent_catalog":null,"metadata":{}}');
CREATE TABLE sessions (session_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
INSERT INTO "sessions" VALUES('ses_fixture','{"schema_version":5,"session_id":"ses_fixture","harness_version":"harness_fixture","history_epoch":0,"namespace":"default","root_session_id":"ses_fixture","parent_session_id":null,"session_kind":"primary","delegation_id":null,"forked_from_message_id":null,"forked_from_run_id":null,"title":"New conversation","title_source":"fallback","title_locked":false,"created_at":"2026-09-01T00:00:00Z","updated_at":"2026-10-29T17:49:50.422426Z","last_activity_at":"2026-09-01T00:00:00Z","archived":false,"pinned":false,"workspace":null,"workspace_trusted":false,"current_model":null,"reasoning_request":{"mode":"default","effort":null},"approval_mode":"inherit","interaction_mode":"interactive","controls":{"revision":0,"activations":[]},"context_selection":{"compaction_id":null,"media_elided_through":null},"status":"idle","active_run_id":null,"continuation_request":null,"last_message_id":"msg_input","last_commit_id":"op_b1981ca6e812447bb3a0914203e6840a","last_run_id":"run_fixture","message_count":4,"run_count":1,"resource_count":0,"pending_input_ids":["input_pending"],"tags":[],"metadata":{"schema_version":999,"private":{"message_id":"unchanged","payload":["safe-example",1,null]}}}');
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
INSERT INTO "tool_call_states" VALUES('ses_fixture','run_fixture','batch_fixture','tool_be80d1e31bad460ead6c96dd4705b1e6','{"schema_version":1,"session_id":"ses_fixture","identity":{"run_id":"run_fixture","batch_id":"batch_fixture","tool_call_id":"tool_be80d1e31bad460ead6c96dd4705b1e6"},"status":"completed","attempt":0,"prepared_binding":{"producer":{"kind":"model","driver_id":null},"tool_call_id":"tool_be80d1e31bad460ead6c96dd4705b1e6","session_id":"ses_fixture","run_id":"run_fixture","turn_id":"turn_fixture","tool_name":"example","canonical_tool_id":null,"owner_id":null,"source":"builtin","effect":"unknown","batch_behavior":"compatible","requested_at":"2026-09-01T00:00:00Z","permission_mode":"auto","permission_control":"host","resumable":null,"definition_contract_hash":null,"approval_id":null,"plugin_id":null,"plugin_version":null,"plugin_class":null,"release_id":null,"package_digest":null,"builtin_contract_hash":null,"generation_id":null,"contract_hash":null,"execution_contract_hash":null,"active_when_controller_id":null,"interaction_controller_id":null,"interaction_activation_id":null,"tool_policy_ids":[],"tool_policy_reason":null},"arguments_override":null,"approval":null,"input_request":null,"input_resolution":null,"input_revision":0,"interaction_activation_id":null,"agent_wait":null,"outcome_attempt":null,"proposals":null,"feedback":null,"result_message_id":null,"metadata":{}}');
CREATE TABLE tool_calls (
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    projection TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY(session_id, run_id, batch_id, tool_call_id)
);
INSERT INTO "tool_calls" VALUES('ses_fixture','run_fixture','batch_fixture','tool_be80d1e31bad460ead6c96dd4705b1e6','{"schema_version":1,"session_id":"ses_fixture","identity":{"run_id":"run_fixture","batch_id":"batch_fixture","tool_call_id":"tool_be80d1e31bad460ead6c96dd4705b1e6"},"origin_session_id":"ses_fixture","history_epoch":0,"turn_id":"turn_fixture","producer":{"kind":"model","driver_id":null},"request":{"id":"tool_be80d1e31bad460ead6c96dd4705b1e6","name":"example","source_index":0},"accepted_at":"2026-09-01T00:00:00Z"}','{"schema_version":1,"session_id":"ses_fixture","identity":{"run_id":"run_fixture","batch_id":"batch_fixture","tool_call_id":"tool_be80d1e31bad460ead6c96dd4705b1e6"},"origin_session_id":"ses_fixture","history_epoch":0,"turn_id":"turn_fixture","producer":{"kind":"model","driver_id":null},"request":{"id":"tool_be80d1e31bad460ead6c96dd4705b1e6","name":"example","arguments":{"schema_version":999,"private":{"message_id":"unchanged","payload":["safe-example",1,null]}},"source_index":0},"accepted_at":"2026-09-01T00:00:00Z"}');
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
CREATE INDEX runs_request_counts
    ON runs(session_id, run_id, model_round_count, summary_request_count);
CREATE INDEX runs_pending
    ON runs(session_id, history_epoch, started_at, run_id) WHERE needs_recovery = 1;
CREATE INDEX call_positions ON call_memberships(position);
CREATE INDEX run_locations
ON locations(run_id, position) WHERE run_id IS NOT NULL;
CREATE INDEX input_locations
ON locations(queued_input_id, position) WHERE queued_input_id IS NOT NULL;
CREATE INDEX assistant_turns
ON locations(run_id, assistant_turn_id, position) WHERE assistant_turn_id IS NOT NULL;
CREATE INDEX context_deliveries
ON locations(run_id, context_delivery_key, position) WHERE context_delivery_key IS NOT NULL;
CREATE INDEX assistant_runs
ON locations(run_id, position) WHERE role = 'assistant';
CREATE INDEX input_messages ON locations(position)
WHERE role IN ('user', 'system');
CREATE INDEX compaction_references
    ON compactions(session_id, row_id, compaction_id,
        previous_compaction_id, covered_end_message_id,
        history_boundary_message_id);
CREATE INDEX events_session_order ON events(session_id, row_id);
CREATE INDEX events_session_run_order ON events(session_id, run_id, row_id);
DELETE FROM "sqlite_sequence";
COMMIT;
