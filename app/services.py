"""Business operations for participants, segments, exports and deletion.

Every operation that can race with deletion/retention commit order uses a
single SQLite ``BEGIN IMMEDIATE`` transaction.  Content is never copied into
deletion/audit records.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from typing import Any

from .db import immediate_transaction, utcnow


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, extra: dict[str, Any] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra or {}


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def audit_locked(
    conn: sqlite3.Connection,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    detail: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO audit_events
            (event_id, occurred_at, aggregate_type, aggregate_id, event_type, detail_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            new_id("evt"),
            utcnow(),
            aggregate_type,
            aggregate_id,
            event_type,
            json.dumps(detail or {}, ensure_ascii=False, sort_keys=True),
        ),
    )


def get_participant(conn: sqlite3.Connection, participant_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM participants WHERE id = ?", (participant_id,)).fetchone()
    if row is None:
        raise ApiError(404, "participant_not_found", f"Participant {participant_id!r} was not found")
    return row


def create_participant(
    conn: sqlite3.Connection,
    *,
    participant_id: str | None,
    display_name: str,
    profile: dict[str, Any],
    session_token: str | None,
) -> dict[str, Any]:
    pid = participant_id or new_id("p")
    token = session_token or new_id("sess")
    now = utcnow()
    with immediate_transaction(conn):
        if conn.execute("SELECT 1 FROM participants WHERE id = ?", (pid,)).fetchone():
            raise ApiError(409, "participant_exists", f"Participant {pid!r} already exists")
        if conn.execute("SELECT 1 FROM participants WHERE session_token = ?", (token,)).fetchone():
            raise ApiError(409, "session_token_exists", "The supplied session token is already in use")
        conn.execute(
            """
            INSERT INTO participants
                (id, display_name, profile_json, status, session_token, created_at)
            VALUES (?, ?, ?, 'active', ?, ?)
            """,
            (pid, display_name, json.dumps(profile, ensure_ascii=False), token, now),
        )
        audit_locked(conn, "participant", pid, "participant.created")
    return participant_public(_require_participant(conn, pid))


def _require_participant(conn: sqlite3.Connection, pid: str) -> sqlite3.Row:
    return get_participant(conn, pid)


def participant_public(row: sqlite3.Row) -> dict[str, Any]:
    """Ordinary read representation.

    Requested accounts have their profile payload physically erased; only the
    non-content tombstone/status needed for write prevention is returned.
    """
    if row["status"] == "deletion_requested":
        return {
            "id": row["id"],
            "status": "deletion_requested",
            "profile_available": False,
            "deletion_requested_at": row["deletion_requested_at"],
            "profile_purged_at": row["profile_purged_at"],
        }
    return {
        "id": row["id"],
        "status": row["status"],
        "display_name": row["display_name"],
        "profile": json.loads(row["profile_json"] or "{}"),
        "profile_available": True,
        "session_token": row["session_token"],
        "created_at": row["created_at"],
    }


def list_participants(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM participants WHERE status = 'active' ORDER BY created_at, id"
    ).fetchall()
    return [participant_public(r) for r in rows]


def get_participant_normal(conn: sqlite3.Connection, pid: str) -> dict[str, Any]:
    row = get_participant(conn, pid)
    if row["status"] == "deletion_requested":
        # 410 makes the tombstone explicit without restoring any profile data.
        raise ApiError(
            410,
            "participant_profile_unavailable",
            "This participant requested deletion; profile data is unavailable through ordinary reads",
            participant_public(row),
        )
    return participant_public(row)


def update_profile(
    conn: sqlite3.Connection,
    pid: str,
    *,
    display_name: str | None = None,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with immediate_transaction(conn):
        row = get_participant(conn, pid)
        if row["status"] == "deletion_requested":
            raise ApiError(
                409,
                "participant_deletion_requested_write_forbidden",
                "Cannot write new profile data after a deletion request",
                {"participant_id": pid},
            )
        new_name = row["display_name"] if display_name is None else display_name
        if profile is None:
            new_profile = row["profile_json"]
        else:
            new_profile = json.dumps(profile, ensure_ascii=False)
        conn.execute(
            "UPDATE participants SET display_name = ?, profile_json = ? WHERE id = ?",
            (new_name, new_profile, pid),
        )
        audit_locked(conn, "participant", pid, "profile.updated")
    return participant_public(get_participant(conn, pid))


def revoke_session(conn: sqlite3.Connection, token: str) -> dict[str, Any]:
    with immediate_transaction(conn):
        row = conn.execute("SELECT id FROM participants WHERE session_token = ?", (token,)).fetchone()
        conn.execute("UPDATE participants SET session_token = NULL WHERE session_token = ?", (token,))
        if row is not None:
            audit_locked(conn, "participant", row["id"], "session.revoked")
    # Logout is intentionally idempotent and cannot affect a deletion request.
    return {"session_token": token, "revoked": row is not None}


def revoke_participant_session(conn: sqlite3.Connection, participant_id: str) -> dict[str, Any]:
    with immediate_transaction(conn):
        row = get_participant(conn, participant_id)
        conn.execute("UPDATE participants SET session_token = NULL WHERE id = ?", (participant_id,))
        audit_locked(conn, "participant", participant_id, "session.revoked_by_participant")
    return {"participant_id": participant_id, "revoked": bool(row["session_token"])}


def create_interview(
    conn: sqlite3.Connection, *, interview_id: str | None, title: str
) -> dict[str, Any]:
    iid = interview_id or new_id("int")
    now = utcnow()
    with immediate_transaction(conn):
        if conn.execute("SELECT 1 FROM interviews WHERE id = ?", (iid,)).fetchone():
            raise ApiError(409, "interview_exists", f"Interview {iid!r} already exists")
        conn.execute("INSERT INTO interviews (id, title, created_at) VALUES (?, ?, ?)", (iid, title, now))
        audit_locked(conn, "interview", iid, "interview.created", {"title": title})
    return dict(row_dict(conn.execute("SELECT * FROM interviews WHERE id=?", (iid,)).fetchone()) or {})


def list_interviews(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM interviews ORDER BY created_at, id").fetchall()
    result = []
    for row in rows:
        visible_count = conn.execute(
            f"SELECT COUNT(*) AS c FROM segments s WHERE s.interview_id = ? AND {_normal_visible_predicate('s')}",
            (row["id"],),
        ).fetchone()["c"]
        item = row_dict(row) or {}
        item["visible_segment_count"] = visible_count
        result.append(item)
    return result


def _normal_visible_predicate(segment_alias: str = "s") -> str:
    """No linked participant may have requested deletion."""
    return f"""
        NOT EXISTS (
            SELECT 1
            FROM segment_participants hidden_sp
            JOIN participants hidden_p ON hidden_p.id = hidden_sp.participant_id
            WHERE hidden_sp.segment_id = {segment_alias}.id
              AND hidden_p.status = 'deletion_requested'
        )
    """


def _segment_participant_ids(conn: sqlite3.Connection, segment_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT participant_id FROM segment_participants WHERE segment_id = ? ORDER BY participant_id",
        (segment_id,),
    ).fetchall()
    return [r["participant_id"] for r in rows]


def create_segment(
    conn: sqlite3.Connection,
    *,
    segment_id: str | None,
    interview_id: str,
    content: str,
    participant_ids: list[str],
    ordinal: int | None = None,
) -> dict[str, Any]:
    sid = segment_id or new_id("seg")
    now = utcnow()
    # De-duplicate while preserving caller order for the response.
    unique_ids = list(dict.fromkeys(participant_ids))

    with immediate_transaction(conn):
        interview = conn.execute("SELECT 1 FROM interviews WHERE id = ?", (interview_id,)).fetchone()
        if interview is None:
            raise ApiError(404, "interview_not_found", f"Interview {interview_id!r} was not found")
        if conn.execute("SELECT 1 FROM segments WHERE id = ?", (sid,)).fetchone():
            raise ApiError(409, "segment_exists", f"Segment {sid!r} already exists")

        placeholders = ",".join("?" for _ in unique_ids)
        rows = conn.execute(
            f"SELECT id, status FROM participants WHERE id IN ({placeholders})", unique_ids
        ).fetchall() if unique_ids else []
        found = {r["id"]: r for r in rows}
        missing = [pid for pid in unique_ids if pid not in found]
        if missing:
            raise ApiError(404, "participant_not_found", "Some listed participants do not exist", {"missing": missing})
        blocked = [pid for pid in unique_ids if found[pid]["status"] == "deletion_requested"]
        if blocked:
            raise ApiError(
                409,
                "participant_deletion_requested_write_forbidden",
                "Cannot write a new segment involving a participant who has requested deletion",
                {"participant_ids": blocked},
            )

        if ordinal is None:
            ordinal = conn.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 AS next_ordinal FROM segments WHERE interview_id = ?",
                (interview_id,),
            ).fetchone()["next_ordinal"]

        conn.execute(
            "INSERT INTO segments (id, interview_id, ordinal, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (sid, interview_id, ordinal, content, now),
        )
        for pid in unique_ids:
            conn.execute(
                "INSERT INTO segment_participants (segment_id, participant_id) VALUES (?, ?)", (sid, pid)
            )
            # IDs-only impact index survives later physical segment deletion.
            conn.execute(
                "INSERT OR IGNORE INTO segment_impact_index (segment_id, participant_id, first_seen_at) VALUES (?, ?, ?)",
                (sid, pid, now),
            )
        audit_locked(
            conn,
            "segment",
            sid,
            "segment.created",
            {"interview_id": interview_id, "participant_ids": unique_ids},
        )

    row = conn.execute("SELECT * FROM segments WHERE id = ?", (sid,)).fetchone()
    return segment_payload(conn, row)


def segment_payload(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "interview_id": row["interview_id"],
        "ordinal": row["ordinal"],
        "content": row["content"],
        "participant_ids": _segment_participant_ids(conn, row["id"]),
        "created_at": row["created_at"],
    }


def get_interview_normal(conn: sqlite3.Connection, interview_id: str) -> dict[str, Any]:
    interview = conn.execute("SELECT * FROM interviews WHERE id = ?", (interview_id,)).fetchone()
    if interview is None:
        raise ApiError(404, "interview_not_found", f"Interview {interview_id!r} was not found")
    rows = conn.execute(
        f"""
        SELECT s.* FROM segments s
        WHERE s.interview_id = ? AND {_normal_visible_predicate('s')}
        ORDER BY s.ordinal, s.id
        """,
        (interview_id,),
    ).fetchall()
    result = row_dict(interview) or {}
    result["segments"] = [segment_payload(conn, r) for r in rows]
    result["visible_segment_count"] = len(result["segments"])
    return result


def get_segment_normal(conn: sqlite3.Connection, segment_id: str) -> dict[str, Any]:
    row = conn.execute(
        f"SELECT s.* FROM segments s WHERE s.id = ? AND {_normal_visible_predicate('s')}",
        (segment_id,),
    ).fetchone()
    if row is None:
        # Same 404 for missing, hidden/retained and purged segments so the
        # ordinary API cannot be used as an oracle for protected bodies.
        raise ApiError(404, "segment_not_visible", "Segment not found or not visible through ordinary reads")
    return segment_payload(conn, row)


def search_segments(conn: sqlite3.Connection, query: str, limit: int = 50) -> list[dict[str, Any]]:
    q = query.strip()
    if not q:
        return []
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{escaped.lower()}%"
    rows = conn.execute(
        f"""
        SELECT s.* FROM segments s
        JOIN interviews i ON i.id = s.interview_id
        WHERE {_normal_visible_predicate('s')}
          AND (LOWER(s.content) LIKE ? ESCAPE '\\' OR LOWER(i.title) LIKE ? ESCAPE '\\')
        ORDER BY i.created_at, s.ordinal, s.id
        LIMIT ?
        """,
        (like, like, limit),
    ).fetchall()
    # When title matches, still only return matching content? Current OR may return full segment content even if only title match; that's okay because segment visible. But search q might not in content. Fine.
    return [segment_payload(conn, r) for r in rows]


def _active_retentions(conn: sqlite3.Connection, segment_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM retentions WHERE segment_id = ? AND released_at IS NULL ORDER BY created_at, id",
        (segment_id,),
    ).fetchall()


def retention_dict(row: sqlite3.Row, *, include_segment: bool = False, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    data = row_dict(row) or {}
    data["active"] = row["released_at"] is None
    if include_segment and conn is not None:
        seg = conn.execute("SELECT * FROM segments WHERE id = ?", (row["segment_id"],)).fetchone()
        if seg is not None and row["released_at"] is None:
            data["segment"] = segment_payload(conn, seg)
            data["body_accessible_through_retention"] = True
        else:
            data["segment"] = None
            data["body_accessible_through_retention"] = False
    return data


def preview_deletion(conn: sqlite3.Connection, participant_id: str) -> dict[str, Any]:
    participant = get_participant(conn, participant_id)
    existing = conn.execute(
        "SELECT * FROM deletion_requests WHERE participant_id = ?", (participant_id,)
    ).fetchone()

    live_rows = conn.execute(
        """
        SELECT s.* FROM segments s
        JOIN segment_participants sp ON sp.segment_id = s.id
        WHERE sp.participant_id = ?
        ORDER BY s.interview_id, s.ordinal, s.id
        """,
        (participant_id,),
    ).fetchall()
    live_ids = {r["id"] for r in live_rows}

    historical_rows = conn.execute(
        """
        SELECT DISTINCT idx.segment_id
        FROM segment_impact_index idx
        WHERE idx.participant_id = ?
          AND NOT EXISTS (SELECT 1 FROM segments s WHERE s.id = idx.segment_id)
        ORDER BY idx.segment_id
        """,
        (participant_id,),
    ).fetchall()

    impacts: list[dict[str, Any]] = []
    for row in live_rows:
        rets = _active_retentions(conn, row["id"])
        item = None
        if existing is not None:
            item = conn.execute(
                "SELECT status FROM deletion_items WHERE request_id = ? AND segment_id = ?",
                (existing["id"], row["id"]),
            ).fetchone()
        impacts.append(
            {
                "segment_id": row["id"],
                "interview_id": row["interview_id"],
                "ordinal": row["ordinal"],
                "participant_ids": _segment_participant_ids(conn, row["id"]),
                "indivisible_segment": True,
                "current_request_item_status": item["status"] if item else None,
                "active_retention_ids": [r["id"] for r in rets],
                "active_retention_reasons": [r["reason"] for r in rets],
                "predicted_action": "restricted_retention" if rets else "purge_body",
            }
        )

    for row in historical_rows:
        if row["segment_id"] in live_ids:
            continue
        prior = conn.execute(
            "SELECT status, purged_at FROM deletion_items WHERE segment_id = ? AND status = 'purged' ORDER BY purged_at LIMIT 1",
            (row["segment_id"],),
        ).fetchone()
        involved = conn.execute(
            "SELECT participant_id FROM segment_impact_index WHERE segment_id = ? ORDER BY participant_id",
            (row["segment_id"],),
        ).fetchall()
        impacts.append(
            {
                "segment_id": row["segment_id"],
                "interview_id": None,
                "ordinal": None,
                "participant_ids": [r["participant_id"] for r in involved],
                "indivisible_segment": True,
                "current_request_item_status": prior["status"] if prior else "purged",
                "active_retention_ids": [],
                "active_retention_reasons": [],
                "predicted_action": "already_purged",
                "body_purged_at": prior["purged_at"] if prior else None,
            }
        )

    return {
        "preview": True,
        "already_requested": existing is not None,
        "request_id": existing["id"] if existing else None,
        "participant": participant_public(participant),
        "profile_will_be_or_has_been_purged": True,
        "segment_count": len(impacts),
        "segments": impacts,
        "execution_note": "Preview is non-binding. Execution re-reads current state in a write transaction, so segments added after preview are included.",
    }


def create_deletion_request(
    conn: sqlite3.Connection,
    participant_id: str,
    *,
    idempotency_key: str | None = None,
    process_now: bool = True,
) -> tuple[dict[str, Any], bool]:
    """Create the unique request for a participant, then settle its items.

    Returns ``(detail, created)``.  Retrying the same participant returns the
    same request rather than creating a second task.
    """
    existing = conn.execute(
        "SELECT * FROM deletion_requests WHERE participant_id = ?", (participant_id,)
    ).fetchone()
    if existing is not None:
        if idempotency_key and existing["idempotency_key"] and existing["idempotency_key"] != idempotency_key:
            raise ApiError(
                409,
                "idempotency_key_mismatch",
                "This participant already has a deletion request with a different idempotency key",
                {"request_id": existing["id"]},
            )
        if idempotency_key and not existing["idempotency_key"]:
            with immediate_transaction(conn):
                conn.execute(
                    "UPDATE deletion_requests SET idempotency_key = ? WHERE id = ? AND idempotency_key IS NULL",
                    (idempotency_key, existing["id"]),
                )
        if process_now:
            process_deletion_request(conn, existing["id"])
        return deletion_request_detail(conn, _require_request(conn, existing["id"])), False

    now = utcnow()
    request_id = new_id("drq")
    with immediate_transaction(conn):
        # Existence/status is checked while holding the write lock.
        get_participant(conn, participant_id)
        if idempotency_key:
            key_row = conn.execute(
                "SELECT participant_id FROM deletion_requests WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if key_row is not None and key_row["participant_id"] != participant_id:
                raise ApiError(
                    409,
                    "idempotency_key_in_use",
                    "Idempotency key belongs to another participant",
                )

        conn.execute(
            """
            INSERT INTO deletion_requests
                (id, participant_id, idempotency_key, status, requested_at)
            VALUES (?, ?, ?, 'processing', ?)
            """,
            (request_id, participant_id, idempotency_key, now),
        )

        # Erase ordinary profile content in the same committed transaction that
        # makes the request durable.  The row remains as a write-blocking
        # tombstone/processing record.
        conn.execute(
            """
            UPDATE participants
            SET status = 'deletion_requested',
                display_name = NULL,
                profile_json = '{}',
                deletion_requested_at = ?,
                profile_purged_at = ?
            WHERE id = ?
            """,
            (now, now, participant_id),
        )

        # Cancel this participant's not-yet-started bookings in the same
        # transaction and invalidate any pending consultations touching them.
        # Other bookings keep their confirmed arrangement.  Imported locally to
        # avoid a module import cycle (scheduling imports from this module).
        from . import scheduling

        booking_effect = scheduling.cancel_bookings_for_participant_purge_locked(
            conn, participant_id, now=now
        )

        live_segments = conn.execute(
            """
            SELECT s.id FROM segments s
            JOIN segment_participants sp ON sp.segment_id = s.id
            WHERE sp.participant_id = ?
            ORDER BY s.id
            """,
            (participant_id,),
        ).fetchall()

        for seg in live_segments:
            sid = seg["id"]
            participant_ids = _segment_participant_ids(conn, sid)
            item_id = new_id("dit")
            conn.execute(
                """
                INSERT INTO deletion_items
                    (id, request_id, participant_id, segment_id, status,
                     involved_participant_ids_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'hidden_pending_purge', ?, ?, ?)
                ON CONFLICT(request_id, segment_id) DO NOTHING
                """,
                (item_id, request_id, participant_id, sid, json.dumps(participant_ids), now, now),
            )
            for pid in participant_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO segment_impact_index (segment_id, participant_id, first_seen_at) VALUES (?, ?, ?)",
                    (sid, pid, now),
                )

        # Segments already physically purged by another participant remain
        # discoverable through the IDs-only impact index.
        historical = conn.execute(
            """
            SELECT DISTINCT idx.segment_id
            FROM segment_impact_index idx
            WHERE idx.participant_id = ?
              AND NOT EXISTS (SELECT 1 FROM segments s WHERE s.id = idx.segment_id)
            ORDER BY idx.segment_id
            """,
            (participant_id,),
        ).fetchall()
        for hrow in historical:
            sid = hrow["segment_id"]
            if conn.execute(
                "SELECT 1 FROM deletion_items WHERE request_id = ? AND segment_id = ?", (request_id, sid)
            ).fetchone():
                continue
            prior = conn.execute(
                "SELECT MIN(purged_at) AS purged_at FROM deletion_items WHERE segment_id = ? AND status = 'purged'",
                (sid,),
            ).fetchone()
            involved_rows = conn.execute(
                "SELECT participant_id FROM segment_impact_index WHERE segment_id = ? ORDER BY participant_id",
                (sid,),
            ).fetchall()
            involved_ids = [r["participant_id"] for r in involved_rows]
            conn.execute(
                """
                INSERT INTO deletion_items
                    (id, request_id, participant_id, segment_id, status,
                     involved_participant_ids_json, created_at, updated_at, purged_at)
                VALUES (?, ?, ?, ?, 'purged', ?, ?, ?, ?)
                """,
                (
                    new_id("dit"),
                    request_id,
                    participant_id,
                    sid,
                    json.dumps(involved_ids),
                    now,
                    now,
                    prior["purged_at"] if prior and prior["purged_at"] else now,
                ),
            )

        audit_locked(
            conn,
            "deletion_request",
            request_id,
            "deletion.requested_and_profile_purged",
            {"participant_id": participant_id, "live_segment_count": len(live_segments)},
        )

    if process_now:
        process_deletion_request(conn, request_id)
    row = _require_request(conn, request_id)
    return deletion_request_detail(conn, row), True


def _require_request(conn: sqlite3.Connection, request_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM deletion_requests WHERE id = ?", (request_id,)).fetchone()
    if row is None:
        raise ApiError(404, "deletion_request_not_found", f"Deletion request {request_id!r} was not found")
    return row


def process_deletion_request(conn: sqlite3.Connection, request_id: str) -> dict[str, Any]:
    _require_request(conn, request_id)
    segment_rows = conn.execute(
        """
        SELECT DISTINCT segment_id FROM deletion_items
        WHERE request_id = ? AND status = 'hidden_pending_purge'
        ORDER BY segment_id
        """,
        (request_id,),
    ).fetchall()
    for row in segment_rows:
        reconcile_segment(conn, row["segment_id"])

    with immediate_transaction(conn):
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM deletion_items WHERE request_id = ? AND status = 'hidden_pending_purge'",
            (request_id,),
        ).fetchone()["c"]
        if pending == 0:
            conn.execute(
                "UPDATE deletion_requests SET status = 'processed', processed_at = COALESCE(processed_at, ?) WHERE id = ?",
                (utcnow(), request_id),
            )
    return deletion_request_detail(conn, _require_request(conn, request_id))


def reconcile_segment(conn: sqlite3.Connection, segment_id: str) -> dict[str, Any]:
    """Reconcile one segment in its own immediate transaction."""
    with immediate_transaction(conn):
        return _reconcile_segment_locked(conn, segment_id)


def _reconcile_segment_locked(conn: sqlite3.Connection, segment_id: str) -> dict[str, Any]:
    now = utcnow()
    segment = conn.execute("SELECT * FROM segments WHERE id = ?", (segment_id,)).fetchone()
    retentions = _active_retentions(conn, segment_id)

    if retentions:
        # Active retention wins while the body exists.  Never change an
        # already-purged item back to retained.
        conn.execute(
            """
            UPDATE deletion_items
            SET status = 'retained', updated_at = ?
            WHERE segment_id = ? AND status != 'purged'
            """,
            (now, segment_id),
        )
        audit_locked(
            conn,
            "segment",
            segment_id,
            "segment.reconciled_retained",
            {"retention_ids": [r["id"] for r in retentions]},
        )
        return {
            "segment_id": segment_id,
            "outcome": "retained",
            "retention_ids": [r["id"] for r in retentions],
        }

    had_body = segment is not None
    if segment is not None:
        links = _segment_participant_ids(conn, segment_id)
        for pid in links:
            conn.execute(
                "INSERT OR IGNORE INTO segment_impact_index (segment_id, participant_id, first_seen_at) VALUES (?, ?, ?)",
                (segment_id, pid, now),
            )
        # Remove new-feature draft history, replacement text and annotation
        # text for this segment in the *same* transaction as the body delete.
        from .proofreading import remove_segment_content_locked

        remove_segment_content_locked(conn, segment_id)
        # The segment row and content are physically deleted.  SQLite cascades
        # live segment_participants rows; segment_impact_index/deletion_items
        # remain and contain IDs/status only.
        conn.execute("DELETE FROM segments WHERE id = ?", (segment_id,))
        purged_at = now
        event = "segment.body_purged"
    else:
        prior = conn.execute(
            "SELECT MIN(purged_at) AS purged_at FROM deletion_items WHERE segment_id = ? AND status = 'purged'",
            (segment_id,),
        ).fetchone()
        purged_at = prior["purged_at"] if prior and prior["purged_at"] else now
        event = "segment.reconciled_absent"

    conn.execute(
        """
        UPDATE deletion_items
        SET status = 'purged',
            updated_at = ?,
            purged_at = COALESCE(purged_at, ?)
        WHERE segment_id = ? AND status != 'purged'
        """,
        (now, purged_at, segment_id),
    )
    audit_locked(
        conn,
        "segment",
        segment_id,
        event,
        {"had_body": had_body, "purged_at": purged_at},
    )
    return {"segment_id": segment_id, "outcome": "purged" if had_body else "already_absent"}


def recover_unfinished(conn: sqlite3.Connection) -> dict[str, Any]:
    """Startup recovery: continue every unsettled segment and request."""
    conn.execute("PRAGMA busy_timeout=30000")
    segment_rows = conn.execute(
        """
        SELECT DISTINCT segment_id
        FROM deletion_items
        WHERE status IN ('hidden_pending_purge', 'retained')
        ORDER BY segment_id
        """
    ).fetchall()
    reconciled = [reconcile_segment(conn, r["segment_id"]) for r in segment_rows]

    request_rows = conn.execute("SELECT id FROM deletion_requests").fetchall()
    recovered_requests = []
    for row in request_rows:
        process_deletion_request(conn, row["id"])
        recovered_requests.append(row["id"])
    return {
        "reconciled_segments": reconciled,
        "processed_requests": recovered_requests,
        "count": len(reconciled),
    }


def deletion_request_detail(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    participant = get_participant(conn, row["participant_id"])
    item_rows = conn.execute(
        "SELECT * FROM deletion_items WHERE request_id = ? ORDER BY created_at, id", (row["id"],)
    ).fetchall()
    items = []
    summary = {"total": len(item_rows), "hidden_pending_purge": 0, "retained": 0, "purged": 0}

    for item in item_rows:
        status = item["status"]
        summary[status] = summary.get(status, 0) + 1
        retention_rows = conn.execute(
            "SELECT * FROM retentions WHERE segment_id = ? ORDER BY created_at, id", (item["segment_id"],)
        ).fetchall()
        segment = conn.execute("SELECT 1 FROM segments WHERE id = ?", (item["segment_id"],)).fetchone()
        active = [retention_dict(r) for r in retention_rows if r["released_at"] is None]
        history = [retention_dict(r) for r in retention_rows]
        items.append(
            {
                "id": item["id"],
                "segment_id": item["segment_id"],
                "status": status,
                "result": {
                    "hidden_pending_purge": "hidden",
                    "retained": "restricted_retention",
                    "purged": "cleared",
                }[status],
                "body_state": {
                    "hidden_pending_purge": "present_but_hidden_pending_purge",
                    "retained": "present_only_in_restricted_retention",
                    "purged": "removed_from_business_storage",
                }[status],
                "visibility": "hidden_from_normal_entries" if status != "purged" else "row_physically_deleted",
                "body_present_in_business_storage": segment is not None,
                "purged_at": item["purged_at"],
                "updated_at": item["updated_at"],
                "involved_participant_ids": json.loads(item["involved_participant_ids_json"] or "[]"),
                "active_retentions": active,
                "active_retention_reasons": [r["reason"] for r in retention_rows if r["released_at"] is None],
                "retention_history": history,
            }
        )

    return {
        "id": row["id"],
        "participant_id": row["participant_id"],
        "idempotency_key": row["idempotency_key"],
        "task_status": row["status"],
        "requested_at": row["requested_at"],
        "processed_at": row["processed_at"],
        "profile_status": "purged" if participant["profile_purged_at"] else participant["status"],
        "profile_purged_at": participant["profile_purged_at"],
        "summary": summary,
        "items": items,
    }


def get_deletion_request(conn: sqlite3.Connection, request_id: str) -> dict[str, Any]:
    return deletion_request_detail(conn, _require_request(conn, request_id))


def get_participant_deletion_request(conn: sqlite3.Connection, participant_id: str) -> dict[str, Any]:
    get_participant(conn, participant_id)
    row = conn.execute(
        "SELECT * FROM deletion_requests WHERE participant_id = ?", (participant_id,)
    ).fetchone()
    if row is None:
        raise ApiError(404, "deletion_request_not_found", "Participant has not requested deletion")
    return deletion_request_detail(conn, row)


def list_deletion_requests(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM deletion_requests ORDER BY requested_at, id").fetchall()
    return [deletion_request_detail(conn, r) for r in rows]


def create_retention(
    conn: sqlite3.Connection, segment_id: str, *, reason: str, created_by: str
) -> dict[str, Any]:
    now = utcnow()
    retention_id = new_id("ret")
    with immediate_transaction(conn):
        segment = conn.execute("SELECT * FROM segments WHERE id = ?", (segment_id,)).fetchone()
        purged_item = conn.execute(
            "SELECT 1 FROM deletion_items WHERE segment_id = ? AND status = 'purged' LIMIT 1",
            (segment_id,),
        ).fetchone()
        if segment is None:
            if purged_item is not None:
                raise ApiError(
                    409,
                    "segment_body_purged_cannot_retain",
                    "The segment body was already physically purged; a later retention cannot revive it",
                    {"segment_id": segment_id},
                )
            raise ApiError(404, "segment_not_found", f"Segment {segment_id!r} was not found")

        conn.execute(
            """
            INSERT INTO retentions (id, segment_id, reason, created_by, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (retention_id, segment_id, reason, created_by, now),
        )
        # If a deletion request already exists, immediately make the item
        # retained.  The write lock makes this atomic against purge.
        conn.execute(
            """
            UPDATE deletion_items
            SET status = 'retained', updated_at = ?
            WHERE segment_id = ? AND status != 'purged'
            """,
            (now, segment_id),
        )
        audit_locked(
            conn,
            "retention",
            retention_id,
            "retention.created",
            {"segment_id": segment_id, "reason": reason},
        )
        row = conn.execute("SELECT * FROM retentions WHERE id = ?", (retention_id,)).fetchone()
        return retention_dict(row)


