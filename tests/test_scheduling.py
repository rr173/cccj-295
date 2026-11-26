"""Unit and concurrency tests for interview booking and batch rescheduling.

The scenarios cover:

* booking overlap rules (room and participant, half-open intervals, back-to-back),
* one active booking per interview and not-started/valid-participant rules,
* reschedule validation, same-order slot reuse, internal and external conflicts,
* the consultation state machine (accept/reject/withdraw/expire/late answers),
* atomic unanimous application, purge integration, restart recovery,
* threaded commit-order races producing only consistent results.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.db import connect, init_db
from app import scheduling
from app.services import (
    ApiError,
    create_deletion_request,
    create_interview,
    create_participant,
    revoke_session,
)


@pytest.fixture()
def clock():
    """A single frozen instant so offsets map to stable, comparable strings."""
    base = datetime.now(timezone.utc).replace(microsecond=0)

    def at(minutes_from_now: float) -> datetime:
        return base + timedelta(minutes=minutes_from_now)

    return at


@pytest.fixture()
def conn(tmp_path: Path, clock):
    db = connect(tmp_path / "scheduling.db")
    init_db(db)
    create_interview(db, interview_id="int-1", title="Interview One")
    create_interview(db, interview_id="int-2", title="Interview Two")
    create_interview(db, interview_id="int-3", title="Interview Three")
    for pid, name, token in [
        ("p-alice", "Alice", "sess-alice"),
        ("p-bob", "Bob", "sess-bob"),
        ("p-carol", "Carol", "sess-carol"),
        ("p-dave", "Dave", "sess-dave"),
    ]:
        create_participant(
            db, participant_id=pid, display_name=name, profile={}, session_token=token
        )
    scheduling.create_room(db, room_id="room-a", label="Room A")
    scheduling.create_room(db, room_id="room-b", label="Room B")
    yield db
    db.close()


def _booking(conn, clock, *, bid, interview_id="int-1", room="room-a", pids, start=100, end=200):
    return scheduling.create_booking(
        conn,
        booking_id=bid,
        interview_id=interview_id,
        room_id=room,
        participant_ids=pids,
        start_at=clock(start),
        end_at=clock(end),
    )


# ---------------------------------------------------------------------------
# Booking rules
# ---------------------------------------------------------------------------


def test_booking_basic_and_one_active_per_interview(conn, clock) -> None:
    data = _booking(conn, clock, bid="bk-1", pids=["p-alice", "p-bob"])
    assert data["status"] == "confirmed"
    assert data["participant_ids"] == ["p-alice", "p-bob"]

    with pytest.raises(ApiError) as exc:
        _booking(conn, clock, bid="bk-2", room="room-b", pids=["p-carol"])
    assert exc.value.code == "active_booking_exists"

    # Cancelling frees the interview for a fresh booking.
    scheduling.cancel_booking(conn, "bk-1")
    again = _booking(conn, clock, bid="bk-3", room="room-b", pids=["p-carol"], start=300, end=400)
    assert again["id"] == "bk-3"


def test_booking_rejects_past_start_and_bad_interval(conn, clock) -> None:
    with pytest.raises(ApiError) as exc:
        scheduling.create_booking(
            conn,
            booking_id="bk-past",
            interview_id="int-1",
            room_id="room-a",
            participant_ids=["p-alice"],
            start_at=clock(-10),
            end_at=clock(10),
        )
    assert exc.value.code == "booking_already_started"

    with pytest.raises(ApiError) as exc:
        scheduling.create_booking(
            conn,
            booking_id="bk-bad-interval",
            interview_id="int-1",
            room_id="room-a",
            participant_ids=["p-alice"],
            start_at=clock(200),
            end_at=clock(100),
        )
    assert exc.value.code == "invalid_interval"


def test_room_and_participant_overlap_blocked_but_back_to_back_allowed(conn, clock) -> None:
    _booking(conn, clock, bid="bk-1", room="room-a", pids=["p-alice"], start=100, end=200)

    # Same room, overlapping interval.
    with pytest.raises(ApiError) as exc:
        _booking(conn, clock, bid="bk-2", interview_id="int-2", room="room-a",
                 pids=["p-bob"], start=150, end=250)
    assert exc.value.code == "slot_conflict"

    # Same participant, different room, overlapping interval.
    with pytest.raises(ApiError) as exc:
        _booking(conn, clock, bid="bk-3", interview_id="int-2", room="room-b",
                 pids=["p-alice", "p-carol"], start=150, end=250)
    assert exc.value.code == "slot_conflict"

    # Back-to-back on the same room and same participant is allowed.
    ok = _booking(conn, clock, bid="bk-4", interview_id="int-2", room="room-a",
                  pids=["p-alice"], start=200, end=300)
    assert ok["id"] == "bk-4"


def test_cancel_started_booking_rejected(conn, clock) -> None:
    _booking(conn, clock, bid="bk-1", pids=["p-alice"], start=100, end=200)
    conn.execute("UPDATE bookings SET start_at = ? WHERE id = 'bk-1'", (clock(-1).isoformat(),))
    with pytest.raises(ApiError) as exc:
        scheduling.cancel_booking(conn, "bk-1")
    assert exc.value.code == "booking_already_started"


def test_booking_needs_valid_participant(conn, clock) -> None:
    create_deletion_request(conn, "p-dave")
    with pytest.raises(ApiError) as exc:
        _booking(conn, clock, bid="bk-invalid", interview_id="int-2",
                 room="room-a", pids=["p-dave"], start=100, end=200)
    assert exc.value.code == "no_valid_participant"


# ---------------------------------------------------------------------------
# Reschedule order validation
# ---------------------------------------------------------------------------


def test_reschedule_validation_rules(conn, clock) -> None:
    _booking(conn, clock, bid="bk-1", room="room-a", pids=["p-alice", "p-bob"], start=100, end=200)

    # Deadline in the past.
    with pytest.raises(ApiError) as exc:
        scheduling.create_reschedule_order(
            conn,
            order_id="ro-bad",
            items=[{"booking_id": "bk-1", "room_id": "room-b",
                    "start_at": clock(300), "end_at": clock(400)}],
            consultation_deadline=clock(-1),
        )
    assert exc.value.code == "deadline_must_be_future"

    # Deadline later than an original start.
    with pytest.raises(ApiError) as exc:
        scheduling.create_reschedule_order(
            conn,
            order_id="ro-bad2",
            items=[{"booking_id": "bk-1", "room_id": "room-b",
                    "start_at": clock(300), "end_at": clock(400)}],
            consultation_deadline=clock(150),
        )
    assert exc.value.code == "deadline_too_late"

    # Item changes nothing (same room and same interval).
    with pytest.raises(ApiError) as exc:
        scheduling.create_reschedule_order(
            conn,
            order_id="ro-bad3",
            items=[{"booking_id": "bk-1", "room_id": "room-a",
                    "start_at": clock(100), "end_at": clock(200)}],
            consultation_deadline=clock(50),
        )
    assert exc.value.code == "reschedule_item_unchanged"

    # Duplicate booking inside one order.
    with pytest.raises(ApiError) as exc:
        scheduling.create_reschedule_order(
            conn,
            order_id="ro-bad4",
            items=[
                {"booking_id": "bk-1", "room_id": "room-b",
                 "start_at": clock(300), "end_at": clock(400)},
                {"booking_id": "bk-1", "room_id": "room-b",
                 "start_at": clock(400), "end_at": clock(500)},
            ],
            consultation_deadline=clock(50),
        )
    assert exc.value.code == "duplicate_booking_in_order"

    # Unknown room.
    with pytest.raises(ApiError) as exc:
        scheduling.create_reschedule_order(
            conn,
            order_id="ro-bad5",
            items=[{"booking_id": "bk-1", "room_id": "room-zzz",
                    "start_at": clock(300), "end_at": clock(400)}],
            consultation_deadline=clock(50),
        )
    assert exc.value.code == "room_not_found"


def test_booking_cannot_join_two_pending_orders(conn, clock) -> None:
    _booking(conn, clock, bid="bk-1", room="room-a", pids=["p-alice"], start=100, end=200)
    scheduling.create_reschedule_order(
        conn,
        order_id="ro-1",
        items=[{"booking_id": "bk-1", "room_id": "room-b",
                "start_at": clock(300), "end_at": clock(400)}],
        consultation_deadline=clock(50),
    )
    with pytest.raises(ApiError) as exc:
        scheduling.create_reschedule_order(
            conn,
            order_id="ro-2",
            items=[{"booking_id": "bk-1", "room_id": "room-b",
                    "start_at": clock(450), "end_at": clock(550)}],
            consultation_deadline=clock(50),
        )
    assert exc.value.code == "booking_in_other_pending_order"


def test_reschedule_internal_room_conflict(conn, clock) -> None:
    _booking(conn, clock, bid="bk-1", interview_id="int-1", room="room-a",
             pids=["p-alice"], start=100, end=200)
    _booking(conn, clock, bid="bk-2", interview_id="int-2", room="room-b",
             pids=["p-bob"], start=300, end=400)
    with pytest.raises(ApiError) as exc:
        scheduling.create_reschedule_order(
            conn,
            order_id="ro-clash",
            items=[
                {"booking_id": "bk-1", "room_id": "room-a",
                 "start_at": clock(500), "end_at": clock(600)},
                {"booking_id": "bk-2", "room_id": "room-a",
                 "start_at": clock(550), "end_at": clock(650)},
            ],
            consultation_deadline=clock(50),
        )
    assert exc.value.code == "reschedule_internal_room_conflict"


def test_reschedule_internal_participant_conflict(conn, clock) -> None:
    _booking(conn, clock, bid="bk-1", interview_id="int-1", room="room-a",
             pids=["p-alice"], start=100, end=200)
    _booking(conn, clock, bid="bk-2", interview_id="int-2", room="room-b",
             pids=["p-alice"], start=300, end=400)
    with pytest.raises(ApiError) as exc:
        scheduling.create_reschedule_order(
            conn,
            order_id="ro-pclash",
            items=[
                {"booking_id": "bk-1", "room_id": "room-a",
                 "start_at": clock(500), "end_at": clock(600)},
                {"booking_id": "bk-2", "room_id": "room-b",
                 "start_at": clock(550), "end_at": clock(650)},
            ],
            consultation_deadline=clock(50),
        )
    assert exc.value.code == "reschedule_internal_participant_conflict"


def test_pending_order_holds_original_and_proposed_slots(conn, clock) -> None:
    _booking(conn, clock, bid="bk-1", room="room-a", pids=["p-alice"], start=100, end=200)
    scheduling.create_reschedule_order(
        conn,
        order_id="ro-1",
        items=[{"booking_id": "bk-1", "room_id": "room-b",
                "start_at": clock(300), "end_at": clock(400)}],
        consultation_deadline=clock(50),
    )

    # Original slot cannot be taken by an outside booking (room and participant).
    with pytest.raises(ApiError) as exc:
        _booking(conn, clock, bid="bk-out-1", interview_id="int-2", room="room-a",
                 pids=["p-bob"], start=150, end=160)
    assert exc.value.code == "slot_conflict"
    with pytest.raises(ApiError) as exc:
        _booking(conn, clock, bid="bk-out-2", interview_id="int-3", room="room-b",
                 pids=["p-alice"], start=150, end=160)
    assert exc.value.code == "slot_conflict"

    # Proposed slot cannot be taken either.
    with pytest.raises(ApiError) as exc:
        _booking(conn, clock, bid="bk-out-3", interview_id="int-2", room="room-b",
                 pids=["p-carol"], start=350, end=360)
    assert exc.value.code == "slot_conflict"
    with pytest.raises(ApiError) as exc:
        _booking(conn, clock, bid="bk-out-4", interview_id="int-3", room="room-a",
                 pids=["p-alice"], start=350, end=360)
    assert exc.value.code == "slot_conflict"

    # Back-to-back against a held slot stays allowed.
    touch = _booking(conn, clock, bid="bk-out-5", interview_id="int-2", room="room-b",
                     pids=["p-carol"], start=400, end=420)
    assert touch["id"] == "bk-out-5"


def test_other_pending_order_cannot_use_held_slots(conn, clock) -> None:
    _booking(conn, clock, bid="bk-1", interview_id="int-1", room="room-a",
             pids=["p-alice"], start=100, end=200)
    _booking(conn, clock, bid="bk-2", interview_id="int-2", room="room-b",
             pids=["p-bob"], start=210, end=290)
    scheduling.create_reschedule_order(
        conn,
        order_id="ro-1",
        items=[{"booking_id": "bk-1", "room_id": "room-b",
                "start_at": clock(500), "end_at": clock(600)}],
        consultation_deadline=clock(50),
    )
    # bk-2 proposes room-b 550-580, overlapping ro-1's proposed room-b slot.
    with pytest.raises(ApiError) as exc:
        scheduling.create_reschedule_order(
            conn,
            order_id="ro-2",
            items=[{"booking_id": "bk-2", "room_id": "room-b",
                    "start_at": clock(550), "end_at": clock(580)}],
            consultation_deadline=clock(50),
        )
    assert exc.value.code == "slot_conflict"


# ---------------------------------------------------------------------------
# Consultation responses and the state machine
# ---------------------------------------------------------------------------


def _two_person_order(conn, clock) -> str:
    _booking(conn, clock, bid="bk-1", room="room-a",
             pids=["p-alice", "p-bob"], start=100, end=200)
    scheduling.create_reschedule_order(
        conn,
        order_id="ro-1",
        items=[{"booking_id": "bk-1", "room_id": "room-b",
                "start_at": clock(300), "end_at": clock(400)}],
        consultation_deadline=clock(50),
    )
    return "ro-1"


def test_unanimous_acceptance_applies_atomically(conn, clock) -> None:
    _two_person_order(conn, clock)
    before = scheduling.get_booking(conn, "bk-1")
    assert before["room_id"] == "room-a"  # original stays confirmed during consultation

    detail = scheduling.respond_to_order(conn, order_id="ro-1",
                                         participant_id="p-alice", decision="accepted")
    assert detail["status"] == "pending"
    assert detail["response_summary"] == {"total": 2, "accepted": 1, "rejected": 0, "pending": 1}

    detail = scheduling.respond_to_order(conn, order_id="ro-1",
                                         participant_id="p-bob", decision="accepted")
    assert detail["status"] == "approved"
    after = scheduling.get_booking(conn, "bk-1")
    assert after["room_id"] == "room-b"
    assert datetime.fromisoformat(after["start_at"]) == clock(300)
    # The original, now-vacated interval is released.
    assert scheduling.list_bookings(conn, room_id="room-a") == []


def test_rejection_ends_order_without_moving_and_releases_capacity(conn, clock) -> None:
    _two_person_order(conn, clock)
    scheduling.respond_to_order(conn, order_id="ro-1",
                                participant_id="p-alice", decision="accepted")
    detail = scheduling.respond_to_order(conn, order_id="ro-1",
                                         participant_id="p-bob", decision="rejected")
    assert detail["status"] == "rejected"
    booking = scheduling.get_booking(conn, "bk-1")
    assert booking["room_id"] == "room-a"

    # Capacity used by the rejected proposal is released: room-b 300-400 is free.
    reuse = _booking(conn, clock, bid="bk-reuse", interview_id="int-2", room="room-b",
                     pids=["p-carol"], start=300, end=400)
    assert reuse["id"] == "bk-reuse"

    # A late answer cannot revive the finished order; the first answer rule
    # and the ended-order rule both block it.
    with pytest.raises(ApiError) as exc:
        scheduling.respond_to_order(conn, order_id="ro-1",
                                    participant_id="p-alice", decision="rejected")
    assert exc.value.code == "response_already_recorded"


def test_first_response_immutable_and_replay_idempotent(conn, clock) -> None:
    _two_person_order(conn, clock)
    first = scheduling.respond_to_order(conn, order_id="ro-1",
                                        participant_id="p-alice", decision="accepted")
    assert first.get("idempotent_replay") is not True

    replay = scheduling.respond_to_order(conn, order_id="ro-1",
                                         participant_id="p-alice", decision="accepted")
    assert replay["idempotent_replay"] is True

    with pytest.raises(ApiError) as exc:
        scheduling.respond_to_order(conn, order_id="ro-1",
                                    participant_id="p-alice", decision="rejected")
    assert exc.value.code == "response_already_recorded"


def test_only_involved_participant_may_answer(conn, clock) -> None:
    _two_person_order(conn, clock)
    with pytest.raises(ApiError) as exc:
        scheduling.respond_to_order(conn, order_id="ro-1",
                                    participant_id="p-carol", decision="accepted")
    assert exc.value.code == "not_reschedule_participant"


def test_withdraw_releases_proposed_slots(conn, clock) -> None:
    _two_person_order(conn, clock)
    detail = scheduling.withdraw_order(conn, "ro-1")
    assert detail["status"] == "withdrawn"
    reuse = _booking(conn, clock, bid="bk-reuse", interview_id="int-2", room="room-b",
                     pids=["p-carol"], start=300, end=400)
    assert reuse["id"] == "bk-reuse"
    with pytest.raises(ApiError) as exc:
        scheduling.withdraw_order(conn, "ro-1")
    assert exc.value.code == "reschedule_not_pending"


def test_expiry_releases_capacity_and_blocks_late_answers(conn, clock) -> None:
    _two_person_order(conn, clock)
    # Force the deadline into the past, then trigger the lazy sweep.
    conn.execute(
        "UPDATE reschedule_orders SET consultation_deadline = ? WHERE id = 'ro-1'",
        (clock(-1).isoformat(),),
    )
    result = scheduling.sweep_expired_orders(conn)
    assert result["expired_order_ids"] == ["ro-1"]

    detail = scheduling.order_detail(conn, "ro-1")
    assert detail["status"] == "expired"
    reuse = _booking(conn, clock, bid="bk-reuse", interview_id="int-2", room="room-b",
                     pids=["p-carol"], start=300, end=400)
    assert reuse["id"] == "bk-reuse"

    with pytest.raises(ApiError) as exc:
        scheduling.respond_to_order(conn, order_id="ro-1",
                                    participant_id="p-carol", decision="accepted")
    # Carol is not involved; use an involved participant for the late-answer rule.
    assert exc.value.code == "not_reschedule_participant"
    with pytest.raises(ApiError) as exc:
        scheduling.respond_to_order(conn, order_id="ro-1",
                                    participant_id="p-alice", decision="accepted")
    assert exc.value.code == "reschedule_not_pending"


def test_cancel_booking_invalidates_pending_order_without_moving_others(conn, clock) -> None:
    _booking(conn, clock, bid="bk-1", interview_id="int-1", room="room-a",
             pids=["p-alice"], start=100, end=200)
    _booking(conn, clock, bid="bk-2", interview_id="int-2", room="room-b",
             pids=["p-bob"], start=210, end=290)
    scheduling.create_reschedule_order(
        conn,
        order_id="ro-1",
        items=[
            {"booking_id": "bk-1", "room_id": "room-a",
             "start_at": clock(500), "end_at": clock(600)},
            {"booking_id": "bk-2", "room_id": "room-b",
             "start_at": clock(610), "end_at": clock(690)},
        ],
        consultation_deadline=clock(50),
    )
    scheduling.cancel_booking(conn, "bk-1", reason="owner cancel")
    detail = scheduling.order_detail(conn, "ro-1")
    assert detail["status"] == "cancelled_booking"
    # The other booking keeps its original arrangement.
    other = scheduling.get_booking(conn, "bk-2")
    assert other["room_id"] == "room-b"
    assert datetime.fromisoformat(other["start_at"]) == clock(210)


def test_one_participant_many_bookings_answers_once(conn, clock) -> None:
    _booking(conn, clock, bid="bk-1", interview_id="int-1", room="room-a",
             pids=["p-alice"], start=100, end=200)
    _booking(conn, clock, bid="bk-2", interview_id="int-2", room="room-b",
             pids=["p-alice", "p-bob"], start=210, end=290)
    scheduling.create_reschedule_order(
        conn,
        order_id="ro-1",
        items=[
            {"booking_id": "bk-1", "room_id": "room-a",
             "start_at": clock(500), "end_at": clock(530)},
            {"booking_id": "bk-2", "room_id": "room-b",
             "start_at": clock(540), "end_at": clock(570)},
        ],
        consultation_deadline=clock(50),
    )
    detail = scheduling.respond_to_order(conn, order_id="ro-1",
                                         participant_id="p-alice", decision="accepted")
    assert detail["response_summary"]["total"] == 2  # distinct participants: Alice, Bob
    assert detail["response_summary"]["accepted"] == 1
    rows = conn.execute(
        "SELECT COUNT(*) AS c FROM reschedule_responses "
        "WHERE order_id='ro-1' AND participant_id='p-alice'"
    ).fetchone()
    assert rows["c"] == 1


# ---------------------------------------------------------------------------
# Same-order slot reuse (the headline swap scenario)
# ---------------------------------------------------------------------------


def test_swap_original_slots_between_two_bookings(conn, clock) -> None:
    # Two interviews swap rooms at the same time via one order.
    _booking(conn, clock, bid="bk-1", interview_id="int-1", room="room-a",
             pids=["p-alice"], start=100, end=200)
    _booking(conn, clock, bid="bk-2", interview_id="int-2", room="room-b",
             pids=["p-bob"], start=100, end=200)
    scheduling.create_reschedule_order(
        conn,
        order_id="ro-swap",
        items=[
            {"booking_id": "bk-1", "room_id": "room-b",
             "start_at": clock(100), "end_at": clock(200)},
            {"booking_id": "bk-2", "room_id": "room-a",
             "start_at": clock(100), "end_at": clock(200)},
        ],
        consultation_deadline=clock(50),
    )
    scheduling.respond_to_order(conn, order_id="ro-swap",
                                participant_id="p-alice", decision="accepted")
    detail = scheduling.respond_to_order(conn, order_id="ro-swap",
                                         participant_id="p-bob", decision="accepted")
    assert detail["status"] == "approved"
    assert scheduling.get_booking(conn, "bk-1")["room_id"] == "room-b"
    assert scheduling.get_booking(conn, "bk-2")["room_id"] == "room-a"


def test_reuse_vacated_interval_inside_one_order(conn, clock) -> None:
    # bk-1 vacates room-a 100-200; bk-2 moves into exactly that interval.
    _booking(conn, clock, bid="bk-1", interview_id="int-1", room="room-a",
             pids=["p-alice"], start=100, end=200)
    _booking(conn, clock, bid="bk-2", interview_id="int-2", room="room-b",
             pids=["p-bob"], start=300, end=400)
    scheduling.create_reschedule_order(
        conn,
        order_id="ro-reuse",
        items=[
            {"booking_id": "bk-1", "room_id": "room-b",
             "start_at": clock(300), "end_at": clock(400)},
            {"booking_id": "bk-2", "room_id": "room-a",
             "start_at": clock(100), "end_at": clock(200)},
        ],
        consultation_deadline=clock(50),
    )
    detail = scheduling.order_detail(conn, "ro-reuse")
    assert len(detail["items"]) == 2


# ---------------------------------------------------------------------------
# Purge integration and restart recovery
# ---------------------------------------------------------------------------


def test_participant_purge_cancels_bookings_and_invalidates_orders(conn, clock) -> None:
    _booking(conn, clock, bid="bk-1", interview_id="int-1", room="room-a",
             pids=["p-alice", "p-bob"], start=100, end=200)
    _booking(conn, clock, bid="bk-2", interview_id="int-2", room="room-b",
             pids=["p-carol"], start=100, end=200)
    scheduling.create_reschedule_order(
        conn,
        order_id="ro-1",
        items=[{"booking_id": "bk-1", "room_id": "room-b",
                "start_at": clock(300), "end_at": clock(400)}],
        consultation_deadline=clock(50),
    )
    create_deletion_request(conn, "p-alice")

    assert scheduling.get_booking(conn, "bk-1")["status"] == "cancelled"
    assert scheduling.order_detail(conn, "ro-1")["status"] == "cancelled_booking"
    # Unaffected booking stays confirmed.
    assert scheduling.get_booking(conn, "bk-2")["status"] == "confirmed"
    # The invalidated consultation accepts no answers.
    with pytest.raises(ApiError) as exc:
        scheduling.respond_to_order(conn, order_id="ro-1",
                                    participant_id="p-bob", decision="accepted")
    assert exc.value.code == "reschedule_not_pending"


def test_revoked_session_cannot_answer_but_booking_survives(conn, clock) -> None:
    _two_person_order(conn, clock)
    revoke_session(conn, "sess-alice")
    with pytest.raises(ApiError) as exc:
        scheduling.authenticate_session(conn, "sess-alice")
    assert exc.value.code == "invalid_session"
    # Revoking the session does not cancel the booking.
    assert scheduling.get_booking(conn, "bk-1")["status"] == "confirmed"


def test_restart_recovers_expired_order(tmp_path: Path, clock) -> None:
    db_path = tmp_path / "restart.db"
    db = connect(db_path)
    init_db(db)
    create_interview(db, interview_id="int-1", title="One")
    create_participant(db, participant_id="p-alice", display_name="Alice",
                       profile={}, session_token="s1")
    scheduling.create_room(db, room_id="room-a", label="A")
    scheduling.create_room(db, room_id="room-b", label="B")
    scheduling.create_booking(
        db, booking_id="bk-1", interview_id="int-1", room_id="room-a",
        participant_ids=["p-alice"], start_at=clock(100), end_at=clock(200),
    )
    scheduling.create_reschedule_order(
        db,
        order_id="ro-1",
        items=[{"booking_id": "bk-1", "room_id": "room-b",
                "start_at": clock(300), "end_at": clock(400)}],
        consultation_deadline=clock(50),
    )
    db.execute(
        "UPDATE reschedule_orders SET consultation_deadline = ? WHERE id = 'ro-1'",
        (clock(-1).isoformat(),),
    )
    db.close()

    # Simulate a process restart: a brand new connection plus startup recovery.
    db2 = connect(db_path)
    recovery = scheduling.recover_rescheduling(db2)
    assert recovery["expired_order_ids"] == ["ro-1"]
    assert scheduling.order_detail(db2, "ro-1")["status"] == "expired"
    db2.close()


# ---------------------------------------------------------------------------
# Concurrency: only one consistent winner per race
# ---------------------------------------------------------------------------


def test_concurrent_bookings_for_same_room(tmp_path: Path, clock) -> None:
    db_path = tmp_path / "race-bookings.db"
    setup = connect(db_path)
    init_db(setup)
    create_interview(setup, interview_id="int-base", title="base")
    for i in range(10):
        create_interview(setup, interview_id=f"int-{i}", title=f"I {i}")
        create_participant(setup, participant_id=f"p-{i}", display_name=f"P{i}",
                           profile={}, session_token=f"s-{i}")
    scheduling.create_room(setup, room_id="room-a", label="A")
    setup.close()

    winners: list[str] = []
    errors: list[ApiError] = []
    barrier = threading.Barrier(10)

    def worker(i: int) -> None:
        barrier.wait()
        conn = connect(db_path)
        try:
            scheduling.create_booking(
                conn,
                booking_id=f"bk-race-{i}",
                interview_id=f"int-{i}",
                room_id="room-a",
                participant_ids=[f"p-{i}"],
                start_at=clock(100),
                end_at=clock(200),
            )
            winners.append(f"bk-race-{i}")
        except ApiError as exc:
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(winners) == 1, winners
    assert all(e.code == "slot_conflict" for e in errors)
    verify = connect(db_path)
    count = verify.execute(
        "SELECT COUNT(*) AS c FROM bookings WHERE room_id='room-a' AND status='confirmed'"
    ).fetchone()["c"]
    verify.close()
    assert count == 1


def test_concurrent_responses_apply_exactly_once(tmp_path: Path, clock) -> None:
    db_path = tmp_path / "race-responses.db"
    setup = connect(db_path)
    init_db(setup)
    create_interview(setup, interview_id="int-1", title="One")
    create_participant(setup, participant_id="p-alice", display_name="A",
                       profile={}, session_token="sa")
    create_participant(setup, participant_id="p-bob", display_name="B",
                       profile={}, session_token="sb")
    scheduling.create_room(setup, room_id="room-a", label="A")
    scheduling.create_room(setup, room_id="room-b", label="B")
    scheduling.create_booking(
        setup, booking_id="bk-1", interview_id="int-1", room_id="room-a",
        participant_ids=["p-alice", "p-bob"], start_at=clock(100), end_at=clock(200),
    )
    scheduling.create_reschedule_order(
        setup,
        order_id="ro-1",
        items=[{"booking_id": "bk-1", "room_id": "room-b",
                "start_at": clock(300), "end_at": clock(400)}],
        consultation_deadline=clock(50),
    )
    setup.close()

    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def worker(pid: str) -> None:
        barrier.wait()
        conn = connect(db_path)
        try:
            detail = scheduling.respond_to_order(
                conn, order_id="ro-1", participant_id=pid, decision="accepted"
            )
            outcomes.append(detail["status"])
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(pid,)) for pid in ("p-alice", "p-bob")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    # Whatever the commit order, the order ends approved exactly once.
    assert sorted(outcomes) == ["approved", "pending"]
    verify = connect(db_path)
    order = verify.execute("SELECT status FROM reschedule_orders WHERE id='ro-1'").fetchone()
    booking = verify.execute("SELECT room_id FROM bookings WHERE id='bk-1'").fetchone()
    responses = verify.execute(
        "SELECT COUNT(*) AS c FROM reschedule_responses WHERE order_id='ro-1'"
    ).fetchone()
    verify.close()
    assert order["status"] == "approved"
    assert booking["room_id"] == "room-b"
    assert responses["c"] == 2
