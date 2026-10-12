BEGIN TRANSACTION;
CREATE TABLE task_executions (
                    execution_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    scheduled_at TEXT NOT NULL, created_at TEXT NOT NULL,
                    ended_at TEXT, payload TEXT NOT NULL,
                    UNIQUE(task_id, scheduled_at)
                );
INSERT INTO "task_executions" VALUES('exec_fixture','task_fixture','2026-09-01T00:00:00.000000+00:00','2026-09-01T00:00:00.000000+00:00','2026-09-01T00:00:00.000000+00:00','{"execution_id":"exec_fixture","task_id":"task_fixture","scheduled_at":"2026-09-01T00:00:00Z","created_at":"2026-09-01T00:00:00Z","updated_at":"2026-09-01T00:00:00Z","session_id":"ses_fixture","run_id":"run_fixture","status":"completed","awaiting_approval":false,"error":null,"ended_at":"2026-09-01T00:00:00Z"}');
CREATE TABLE tasks (
                    task_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    enabled INTEGER NOT NULL, next_run_at TEXT,
                    active_execution_id TEXT, status TEXT NOT NULL,
                    updated_at TEXT NOT NULL, payload TEXT NOT NULL
                );
INSERT INTO "tasks" VALUES('task_fixture',1,0,NULL,NULL,'completed','2026-09-01T00:00:00.000000+00:00','{"task_id":"task_fixture","revision":1,"configuration":{"title":"fixture","description":"fixture scheduled prompt","workspace":"C:/fixture","approval_mode":"afk","model":"fixture/model","reasoning":{"mode":"default","effort":null},"schedule":{"frequency":"daily","timezone":"UTC","starts_at":"2026-09-01T00:00:00Z","ends_at":null,"at":"09:00:00","interval":1,"unit":"days","weekdays":[],"month_day":null},"session_mode":"new"},"enabled":false,"created_at":"2026-09-01T00:00:00Z","updated_at":"2026-09-01T00:00:00Z","next_run_at":null,"session_id":null,"active_execution_id":null,"last_execution_id":"exec_fixture","status":"completed","error":null}');
CREATE INDEX tasks_due ON tasks(next_run_at, task_id)
                    WHERE enabled = 1 AND active_execution_id IS NULL;
CREATE INDEX tasks_status ON tasks(status);
CREATE INDEX executions_history
                    ON task_executions(task_id, created_at DESC);
CREATE INDEX executions_outcome
                    ON task_executions(json_extract(payload, '$.status'))
                    WHERE ended_at IS NOT NULL;
COMMIT;
PRAGMA application_id=0;
PRAGMA user_version=1;