def list_retentions(
    conn: sqlite3.Connection, *, segment_id: str | None = None, active_only: bool = False
) -> list[dict[str, Any]]:
    where = []
    params: list[Any] = []
    if segment_id:
        where.append("segment_id = ?")
        params.append(segment_id)
    if active_only:
        where.append("released_at IS NULL")
    sql = "SELECT * FROM retentions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at, id"
    rows = conn.execute(sql, params).fetchall()
    return [retention_dict(r) for r in rows]


def get_retention_restricted(conn: sqlite3.Connection, retention_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM retentions WHERE id = ?", (retention_id,)).fetchone()
    if row is None:
        raise ApiError(404, "retention_not_found", f"Retention {retention_id!r} was not found")
    data = retention_dict(row, include_segment=True, conn=conn)
    if row["released_at"] is None:
        segment = conn.execute("SELECT 1 FROM segments WHERE id = ?", (row["segment_id"],)).fetchone()
        if segment is None:
            # The state machine forbids reaching this with an active retention.
            raise ApiError(
                410,
                "retention_body_unavailable",
                "Retention is active but body is absent; state invariant violation",
                data,
            )
    return data


def release_retention(
    conn: sqlite3.Connection, retention_id: str, *, release_note: str | None = None
) -> dict[str, Any]:
    with immediate_transaction(conn):
        row = conn.execute("SELECT * FROM retentions WHERE id = ?", (retention_id,)).fetchone()
        if row is None:
            raise ApiError(404, "retention_not_found", f"Retention {retention_id!r} was not found")

        reconciliation = None
        if row["released_at"] is None:
            now = utcnow()
            conn.execute(
                "UPDATE retentions SET released_at = ?, release_note = ? WHERE id = ?",
                (now, release_note, retention_id),
            )
            audit_locked(
                conn,
                "retention",
                retention_id,
                "retention.released",
                {"segment_id": row["segment_id"], "release_note": release_note},
            )
            active_count = conn.execute(
                "SELECT COUNT(*) AS c FROM retentions WHERE segment_id = ? AND released_at IS NULL",
                (row["segment_id"],),
            ).fetchone()["c"]
            item_count = conn.execute(
                "SELECT COUNT(*) AS c FROM deletion_items WHERE segment_id = ?", (row["segment_id"],)
            ).fetchone()["c"]
            if active_count == 0 and item_count > 0:
                # Same immediate transaction: the last release and physical
                # purge have one commit order and cannot diverge.
                reconciliation = _reconcile_segment_locked(conn, row["segment_id"])
            elif active_count == 0:
                reconciliation = {"segment_id": row["segment_id"], "outcome": "no_deletion_request_remains_visible_normally"}
        else:
            reconciliation = {"segment_id": row["segment_id"], "outcome": "retention_already_released"}

        updated = conn.execute("SELECT * FROM retentions WHERE id = ?", (retention_id,)).fetchone()
        result = retention_dict(updated, include_segment=True, conn=conn)
        result["reconciliation"] = reconciliation
        return result


def create_export_request(
    conn: sqlite3.Connection, *, export_id: str | None, scope: str, interview_id: str | None, export_format: str
) -> dict[str, Any]:
    eid = export_id or new_id("exp")
    now = utcnow()
    with immediate_transaction(conn):
        if conn.execute("SELECT 1 FROM export_requests WHERE id = ?", (eid,)).fetchone():
            raise ApiError(409, "export_exists", f"Export request {eid!r} already exists")
        if scope == "interview":
            if not interview_id:
                raise ApiError(422, "interview_id_required", "interview scope requires interview_id")
            if not conn.execute("SELECT 1 FROM interviews WHERE id = ?", (interview_id,)).fetchone():
                raise ApiError(404, "interview_not_found", f"Interview {interview_id!r} was not found")
        else:
            interview_id = None
        conn.execute(
            """
            INSERT INTO export_requests (id, scope, interview_id, format, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (eid, scope, interview_id, export_format, now),
        )
    return dict(row_dict(conn.execute("SELECT * FROM export_requests WHERE id=?", (eid,)).fetchone()) or {})


def _require_export(conn: sqlite3.Connection, export_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM export_requests WHERE id = ?", (export_id,)).fetchone()
    if row is None:
        raise ApiError(404, "export_not_found", f"Export request {export_id!r} was not found")
    return row


def build_export_content(conn: sqlite3.Connection, export_id: str, *, export_format: str | None = None) -> tuple[str, str, dict[str, Any]]:
    """Build an export while serializing against deletion/purge commits."""
    with immediate_transaction(conn):
        return _build_export_content_locked(conn, export_id, export_format=export_format)


def _build_export_content_locked(conn: sqlite3.Connection, export_id: str, *, export_format: str | None = None) -> tuple[str, str, dict[str, Any]]:
    """Generate export at return time using the current visibility state.

    The persisted export row stores only the request/scope, never a content
    snapshot.  Therefore an old export request created before deletion cannot
    return bodies that are hidden now.
    """
    export = _require_export(conn, export_id)
    fmt = export_format or export["format"]
    if fmt not in ("json", "markdown"):
        raise ApiError(422, "unsupported_format", "format must be json or markdown")

    params: list[Any] = []
    where = [_normal_visible_predicate("s")]
    if export["scope"] == "interview":
        where.append("s.interview_id = ?")
        params.append(export["interview_id"])
    rows = conn.execute(
        f"""
        SELECT s.* FROM segments s
        JOIN interviews i ON i.id = s.interview_id
        WHERE {' AND '.join(where)}
        ORDER BY i.created_at, s.interview_id, s.ordinal, s.id
        """,
        params,
    ).fetchall()
    segments = [segment_payload(conn, r) for r in rows]
    payload = {
        "export_id": export["id"],
        "generated_at": utcnow(),
        "scope": export["scope"],
        "interview_id": export["interview_id"],
        "current_state_validated": True,
        "segment_count": len(segments),
        "segments": segments,
    }
    if fmt == "json":
        return json.dumps(payload, ensure_ascii=False, indent=2), "application/json; charset=utf-8", payload

    lines = [f"# Export {export['id']}", "", f"Generated: {payload['generated_at']}", ""]
    if export["interview_id"]:
        interview = conn.execute("SELECT title FROM interviews WHERE id=?", (export["interview_id"],)).fetchone()
        title = interview["title"] if interview else export["interview_id"]
        lines += [f"## {title}", ""]
    for seg in segments:
        lines += [
            f"### Segment {seg['id']} (ordinal {seg['ordinal']})",
            f"Participants: {', '.join(seg['participant_ids'])}",
            "",
            seg["content"],
            "",
        ]
    return "\n".join(lines), "text/markdown; charset=utf-8", payload


def list_audit_events(conn: sqlite3.Connection, limit: int = 200) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    result = []
    for row in rows:
        item = row_dict(row) or {}
        item["detail"] = json.loads(item.pop("detail_json") or "{}")
        result.append(item)
    return result
