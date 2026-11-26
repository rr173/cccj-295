"""Collaborative proofreading (校稿) for interviews.

Design summary
--------------

* A session selects a set of *currently visible* segments of one interview.
  At most one session per interview may be ``open`` (enforced by a partial
  unique index).
* Draft text is versioned.  Version 1 is a snapshot of the official
  ``segments.content`` at session creation; every merged proposal creates the
  next version.  Official bodies are untouched until an explicit publish.
* A proposal is interpreted against one base version.  Ranges are zero-based
  Unicode code-point half-open intervals.  Proposals based on an older version
  are rebased through the *actual stored edit operations* of every later
  version, so a non-conflicting proposal merges into the current draft instead
  of overwriting other people's changes.
* Conflicts are defined by the operations that actually happened (never by a
  text diff), so undoing text does not erase a historical conflict:
    - two non-empty ranges overlap,
    - an insertion falls strictly inside another non-empty range,
    - two insertions fall on the same point.
  Touching endpoints does not conflict; an insertion at a range endpoint stays
  outside the replacement text.
* Annotations are bound to a non-empty range of a specific version.  Edits
  that avoid the annotated text shift the anchor; any edit that replaces or
  deletes part of it, or inserts strictly inside it, loses the anchor.  Lost
  anchors are never guessed.
* Publishing is one ``BEGIN IMMEDIATE`` transaction that rewrites every
  publishable segment body, so ordinary reads/searches/exports can only observe
  the complete old or the complete new set.
* When a segment body is physically purged, draft history, replacement text and
  annotation text for that segment are removed in the same transaction (see
  :func:`remove_segment_content_locked`).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Iterable

from .db import immediate_transaction, utcnow
from .services import (
    ApiError,
    _normal_visible_predicate,
    audit_locked,
    new_id,
)

OPEN = "open"
PUBLISHED = "published"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _require_session(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM proofreading_sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        raise ApiError(
            404,
            "proofreading_session_not_found",
            f"Proofreading session {session_id!r} was not found",
        )
    return row


def _require_open(session: sqlite3.Row) -> None:
    if session["status"] != OPEN:
        raise ApiError(
            409,
            "proofreading_session_closed",
            "This proofreading session has already been published and accepts no further changes",
            {"session_id": session["id"], "status": session["status"]},
        )


def _visible_segment(conn: sqlite3.Connection, segment_id: str) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT s.* FROM segments s WHERE s.id = ? AND {_normal_visible_predicate('s')}",
        (segment_id,),
    ).fetchone()


def _in_scope_segment_ids(conn: sqlite3.Connection, session_id: str) -> list[str]:
    rows = conn.execute(
        f"""
        SELECT ss.segment_id, ss.ordinal
        FROM proofreading_session_segments ss
        JOIN segments s ON s.id = ss.segment_id
        WHERE ss.session_id = ? AND {_normal_visible_predicate('s')}
        ORDER BY ss.ordinal, ss.segment_id
        """,
        (session_id,),
    ).fetchall()
    return [r["segment_id"] for r in rows]


def _snapshot(
    conn: sqlite3.Connection, session_id: str, segment_id: str, version: int
) -> str | None:
    row = conn.execute(
        """
        SELECT content FROM proofreading_segment_contents
        WHERE session_id = ? AND segment_id = ? AND version = ?
        """,
        (session_id, segment_id, version),
    ).fetchone()
    return row["content"] if row is not None else None


def _version_exists(conn: sqlite3.Connection, session_id: str, version: int) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM proofreading_versions WHERE session_id = ? AND version = ?",
            (session_id, version),
        ).fetchone()
        is not None
    )


# ---------------------------------------------------------------------------
# Conflict detection and coordinate mapping
# ---------------------------------------------------------------------------


def _conflict(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """Return True when two changes conflict under the product rules."""
    a_empty = a_start == a_end
    b_empty = b_start == b_end
    if a_empty and b_empty:
        # Two insertions at the same point.
        return a_start == b_start
    if a_empty:
        # Insertion strictly inside the other non-empty range.
        return b_start < a_start < b_end
    if b_empty:
        return a_start < b_start < a_end
    # Proper overlap of two non-empty ranges.  Touching endpoints is fine.
    return max(a_start, b_start) < min(a_end, b_end)


def _map_start(pos: int, applied: Iterable[tuple[int, int, int]]) -> int:
    """Map a range start / insertion point into the next version's coordinates.

    Every applied change whose end is at or before ``pos`` has already shifted
    the text.  An insertion exactly at ``pos`` is included, so the mapped range
    starts *after* the inserted text (the insertion stays outside the target).
    """
    shift = 0
    for start, end, repl_len in applied:
        if end <= pos:
            shift += repl_len - (end - start)
    return pos + shift


def _map_end(pos: int, applied: Iterable[tuple[int, int, int]]) -> int:
    """Map a range end into the next version's coordinates.

    Non-empty changes ending at or before ``pos`` shift it.  Insertions strictly
    before ``pos`` shift it; an insertion exactly at ``pos`` is excluded so it
    stays outside the target range.
    """
    shift = 0
    for start, end, repl_len in applied:
        if start == end:
            if start < pos:
                shift += repl_len
        elif end <= pos:
            shift += repl_len - (end - start)
    return pos + shift


def _apply_edits(text: str, edits: list[tuple[int, int, str]]) -> str:
    """Apply mutually non-conflicting edits interpreted on the same text.

    Sorting by ``(start, end)`` places a zero-width insertion *before* a
    non-empty change that begins at the same point.  This matches the coordinate
    mapping, where an insertion at a range's start endpoint stays outside the
    replacement text.
    """
    ordered = sorted(edits, key=lambda e: (e[0], e[1]))
    out: list[str] = []
    pos = 0
    for start, end, replacement in ordered:
        if start < pos:
            # Endpoint-touching changes may share a coordinate; a degenerate
            # overlap can never be produced by a merged proposal.
            raise ApiError(
                500,
                "proofreading_internal_overlap",
                "Internal edit ordering invariant violated",
            )
        out.append(text[pos:start])
        out.append(replacement)
        pos = end
    out.append(text[pos:])
    return "".join(out)


# ---------------------------------------------------------------------------
# Selection / session creation
# ---------------------------------------------------------------------------


def open_session(
    conn: sqlite3.Connection,
    *,
    interview_id: str,
    segment_ids: list[str],
    created_by: str = "project-owner",
) -> dict[str, Any]:
    unique_ids = list(dict.fromkeys(segment_ids))
    if not unique_ids:
        raise ApiError(
            422, "proofreading_no_segments", "At least one segment must be selected"
        )
    now = utcnow()
    with immediate_transaction(conn):
        if (
            conn.execute("SELECT 1 FROM interviews WHERE id = ?", (interview_id,)).fetchone()
            is None
        ):
            raise ApiError(
                404, "interview_not_found", f"Interview {interview_id!r} was not found"
            )
        existing = conn.execute(
            "SELECT id FROM proofreading_sessions WHERE interview_id = ? AND status = 'open'",
            (interview_id,),
        ).fetchone()
        if existing is not None:
            raise ApiError(
                409,
                "proofreading_session_exists",
                "This interview already has an open proofreading session",
                {"session_id": existing["id"], "interview_id": interview_id},
            )

        selected: list[sqlite3.Row] = []
        rejected: list[dict[str, Any]] = []
        for sid in unique_ids:
            row = conn.execute("SELECT * FROM segments WHERE id = ?", (sid,)).fetchone()
            if row is None:
                rejected.append({"segment_id": sid, "reason": "not_found"})
                continue
            if row["interview_id"] != interview_id:
                rejected.append({"segment_id": sid, "reason": "different_interview"})
                continue
            if _visible_segment(conn, sid) is None:
                rejected.append({"segment_id": sid, "reason": "not_visible"})
                continue
            selected.append(row)
        if rejected:
            raise ApiError(
                409,
                "proofreading_segment_not_selectable",
                "Every selected segment must exist in this interview and be visible through ordinary reads",
                {"segments": rejected},
            )

        session_id = new_id("prf")
        try:
            conn.execute(
                """
                INSERT INTO proofreading_sessions
                    (id, interview_id, status, current_version, created_by, created_at, updated_at)
                VALUES (?, ?, 'open', 1, ?, ?, ?)
                """,
                (session_id, interview_id, created_by, now, now),
            )
        except sqlite3.IntegrityError:
            # The partial unique index is the final authority for "one open
            # session per interview" even if an earlier check raced.
            raise ApiError(
                409,
                "proofreading_session_exists",
                "This interview already has an open proofreading session",
                {"interview_id": interview_id},
            )
        conn.execute(
            """
            INSERT INTO proofreading_versions (session_id, version, submission_id, created_at)
            VALUES (?, 1, NULL, ?)
            """,
            (session_id, now),
        )
        for row in selected:
            conn.execute(
                """
                INSERT INTO proofreading_session_segments (session_id, segment_id, ordinal)
                VALUES (?, ?, ?)
                """,
                (session_id, row["id"], row["ordinal"]),
            )
            conn.execute(
                """
                INSERT INTO proofreading_segment_contents
                    (session_id, segment_id, version, content)
                VALUES (?, ?, 1, ?)
                """,
                (session_id, row["id"], row["content"]),
            )
        audit_locked(
            conn,
            "proofreading_session",
            session_id,
            "proofreading.session_opened",
            {"interview_id": interview_id, "segment_ids": [r["id"] for r in selected]},
        )
    return session_detail(conn, session_id)


def list_sessions(
    conn: sqlite3.Connection, *, interview_id: str | None = None, status: str | None = None
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if interview_id:
        where.append("interview_id = ?")
        params.append(interview_id)
    if status:
        where.append("status = ?")
        params.append(status)
    sql = "SELECT * FROM proofreading_sessions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at, id"
    rows = conn.execute(sql, params).fetchall()
    return [session_summary(conn, r) for r in rows]


def session_summary(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "interview_id": row["interview_id"],
        "status": row["status"],
        "current_version": row["current_version"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "published_version": row["published_version"],
        "published_at": row["published_at"],
        "publishable_segment_ids": _in_scope_segment_ids(conn, row["id"]),
    }


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------


def annotation_public(row: sqlite3.Row) -> dict[str, Any]:
    anchored = bool(row["anchored"])
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "segment_id": row["segment_id"],
        "text": row["text"],
        "anchored": anchored,
        "anchor_status": "anchored" if anchored else "lost",
        "start": row["start_pos"] if anchored else None,
        "end": row["end_pos"] if anchored else None,
        "anchor_version": row["anchor_version"],
        "resolved": bool(row["resolved"]),
        "resolved_at": row["resolved_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _annotation_rows(
    conn: sqlite3.Connection, session_id: str, *, in_scope_only: bool = True
) -> list[sqlite3.Row]:
    sql = """
        SELECT a.* FROM proofreading_annotations a
        WHERE a.session_id = ?
    """
    params: list[Any] = [session_id]
    if in_scope_only:
        sql += """ AND EXISTS (
            SELECT 1 FROM segments s
            WHERE s.id = a.segment_id AND """
        sql += _normal_visible_predicate("s")
        sql += ")"
    sql += " ORDER BY a.created_at, a.id"
    return conn.execute(sql, params).fetchall()


def list_annotations(
    conn: sqlite3.Connection, session_id: str, *, include_out_of_scope: bool = False
) -> list[dict[str, Any]]:
    _require_session(conn, session_id)
    rows = _annotation_rows(conn, session_id, in_scope_only=not include_out_of_scope)
    return [annotation_public(r) for r in rows]


def _require_annotation(
    conn: sqlite3.Connection, session_id: str, annotation_id: str
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM proofreading_annotations WHERE id = ? AND session_id = ?",
        (annotation_id, session_id),
    ).fetchone()
    if row is None:
        raise ApiError(
            404,
            "proofreading_annotation_not_found",
            f"Annotation {annotation_id!r} was not found in this session",
        )
    return row


def create_annotation(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    segment_id: str,
    start: int,
    end: int,
    text: str,
) -> dict[str, Any]:
    if not text:
        raise ApiError(422, "proofreading_annotation_text_required", "Annotation text is required")
    now = utcnow()
    with immediate_transaction(conn):
        session = _require_session(conn, session_id)
        _require_open(session)
        if segment_id not in _in_scope_segment_ids(conn, session_id):
            raise ApiError(
                409,
                "proofreading_segment_out_of_scope",
                "The segment is not in the visible proofreading scope",
                {"segment_id": segment_id},
            )
        current = session["current_version"]
        content = _snapshot(conn, session_id, segment_id, current)
        if content is None:
            raise ApiError(
                409,
                "proofreading_segment_out_of_scope",
                "The segment has no draft text in the current version",
                {"segment_id": segment_id},
            )
        if start < 0 or end <= start or end > len(content):
            raise ApiError(
                422,
                "proofreading_range_invalid",
                "Annotations require a non-empty range inside the current draft text",
                {"start": start, "end": end, "text_length": len(content)},
            )
        annotation_id = new_id("ann")
        conn.execute(
            """
            INSERT INTO proofreading_annotations
                (id, session_id, segment_id, text, start_pos, end_pos, anchored,
                 resolved, anchor_version, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?, ?, ?)
            """,
            (annotation_id, session_id, segment_id, text, start, end, current, now, now),
        )
        audit_locked(
            conn,
            "proofreading_annotation",
            annotation_id,
            "proofreading.annotation_created",
            {"session_id": session_id, "segment_id": segment_id, "start": start, "end": end},
        )
        row = conn.execute(
            "SELECT * FROM proofreading_annotations WHERE id = ?", (annotation_id,)
        ).fetchone()
        return annotation_public(row)


def reanchor_annotation(
    conn: sqlite3.Connection,
    session_id: str,
    annotation_id: str,
    *,
    start: int,
    end: int,
) -> dict[str, Any]:
    now = utcnow()
    with immediate_transaction(conn):
        session = _require_session(conn, session_id)
        _require_open(session)
        row = _require_annotation(conn, session_id, annotation_id)
        if row["resolved"]:
            raise ApiError(
                409,
                "proofreading_annotation_resolved",
                "A resolved annotation cannot be re-anchored",
                {"annotation_id": annotation_id},
            )
        segment_id = row["segment_id"]
        if segment_id not in _in_scope_segment_ids(conn, session_id):
            raise ApiError(
                409,
                "proofreading_segment_out_of_scope",
                "The segment is not in the visible proofreading scope",
                {"segment_id": segment_id},
            )
        current = session["current_version"]
        content = _snapshot(conn, session_id, segment_id, current)
        if content is None:
            raise ApiError(
                409,
                "proofreading_segment_out_of_scope",
                "The segment has no draft text in the current version",
                {"segment_id": segment_id},
            )
        if start < 0 or end <= start or end > len(content):
            raise ApiError(
                422,
                "proofreading_range_invalid",
                "Re-anchoring requires a non-empty range inside the current draft text",
                {"start": start, "end": end, "text_length": len(content)},
            )
        conn.execute(
            """
            UPDATE proofreading_annotations
            SET start_pos = ?, end_pos = ?, anchored = 1, anchor_version = ?, updated_at = ?
            WHERE id = ?
            """,
            (start, end, current, now, annotation_id),
        )
        audit_locked(
            conn,
            "proofreading_annotation",
            annotation_id,
            "proofreading.annotation_reanchored",
            {"session_id": session_id, "segment_id": segment_id, "start": start, "end": end},
        )
        updated = conn.execute(
            "SELECT * FROM proofreading_annotations WHERE id = ?", (annotation_id,)
        ).fetchone()
        return annotation_public(updated)


def resolve_annotation(
    conn: sqlite3.Connection, session_id: str, annotation_id: str
) -> dict[str, Any]:
    now = utcnow()
    with immediate_transaction(conn):
        _require_session(conn, session_id)
        row = _require_annotation(conn, session_id, annotation_id)
        if not row["resolved"]:
            conn.execute(
                """
                UPDATE proofreading_annotations
                SET resolved = 1, resolved_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, annotation_id),
            )
            audit_locked(
                conn,
                "proofreading_annotation",
                annotation_id,
                "proofreading.annotation_resolved",
                {"session_id": session_id, "segment_id": row["segment_id"]},
            )
        updated = conn.execute(
            "SELECT * FROM proofreading_annotations WHERE id = ?", (annotation_id,)
        ).fetchone()
        return annotation_public(updated)


