"""Interview room bookings and batch rescheduling (访谈预约 / 批量改期).

Design summary
--------------

* A **booking** registers one existing interview against a room, a fixed
  participant list and a UTC half-open ``[start_at, end_at)`` interval.  A
  partial unique index guarantees at most one non-cancelled booking per
  interview.  Two intervals are conflicting only when they *overlap*;
  back-to-back intervals that merely touch (one ends when the other starts)
  never conflict.
* A **reschedule order** proposes new rooms/times for a set of not-yet-started
  confirmed bookings without changing their participant lists.  While the
  consultation is open the original booking rows remain the confirmed result;
  the proposed arrangement exists only in ``reschedule_orders`` /
  ``reschedule_items``.
* While an order is ``pending`` it holds two kinds of slots: every original
  slot (so nobody outside the order can take them) and every proposed slot
  (including the ones a same-order booking will vacate).  New bookings and
  other pending orders therefore cannot collide with either set, while items
  inside the same order may reuse slots another item of the same order frees.
* Each affected participant answers exactly once.  The first valid answer is
  immutable; retries of the same answer are idempotent replays.  Any rejection,
  owner withdrawal, consultation deadline or cancellation of an underlying
  booking ends the order without moving anything; only unanimous acceptance
  applies the whole new arrangement in one ``BEGIN IMMEDIATE`` transaction.
* Reads run inside a deferred read transaction against one snapshot, so a
  calendar query can never observe a half-applied batch move.  Expired orders
  are settled lazily at the start of every read/mutation and again at startup,
  which releases their proposed slots even if no request arrives exactly at the
  deadline.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from .db import immediate_transaction, read_transaction, utcnow
from .services import ApiError, audit_locked, new_id

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
WITHDRAWN = "withdrawn"
EXPIRED = "expired"
CANCELLED_BOOKING = "cancelled_booking"
TERMINAL_STATUSES = {APPROVED, REJECTED, WITHDRAWN, EXPIRED, CANCELLED_BOOKING}

CONFIRMED = "confirmed"
CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# Time helpers.  Every stored timestamp is normalized to fixed-width UTC
# ISO-8601 so lexicographic ordering equals chronological ordering.
# ---------------------------------------------------------------------------


def normalize_dt(value: datetime, *, field: str) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ApiError(422, "timezone_required", f"{field} must be a timezone-aware UTC timestamp")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _overlaps(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    """Half-open interval overlap; touching endpoints do not overlap."""
    return max(start_a, start_b) < min(end_a, end_b)


# ---------------------------------------------------------------------------
# Small lookup helpers
# ---------------------------------------------------------------------------


def _require_room(conn: sqlite3.Connection, room_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
    if row is None:
        raise ApiError(404, "room_not_found", f"Room {room_id!r} was not found")
    return row


def _require_booking(conn: sqlite3.Connection, booking_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
    if row is None:
        raise ApiError(404, "booking_not_found", f"Booking {booking_id!r} was not found")
    return row


def _require_order(conn: sqlite3.Connection, order_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM reschedule_orders WHERE id = ?", (order_id,)).fetchone()
    if row is None:
        raise ApiError(404, "reschedule_order_not_found", f"Reschedule order {order_id!r} was not found")
    return row


def _booking_participant_ids(conn: sqlite3.Connection, booking_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT participant_id FROM booking_participants WHERE booking_id = ? ORDER BY participant_id",
        (booking_id,),
    ).fetchall()
    return [r["participant_id"] for r in rows]


def _order_participant_ids(conn: sqlite3.Connection, order_id: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT bp.participant_id
        FROM reschedule_items ri
        JOIN booking_participants bp ON bp.booking_id = ri.booking_id
        WHERE ri.order_id = ?
        ORDER BY bp.participant_id
        """,
        (order_id,),
    ).fetchall()
    return [r["participant_id"] for r in rows]


def _intervals_conflict(
    intervals: Iterable[tuple[str, str, str, str]],
) -> tuple[str, str] | None:
    """Check room-keyed and participant-less interval pairs for self overlap.

    Each element is ``(resource_key, label, start_at, end_at)``.  Returns the
    labels of the first conflicting pair or ``None`` when all are compatible.
    """
    seen: list[tuple[str, str, str, str]] = []
    for key, label, start, end in intervals:
        for other_key, other_label, other_start, other_end in seen:
            if key == other_key and _overlaps(start, end, other_start, other_end):
                return label, other_label
        seen.append((key, label, start, end))
    return None


# ---------------------------------------------------------------------------
# Rooms
# ---------------------------------------------------------------------------


def create_room(conn: sqlite3.Connection, *, room_id: str, label: str) -> dict[str, Any]:
    now = utcnow()
    with immediate_transaction(conn):
        if conn.execute("SELECT 1 FROM rooms WHERE id = ?", (room_id,)).fetchone():
            raise ApiError(409, "room_exists", f"Room {room_id!r} already exists")
        conn.execute(
            "INSERT INTO rooms (id, label, created_at) VALUES (?, ?, ?)",
            (room_id, label, now),
        )
        audit_locked(conn, "room", room_id, "room.created", {"label": label})
    return dict(conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone())


