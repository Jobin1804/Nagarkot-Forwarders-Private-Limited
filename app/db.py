"""SQLite storage: schema, connection and time helpers.

Design notes
------------
* Nothing that affects authority is ever deleted or overwritten in place.
  Grants are revoked (revoked_at), role permission sets are versioned,
  users are deactivated (deactivated_at). That is what makes
  "who could have changed this job on the 3rd?" answerable.
* Every job event stores a full snapshot of the job *after* the event, so the
  state of a job at any instant can be rebuilt without replaying logic.
* All timestamps are UTC ISO-8601 strings 'YYYY-MM-DDTHH:MM:SSZ', which sort
  lexicographically, so plain string comparison works in SQL and Python.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta

DEFAULT_DB = os.environ.get("NFPL_DB", os.path.join(os.path.dirname(__file__), "..", "data", "nfpl.db"))

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS branches (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clients (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN ('staff','client','service')),
    client_id      TEXT REFERENCES clients(id),      -- only for kind='client'
    home_branch    TEXT REFERENCES branches(id),
    title          TEXT,
    created_at     TEXT NOT NULL,
    deactivated_at TEXT,
    CHECK ((kind = 'client') = (client_id IS NOT NULL))
);

-- A role is a named bundle of permissions. Roles are data, not code.
CREATE TABLE IF NOT EXISTS roles (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT,
    audience    TEXT NOT NULL CHECK (audience IN ('staff','client','service')),
    privileged  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    created_by  TEXT
);

-- Role permission sets are versioned so history can be evaluated as-of a date.
CREATE TABLE IF NOT EXISTS role_versions (
    role_id        TEXT NOT NULL REFERENCES roles(id),
    version        INTEGER NOT NULL,
    permissions    TEXT NOT NULL,               -- JSON array
    status         TEXT NOT NULL CHECK (status IN ('pending','active','rejected')),
    effective_from TEXT,                        -- set on activation
    changed_by     TEXT,
    approved_by    TEXT,
    created_at     TEXT NOT NULL,
    reason         TEXT,
    PRIMARY KEY (role_id, version)
);

-- A grant = (user, role, scope, time window). NULL scope column = "any".
CREATE TABLE IF NOT EXISTS grants (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        TEXT NOT NULL REFERENCES users(id),
    role_id        TEXT NOT NULL REFERENCES roles(id),
    client_id      TEXT REFERENCES clients(id),
    branch_id      TEXT REFERENCES branches(id),
    direction      TEXT CHECK (direction IN ('import','export')),
    mode           TEXT CHECK (mode IN ('air','fcl','lcl')),
    valid_from     TEXT NOT NULL,
    valid_to       TEXT,
    status         TEXT NOT NULL CHECK (status IN ('pending','active','rejected','revoked')),
    effective_from TEXT,                        -- max(valid_from, approval time); never backdated
    reason         TEXT NOT NULL,
    granted_by     TEXT NOT NULL,
    approved_by    TEXT,
    created_at     TEXT NOT NULL,
    revoked_at     TEXT,
    revoked_by     TEXT,
    revoke_reason  TEXT
);
CREATE INDEX IF NOT EXISTS ix_grants_user ON grants(user_id);

-- Absence and (optional) delegate who covers the absent person's queue.
CREATE TABLE IF NOT EXISTS absences (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL REFERENCES users(id),
    starts_at   TEXT NOT NULL,
    ends_at     TEXT NOT NULL,
    delegate_id TEXT REFERENCES users(id),
    reason      TEXT,
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_absences_window ON absences(starts_at, ends_at);

CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    external_ref    TEXT UNIQUE,                -- key used by the overnight parser
    client_id       TEXT NOT NULL REFERENCES clients(id),
    branch_id       TEXT NOT NULL REFERENCES branches(id),
    direction       TEXT NOT NULL CHECK (direction IN ('import','export')),
    mode            TEXT NOT NULL CHECK (mode IN ('air','fcl','lcl')),
    stage           TEXT NOT NULL CHECK (stage IN ('Confirmed','Prepared','Checked','Filed','Cancelled','Reopened')),
    assignee_id     TEXT REFERENCES users(id),
    prepared_by     TEXT REFERENCES users(id),
    checked_by      TEXT REFERENCES users(id),
    filed_by        TEXT REFERENCES users(id),
    needs_attention TEXT,
    data            TEXT NOT NULL DEFAULT '{}',
    version         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_jobs_stage_scope ON jobs(stage, client_id, branch_id, direction, mode);
CREATE INDEX IF NOT EXISTS ix_jobs_scope ON jobs(client_id, branch_id, direction, mode);
CREATE INDEX IF NOT EXISTS ix_jobs_assignee ON jobs(assignee_id, stage);
CREATE INDEX IF NOT EXISTS ix_jobs_updated ON jobs(updated_at, id);

-- Append-only job history. snapshot = job state after the event.
CREATE TABLE IF NOT EXISTS job_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     TEXT NOT NULL REFERENCES jobs(id),
    at         TEXT NOT NULL,
    actor_id   TEXT NOT NULL,
    action     TEXT NOT NULL,
    from_stage TEXT,
    to_stage   TEXT,
    reason     TEXT,
    detail     TEXT,                            -- JSON
    authority  TEXT,                            -- JSON: grant ids / approvals that allowed it
    snapshot   TEXT NOT NULL                    -- JSON
);
CREATE INDEX IF NOT EXISTS ix_job_events_job ON job_events(job_id, at);
CREATE TRIGGER IF NOT EXISTS job_events_no_update
BEFORE UPDATE ON job_events BEGIN SELECT RAISE(ABORT, 'job_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS job_events_no_delete
BEFORE DELETE ON job_events BEGIN SELECT RAISE(ABORT, 'job_events is append-only'); END;

-- Two-person workflow for cancelling a filed job or reopening a closed one.
CREATE TABLE IF NOT EXISTS requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        TEXT NOT NULL REFERENCES jobs(id),
    kind          TEXT NOT NULL CHECK (kind IN ('cancel_filed','reopen')),
    status        TEXT NOT NULL CHECK (status IN ('pending','approved','rejected')),
    reason        TEXT NOT NULL,
    requested_by  TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    decided_by    TEXT,
    decided_at    TEXT,
    decision_note TEXT
);
CREATE INDEX IF NOT EXISTS ix_requests_status ON requests(status);

-- Audit of changes to access itself (grants, roles, absences, parser rejects).
CREATE TABLE IF NOT EXISTS access_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    actor_id    TEXT NOT NULL,
    action      TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS ix_access_events_target ON access_events(target_type, target_id);
CREATE TRIGGER IF NOT EXISTS access_events_no_update
BEFORE UPDATE ON access_events BEGIN SELECT RAISE(ABORT, 'access_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS access_events_no_delete
BEFORE DELETE ON access_events BEGIN SELECT RAISE(ABORT, 'access_events is append-only'); END;

-- Every attempt to change something, allowed or denied, with its explanation.
CREATE TABLE IF NOT EXISTS decision_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT NOT NULL,
    user_id  TEXT NOT NULL,
    action   TEXT NOT NULL,
    job_id   TEXT,
    allowed  INTEGER NOT NULL,
    summary  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_decision_log_job ON decision_log(job_id, at);
CREATE TRIGGER IF NOT EXISTS decision_log_no_update
BEFORE UPDATE ON decision_log BEGIN SELECT RAISE(ABORT, 'decision_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS decision_log_no_delete
BEFORE DELETE ON decision_log BEGIN SELECT RAISE(ABORT, 'decision_log is append-only'); END;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or DEFAULT_DB
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


class tx:
    """Explicit transaction: `with tx(conn): ...` (BEGIN IMMEDIATE / COMMIT / ROLLBACK)."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, _tb):
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        # Some failures must leave a trace even though the change is rolled back
        # (a denied attempt is evidence). They carry an `after_rollback` hook.
        hook = getattr(exc, "after_rollback", None)
        if callable(hook):
            hook(self.conn)
        return False


# ---------------------------------------------------------------- time helpers
def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utcnow() -> str:
    return iso(datetime.now(timezone.utc))


def parse_ts(value: str) -> datetime:
    """Accept '2026-10-03', '2026-10-03T10:00', '...Z', '...+05:30'; return aware UTC."""
    v = value.strip().replace("Z", "+00:00")
    if len(v) == 10:
        v += "T00:00:00+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def norm_ts(value: str | None) -> str | None:
    return None if value in (None, "") else iso(parse_ts(value))


def shift(ts: str, **kw) -> str:
    return iso(parse_ts(ts) + timedelta(**kw))


def dumps(obj) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


def row_dict(row) -> dict | None:
    return None if row is None else dict(row)
