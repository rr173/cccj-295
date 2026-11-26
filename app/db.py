"""SQLite persistence helpers and schema for the verifiable deletion service."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "deletion_demo.db"


def utcnow() -> str:
    """Return a stable ISO-8601 timestamp with microseconds."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def get_db_path() -> Path:
    return Path(os.environ.get("PDC_DB_PATH", str(DEFAULT_DB_PATH))).expanduser().resolve()


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    # autocommit mode: every write transaction is explicitly opened with
    # BEGIN IMMEDIATE so concurrent retention/purge operations have one
    # SQLite-defined serial commit order.
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    # Overwrite deleted SQLite records rather than leaving payload bytes in
    # free pages where the storage engine supports secure deletion.
    conn.execute("PRAGMA secure_delete=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def immediate_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Serialize a write section using SQLite's immediate write lock."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS participants (
    id TEXT PRIMARY KEY,
    display_name TEXT,
    profile_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'deletion_requested')),
    session_token TEXT UNIQUE,
    created_at TEXT NOT NULL,
    deletion_requested_at TEXT,
    profile_purged_at TEXT
);

CREATE TABLE IF NOT EXISTS interviews (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS segments (
    id TEXT PRIMARY KEY,
    interview_id TEXT NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (interview_id, ordinal)
);

CREATE TABLE IF NOT EXISTS segment_participants (
    segment_id TEXT NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    participant_id TEXT NOT NULL REFERENCES participants(id),
    PRIMARY KEY (segment_id, participant_id)
);

-- IDs only, never content.  This survives segment body deletion and is the
-- processing index that lets a later participant request discover that a
-- shared segment was already physically purged because of another person.
CREATE TABLE IF NOT EXISTS segment_impact_index (
    segment_id TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (segment_id, participant_id)
);

CREATE TABLE IF NOT EXISTS deletion_requests (
    id TEXT PRIMARY KEY,
    participant_id TEXT NOT NULL UNIQUE REFERENCES participants(id),
    idempotency_key TEXT UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('processing', 'processed')),
    requested_at TEXT NOT NULL,
    processed_at TEXT
);

CREATE TABLE IF NOT EXISTS deletion_items (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES deletion_requests(id) ON DELETE CASCADE,
    participant_id TEXT NOT NULL REFERENCES participants(id),
    segment_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('hidden_pending_purge', 'retained', 'purged')
    ),
    involved_participant_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    purged_at TEXT,
    UNIQUE (request_id, segment_id)
);

CREATE INDEX IF NOT EXISTS idx_deletion_items_segment
    ON deletion_items(segment_id);
CREATE INDEX IF NOT EXISTS idx_deletion_items_status
    ON deletion_items(status);

CREATE TABLE IF NOT EXISTS retentions (
    id TEXT PRIMARY KEY,
    segment_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT 'project-owner',
    created_at TEXT NOT NULL,
    released_at TEXT,
    release_note TEXT
);

CREATE INDEX IF NOT EXISTS idx_retentions_segment_active
    ON retentions(segment_id, released_at);

CREATE TABLE IF NOT EXISTS export_requests (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL CHECK (scope IN ('all', 'interview')),
    interview_id TEXT REFERENCES interviews(id),
    format TEXT NOT NULL DEFAULT 'json' CHECK (format IN ('json', 'markdown')),
    created_by TEXT NOT NULL DEFAULT 'caller',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_audit_aggregate
    ON audit_events(aggregate_type, aggregate_id, id);

-- ---------------------------------------------------------------------------
-- Collaborative proofreading (校稿).  Draft text lives only in these tables;
-- ``segments.content`` remains the official body until a publish transaction
-- atomically updates it.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS proofreading_sessions (
    id TEXT PRIMARY KEY,
    interview_id TEXT NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('open', 'published')),
    current_version INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL DEFAULT 'project-owner',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    published_version INTEGER,
    published_at TEXT
);

-- At most one open proofreading session per interview.
CREATE UNIQUE INDEX IF NOT EXISTS idx_proofreading_one_open_per_interview
    ON proofreading_sessions(interview_id) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS proofreading_session_segments (
    session_id TEXT NOT NULL REFERENCES proofreading_sessions(id) ON DELETE CASCADE,
    segment_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    PRIMARY KEY (session_id, segment_id)
);

CREATE TABLE IF NOT EXISTS proofreading_versions (
    session_id TEXT NOT NULL REFERENCES proofreading_sessions(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    submission_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, version)
);

-- Full draft snapshot for every selected segment at every version.  Text here
-- is removed when the underlying segment body is physically purged.
CREATE TABLE IF NOT EXISTS proofreading_segment_contents (
    session_id TEXT NOT NULL REFERENCES proofreading_sessions(id) ON DELETE CASCADE,
    segment_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY (session_id, segment_id, version)
);

CREATE INDEX IF NOT EXISTS idx_proofreading_contents_segment
    ON proofreading_segment_contents(segment_id);

-- Actual applied edit operations.  Rows are stored in the coordinate space of
-- version-1, so later proposals can be rebased through the real change
-- history (not merely a diff of the current text).
CREATE TABLE IF NOT EXISTS proofreading_edits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES proofreading_sessions(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    segment_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    start_pos INTEGER NOT NULL,
    end_pos INTEGER NOT NULL,
    replacement_text TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_proofreading_edits_version
    ON proofreading_edits(session_id, version, segment_id);

-- Submission idempotency records.  Only a payload fingerprint is stored, never
-- the proposal text, so deletion cleanup needs to remove no content here.
CREATE TABLE IF NOT EXISTS proofreading_submissions (
    session_id TEXT NOT NULL REFERENCES proofreading_sessions(id) ON DELETE CASCADE,
    submission_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('merged', 'rejected')),
    base_version INTEGER NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, submission_id)
);

CREATE TABLE IF NOT EXISTS proofreading_annotations (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES proofreading_sessions(id) ON DELETE CASCADE,
    segment_id TEXT NOT NULL,
    text TEXT NOT NULL,
    start_pos INTEGER,
    end_pos INTEGER,
    anchored INTEGER NOT NULL DEFAULT 1,
    resolved INTEGER NOT NULL DEFAULT 0,
    anchor_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_proofreading_annotations_session
    ON proofreading_annotations(session_id, resolved, anchored);
CREATE INDEX IF NOT EXISTS idx_proofreading_annotations_segment
    ON proofreading_annotations(segment_id);
"""


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.commit()