def list_rooms(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    with read_transaction(conn):
        rows = conn.execute("SELECT * FROM rooms ORDER BY id").fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Bookings
# ---------------------------------------------------------------------------


def _confirmed_booking_rows(
    conn: sqlite3.Connection, *, room_id: str | None = None, participant_id: str | None = None
) -> list[sqlite3.Row]:
    sql = """
        SELECT DISTINCT b.*
        FROM bookings b
        LEFT JOIN booking_participants bp ON bp.booking_id = b.id
        WHERE b.status = 'confirmed'
    """
    params: list[Any] = []
    if room_id is not None:
        sql += " AND b.room_id = ?"
        params.append(room_id)
    if participant_id is not None:
        sql += " AND bp.participant_id = ?"
        params.append(participant_id)
    sql += " ORDER BY b.start_at, b.id"
    return conn.execute(sql, params).fetchall()


def _pending_hold_intervals(
    conn: sqlite3.Connection, *, exclude_order_id: str | None = None
) -> list[dict[str, str]]:
    """Room-keyed slots held by pending consultations: original and proposed."""
    where = "WHERE ro.status = 'pending'"
    params: list[Any] = []
    if exclude_order_id is not None:
        where += " AND ro.id != ?"
        params.append(exclude_order_id)
    rows = conn.execute(
        f"""
        SELECT ro.id AS order_id, ri.booking_id,
               ri.original_room_id AS room_id,
               ri.original_start_at AS start_at,
               ri.original_end_at AS end_at,
               'original' AS slot_kind
        FROM reschedule_orders ro
        JOIN reschedule_items ri ON ri.order_id = ro.id
        {where}
        UNION ALL
        SELECT ro.id AS order_id, ri.booking_id,
               ri.new_room_id AS room_id,
               ri.new_start_at AS start_at,
               ri.new_end_at AS end_at,
               'proposed' AS slot_kind
        FROM reschedule_orders ro
        JOIN reschedule_items ri ON ri.order_id = ro.id
        {where}
        """,
        params + params,
    ).fetchall()
    return [dict(r) for r in rows]


def _pending_participant_holds(
    conn: sqlite3.Connection, *, exclude_order_id: str | None = None
) -> list[dict[str, str]]:
    """Participant-keyed held slots (original and proposed) of pending orders."""
    where = "WHERE ro.status = 'pending'"
    params: list[Any] = []
    if exclude_order_id is not None:
        where += " AND ro.id != ?"
        params.append(exclude_order_id)
    rows = conn.execute(
        f"""
        SELECT ro.id AS order_id, bp.participant_id,
               ri.original_start_at AS start_at,
               ri.original_end_at AS end_at,
               'original' AS slot_kind
        FROM reschedule_orders ro
        JOIN reschedule_items ri ON ri.order_id = ro.id
        JOIN booking_participants bp ON bp.booking_id = ri.booking_id
        {where}
        UNION ALL
        SELECT ro.id AS order_id, bp.participant_id,
               ri.new_start_at AS start_at,
               ri.new_end_at AS end_at,
               'proposed' AS slot_kind
        FROM reschedule_orders ro
        JOIN reschedule_items ri ON ri.order_id = ro.id
        JOIN booking_participants bp ON bp.booking_id = ri.booking_id
        {where}
        """,
        params + params,
    ).fetchall()
    return [dict(r) for r in rows]


def _assert_no_room_or_participant_conflict(
    conn: sqlite3.Connection,
    *,
    candidates: list[dict[str, Any]],
    exclude_booking_ids: set[str] | None = None,
    exclude_order_id: str | None = None,
    participant_override: dict[str, list[str]] | None = None,
) -> None:
    """Validate room/participant availability for candidate intervals.

    ``candidates`` are ``{"booking_id", "room_id", "start_at", "end_at"}``
    dicts.  Participant lists are read from stored booking rows unless supplied
    through ``participant_override`` (used when the booking row does not exist
    yet).  Confirmed bookings (except ``exclude_booking_ids``, which are the
    order's own bookings about to move) and slots held by *other* pending
    consultations both block.  Back-to-back intervals are allowed.
    """
    exclude_booking_ids = exclude_booking_ids or set()
    participant_override = participant_override or {}
    details: list[dict[str, Any]] = []

    confirmed = conn.execute(
        "SELECT * FROM bookings WHERE status = 'confirmed' ORDER BY id"
    ).fetchall()
    confirmed_participants = {
        booking["id"]: _booking_participant_ids(conn, booking["id"]) for booking in confirmed
    }
    holds = _pending_hold_intervals(conn, exclude_order_id=exclude_order_id)
    participant_holds = _pending_participant_holds(conn, exclude_order_id=exclude_order_id)

    for cand in candidates:
        c_start, c_end = cand["start_at"], cand["end_at"]
        candidate_pids = participant_override.get(
            cand["booking_id"], _booking_participant_ids(conn, cand["booking_id"])
        )
        # Room: confirmed bookings outside the moving set.
        for booking in confirmed:
            if booking["id"] in exclude_booking_ids:
                continue
            if booking["room_id"] == cand["room_id"] and _overlaps(
                c_start, c_end, booking["start_at"], booking["end_at"]
            ):
                details.append(
                    {
                        "candidate_booking_id": cand["booking_id"],
                        "resource": "room",
                        "resource_id": cand["room_id"],
                        "conflicts_with_booking_id": booking["id"],
                    }
                )
        # Room: slots held by other pending orders (original and proposed).
        for hold in holds:
            if hold["room_id"] == cand["room_id"] and _overlaps(
                c_start, c_end, hold["start_at"], hold["end_at"]
            ):
                details.append(
                    {
                        "candidate_booking_id": cand["booking_id"],
                        "resource": "room",
                        "resource_id": cand["room_id"],
                        "conflicts_with_reschedule_order_id": hold["order_id"],
                        "held_slot": hold["slot_kind"],
                    }
                )
        # Participant: confirmed bookings outside the moving set.
        for pid in candidate_pids:
            for booking in confirmed:
                if booking["id"] in exclude_booking_ids or booking["id"] == cand["booking_id"]:
                    continue
                if pid in confirmed_participants[booking["id"]] and _overlaps(
                    c_start, c_end, booking["start_at"], booking["end_at"]
                ):
                    details.append(
                        {
                            "candidate_booking_id": cand["booking_id"],
                            "resource": "participant",
                            "resource_id": pid,
                            "conflicts_with_booking_id": booking["id"],
                        }
                    )
            # Participant: slots held by other pending orders.
            for hold in participant_holds:
                if hold["participant_id"] == pid and _overlaps(
                    c_start, c_end, hold["start_at"], hold["end_at"]
                ):
                    details.append(
                        {
                            "candidate_booking_id": cand["booking_id"],
                            "resource": "participant",
                            "resource_id": pid,
                            "conflicts_with_reschedule_order_id": hold["order_id"],
                            "held_slot": hold["slot_kind"],
                        }
                    )

    if details:
        raise ApiError(
            409,
            "slot_conflict",
            "The requested room or participant is already occupied for an overlapping interval",
            {"conflicts": details},
        )


def booking_payload(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["participant_ids"] = _booking_participant_ids(conn, row["id"])
    return data


def create_booking(
    conn: sqlite3.Connection,
    *,
    booking_id: str | None,
    interview_id: str,
    room_id: str,
    participant_ids: list[str],
    start_at: datetime,
    end_at: datetime,
    created_by: str = "project-owner",
) -> dict[str, Any]:
    start = normalize_dt(start_at, field="start_at")
    end = normalize_dt(end_at, field="end_at")
    if not end > start:
        raise ApiError(422, "invalid_interval", "end_at must be later than start_at")
    unique_ids = list(dict.fromkeys(participant_ids))
    bid = booking_id or new_id("bk")
    now = utcnow()

    with immediate_transaction(conn):
        settle_expired_orders_locked(conn)
        if now >= start:
            raise ApiError(
                409,
                "booking_already_started",
                "A new booking must start in the future",
                {"now": now, "start_at": start},
            )
        if conn.execute("SELECT 1 FROM interviews WHERE id = ?", (interview_id,)).fetchone() is None:
            raise ApiError(404, "interview_not_found", f"Interview {interview_id!r} was not found")
        _require_room(conn, room_id)

        rows = conn.execute(
            f"SELECT id, status FROM participants WHERE id IN ({','.join('?' for _ in unique_ids)})",
            unique_ids,
        ).fetchall() if unique_ids else []
        found = {r["id"]: r for r in rows}
        missing = [pid for pid in unique_ids if pid not in found]
        if missing:
            raise ApiError(
                404, "participant_not_found", "Some listed participants do not exist", {"missing": missing}
            )
        # A booking needs at least one still-valid participant; participants who
        # already requested deletion cannot be the reason for a new booking.
        if not any(found[pid]["status"] == "active" for pid in unique_ids):
            raise ApiError(
                409,
                "no_valid_participant",
                "A new booking must include at least one valid (active) participant",
            )

        active = conn.execute(
            "SELECT id FROM bookings WHERE interview_id = ? AND status = 'confirmed'",
            (interview_id,),
        ).fetchone()
        if active is not None:
            raise ApiError(
                409,
                "active_booking_exists",
                "This interview already has a non-cancelled booking",
                {"booking_id": active["id"]},
            )

        _assert_no_room_or_participant_conflict(
            conn,
            candidates=[{"booking_id": bid, "room_id": room_id, "start_at": start, "end_at": end}],
            participant_override={bid: unique_ids},
        )

        conn.execute(
            """
            INSERT INTO bookings
                (id, interview_id, room_id, start_at, end_at, status, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, 'confirmed', ?, ?)
            """,
            (bid, interview_id, room_id, start, end, created_by, now),
        )
        for pid in unique_ids:
            conn.execute(
                "INSERT INTO booking_participants (booking_id, participant_id) VALUES (?, ?)",
                (bid, pid),
            )
        audit_locked(
            conn,
            "booking",
            bid,
            "booking.created",
            {"interview_id": interview_id, "room_id": room_id, "participant_ids": unique_ids},
        )
    return booking_payload(conn, _require_booking(conn, bid))


def list_bookings(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    room_id: str | None = None,
    interview_id: str | None = None,
) -> list[dict[str, Any]]:
    if status is not None and status not in (CONFIRMED, CANCELLED):
        raise ApiError(422, "invalid_booking_status", "status must be confirmed or cancelled")
    sweep_expired_orders(conn)
    with read_transaction(conn):
        where: list[str] = []
        params: list[Any] = []
        if status:
            where.append("b.status = ?")
            params.append(status)
        if room_id:
            where.append("b.room_id = ?")
            params.append(room_id)
        if interview_id:
            where.append("b.interview_id = ?")
            params.append(interview_id)
        sql = "SELECT * FROM bookings b"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY b.start_at, b.id"
        rows = conn.execute(sql, params).fetchall()
        return [booking_payload(conn, r) for r in rows]


def get_booking(conn: sqlite3.Connection, booking_id: str) -> dict[str, Any]:
    sweep_expired_orders(conn)
    with read_transaction(conn):
        return booking_payload(conn, _require_booking(conn, booking_id))


def cancel_booking(
    conn: sqlite3.Connection,
    booking_id: str,
    *,
    reason: str | None = None,
    triggered_by: str = "project-owner",
    force: bool = False,
) -> dict[str, Any]:
    """Cancel a not-yet-started confirmed booking.

    Any pending consultation involving the booking ends as
    ``cancelled_booking`` in the same transaction; the other bookings of that
    order stay in place.  ``force`` is used by participant purge, which may
    cancel a booking whose start time has technically passed only when no
    confirmed row would otherwise remain inconsistent.
    """
    now = utcnow()
    with immediate_transaction(conn):
        settle_expired_orders_locked(conn)
        row = _require_booking(conn, booking_id)
        if row["status"] == CANCELLED:
            return booking_payload(conn, row)
        if not force and now >= row["start_at"]:
            raise ApiError(
                409,
                "booking_already_started",
                "Only bookings that have not started can be cancelled",
                {"booking_id": booking_id, "start_at": row["start_at"], "now": now},
            )
        conn.execute(
            """
            UPDATE bookings
            SET status = 'cancelled', cancelled_at = ?, cancellation_reason = ?
            WHERE id = ? AND status = 'confirmed'
            """,
            (now, reason, booking_id),
        )
        invalidated = _invalidate_orders_for_bookings_locked(
            conn, [booking_id], status=CANCELLED_BOOKING, reason="booking_cancelled", now=now
        )
        audit_locked(
            conn,
            "booking",
            booking_id,
            "booking.cancelled",
            {"reason": reason, "triggered_by": triggered_by, "invalidated_orders": invalidated},
        )
    return booking_payload(conn, _require_booking(conn, booking_id))


# ---------------------------------------------------------------------------
# Reschedule orders
# ---------------------------------------------------------------------------


def settle_expired_orders_locked(conn: sqlite3.Connection) -> list[str]:
    """Mark pending orders whose consultation deadline has passed as expired.

    Must be called inside an open write transaction.  Expiring an order only
    changes its status; the proposed slots disappear from the pending-hold
    queries immediately, which restores room/participant capacity.
    """
    now = utcnow()
    rows = conn.execute(
        "SELECT id FROM reschedule_orders WHERE status = 'pending' AND consultation_deadline <= ? ORDER BY id",
        (now,),
    ).fetchall()
    expired_ids = [r["id"] for r in rows]
    for oid in expired_ids:
        conn.execute(
            """
            UPDATE reschedule_orders
            SET status = 'expired', finalized_at = ?, end_reason = 'consultation_deadline_reached'
            WHERE id = ? AND status = 'pending'
            """,
            (now, oid),
        )
        audit_locked(conn, "reschedule_order", oid, "reschedule.expired", {"finalized_at": now})
    return expired_ids


def _invalidate_orders_for_bookings_locked(
    conn: sqlite3.Connection,
    booking_ids: list[str],
    *,
    status: str,
    reason: str,
    now: str,
) -> list[str]:
    if not booking_ids:
        return []
    placeholders = ",".join("?" for _ in booking_ids)
    rows = conn.execute(
        f"""
        SELECT DISTINCT ro.id
        FROM reschedule_orders ro
        JOIN reschedule_items ri ON ri.order_id = ro.id
        WHERE ro.status = 'pending' AND ri.booking_id IN ({placeholders})
        ORDER BY ro.id
        """,
        booking_ids,
    ).fetchall()
    order_ids = [r["id"] for r in rows]
    for oid in order_ids:
        conn.execute(
            """
            UPDATE reschedule_orders
            SET status = ?, finalized_at = ?, end_reason = ?
            WHERE id = ? AND status = 'pending'
            """,
            (status, now, reason, oid),
        )
        audit_locked(
            conn,
            "reschedule_order",
            oid,
            f"reschedule.{status}",
            {"finalized_at": now, "reason": reason, "booking_ids": booking_ids},
        )
    return order_ids


def create_reschedule_order(
    conn: sqlite3.Connection,
    *,
    order_id: str | None,
    items: list[dict[str, Any]],
    consultation_deadline: datetime,
    created_by: str = "project-owner",
) -> dict[str, Any]:
    deadline = normalize_dt(consultation_deadline, field="consultation_deadline")
    now = utcnow()
    oid = order_id or new_id("rso")

    normalized: list[dict[str, str]] = []
    for index, item in enumerate(items, start=1):
        start = normalize_dt(item["start_at"], field=f"items[{index}].start_at")
        end = normalize_dt(item["end_at"], field=f"items[{index}].end_at")
        if not end > start:
            raise ApiError(
                422, "invalid_interval", f"items[{index}].end_at must be later than start_at"
            )
        normalized.append(
            {
                "booking_id": item["booking_id"],
                "room_id": item["room_id"],
                "start_at": start,
                "end_at": end,
            }
        )

    with immediate_transaction(conn):
        settle_expired_orders_locked(conn)
        if deadline <= now:
            raise ApiError(
                422,
                "deadline_must_be_future",
                "consultation_deadline must be later than the current time",
                {"now": now, "consultation_deadline": deadline},
            )

        booking_ids = [item["booking_id"] for item in normalized]
        if len(set(booking_ids)) != len(booking_ids):
            raise ApiError(422, "duplicate_booking_in_order", "Each booking may appear at most once in an order")

        bookings: dict[str, sqlite3.Row] = {}
        for item in normalized:
            booking = _require_booking(conn, item["booking_id"])
            if booking["status"] != CONFIRMED:
                raise ApiError(
                    409,
                    "booking_not_confirmed",
                    "Only confirmed bookings can be rescheduled",
                    {"booking_id": booking["id"], "status": booking["status"]},
                )
            if now >= booking["start_at"]:
                raise ApiError(
                    409,
                    "booking_already_started",
                    "Only bookings that have not started can be rescheduled",
                    {"booking_id": booking["id"], "start_at": booking["start_at"], "now": now},
                )
            _require_room(conn, item["room_id"])
            # Deadline must not be later than any original or new start.
            if deadline > booking["start_at"] or deadline > item["start_at"]:
                raise ApiError(
                    422,
                    "deadline_too_late",
                    "consultation_deadline must be no later than every original and new start",
                    {
                        "booking_id": booking["id"],
                        "original_start_at": booking["start_at"],
                        "new_start_at": item["start_at"],
                        "consultation_deadline": deadline,
                    },
                )
            # Every item must actually change room or interval.
            if (
                item["room_id"] == booking["room_id"]
                and item["start_at"] == booking["start_at"]
                and item["end_at"] == booking["end_at"]
            ):
                raise ApiError(
                    422,
                    "reschedule_item_unchanged",
                    "Each item must change the room or the interval",
                    {"booking_id": booking["id"]},
                )
            bookings[booking["id"]] = booking

        # No booking may already belong to another pending consultation.
        placeholders = ",".join("?" for _ in booking_ids)
        occupied = conn.execute(
            f"""
            SELECT ri.booking_id, ro.id AS order_id
            FROM reschedule_items ri
            JOIN reschedule_orders ro ON ro.id = ri.order_id
            WHERE ro.status = 'pending'
              AND ri.booking_id IN ({placeholders})
            """,
            booking_ids,
        ).fetchall()
        if occupied:
            raise ApiError(
                409,
                "booking_in_other_pending_order",
                "A booking cannot take part in two pending consultations",
                [dict(r) for r in occupied],
            )

        # The new arrangement must be internally compatible: rooms and every
        # participant list must remain free of overlapping intervals.
        room_intervals: list[tuple[str, str, str, str]] = []
        participant_intervals: list[tuple[str, str, str, str]] = []
        booking_participants: dict[str, list[str]] = {}
        for item in normalized:
            booking = bookings[item["booking_id"]]
            pids = _booking_participant_ids(conn, item["booking_id"])
            booking_participants[item["booking_id"]] = pids
            room_intervals.append(
                (item["room_id"], item["booking_id"], item["start_at"], item["end_at"])
            )
            for pid in pids:
                participant_intervals.append(
                    (pid, item["booking_id"], item["start_at"], item["end_at"])
                )
        room_clash = _intervals_conflict(room_intervals)
        if room_clash:
            raise ApiError(
                409,
                "reschedule_internal_room_conflict",
                "The proposed arrangement overlaps on a room inside the order",
                {"bookings": list(room_clash)},
            )
        participant_clash = _intervals_conflict(participant_intervals)
        if participant_clash:
            raise ApiError(
                409,
                "reschedule_internal_participant_conflict",
                "The proposed arrangement overlaps for a participant inside the order",
                {"bookings": list(participant_clash)},
            )

        # Compatibility with the world outside this order.  The order's own
        # bookings are excluded because their confirmed slots are being vacated
        # (and may be reused by another item of the same order).
        all_participants = sorted({pid for pids in booking_participants.values() for pid in pids})
        _assert_no_room_or_participant_conflict(
            conn,
            candidates=[
                {
                    "booking_id": item["booking_id"],
                    "room_id": item["room_id"],
                    "start_at": item["start_at"],
                    "end_at": item["end_at"],
                }
                for item in normalized
            ],
            exclude_booking_ids=set(booking_ids),
        )

        conn.execute(
            """
            INSERT INTO reschedule_orders
                (id, status, consultation_deadline, created_by, created_at)
            VALUES (?, 'pending', ?, ?, ?)
            """,
            (oid, deadline, created_by, now),
        )
        for ordinal, item in enumerate(normalized, start=1):
            booking = bookings[item["booking_id"]]
            conn.execute(
                """
                INSERT INTO reschedule_items
                    (id, order_id, booking_id,
                     original_room_id, original_start_at, original_end_at,
                     new_room_id, new_start_at, new_end_at, ordinal)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("rsi"),
                    oid,
                    item["booking_id"],
                    booking["room_id"],
                    booking["start_at"],
                    booking["end_at"],
                    item["room_id"],
                    item["start_at"],
                    item["end_at"],
                    ordinal,
                ),
            )
        audit_locked(
            conn,
            "reschedule_order",
            oid,
            "reschedule.order_created",
            {
                "consultation_deadline": deadline,
                "booking_ids": booking_ids,
                "participant_ids": all_participants,
            },
        )
    return order_detail(conn, oid)


def _item_payloads(conn: sqlite3.Connection, order_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM reschedule_items WHERE order_id = ? ORDER BY ordinal, id",
        (order_id,),
    ).fetchall()
    payloads = []
    for row in rows:
        item = dict(row)
        item["participant_ids"] = _booking_participant_ids(conn, row["booking_id"])
        item["changed"] = not (
            row["original_room_id"] == row["new_room_id"]
            and row["original_start_at"] == row["new_start_at"]
            and row["original_end_at"] == row["new_end_at"]
        )
        payloads.append(item)
    return payloads


def _order_detail_locked(conn: sqlite3.Connection, order_id: str) -> dict[str, Any]:
    """Build an order detail inside an already-open transaction."""
    order = _require_order(conn, order_id)
    participant_ids = _order_participant_ids(conn, order_id)
    response_rows = conn.execute(
        "SELECT * FROM reschedule_responses WHERE order_id = ? ORDER BY participant_id",
        (order_id,),
    ).fetchall()
    responses = [dict(r) for r in response_rows]
    responded = {r["participant_id"] for r in response_rows}
    return {
        **dict(order),
        "participant_ids": participant_ids,
        "items": _item_payloads(conn, order_id),
        "responses": responses,
        "responded_participant_ids": sorted(responded),
        "pending_participant_ids": [pid for pid in participant_ids if pid not in responded],
        "response_summary": {
            "total": len(participant_ids),
            "accepted": sum(1 for r in response_rows if r["decision"] == "accepted"),
            "rejected": sum(1 for r in response_rows if r["decision"] == "rejected"),
            "pending": len(participant_ids) - len(response_rows),
        },
    }


def order_detail(conn: sqlite3.Connection, order_id: str) -> dict[str, Any]:
    sweep_expired_orders(conn)
    with read_transaction(conn):
        return _order_detail_locked(conn, order_id)


def list_orders(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    participant_id: str | None = None,
) -> list[dict[str, Any]]:
    sweep_expired_orders(conn)
    with read_transaction(conn):
        sql = "SELECT DISTINCT ro.* FROM reschedule_orders ro"
        params: list[Any] = []
        where: list[str] = []
        if participant_id:
            sql += (
                " JOIN reschedule_items ri ON ri.order_id = ro.id"
                " JOIN booking_participants bp ON bp.booking_id = ri.booking_id"
            )
            where.append("bp.participant_id = ?")
            params.append(participant_id)
        if status:
            where.append("ro.status = ?")
            params.append(status)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ro.created_at, ro.id"
        rows = conn.execute(sql, params).fetchall()
        return [_order_detail_locked(conn, r["id"]) for r in rows]


def respond_to_order(
    conn: sqlite3.Connection, *, order_id: str, participant_id: str, decision: str
) -> dict[str, Any]:
    if decision not in ("accepted", "rejected"):
        raise ApiError(422, "invalid_decision", "decision must be accepted or rejected")
    now = utcnow()
    with immediate_transaction(conn):
        settle_expired_orders_locked(conn)
        order = _require_order(conn, order_id)
        affected = _order_participant_ids(conn, order_id)
        if participant_id not in affected:
            raise ApiError(
                403,
                "not_reschedule_participant",
                "Only participants involved in this consultation may answer it",
                {"order_id": order_id, "participant_id": participant_id},
            )

        existing = conn.execute(
            "SELECT * FROM reschedule_responses WHERE order_id = ? AND participant_id = ?",
            (order_id, participant_id),
        ).fetchone()
        if existing is not None:
            # First valid answer is immutable.  Repeating the same answer is an
            # idempotent replay; changing it is rejected.
            if existing["decision"] != decision:
                raise ApiError(
                    409,
                    "response_already_recorded",
                    "The first valid response cannot be changed",
                    {"recorded_decision": existing["decision"], "attempted_decision": decision},
                )
            payload = _order_detail_locked(conn, order_id)
            payload["idempotent_replay"] = True
            return payload

        if order["status"] != PENDING:
            # A late answer can never revive or apply a finished order.
            raise ApiError(
                409,
                "reschedule_not_pending",
                "This consultation has already ended and accepts no responses",
                {"order_id": order_id, "status": order["status"], "end_reason": order["end_reason"]},
            )

        conn.execute(
            """
            INSERT INTO reschedule_responses (order_id, participant_id, decision, responded_at)
            VALUES (?, ?, ?, ?)
            """,
            (order_id, participant_id, decision, now),
        )
        audit_locked(
            conn,
            "reschedule_order",
            order_id,
            "reschedule.response_recorded",
            {"participant_id": participant_id, "decision": decision},
        )

        if decision == "rejected":
            conn.execute(
                """
                UPDATE reschedule_orders
                SET status = 'rejected', finalized_at = ?, end_reason = 'participant_rejected'
                WHERE id = ? AND status = 'pending'
                """,
                (now, order_id),
            )
            audit_locked(
                conn,
                "reschedule_order",
                order_id,
                "reschedule.rejected",
                {"rejected_by": participant_id},
            )
            return _order_detail_locked(conn, order_id)

        # Unanimous acceptance applies the whole arrangement atomically.
        all_responses = conn.execute(
            "SELECT decision FROM reschedule_responses WHERE order_id = ?",
            (order_id,),
        ).fetchall()
        decided = {r["decision"] for r in all_responses}
        if len(all_responses) == len(affected) and decided == {"accepted"}:
            _apply_order_locked(conn, order_id, now)
        return _order_detail_locked(conn, order_id)


def _apply_order_locked(conn: sqlite3.Connection, order_id: str, now: str) -> None:
    """Move every booking of an order in one transaction (must hold write lock)."""
    items = conn.execute(
        "SELECT * FROM reschedule_items WHERE order_id = ? ORDER BY ordinal, id",
        (order_id,),
    ).fetchall()
    for item in items:
        # Re-validate the booking is still confirmed and not started; another
        # transaction could only have cancelled it, which would have ended this
        # order already, but keep the invariant explicit.
        booking = conn.execute(
            "SELECT * FROM bookings WHERE id = ? AND status = 'confirmed'",
            (item["booking_id"],),
        ).fetchone()
        if booking is None:
            raise ApiError(
                409,
                "booking_not_confirmed",
                "An underlying booking is no longer confirmed; the order cannot be applied",
                {"booking_id": item["booking_id"]},
            )
        if now >= booking["start_at"] or now >= item["new_start_at"]:
            conn.execute(
                """
                UPDATE reschedule_orders
                SET status = 'expired', finalized_at = ?, end_reason = 'start_time_reached_before_apply'
                WHERE id = ? AND status = 'pending'
                """,
                (now, order_id),
            )
            audit_locked(
                conn, "reschedule_order", order_id, "reschedule.expired_before_apply", {"now": now}
            )
            return
        conn.execute(
            """
            UPDATE bookings
            SET room_id = ?, start_at = ?, end_at = ?
            WHERE id = ?
            """,
            (item["new_room_id"], item["new_start_at"], item["new_end_at"], item["booking_id"]),
        )
        audit_locked(
            conn,
            "booking",
            item["booking_id"],
            "booking.rescheduled",
            {
                "order_id": order_id,
                "from_room_id": item["original_room_id"],
                "to_room_id": item["new_room_id"],
                "from_start_at": item["original_start_at"],
                "to_start_at": item["new_start_at"],
            },
        )
    conn.execute(
        """
        UPDATE reschedule_orders
        SET status = 'approved', finalized_at = ?, end_reason = 'unanimous_acceptance'
        WHERE id = ? AND status = 'pending'
        """,
        (now, order_id),
    )
    audit_locked(conn, "reschedule_order", order_id, "reschedule.applied", {"finalized_at": now})


def withdraw_order(conn: sqlite3.Connection, order_id: str) -> dict[str, Any]:
    now = utcnow()
    with immediate_transaction(conn):
        settle_expired_orders_locked(conn)
        order = _require_order(conn, order_id)
        if order["status"] != PENDING:
            raise ApiError(
                409,
                "reschedule_not_pending",
                "Only pending consultations can be withdrawn",
                {"order_id": order_id, "status": order["status"]},
            )
        conn.execute(
            """
            UPDATE reschedule_orders
            SET status = 'withdrawn', finalized_at = ?, end_reason = 'owner_withdrew'
            WHERE id = ? AND status = 'pending'
            """,
            (now, order_id),
        )
        audit_locked(conn, "reschedule_order", order_id, "reschedule.withdrawn", {"finalized_at": now})
    return order_detail(conn, order_id)


def sweep_expired_orders(conn: sqlite3.Connection) -> dict[str, Any]:
    """Explicit maintenance entry (also runs lazily and at startup)."""
    with immediate_transaction(conn):
        expired = settle_expired_orders_locked(conn)
    return {"expired_order_ids": expired, "count": len(expired)}


# ---------------------------------------------------------------------------
# Participant calendar and session authentication
# ---------------------------------------------------------------------------


def authenticate_session(conn: sqlite3.Connection, session_token: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM participants WHERE session_token = ?", (session_token,)
    ).fetchone()
    if row is None:
        raise ApiError(401, "invalid_session", "A valid participant session token is required")
    return row


def participant_calendar(conn: sqlite3.Connection, participant_id: str) -> dict[str, Any]:
    sweep_expired_orders(conn)
    with read_transaction(conn):
        get_participant_or_404(conn, participant_id)
        bookings = [
            booking_payload(conn, r)
            for r in _confirmed_booking_rows(conn, participant_id=participant_id)
        ]
        order_rows = conn.execute(
            """
            SELECT DISTINCT ro.id
            FROM reschedule_orders ro
            JOIN reschedule_items ri ON ri.order_id = ro.id
            JOIN booking_participants bp ON bp.booking_id = ri.booking_id
            WHERE bp.participant_id = ?
            ORDER BY ro.created_at, ro.id
            """,
            (participant_id,),
        ).fetchall()
        orders = [_order_detail_locked(conn, r["id"]) for r in order_rows]
        return {
            "participant_id": participant_id,
            "generated_at": utcnow(),
            "confirmed_bookings": bookings,
            "reschedule_orders": orders,
        }


def get_participant_or_404(conn: sqlite3.Connection, participant_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM participants WHERE id = ?", (participant_id,)).fetchone()
    if row is None:
        raise ApiError(404, "participant_not_found", f"Participant {participant_id!r} was not found")
    return row


# ---------------------------------------------------------------------------
# Participant purge integration
# ---------------------------------------------------------------------------


def cancel_bookings_for_participant_purge_locked(
    conn: sqlite3.Connection, participant_id: str, *, now: str
) -> dict[str, Any]:
    """Cancel the participant's not-yet-started bookings and invalidate every
    pending consultation touching them.  Runs inside the purge transaction.

    Bookings involving other participants are cancelled as well (a booking has
    one fixed participant list); the other participants' unaffected bookings
    remain confirmed, and the invalidated order moves none of them.
    """
    # Any consultation whose deadline has already passed is settled first, so
    # no stale pending order can survive the purge.
    settle_expired_orders_locked(conn)
    rows = conn.execute(
        """
        SELECT b.* FROM bookings b
        JOIN booking_participants bp ON bp.booking_id = b.id
        WHERE bp.participant_id = ? AND b.status = 'confirmed'
        ORDER BY b.id
        """,
        (participant_id,),
    ).fetchall()
    cancelled: list[str] = []
    for booking in rows:
        if now < booking["start_at"]:
            conn.execute(
                """
                UPDATE bookings
                SET status = 'cancelled', cancelled_at = ?,
                    cancellation_reason = 'participant_deletion_request'
                WHERE id = ? AND status = 'confirmed'
                """,
                (now, booking["id"]),
            )
            cancelled.append(booking["id"])
            audit_locked(
                conn,
                "booking",
                booking["id"],
                "booking.cancelled_by_participant_purge",
                {"participant_id": participant_id},
            )
    invalidated = _invalidate_orders_for_bookings_locked(
        conn,
        cancelled,
        status=CANCELLED_BOOKING,
        reason="booking_cancelled_by_participant_purge",
        now=now,
    )
    return {
        "cancelled_booking_ids": cancelled,
        "invalidated_reschedule_order_ids": invalidated,
    }


def recover_rescheduling(conn: sqlite3.Connection) -> dict[str, Any]:
    """Startup recovery: release proposed slots held by orders whose deadline
    passed while the process was down."""
    with immediate_transaction(conn):
        expired = settle_expired_orders_locked(conn)
    return {"expired_order_ids": expired, "count": len(expired)}
