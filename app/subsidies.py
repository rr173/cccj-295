"""Subsidy attendance, weekly caps, payment lists and reconciliation.

Money is stored in integer fen (1/100 yuan).  The public HTTP representation
uses yuan; whole-yuan values are emitted as integers so ordinary assertions
see ``100`` rather than ``100.00``.

All write operations use one SQLite ``BEGIN IMMEDIATE`` transaction.  Batch
creation therefore checks the currently sendable balance and inserts the
complete fixed list atomically.  Idempotency identifiers and immutable line
results are durable tables, so process restart cannot duplicate reservations
or lose offline payment/return outcomes.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from .db import immediate_transaction, read_transaction, utcnow
from .services import ApiError, audit_locked, get_participant, new_id

UNIT_MINUTES = 15
UNIT_REWARD_FEN = 1_000  # 10 yuan
WEEKLY_CAP_FEN = 10_000  # 100 yuan

PENDING = "pending"
PAID = "paid"
UNPAID = "unpaid"
LINE_RESULTS = {PENDING, PAID, UNPAID}


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------


def parse_week_start(value: str | date | datetime | None) -> str:
    """Return the Monday (UTC calendar date) of the selected week.

    Callers may pass a Monday or any other date in the desired week.  The
    canonical response always contains the Monday ``YYYY-MM-DD``.
    """
    if value is None:
        raise ApiError(422, "week_required", "A week start date is required")
    if isinstance(value, datetime):
        day = value.astimezone(timezone.utc).date()
    elif isinstance(value, date):
        day = value
    else:
        text = str(value).strip()
        try:
            if "T" in text:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                day = parsed.astimezone(timezone.utc).date() if parsed.tzinfo else parsed.date()
            else:
                day = date.fromisoformat(text[:10])
        except ValueError as exc:
            raise ApiError(
                422,
                "invalid_week",
                "Week must be an ISO date such as 2026-11-23",
                {"value": value},
            ) from exc
    monday = day - timedelta(days=day.weekday())
    return monday.isoformat()


def booking_week_start(start_at: str) -> str:
    parsed = datetime.fromisoformat(start_at)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
    return parse_week_start(parsed.date())


def parse_money_fen(value: Any, *, field: str = "amount") -> int:
    """Parse an API yuan amount into exact integer fen."""
    if isinstance(value, bool):
        raise ApiError(422, "invalid_amount", f"{field} must be a positive money amount")
    if isinstance(value, int):
        decimal_value = Decimal(value)
    elif isinstance(value, float):
        decimal_value = Decimal(str(value))
    elif isinstance(value, Decimal):
        decimal_value = value
    elif isinstance(value, str):
        try:
            decimal_value = Decimal(value.strip())
        except InvalidOperation as exc:
            raise ApiError(422, "invalid_amount", f"{field} must be a money amount") from exc
    else:
        raise ApiError(422, "invalid_amount", f"{field} must be a money amount")
    if not decimal_value.is_finite() or decimal_value < 0:
        raise ApiError(422, "invalid_amount", f"{field} must be a non-negative money amount")
    fen = decimal_value * 100
    if fen != fen.to_integral_value():
        raise ApiError(
            422,
            "amount_precision_too_fine",
            f"{field} supports at most two decimal places",
        )
    return int(fen)


def money_public(fen: int | None) -> int | float:
    value = int(fen or 0)
    if value % 100 == 0:
        return value // 100
    return float(Decimal(value) / Decimal(100))


def _entry_payload(row: sqlite3.Row | dict[str, Any], *, remaining_before: int | None = None) -> dict[str, Any]:
    data = dict(row)
    raw_fen = int(data["raw_amount_fen"])
    counted_fen = int(data["counted_amount_fen"])
    payload = {
        "booking_id": data["booking_id"],
        "interview_id": data.get("interview_id"),
        "participant_id": data["participant_id"],
        "week_start": data["week_start"],
        "start_at": data["start_at"],
        "end_at": data["end_at"],
        "attended_minutes": int(data["attended_minutes"]),
        "billable_units": int(data["attended_minutes"]) // UNIT_MINUTES,
        "raw_amount_fen": raw_fen,
        "raw_amount": money_public(raw_fen),
        "weekly_cap_fen": WEEKLY_CAP_FEN,
        "weekly_cap": money_public(WEEKLY_CAP_FEN),
        "counted_amount_fen": counted_fen,
        "counted_amount": money_public(counted_fen),
        # Convenience alias for the amount currently included after the cap.
        "amount_fen": counted_fen,
        "amount": money_public(counted_fen),
        "cap_reduction_fen": raw_fen - counted_fen,
        "cap_reduction": money_public(raw_fen - counted_fen),
        "same_session_billing_key": f"{data['booking_id']}:{data['participant_id']}",
    }
    if remaining_before is not None:
        payload["cap_remaining_before_fen"] = remaining_before
        payload["cap_remaining_before"] = money_public(remaining_before)
        after = max(0, remaining_before - counted_fen)
        payload["cap_remaining_after_fen"] = after
        payload["cap_remaining_after"] = money_public(after)
    if data.get("updated_at") is not None:
        payload["updated_at"] = data["updated_at"]
    return payload


# ---------------------------------------------------------------------------
# Attendance registration/correction and current session amounts
# ---------------------------------------------------------------------------


def _require_booking(conn: sqlite3.Connection, booking_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
    if row is None:
        raise ApiError(404, "booking_not_found", f"Booking {booking_id!r} was not found")
    return row


def _roster_ids(conn: sqlite3.Connection, booking_id: str) -> set[str]:
    rows = conn.execute(
        "SELECT participant_id FROM booking_participants WHERE booking_id = ?",
        (booking_id,),
    ).fetchall()
    return {str(r["participant_id"]) for r in rows}


def _duration_timedelta(row: sqlite3.Row) -> timedelta:
    start = datetime.fromisoformat(row["start_at"])
    end = datetime.fromisoformat(row["end_at"])
    return end - start


def upsert_attendance(
    conn: sqlite3.Connection,
    booking_id: str,
    records: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    normalized: list[tuple[str, int]] = []
    seen: set[str] = set()
    for index, record in enumerate(records):
        pid = str(record.get("participant_id", "")).strip()
        if not pid:
            raise ApiError(422, "participant_id_required", f"records[{index}].participant_id is required")
        if pid in seen:
            raise ApiError(
                409,
                "duplicate_attendance_participant",
                f"Participant {pid!r} appears more than once in the attendance request",
                {"participant_id": pid},
            )
        seen.add(pid)
        raw_minutes = record.get("attended_minutes")
        if isinstance(raw_minutes, bool):
            raise ApiError(
                422,
                "invalid_attended_minutes",
                "attended_minutes must be a non-negative integer",
                {"participant_id": pid},
            )
        if isinstance(raw_minutes, float):
            if not raw_minutes.is_integer():
                raise ApiError(
                    422,
                    "invalid_attended_minutes",
                    "attended_minutes must be a whole number of minutes",
                    {"participant_id": pid},
                )
            minutes = int(raw_minutes)
        else:
            try:
                minutes = int(raw_minutes)
            except (TypeError, ValueError) as exc:
                raise ApiError(
                    422,
                    "invalid_attended_minutes",
                    "attended_minutes must be a non-negative integer",
                    {"participant_id": pid},
                ) from exc
        if minutes < 0:
            raise ApiError(
                422,
                "invalid_attended_minutes",
                "attended_minutes must be non-negative",
                {"participant_id": pid},
            )
        normalized.append((pid, minutes))

    now = utcnow()
    with immediate_transaction(conn):
        booking = _require_booking(conn, booking_id)
        if booking["status"] != "confirmed":
            raise ApiError(
                409,
                "booking_cancelled",
                "Attendance can only be registered for a non-cancelled booking",
                {"booking_id": booking_id, "status": booking["status"]},
            )
        if now < booking["end_at"]:
            raise ApiError(
                409,
                "booking_not_ended",
                "Attendance can only be registered after the booking has ended",
                {"booking_id": booking_id, "end_at": booking["end_at"], "now": now},
            )

        roster = _roster_ids(conn, booking_id)
        duration = _duration_timedelta(booking)
        for pid, minutes in normalized:
            if pid not in roster:
                raise ApiError(
                    422,
                    "participant_not_in_booking",
                    "Attendance may only be recorded for a participant on the fixed booking list",
                    {"booking_id": booking_id, "participant_id": pid},
                )
            if timedelta(minutes=minutes) > duration:
                duration_seconds = max(0, int(duration.total_seconds()))
                raise ApiError(
                    422,
                    "attended_minutes_exceed_booking_duration",
                    "Actual attended minutes cannot exceed the booking duration",
                    {
                        "booking_id": booking_id,
                        "participant_id": pid,
                        "attended_minutes": minutes,
                        "maximum_whole_minutes": duration_seconds // 60,
                        "booking_duration_seconds": duration_seconds,
                    },
                )

        for pid, minutes in normalized:
            conn.execute(
                """
                INSERT INTO attendance_records
                    (booking_id, participant_id, attended_minutes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(booking_id, participant_id) DO UPDATE SET
                    attended_minutes = excluded.attended_minutes,
                    updated_at = excluded.updated_at
                """,
                (booking_id, pid, minutes, now, now),
            )
        audit_locked(
            conn,
            "booking_attendance",
            booking_id,
            "attendance.upserted",
            {"participant_ids": [pid for pid, _ in normalized], "count": len(normalized)},
        )

    return get_booking_attendance(conn, booking_id)


def _compute_entries_locked(
    conn: sqlite3.Connection,
    *,
    week_start: str | None = None,
    participant_id: str | None = None,
    booking_id: str | None = None,
) -> list[dict[str, Any]]:
    """Compute current cap-allocated session entries.

    Cap allocation is deliberately recomputed from all attendance rows.  This
    is what lets a correction to an earlier session change a later session's
    currently included amount while leaving issued payment lines untouched.
    """
    sql = """
        SELECT b.id AS booking_id, b.interview_id, b.start_at, b.end_at,
               ar.participant_id, ar.attended_minutes, ar.updated_at
        FROM attendance_records ar
        JOIN bookings b ON b.id = ar.booking_id
        WHERE b.status = 'confirmed'
    """
    params: list[Any] = []
    if participant_id is not None:
        sql += " AND ar.participant_id = ?"
        params.append(participant_id)
    if booking_id is not None:
        sql += " AND ar.booking_id = ?"
        params.append(booking_id)
    sql += " ORDER BY ar.participant_id, b.start_at, b.id"
    rows = conn.execute(sql, params).fetchall()

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        item = dict(row)
        week = booking_week_start(item["start_at"])
        if week_start is not None and week != week_start:
            continue
        minutes = int(item["attended_minutes"])
        item["week_start"] = week
        item["raw_amount_fen"] = (minutes // UNIT_MINUTES) * UNIT_REWARD_FEN
        grouped[(str(item["participant_id"]), week)].append(item)

    result: list[dict[str, Any]] = []
    for (_pid, week), items in grouped.items():
        remaining = WEEKLY_CAP_FEN
        for item in sorted(items, key=lambda x: (x["start_at"], x["booking_id"])):
            before = remaining
            counted = min(int(item["raw_amount_fen"]), max(0, remaining))
            item["counted_amount_fen"] = counted
            remaining -= counted
            result.append(_entry_payload(item, remaining_before=before))

    result.sort(key=lambda e: (e["week_start"], e["participant_id"], e["start_at"], e["booking_id"]))
    return result


def list_session_entries(
    conn: sqlite3.Connection,
    *,
    week_start: str | None = None,
    participant_id: str | None = None,
) -> dict[str, Any]:
    week = parse_week_start(week_start) if week_start else None
    with read_transaction(conn):
        entries = _compute_entries_locked(conn, week_start=week, participant_id=participant_id)
    return {"week_start": week, "entries": entries, "count": len(entries)}


def get_booking_attendance(conn: sqlite3.Connection, booking_id: str) -> dict[str, Any]:
    with read_transaction(conn):
        booking = _require_booking(conn, booking_id)
        roster = conn.execute(
            """
            SELECT bp.participant_id, ar.attended_minutes, ar.updated_at
            FROM booking_participants bp
            LEFT JOIN attendance_records ar
              ON ar.booking_id = bp.booking_id AND ar.participant_id = bp.participant_id
            WHERE bp.booking_id = ?
            ORDER BY bp.participant_id
            """,
            (booking_id,),
        ).fetchall()
        entries = {
            e["participant_id"]: e
            for e in _compute_entries_locked(conn, booking_id=booking_id)
        }
        start_week = booking_week_start(booking["start_at"])
        duration = _duration_timedelta(booking)
        maximum = max(0, int(duration.total_seconds()) // 60)
        records: list[dict[str, Any]] = []
        for row in roster:
            pid = str(row["participant_id"])
            entry = entries.get(pid)
            if entry is not None:
                records.append(entry)
            else:
                records.append(
                    {
                        "booking_id": booking_id,
                        "interview_id": booking["interview_id"],
                        "participant_id": pid,
                        "week_start": start_week,
                        "start_at": booking["start_at"],
                        "end_at": booking["end_at"],
                        "registered": False,
                        "attended_minutes": None,
                        "maximum_minutes": maximum,
                        "raw_amount": 0,
                        "raw_amount_fen": 0,
                        "counted_amount": 0,
                        "counted_amount_fen": 0,
                        "amount": 0,
                        "amount_fen": 0,
                    }
                )
        for record in records:
            record.setdefault("registered", True)
            record["maximum_minutes"] = maximum
        return {
            "booking_id": booking_id,
            "interview_id": booking["interview_id"],
            "status": booking["status"],
            "start_at": booking["start_at"],
            "end_at": booking["end_at"],
            "week_start": start_week,
            "ended": utcnow() >= booking["end_at"],
            "records": records,
            # Alias used by the subsidy session view.
            "entries": records,
        }


# ---------------------------------------------------------------------------
# Balances and reconciliation
# ---------------------------------------------------------------------------


def _line_aggregates_locked(conn: sqlite3.Connection, week_start: str) -> dict[str, dict[str, int]]:
    lines = conn.execute(
        """
        SELECT participant_id,
               COALESCE(SUM(CASE WHEN result_status = 'pending' THEN amount_fen ELSE 0 END), 0)
                 AS pending_fen,
               COALESCE(SUM(CASE WHEN result_status = 'paid' THEN amount_fen ELSE 0 END), 0)
                 AS gross_paid_fen,
               COALESCE(SUM(amount_fen), 0) AS issued_fen
        FROM subsidy_batch_lines
        WHERE week_start = ?
        GROUP BY participant_id
        """,
        (week_start,),
    ).fetchall()
    result: dict[str, dict[str, int]] = {
        str(r["participant_id"]): {
            "pending_fen": int(r["pending_fen"]),
            "gross_paid_fen": int(r["gross_paid_fen"]),
            "issued_fen": int(r["issued_fen"]),
        }
        for r in lines
    }
    refunds = conn.execute(
        """
        SELECT l.participant_id, COALESCE(SUM(r.amount_fen), 0) AS refunded_fen
        FROM subsidy_refunds r
        JOIN subsidy_batch_lines l ON l.id = r.line_id
        WHERE l.week_start = ?
        GROUP BY l.participant_id
        """,
        (week_start,),
    ).fetchall()
    for row in refunds:
        pid = str(row["participant_id"])
        result.setdefault(pid, {"pending_fen": 0, "gross_paid_fen": 0, "issued_fen": 0})
        result[pid]["refunded_fen"] = int(row["refunded_fen"])
    for agg in result.values():
        agg.setdefault("refunded_fen", 0)
    return result


def _balance_for_participant_locked(
    conn: sqlite3.Connection,
    week_start: str,
    participant_id: str,
    entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if entries is None:
        entries = _compute_entries_locked(
            conn, week_start=week_start, participant_id=participant_id
        )
    current_fen = sum(int(e["counted_amount_fen"]) for e in entries)
    aggregates = _line_aggregates_locked(conn, week_start).get(
        participant_id,
        {"pending_fen": 0, "gross_paid_fen": 0, "refunded_fen": 0, "issued_fen": 0},
    )
    pending_fen = int(aggregates["pending_fen"])
    gross_paid_fen = int(aggregates["gross_paid_fen"])
    refunded_fen = int(aggregates.get("refunded_fen", 0))
    net_paid_fen = gross_paid_fen - refunded_fen
    remaining_fen = current_fen - net_paid_fen - pending_fen
    sendable_fen = max(0, remaining_fen)
    overpaid_fen = max(0, net_paid_fen - current_fen)

    return {
        "participant_id": participant_id,
        "week_start": week_start,
        "weekly_cap": money_public(WEEKLY_CAP_FEN),
        "weekly_cap_fen": WEEKLY_CAP_FEN,
        # Current payable after current attendance and the weekly cap.
        "current_payable_fen": current_fen,
        "current_payable": money_public(current_fen),
        "payable_amount_fen": current_fen,
        "payable_amount": money_public(current_fen),
        "payable_fen": current_fen,
        "payable": money_public(current_fen),
        # Delivered lines without paid/unpaid result.
        "pending_confirmation_fen": pending_fen,
        "pending_confirmation": money_public(pending_fen),
        "pending_amount_fen": pending_fen,
        "pending_amount": money_public(pending_fen),
        "pending_fen": pending_fen,
        "pending": money_public(pending_fen),
        # Confirmed offline payments less received refunds.
        "gross_paid_fen": gross_paid_fen,
        "gross_paid": money_public(gross_paid_fen),
        "gross_paid_amount_fen": gross_paid_fen,
        "gross_paid_amount": money_public(gross_paid_fen),
        "refunded_fen": refunded_fen,
        "refunded": money_public(refunded_fen),
        "refunded_amount_fen": refunded_fen,
        "refunded_amount": money_public(refunded_fen),
        "net_paid_fen": net_paid_fen,
        "net_paid": money_public(net_paid_fen),
        "net_paid_amount_fen": net_paid_fen,
        "net_paid_amount": money_public(net_paid_fen),
        # Positive amount that may be put on a new complete payment list.
        "sendable_amount_fen": sendable_fen,
        "sendable_amount": money_public(sendable_fen),
        "sendable_fen": sendable_fen,
        "sendable": money_public(sendable_fen),
        "available_amount_fen": sendable_fen,
        "available_amount": money_public(sendable_fen),
        "available_fen": sendable_fen,
        "available": money_public(sendable_fen),
        "now_payable_fen": sendable_fen,
        "now_payable": money_public(sendable_fen),
        # Confirmed net payment above the current payable.
        "overpaid_amount_fen": overpaid_fen,
        "overpaid_amount": money_public(overpaid_fen),
        "overpayment_fen": overpaid_fen,
        "overpayment": money_public(overpaid_fen),
        "overpaid_fen": overpaid_fen,
        "overpaid": money_public(overpaid_fen),
        # Useful reconciliation residual, may be negative.
        "unreserved_remainder_fen": remaining_fen,
        "unreserved_remainder": money_public(remaining_fen),
    }


def week_balances(
    conn: sqlite3.Connection,
    week_start: str | None = None,
    *,
    participant_id: str | None = None,
) -> dict[str, Any]:
    week = parse_week_start(week_start) if week_start else None
    with read_transaction(conn):
        # Do not require a participants row here: profile clearing may leave an
        # internal ID with subsidy obligations but no usable profile/account.
        entries = _compute_entries_locked(conn, week_start=week, participant_id=participant_id)
        if week is not None:
            pids = {e["participant_id"] for e in entries}
            pids.update(_line_aggregates_locked(conn, week).keys())
            if participant_id is not None:
                pids = {participant_id}
            balances = [
                _balance_for_participant_locked(
                    conn,
                    week,
                    pid,
                    [e for e in entries if e["participant_id"] == pid],
                )
                for pid in sorted(pids)
            ]
            return {"week_start": week, "balances": balances, "count": len(balances)}

        weeks = sorted({e["week_start"] for e in entries})
        # Include weeks that only have payment lists/refunds.
        for row in conn.execute("SELECT DISTINCT week_start FROM subsidy_batches").fetchall():
            weeks.append(str(row["week_start"]))
        weeks = sorted(set(weeks))
        all_balances = []
        for one_week in weeks:
            week_entries = [e for e in entries if e["week_start"] == one_week]
            pids = {e["participant_id"] for e in week_entries}
            pids.update(_line_aggregates_locked(conn, one_week).keys())
            if participant_id is not None:
                pids = {participant_id}
            for pid in sorted(pids):
                all_balances.append(
                    _balance_for_participant_locked(
                        conn,
                        one_week,
                        pid,
                        [e for e in week_entries if e["participant_id"] == pid],
                    )
                )
        return {"weeks": weeks, "balances": all_balances, "count": len(all_balances)}


def participant_settlement(conn: sqlite3.Connection, participant_id: str, week_start: str | None = None):
    """Participant-facing settlement.  It intentionally contains no profile."""
    get_participant(conn, participant_id)
    if week_start:
        result = week_detail(conn, week_start, participant_id)
        result["participant_id"] = participant_id
        result["profile_included"] = False
        return result

    result = week_balances(conn, None, participant_id=participant_id)
    with read_transaction(conn):
        result["session_entries"] = _compute_entries_locked(
            conn, participant_id=participant_id
        )
        result["payment_batches"] = _batches_for_participant_week_locked(
            conn, participant_id, None
        )
    result["participant_id"] = participant_id
    result["profile_included"] = False
    return result


# ---------------------------------------------------------------------------
# Payment batches / fixed lists
# ---------------------------------------------------------------------------


def _fingerprint(week_start: str, lines: list[dict[str, Any]]) -> str:
    canonical = json.dumps(
        {
            "week_start": week_start,
            "lines": [[str(x["participant_id"]), int(x["amount_fen"])] for x in lines],
        },
        separators=(",", ":"),
        sort_keys=False,
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _refunds_for_line_locked(conn: sqlite3.Connection, line_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM subsidy_refunds WHERE line_id = ? ORDER BY created_at, id",
        (line_id,),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["refund_id"] = item["id"]
        item["amount_fen"] = int(item["amount_fen"])
        item["amount"] = money_public(item["amount_fen"])
        result.append(item)
    return result


def _line_payload_locked(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    line = dict(row)
    line["amount_fen"] = int(line["amount_fen"])
    line["amount"] = money_public(line["amount_fen"])
    line["batch_id"] = line["batch_id"]
    line["line_no"] = int(line["line_no"])
    refunds = _refunds_for_line_locked(conn, line["id"])
    refunded_fen = sum(int(r["amount_fen"]) for r in refunds)
    line["refunds"] = refunds
    line["refunded_fen"] = refunded_fen
    line["refunded_amount"] = money_public(refunded_fen)
    line["net_paid_fen"] = line["amount_fen"] - refunded_fen if line["result_status"] == PAID else 0
    line["net_paid_amount"] = money_public(line["net_paid_fen"])
    # Convenience status alias while preserving the explicit field.
    line["status"] = line["result_status"]
    return line


def _batch_detail_locked(conn: sqlite3.Connection, batch_id: str) -> dict[str, Any]:
    batch = conn.execute("SELECT * FROM subsidy_batches WHERE id = ?", (batch_id,)).fetchone()
    if batch is None:
        raise ApiError(404, "subsidy_batch_not_found", f"Subsidy batch {batch_id!r} was not found")
    line_rows = conn.execute(
        "SELECT * FROM subsidy_batch_lines WHERE batch_id = ? ORDER BY line_no",
        (batch_id,),
    ).fetchall()
    lines = [_line_payload_locked(conn, row) for row in line_rows]
    total_fen = sum(int(x["amount_fen"]) for x in lines)
    pending_fen = sum(int(x["amount_fen"]) for x in lines if x["result_status"] == PENDING)
    paid_fen = sum(int(x["amount_fen"]) for x in lines if x["result_status"] == PAID)
    unpaid_fen = sum(int(x["amount_fen"]) for x in lines if x["result_status"] == UNPAID)
    refunded_fen = sum(int(x["refunded_fen"]) for x in lines)
    decided = len(lines) - sum(1 for x in lines if x["result_status"] == PENDING)
    if decided == len(lines):
        confirmation_state = "all_confirmed"
    elif decided == 0:
        confirmation_state = "pending"
    else:
        confirmation_state = "partially_confirmed"
    return {
        "batch_id": batch["id"],
        "id": batch["id"],
        "week_start": batch["week_start"],
        "created_at": batch["created_at"],
        "request_fingerprint": batch["request_fingerprint"],
        "lines": lines,
        "line_count": len(lines),
        "total_amount_fen": total_fen,
        "total_amount": money_public(total_fen),
        "pending_amount_fen": pending_fen,
        "pending_amount": money_public(pending_fen),
        "paid_amount_fen": paid_fen,
        "paid_amount": money_public(paid_fen),
        "unpaid_amount_fen": unpaid_fen,
        "unpaid_amount": money_public(unpaid_fen),
        "refunded_amount_fen": refunded_fen,
        "refunded_amount": money_public(refunded_fen),
        "net_paid_amount_fen": paid_fen - refunded_fen,
        "net_paid_amount": money_public(paid_fen - refunded_fen),
        "confirmation_state": confirmation_state,
        # The list itself is always issued; line results track offline outcome.
        "status": "issued",
    }


def create_batch(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    week_start: str,
    lines: list[dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    if not batch_id or not str(batch_id).strip():
        raise ApiError(422, "batch_id_required", "A caller-defined batch number is required")
    bid = str(batch_id).strip()
    week = parse_week_start(week_start)
    if not lines:
        raise ApiError(422, "subsidy_batch_lines_required", "At least one recipient row is required")

    normalized_lines: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(lines):
        pid = str(raw.get("participant_id", "")).strip()
        if not pid:
            raise ApiError(422, "participant_id_required", f"lines[{index}].participant_id is required")
        if pid in seen:
            raise ApiError(
                409,
                "duplicate_participant_in_batch",
                "Each participant may appear at most once in a weekly subsidy batch",
                {"participant_id": pid},
            )
        seen.add(pid)
        if raw.get("amount_fen") is not None:
            amount_fen = int(raw["amount_fen"])
            if amount_fen < 0:
                raise ApiError(422, "invalid_amount", "amount_fen must be non-negative")
        else:
            amount_fen = parse_money_fen(
                raw.get("amount"), field=f"lines[{index}].amount"
            )
        if amount_fen <= 0:
            raise ApiError(
                422,
                "subsidy_amount_must_be_positive",
                "Each batch row must contain a positive sendable amount",
                {"participant_id": pid},
            )
        normalized_lines.append({"participant_id": pid, "amount_fen": amount_fen})

    fingerprint = _fingerprint(week, normalized_lines)
    now = utcnow()

    with immediate_transaction(conn):
        existing = conn.execute(
            "SELECT id, request_fingerprint FROM subsidy_batches WHERE id = ?", (bid,)
        ).fetchone()
        if existing is not None:
            if existing["request_fingerprint"] != fingerprint:
                raise ApiError(
                    409,
                    "batch_id_reused_for_different_request",
                    "This batch number already identifies a different week/request",
                    {"batch_id": bid},
                )
            detail = _batch_detail_locked(conn, bid)
            detail["idempotent_replay"] = True
            return detail, False

        errors: list[dict[str, Any]] = []
        for index, line in enumerate(normalized_lines, start=1):
            # Internal IDs remain usable even if the ordinary profile/account
            # has been cleared.  An ID with no payable history simply has a
            # zero sendable balance and fails the full-amount check below.
            balance = _balance_for_participant_locked(conn, week, line["participant_id"])
            sendable = int(balance["sendable_amount_fen"])
            if line["amount_fen"] != sendable or sendable <= 0:
                errors.append(
                    {
                        "line_no": index,
                        "participant_id": line["participant_id"],
                        "submitted_amount": money_public(line["amount_fen"]),
                        "submitted_amount_fen": line["amount_fen"],
                        "sendable_amount": money_public(sendable),
                        "sendable_amount_fen": sendable,
                        "current_payable": balance["current_payable"],
                        "pending_amount": balance["pending_amount"],
                        "net_paid": balance["net_paid"],
                    }
                )
        if errors:
            raise ApiError(
                409,
                "subsidy_batch_not_full_sendable_amount",
                "Whole batch rejected: every row must equal the currently full positive sendable amount",
                {"week_start": week, "lines": errors},
            )

        conn.execute(
            """
            INSERT INTO subsidy_batches (id, week_start, request_fingerprint, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (bid, week, fingerprint, now),
        )
        for line_no, line in enumerate(normalized_lines, start=1):
            line_id = new_id("sbl")
            conn.execute(
                """
                INSERT INTO subsidy_batch_lines
                    (id, batch_id, line_no, participant_id, week_start,
                     amount_fen, result_status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (line_id, bid, line_no, line["participant_id"], week, line["amount_fen"], now),
            )
        audit_locked(
            conn,
            "subsidy_batch",
            bid,
            "subsidy_batch.issued",
            {"week_start": week, "line_count": len(normalized_lines)},
        )
        detail = _batch_detail_locked(conn, bid)
        detail["idempotent_replay"] = False
        return detail, True


def get_batch(conn: sqlite3.Connection, batch_id: str) -> dict[str, Any]:
    with read_transaction(conn):
        return _batch_detail_locked(conn, batch_id)


def list_batches(conn: sqlite3.Connection, week_start: str | None = None) -> dict[str, Any]:
    week = parse_week_start(week_start) if week_start else None
    with read_transaction(conn):
        batches = _list_batches_locked(conn, week)
    return {"week_start": week, "batches": batches, "count": len(batches)}


def _list_batches_locked(conn: sqlite3.Connection, week: str | None) -> list[dict[str, Any]]:
    if week:
        rows = conn.execute(
            "SELECT id FROM subsidy_batches WHERE week_start = ? ORDER BY created_at, id",
            (week,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT id FROM subsidy_batches ORDER BY created_at, id").fetchall()
    return [_batch_detail_locked(conn, str(r["id"])) for r in rows]


def week_detail(
    conn: sqlite3.Connection, week_start: str, participant_id: str | None = None
) -> dict[str, Any]:
    """One-snapshot owner/participant week detail."""
    week = parse_week_start(week_start)
    with read_transaction(conn):
        entries = _compute_entries_locked(
            conn, week_start=week, participant_id=participant_id
        )
        pids = {str(e["participant_id"]) for e in entries}
        pids.update(_line_aggregates_locked(conn, week).keys())
        if participant_id is not None:
            pids = {participant_id}
        balances = [
            _balance_for_participant_locked(
                conn,
                week,
                pid,
                [e for e in entries if e["participant_id"] == pid],
            )
            for pid in sorted(pids)
        ]
        if participant_id is None:
            batches = _list_batches_locked(conn, week)
        else:
            batches = _batches_for_participant_week_locked(conn, participant_id, week)
    return {
        "week_start": week,
        "balances": balances,
        "count": len(balances),
        "session_entries": entries,
        "entries": entries,
        "batches": batches,
        "payment_batches": batches,
    }


def _batches_for_participant_week_locked(
    conn: sqlite3.Connection, participant_id: str, week_start: str | None
) -> list[dict[str, Any]]:
    sql = """
        SELECT DISTINCT b.id
        FROM subsidy_batches b
        JOIN subsidy_batch_lines l ON l.batch_id = b.id
        WHERE l.participant_id = ?
    """
    params: list[Any] = [participant_id]
    if week_start is not None:
        sql += " AND b.week_start = ?"
        params.append(week_start)
    sql += " ORDER BY b.created_at, b.id"
    ids = [str(r["id"]) for r in conn.execute(sql, params).fetchall()]
    return [_batch_detail_locked(conn, bid) for bid in ids]


# ---------------------------------------------------------------------------
# Offline line confirmation
# ---------------------------------------------------------------------------


def _find_line_locked(
    conn: sqlite3.Connection,
    *,
    line_id: str | None = None,
    batch_id: str | None = None,
    line_no: int | None = None,
) -> sqlite3.Row:
    if line_id is not None:
        row = conn.execute(
            "SELECT * FROM subsidy_batch_lines WHERE id = ?", (line_id,)
        ).fetchone()
        if row is None:
            raise ApiError(404, "subsidy_line_not_found", f"Payment line {line_id!r} was not found")
        return row
    if batch_id is None or line_no is None:
        raise ApiError(422, "line_locator_required", "line_id or batch_id plus line_no is required")
    row = conn.execute(
        "SELECT * FROM subsidy_batch_lines WHERE batch_id = ? AND line_no = ?",
        (batch_id, int(line_no)),
    ).fetchone()
    if row is None:
        raise ApiError(
            404,
            "subsidy_line_not_found",
            f"Line {line_no} of batch {batch_id!r} was not found",
        )
    return row


def confirm_line(
    conn: sqlite3.Connection,
    *,
    result: str,
    line_id: str | None = None,
    batch_id: str | None = None,
    line_no: int | None = None,
    note: str | None = None,
) -> tuple[dict[str, Any], bool]:
    if result not in {PAID, UNPAID}:
        raise ApiError(422, "invalid_payment_result", "result must be 'paid' or 'unpaid'")
    now = utcnow()
    with immediate_transaction(conn):
        row = _find_line_locked(conn, line_id=line_id, batch_id=batch_id, line_no=line_no)
        current = row["result_status"]
        if current != PENDING:
            if current == result:
                detail = _batch_detail_locked(conn, row["batch_id"])
                target = next(x for x in detail["lines"] if x["id"] == row["id"])
                target["idempotent_replay"] = True
                return target, False
            raise ApiError(
                409,
                "payment_result_already_recorded",
                "A confirmed offline line result cannot be changed",
                {
                    "line_id": row["id"],
                    "line_no": row["line_no"],
                    "recorded_result": current,
                    "submitted_result": result,
                },
            )

        conn.execute(
            """
            UPDATE subsidy_batch_lines
            SET result_status = ?, confirmed_at = ?, confirmation_note = ?
            WHERE id = ? AND result_status = 'pending'
            """,
            (result, now, note, row["id"]),
        )
        audit_locked(
            conn,
            "subsidy_line",
            row["id"],
            f"subsidy_line.confirmed_{result}",
            {"batch_id": row["batch_id"], "line_no": row["line_no"]},
        )
        fresh = conn.execute("SELECT * FROM subsidy_batch_lines WHERE id = ?", (row["id"],)).fetchone()
        payload = _line_payload_locked(conn, fresh)
        payload["idempotent_replay"] = False
        return payload, True


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------


def register_refund(
    conn: sqlite3.Connection,
    *,
    refund_id: str,
    amount: Any = None,
    amount_fen: int | None = None,
    line_id: str | None = None,
    batch_id: str | None = None,
    line_no: int | None = None,
    note: str | None = None,
) -> tuple[dict[str, Any], bool]:
    if not refund_id or not str(refund_id).strip():
        raise ApiError(422, "refund_id_required", "A caller-defined refund number is required")
    rid = str(refund_id).strip()
    if amount_fen is not None:
        amount_fen = int(amount_fen)
        if amount_fen <= 0:
            raise ApiError(422, "refund_amount_must_be_positive", "Refund amount must be positive")
    else:
        amount_fen = parse_money_fen(amount, field="refund amount")
        if amount_fen <= 0:
            raise ApiError(422, "refund_amount_must_be_positive", "Refund amount must be positive")

    with immediate_transaction(conn):
        line = _find_line_locked(
            conn, line_id=line_id, batch_id=batch_id, line_no=line_no
        )
        existing = conn.execute(
            "SELECT * FROM subsidy_refunds WHERE id = ?", (rid,)
        ).fetchone()
        if existing is not None:
            if existing["line_id"] != line["id"] or int(existing["amount_fen"]) != amount_fen:
                raise ApiError(
                    409,
                    "refund_id_reused_for_different_content",
                    "This refund number already identifies a different line or amount",
                    {"refund_id": rid},
                )
            payload = dict(existing)
            payload["refund_id"] = payload["id"]
            payload["amount"] = money_public(int(payload["amount_fen"]))
            payload["idempotent_replay"] = True
            return payload, False

        if line["result_status"] != PAID:
            raise ApiError(
                409,
                "refund_requires_paid_line",
                "A return can only be registered against an offline-confirmed paid line",
                {"line_id": line["id"], "status": line["result_status"]},
            )
        previously_refunded = conn.execute(
            "SELECT COALESCE(SUM(amount_fen), 0) FROM subsidy_refunds WHERE line_id = ?",
            (line["id"],),
        ).fetchone()[0]
        if int(previously_refunded) + amount_fen > int(line["amount_fen"]):
            raise ApiError(
                409,
                "refund_amount_exceeds_line_payment",
                "Cumulative returns cannot exceed the confirmed amount paid on this line",
                {
                    "line_id": line["id"],
                    "paid_amount": money_public(int(line["amount_fen"])),
                    "already_refunded": money_public(int(previously_refunded)),
                    "requested_refund": money_public(amount_fen),
                    "maximum_additional_refund": money_public(
                        int(line["amount_fen"]) - int(previously_refunded)
                    ),
                },
            )

        now = utcnow()
        conn.execute(
            """
            INSERT INTO subsidy_refunds
                (id, line_id, amount_fen, note, status, created_at)
            VALUES (?, ?, ?, ?, 'received', ?)
            """,
            (rid, line["id"], amount_fen, note, now),
        )
        audit_locked(
            conn,
            "subsidy_refund",
            rid,
            "subsidy_refund.received",
            {"line_id": line["id"], "amount_fen": amount_fen},
        )
        row = conn.execute("SELECT * FROM subsidy_refunds WHERE id = ?", (rid,)).fetchone()
        payload = dict(row)
        payload["refund_id"] = rid
        payload["amount"] = money_public(amount_fen)
        payload["idempotent_replay"] = False
        return payload, True


def get_refund(conn: sqlite3.Connection, refund_id: str) -> dict[str, Any]:
    with read_transaction(conn):
        row = conn.execute("SELECT * FROM subsidy_refunds WHERE id = ?", (refund_id,)).fetchone()
        if row is None:
            raise ApiError(404, "refund_not_found", f"Refund {refund_id!r} was not found")
        payload = dict(row)
        payload["refund_id"] = payload["id"]
        payload["amount"] = money_public(int(payload["amount_fen"]))
        return payload