# ---------------------------------------------------------------------------
# Proposal merging
# ---------------------------------------------------------------------------


def _fingerprint(base_version: int, edits: list[dict[str, Any]]) -> str:
    canonical = json.dumps(
        {
            "base_version": base_version,
            "edits": sorted(
                (
                    {
                        "segment_id": e["segment_id"],
                        "start": e["start"],
                        "end": e["end"],
                        "replacement": e["replacement"],
                    }
                    for e in edits
                ),
                key=lambda e: (e["segment_id"], e["start"], e["end"], e["replacement"]),
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _stored_submission(
    conn: sqlite3.Connection, session_id: str, submission_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM proofreading_submissions WHERE session_id = ? AND submission_id = ?",
        (session_id, submission_id),
    ).fetchone()


def _record_submission(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    submission_id: str,
    fingerprint: str,
    status: str,
    base_version: int,
    result: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO proofreading_submissions
            (session_id, submission_id, fingerprint, status, base_version, result_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            submission_id,
            fingerprint,
            status,
            base_version,
            json.dumps(result, ensure_ascii=False, sort_keys=True),
            utcnow(),
        ),
    )


def submit_proposal(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    submission_id: str,
    base_version: int,
    edits: list[dict[str, Any]],
) -> dict[str, Any]:
    """Merge one proposal.  Raises :class:`ApiError` when it is rejected."""
    fingerprint = _fingerprint(base_version, edits)
    rejection: ApiError | None = None
    result: dict[str, Any] | None = None

    with immediate_transaction(conn):
        session = _require_session(conn, session_id)

        stored = _stored_submission(conn, session_id, submission_id)
        if stored is not None:
            if stored["fingerprint"] != fingerprint:
                raise ApiError(
                    409,
                    "submission_id_reused",
                    "This submission ID was already used for a different proposal",
                    {"submission_id": submission_id, "base_version": stored["base_version"]},
                )
            replay = json.loads(stored["result_json"])
            replay["idempotent_replay"] = True
            if stored["status"] == "merged":
                return replay
            raise ApiError(
                409,
                "proofreading_merge_conflict",
                "This submission was rejected as conflicting with merged changes",
                replay.get("conflict", {}),
            )

        _require_open(session)
        current = session["current_version"]

        # --- validate base version and ranges -----------------------------
        if base_version < 1 or base_version > current or not _version_exists(
            conn, session_id, base_version
        ):
            raise ApiError(
                422,
                "proofreading_version_not_found",
                "The proposal refers to a draft version that does not exist",
                {"base_version": base_version, "current_version": current},
            )

        scope_ids = set(_in_scope_segment_ids(conn, session_id))
        out_of_scope: list[str] = []
        base_texts: dict[str, str] = {}
        normalized: list[dict[str, Any]] = []
        for raw in edits:
            sid = raw["segment_id"]
            start = int(raw["start"])
            end = int(raw["end"])
            replacement = raw.get("replacement", "")
            if sid not in scope_ids:
                if sid not in out_of_scope:
                    out_of_scope.append(sid)
                continue
            text = _snapshot(conn, session_id, sid, base_version)
            if text is None:
                if sid not in out_of_scope:
                    out_of_scope.append(sid)
                continue
            if start < 0 or end < start or end > len(text):
                raise ApiError(
                    422,
                    "proofreading_range_invalid",
                    "A proposal range is outside the base version text",
                    {
                        "segment_id": sid,
                        "start": start,
                        "end": end,
                        "text_length": len(text),
                    },
                )
            base_texts.setdefault(sid, text)
            normalized.append(
                {"segment_id": sid, "start": start, "end": end, "replacement": replacement}
            )

        if out_of_scope:
            raise ApiError(
                409,
                "proofreading_segment_out_of_scope",
                "A proposal may only touch segments that remain visible in this session",
                {"segments": out_of_scope},
            )

        if not normalized:
            raise ApiError(
                422, "proofreading_no_edits", "A proposal must contain at least one edit"
            )

        # --- conflicts inside the same proposal (same base coordinates) ---
        conflict_segments: set[str] = set()
        by_segment: dict[str, list[dict[str, Any]]] = {}
        for edit in normalized:
            by_segment.setdefault(edit["segment_id"], []).append(edit)
        for sid, seg_edits in by_segment.items():
            for i in range(len(seg_edits)):
                for j in range(i + 1, len(seg_edits)):
                    a, b = seg_edits[i], seg_edits[j]
                    if _conflict(a["start"], a["end"], b["start"], b["end"]):
                        conflict_segments.add(sid)

        # --- rebase through every later version ---------------------------
        mapped: dict[str, list[dict[str, Any]]] = {}
        if not conflict_segments:
            for sid, seg_edits in by_segment.items():
                mapped_edits: list[dict[str, Any]] = []
                for edit in seg_edits:
                    cur_start, cur_end = edit["start"], edit["end"]
                    conflicted = False
                    for version in range(base_version + 1, current + 1):
                        applied_rows = conn.execute(
                            """
                            SELECT start_pos, end_pos, replacement_text
                            FROM proofreading_edits
                            WHERE session_id = ? AND version = ? AND segment_id = ?
                            ORDER BY id
                            """,
                            (session_id, version, sid),
                        ).fetchall()
                        applied = [
                            (r["start_pos"], r["end_pos"], len(r["replacement_text"]))
                            for r in applied_rows
                        ]
                        for a_start, a_end, _len in applied:
                            if _conflict(cur_start, cur_end, a_start, a_end):
                                conflicted = True
                                break
                        if conflicted:
                            break
                        cur_start = _map_start(cur_start, applied)
                        cur_end = _map_end(cur_end, applied)
                    if conflicted:
                        conflict_segments.add(sid)
                        break
                    mapped_edits.append(
                        {
                            "start": cur_start,
                            "end": cur_end,
                            "replacement": edit["replacement"],
                            "base_start": edit["start"],
                            "base_end": edit["end"],
                        }
                    )
                if conflicted:
                    break
                mapped[sid] = mapped_edits

        # Defensive re-check in the current coordinate space.  Mapping is
        # order-preserving, so a conflict here should be unreachable.
        if not conflict_segments:
            for sid, seg_edits in mapped.items():
                for i in range(len(seg_edits)):
                    for j in range(i + 1, len(seg_edits)):
                        a, b = seg_edits[i], seg_edits[j]
                        if _conflict(a["start"], a["end"], b["start"], b["end"]):
                            conflict_segments.add(sid)

        if conflict_segments:
            conflict_payload = {
                "submission_id": submission_id,
                "status": "rejected",
                "base_version": base_version,
                "current_version": current,
                "conflict": {
                    "segments": sorted(conflict_segments),
                    "rule": "overlap, insertion inside another change, or duplicate insertion point",
                },
                "merged": False,
            }
            _record_submission(
                conn,
                session_id=session_id,
                submission_id=submission_id,
                fingerprint=fingerprint,
                status="rejected",
                base_version=base_version,
                result=conflict_payload,
            )
            audit_locked(
                conn,
                "proofreading_session",
                session_id,
                "proofreading.proposal_conflict",
                {
                    "submission_id": submission_id,
                    "base_version": base_version,
                    "segments": sorted(conflict_segments),
                },
            )
            rejection = ApiError(
                409,
                "proofreading_merge_conflict",
                "The proposal conflicts with changes merged after its base version; nothing was merged",
                conflict_payload["conflict"],
            )
        else:
            result = _merge_locked(
                conn,
                session=session,
                submission_id=submission_id,
                base_version=base_version,
                fingerprint=fingerprint,
                mapped=mapped,
                base_texts=base_texts,
            )

    if rejection is not None:
        raise rejection
    assert result is not None
    return result


def _merge_locked(
    conn: sqlite3.Connection,
    *,
    session: sqlite3.Row,
    submission_id: str,
    base_version: int,
    fingerprint: str,
    mapped: dict[str, list[dict[str, Any]]],
    base_texts: dict[str, str],
) -> dict[str, Any]:
    session_id = session["id"]
    current = session["current_version"]
    new_version = current + 1
    now = utcnow()

    segment_results: list[dict[str, Any]] = []

    # Snapshot every in-scope segment at the new version; apply edits to the
    # changed ones.  Out-of-scope segments are intentionally not extended.
    for sid in _in_scope_segment_ids(conn, session_id):
        if sid in mapped:
            previous = _snapshot(conn, session_id, sid, current)
            if previous is None:
                # Cannot happen for an in-scope segment, but fail closed.
                raise ApiError(
                    409,
                    "proofreading_segment_out_of_scope",
                    "The segment has no draft text in the current version",
                    {"segment_id": sid},
                )
            edits_tuples = [
                (e["start"], e["end"], e["replacement"]) for e in mapped[sid]
            ]
            new_text = _apply_edits(previous, edits_tuples)
            segment_results.append(
                {
                    "segment_id": sid,
                    "version": new_version,
                    "text": new_text,
                    "edits": [
                        {
                            "start": e["start"],
                            "end": e["end"],
                            "replacement": e["replacement"],
                            "base_start": e["base_start"],
                            "base_end": e["base_end"],
                        }
                        for e in sorted(mapped[sid], key=lambda e: (e["start"], e["end"]))
                    ],
                }
            )
        else:
            previous = _snapshot(conn, session_id, sid, current)
            if previous is None:
                continue
            new_text = previous
        conn.execute(
            """
            INSERT INTO proofreading_segment_contents (session_id, segment_id, version, content)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, sid, new_version, new_text),
        )

    # Persist the actual applied operations in pre-merge coordinates.
    for sid, edits in mapped.items():
        for order, edit in enumerate(sorted(edits, key=lambda e: (e["start"], e["end"]))):
            conn.execute(
                """
                INSERT INTO proofreading_edits
                    (session_id, version, segment_id, ordinal, start_pos, end_pos, replacement_text)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    new_version,
                    sid,
                    order,
                    edit["start"],
                    edit["end"],
                    edit["replacement"],
                ),
            )

    # Update unresolved anchored annotations on changed segments.
    for sid, edits in mapped.items():
        applied = [(e["start"], e["end"], len(e["replacement"])) for e in edits]
        annotations = conn.execute(
            """
            SELECT * FROM proofreading_annotations
            WHERE session_id = ? AND segment_id = ? AND resolved = 0 AND anchored = 1
            """,
            (session_id, sid),
        ).fetchall()
        for ann in annotations:
            ann_start, ann_end = ann["start_pos"], ann["end_pos"]
            lost = any(
                _conflict(ann_start, ann_end, a_start, a_end)
                for a_start, a_end, _len in applied
            )
            if lost:
                conn.execute(
                    """
                    UPDATE proofreading_annotations
                    SET anchored = 0, start_pos = NULL, end_pos = NULL,
                        anchor_version = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (new_version, now, ann["id"]),
                )
            else:
                new_start = _map_start(ann_start, applied)
                new_end = _map_end(ann_end, applied)
                conn.execute(
                    """
                    UPDATE proofreading_annotations
                    SET start_pos = ?, end_pos = ?, anchor_version = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (new_start, new_end, new_version, now, ann["id"]),
                )

    conn.execute(
        "INSERT INTO proofreading_versions (session_id, version, submission_id, created_at) VALUES (?, ?, ?, ?)",
        (session_id, new_version, submission_id, now),
    )
    conn.execute(
        "UPDATE proofreading_sessions SET current_version = ?, updated_at = ? WHERE id = ?",
        (new_version, now, session_id),
    )

    result: dict[str, Any] = {
        "submission_id": submission_id,
        "status": "merged",
        "base_version": base_version,
        "merged_version": new_version,
        "current_version": new_version,
        "rebased": base_version < current,
        "idempotent_replay": False,
        "segments": sorted(segment_results, key=lambda s: s["segment_id"]),
    }
    _record_submission(
        conn,
        session_id=session_id,
        submission_id=submission_id,
        fingerprint=fingerprint,
        status="merged",
        base_version=base_version,
        result=result,
    )
    audit_locked(
        conn,
        "proofreading_session",
        session_id,
        "proofreading.proposal_merged",
        {
            "submission_id": submission_id,
            "base_version": base_version,
            "merged_version": new_version,
            "segment_ids": sorted(mapped),
            "rebased": base_version < current,
        },
    )
    return result


# ---------------------------------------------------------------------------
# Reading the draft
# ---------------------------------------------------------------------------


def _draft_segment_payload(
    conn: sqlite3.Connection, session: sqlite3.Row, segment_id: str, version: int
) -> dict[str, Any] | None:
    text = _snapshot(conn, session["id"], segment_id, version)
    if text is None:
        return None
    row = conn.execute(
        "SELECT ordinal FROM proofreading_session_segments WHERE session_id = ? AND segment_id = ?",
        (session["id"], segment_id),
    ).fetchone()
    # Annotation coordinates always refer to the current version.  Historical
    # version reads therefore list no annotations rather than misleading ones.
    annotations: list[dict[str, Any]] = []
    if version == session["current_version"]:
        annotations = [
            annotation_public(a)
            for a in conn.execute(
                """
                SELECT * FROM proofreading_annotations
                WHERE session_id = ? AND segment_id = ?
                ORDER BY created_at, id
                """,
                (session["id"], segment_id),
            ).fetchall()
        ]
    return {
        "segment_id": segment_id,
        "ordinal": row["ordinal"] if row is not None else None,
        "text": text,
        "text_length": len(text),
        "annotations": annotations,
    }


def session_detail(
    conn: sqlite3.Connection, session_id: str, *, version: int | None = None
) -> dict[str, Any]:
    session = _require_session(conn, session_id)
    current = session["current_version"]
    requested = current if version is None else version
    if requested < 1 or requested > current or not _version_exists(conn, session_id, requested):
        raise ApiError(
            404,
            "proofreading_version_not_found",
            "The requested draft version does not exist",
            {"version": requested, "current_version": current},
        )
    in_scope = _in_scope_segment_ids(conn, session_id)
    segments = []
    for sid in in_scope:
        payload = _draft_segment_payload(conn, session, sid, requested)
        if payload is not None:
            segments.append(payload)

    unresolved = [
        annotation_public(a)
        for a in _annotation_rows(conn, session_id)
        if not a["resolved"]
    ]
    return {
        "id": session["id"],
        "interview_id": session["interview_id"],
        "status": session["status"],
        "current_version": current,
        "version": requested,
        "created_by": session["created_by"],
        "created_at": session["created_at"],
        "updated_at": session["updated_at"],
        "published_version": session["published_version"],
        "published_at": session["published_at"],
        "segments": segments,
        "publishable_segment_ids": in_scope,
        "unresolved_annotations": unresolved,
        "unresolved_annotation_count": len(unresolved),
        "can_publish": (
            session["status"] == OPEN
            and requested == current
            and bool(in_scope)
            and not unresolved
        ),
    }


def list_versions(conn: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
    _require_session(conn, session_id)
    rows = conn.execute(
        "SELECT * FROM proofreading_versions WHERE session_id = ? ORDER BY version",
        (session_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_version_draft(
    conn: sqlite3.Connection, session_id: str, version: int
) -> dict[str, Any]:
    return session_detail(conn, session_id, version=version)


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------


def publish(conn: sqlite3.Connection, session_id: str, *, version: int) -> dict[str, Any]:
    with immediate_transaction(conn):
        session = _require_session(conn, session_id)
        _require_open(session)
        current = session["current_version"]
        if version != current:
            raise ApiError(
                409,
                "proofreading_version_mismatch",
                "Publish must name the current draft version",
                {"version": version, "current_version": current},
            )

        in_scope = _in_scope_segment_ids(conn, session_id)
        if not in_scope:
            raise ApiError(
                409,
                "proofreading_no_publishable_segments",
                "No selected segment remains visible and publishable",
                {"session_id": session_id},
            )

        blocking = [
            annotation_public(a)
            for a in _annotation_rows(conn, session_id)
            if not a["resolved"]
        ]
        if blocking:
            raise ApiError(
                409,
                "proofreading_unresolved_annotations",
                "Unresolved annotations still block publication, whether or not they are anchored",
                {
                    "annotations": [
                        {
                            "id": a["id"],
                            "segment_id": a["segment_id"],
                            "anchored": a["anchored"],
                            "anchor_status": a["anchor_status"],
                        }
                        for a in blocking
                    ],
                    "annotation_ids": [a["id"] for a in blocking],
                },
            )

        published: list[str] = []
        missing_snapshot: list[str] = []
        for sid in in_scope:
            text = _snapshot(conn, session_id, sid, current)
            if text is None:
                missing_snapshot.append(sid)
                continue
            # Re-check scope inside the write transaction: a concurrent purge
            # request must make the segment un-publishable, not partially so.
            if _visible_segment(conn, sid) is None:
                continue
            conn.execute("UPDATE segments SET content = ? WHERE id = ?", (text, sid))
            published.append(sid)
        if missing_snapshot and not published:
            raise ApiError(
                409,
                "proofreading_no_publishable_segments",
                "No selected segment remains visible and publishable",
                {"segments": missing_snapshot},
            )
        if not published:
            raise ApiError(
                409,
                "proofreading_no_publishable_segments",
                "No selected segment remains visible and publishable",
                {"session_id": session_id},
            )

        now = utcnow()
        conn.execute(
            """
            UPDATE proofreading_sessions
            SET status = 'published', published_version = ?, published_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (version, now, now, session_id),
        )
        audit_locked(
            conn,
            "proofreading_session",
            session_id,
            "proofreading.published",
            {
                "version": version,
                "segment_ids": published,
                "segment_count": len(published),
            },
        )
        updated = _require_session(conn, session_id)
        detail = session_detail(conn, session_id)
        detail["published_segment_ids"] = published
        detail["published_version"] = updated["published_version"]
        return detail


# ---------------------------------------------------------------------------
# Deletion integration
# ---------------------------------------------------------------------------


def _scrub_submission_results_locked(conn: sqlite3.Connection, segment_id: str) -> None:
    """Remove a purged segment's text/replacement from stored merge results.

    Submission rows keep only a payload fingerprint plus the stored outcome.
    The outcome may echo draft text and replacement strings, so those entries
    for the purged segment must be removed.  The idempotent replay then returns
    the remaining, content-free result instead of resurrecting purged text.
    """
    rows = conn.execute(
        "SELECT rowid, result_json FROM proofreading_submissions"
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["result_json"])
        except (TypeError, ValueError):
            continue
        changed = False
        if isinstance(payload, dict):
            segments = payload.get("segments")
            if isinstance(segments, list):
                kept = [s for s in segments if s.get("segment_id") != segment_id]
                if len(kept) != len(segments):
                    payload["segments"] = kept
                    redacted = payload.setdefault("redacted_segment_ids", [])
                    if segment_id not in redacted:
                        redacted.append(segment_id)
                    payload["redacted"] = True
                    changed = True
            conflict = payload.get("conflict")
            if isinstance(conflict, dict):
                # Conflict details contain segment IDs only, but keep them
                # consistent when the segment disappears.
                pass
        if changed:
            conn.execute(
                "UPDATE proofreading_submissions SET result_json = ? WHERE rowid = ?",
                (json.dumps(payload, ensure_ascii=False, sort_keys=True), row["rowid"]),
            )


def remove_segment_content_locked(conn: sqlite3.Connection, segment_id: str) -> None:
    """Delete new-feature text for a physically purged segment.

    Must be called inside the same immediate transaction that deletes the
    segment row.  Removes historical draft snapshots, applied replacement text,
    annotation text and session membership, and scrubs stored merge results.
    """
    session_ids = [
        r["session_id"]
        for r in conn.execute(
            "SELECT DISTINCT session_id FROM proofreading_session_segments WHERE segment_id = ?",
            (segment_id,),
        ).fetchall()
    ]
    if not session_ids:
        # There may still be annotations/history without membership.
        has_data = conn.execute(
            """
            SELECT 1 FROM proofreading_segment_contents WHERE segment_id = ?
            UNION ALL SELECT 1 FROM proofreading_edits WHERE segment_id = ?
            UNION ALL SELECT 1 FROM proofreading_annotations WHERE segment_id = ?
            LIMIT 1
            """,
            (segment_id, segment_id, segment_id),
        ).fetchone()
        if has_data is None:
            return

    _scrub_submission_results_locked(conn, segment_id)
    conn.execute(
        "DELETE FROM proofreading_segment_contents WHERE segment_id = ?", (segment_id,)
    )
    conn.execute("DELETE FROM proofreading_edits WHERE segment_id = ?", (segment_id,))
    conn.execute("DELETE FROM proofreading_annotations WHERE segment_id = ?", (segment_id,))
    conn.execute(
        "DELETE FROM proofreading_session_segments WHERE segment_id = ?", (segment_id,)
    )
    for sid in session_ids:
        audit_locked(
            conn,
            "proofreading_session",
            sid,
            "proofreading.segment_removed_after_purge",
            {"segment_id": segment_id},
        )
