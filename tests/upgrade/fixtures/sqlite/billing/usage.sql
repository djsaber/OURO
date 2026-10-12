BEGIN TRANSACTION;
CREATE TABLE entries (
        entry_id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL, day TEXT NOT NULL,
        provider_id TEXT NOT NULL, endpoint_id TEXT, model_id TEXT NOT NULL, workspace TEXT,
        session_id TEXT NOT NULL, run_id TEXT NOT NULL,
        purpose TEXT NOT NULL, billing_mode TEXT NOT NULL,
        input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
        cache_read_tokens INTEGER NOT NULL, cache_write_tokens INTEGER NOT NULL, estimated_cost REAL
    );
INSERT INTO "entries" VALUES('usage_kept','2026-09-01T00:00:00.000000+00:00','2026-09-01','fixture',NULL,'fixture-model',NULL,'ses_fixture','run_fixture','agent','usage_based',123,7,0,0,0.125);
CREATE TABLE entry_records (
        entry_id TEXT PRIMARY KEY REFERENCES entries(entry_id) ON DELETE CASCADE,
        payload TEXT NOT NULL, source_event_id TEXT NOT NULL REFERENCES events(event_id)
    );
INSERT INTO "entry_records" VALUES('usage_kept','{"schema_version":1,"entry_id":"usage_kept","occurred_at":"2026-09-01T00:00:00Z","provider_id":"fixture","endpoint_id":null,"model_id":"fixture-model","session_id":"ses_fixture","workspace":null,"run_id":"run_fixture","run_kind":"agent","purpose":"agent","request_id":"req_kept","input_tokens":123,"output_tokens":7,"cache_read_tokens":0,"cache_write_tokens":0,"estimated_cost":0.125,"revision":2,"modified_at":null,"modification_note":null,"metadata":{"billing_mode":"usage_based","harness_scopes":{"example":["scope"]},"schema_version":999,"private":{"message_id":"unchanged","payload":["safe-example",1,null]}}}','uev_3f19e2003c124e759548f5b93f5ea5d6');
CREATE TABLE events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL
    );
INSERT INTO "events" VALUES(1,'uev_aad83a31b2eb47adac4761e46d8b3610','{"schema_version":1,"event_id":"uev_aad83a31b2eb47adac4761e46d8b3610","event_type":"record","recorded_at":"2026-10-12T13:33:34.511842Z","entry_id":"usage_removed","entry":{"schema_version":1,"entry_id":"usage_removed","occurred_at":"2026-09-01T00:00:00Z","provider_id":"fixture","endpoint_id":null,"model_id":"fixture-model","session_id":"ses_fixture","workspace":null,"run_id":"run_fixture","run_kind":"agent","purpose":"agent","request_id":"req_reset","input_tokens":99,"output_tokens":0,"cache_read_tokens":0,"cache_write_tokens":0,"estimated_cost":null,"revision":1,"modified_at":null,"modification_note":null,"metadata":{}},"usage_filter":null,"note":null}');
INSERT INTO "events" VALUES(2,'uev_07d833c9b0b24095b2f0e79abdadbc8d','{"schema_version":1,"event_id":"uev_07d833c9b0b24095b2f0e79abdadbc8d","event_type":"reset","recorded_at":"2026-10-12T13:33:34.522878Z","entry_id":null,"entry":null,"usage_filter":{"occurred_from":null,"occurred_to":null,"provider_ids":[],"endpoint_ids":[],"model_ids":[],"session_ids":[],"run_ids":[],"workspaces":[],"purposes":[],"billing_modes":[]},"note":"fixture reset"}');
INSERT INTO "events" VALUES(3,'uev_3f19e2003c124e759548f5b93f5ea5d6','{"schema_version":1,"event_id":"uev_3f19e2003c124e759548f5b93f5ea5d6","event_type":"record","recorded_at":"2026-10-12T13:33:34.533763Z","entry_id":"usage_kept","entry":{"schema_version":1,"entry_id":"usage_kept","occurred_at":"2026-09-01T00:00:00Z","provider_id":"fixture","endpoint_id":null,"model_id":"fixture-model","session_id":"ses_fixture","workspace":null,"run_id":"run_fixture","run_kind":"agent","purpose":"agent","request_id":"req_kept","input_tokens":100,"output_tokens":7,"cache_read_tokens":0,"cache_write_tokens":0,"estimated_cost":0.125,"revision":1,"modified_at":null,"modification_note":null,"metadata":{"billing_mode":"usage_based","harness_scopes":{"example":["scope"]},"schema_version":999,"private":{"message_id":"unchanged","payload":["safe-example",1,null]}}},"usage_filter":null,"note":null}');
INSERT INTO "events" VALUES(4,'uev_c5842e0e89f5439cbc51290f7c1d6ef4','{"schema_version":1,"event_id":"uev_c5842e0e89f5439cbc51290f7c1d6ef4","event_type":"replace","recorded_at":"2026-10-12T13:33:34.545834Z","entry_id":"usage_kept","entry":{"schema_version":1,"entry_id":"usage_kept","occurred_at":"2026-09-01T00:00:00Z","provider_id":"fixture","endpoint_id":null,"model_id":"fixture-model","session_id":"ses_fixture","workspace":null,"run_id":"run_fixture","run_kind":"agent","purpose":"agent","request_id":"req_kept","input_tokens":123,"output_tokens":7,"cache_read_tokens":0,"cache_write_tokens":0,"estimated_cost":0.125,"revision":2,"modified_at":null,"modification_note":null,"metadata":{"billing_mode":"usage_based","harness_scopes":{"example":["scope"]},"schema_version":999,"private":{"message_id":"unchanged","payload":["safe-example",1,null]}}},"usage_filter":null,"note":"fixture correction"}');
CREATE TABLE scopes (
        entry_id TEXT NOT NULL REFERENCES entries(entry_id) ON DELETE CASCADE,
        owner_id TEXT NOT NULL, scope TEXT NOT NULL, PRIMARY KEY(entry_id, owner_id, scope)
    ) WITHOUT ROWID;
INSERT INTO "scopes" VALUES('usage_kept','example','scope');
ANALYZE "sqlite_master";
INSERT INTO "sqlite_stat1" VALUES('events','sqlite_autoindex_events_1','1 1');
INSERT INTO "sqlite_stat1" VALUES('entries','entries_billing_mode','1 1 1 1');
INSERT INTO "sqlite_stat1" VALUES('entries','entries_purpose','1 1 1 1');
INSERT INTO "sqlite_stat1" VALUES('entries','entries_run_id','1 1 1 1');
INSERT INTO "sqlite_stat1" VALUES('entries','entries_session_id','1 1 1 1');
INSERT INTO "sqlite_stat1" VALUES('entries','entries_workspace','1 1 1 1');
INSERT INTO "sqlite_stat1" VALUES('entries','entries_model_id','1 1 1 1');
INSERT INTO "sqlite_stat1" VALUES('entries','entries_endpoint_id','1 1 1 1');
INSERT INTO "sqlite_stat1" VALUES('entries','entries_provider_id','1 1 1 1');
INSERT INTO "sqlite_stat1" VALUES('entries','entries_totals','1 1 1 1 1 1 1 1');
INSERT INTO "sqlite_stat1" VALUES('entries','entries_time','1 1 1');
INSERT INTO "sqlite_stat1" VALUES('entries','sqlite_autoindex_entries_1','1 1');
INSERT INTO "sqlite_stat1" VALUES('entry_records','sqlite_autoindex_entry_records_1','1 1');
INSERT INTO "sqlite_stat1" VALUES('scopes','scopes','1 1 1 1');
CREATE INDEX entries_time ON entries(occurred_at DESC, entry_id DESC);
CREATE INDEX entries_totals ON entries(day, billing_mode, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, estimated_cost);
CREATE INDEX entries_provider_id ON entries(provider_id, occurred_at DESC, entry_id DESC);
CREATE INDEX entries_endpoint_id ON entries(endpoint_id, occurred_at DESC, entry_id DESC);
CREATE INDEX entries_model_id ON entries(model_id, occurred_at DESC, entry_id DESC);
CREATE INDEX entries_workspace ON entries(workspace, occurred_at DESC, entry_id DESC);
CREATE INDEX entries_session_id ON entries(session_id, occurred_at DESC, entry_id DESC);
CREATE INDEX entries_run_id ON entries(run_id, occurred_at DESC, entry_id DESC);
CREATE INDEX entries_purpose ON entries(purpose, occurred_at DESC, entry_id DESC);
CREATE INDEX entries_billing_mode ON entries(billing_mode, occurred_at DESC, entry_id DESC);
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('events',4);
COMMIT;
PRAGMA application_id=1330991682;
PRAGMA user_version=1;
