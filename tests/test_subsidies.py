from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import scheduling, services, subsidies
from app.db import connect, init_db
from app.services import ApiError


MONDAY = "2026-11-23"


@pytest.fixture()
def db(tmp_path: Path):
    path = tmp_path / "subsidies.db"
    conn = connect(path)
    init_db(conn)
    conn.row_factory = None
    conn.close()
    conn = connect(path)
    init_db(conn)
    yield conn
    conn.close()


def make_participant(db, pid="p1", token="sess-1"):
    services.create_participant(
        db, participant_id=pid, display_name=pid.title(), profile={"secret": "x"}, session_token=token
    )


def make_past_booking(
    db,
    *,
    bid="bk1",
    pid="p1",
    start="2026-11-23T10:00:00+00:00",
    duration_minutes=180,
    iid=None,
    rid="room1",
):
    iid = iid or f"int-{bid}"
    services.create_interview(db, interview_id=iid, title=f"Interview {bid}")
    scheduling.create_room(db, room_id=rid, label=f"Room {rid}")
    when = datetime.now(timezone.utc) + timedelta(days=2)
    scheduling.create_booking(
        db,
        booking_id=bid,
        interview_id=iid,
        room_id=rid,
        participant_ids=[pid],
        start_at=when,
        end_at=when + timedelta(minutes=duration_minutes),
    )
    start_dt = datetime.fromisoformat(start)
    end_dt = start_dt + timedelta(minutes=duration_minutes)
    db.execute(
        "UPDATE bookings SET start_at = ?, end_at = ? WHERE id = ?",
        (start_dt.isoformat(), end_dt.isoformat(), bid),
    )
    db.commit()
    return bid


def balance(db, pid="p1", week=MONDAY):
    return next(b for b in subsidies.week_balances(db, week)["balances"] if b["participant_id"] == pid)


def test_floor_billing_and_booking_duration(db):
    make_participant(db)
    make_past_booking(db, bid="bk-floor", duration_minutes=60)

    result = subsidies.upsert_attendance(
        db, "bk-floor", [{"participant_id": "p1", "attended_minutes": 59}]
    )
    record = result["records"][0]
    assert record["billable_units"] == 3
    assert record["raw_amount"] == 30
    assert record["counted_amount"] == 30

    with pytest.raises(ApiError) as exc:
        subsidies.upsert_attendance(
            db, "bk-floor", [{"participant_id": "p1", "attended_minutes": 61}]
        )
    assert exc.value.code == "attended_minutes_exceed_booking_duration"


def test_correction_reallocates_weekly_cap_to_later_session(db):
    make_participant(db)
    make_past_booking(
        db,
        bid="bk-early",
        start="2026-11-23T10:00:00+00:00",
        duration_minutes=90,
        rid="r-a",
    )
    make_past_booking(
        db,
        bid="bk-late",
        start="2026-11-25T10:00:00+00:00",
        duration_minutes=180,
        rid="r-b",
    )
    subsidies.upsert_attendance(db, "bk-early", [{"participant_id": "p1", "attended_minutes": 90}])
    subsidies.upsert_attendance(db, "bk-late", [{"participant_id": "p1", "attended_minutes": 120}])

    entries = {e["booking_id"]: e for e in subsidies.list_session_entries(db, week_start=MONDAY)["entries"]}
    assert entries["bk-early"]["counted_amount"] == 60
    assert entries["bk-late"]["counted_amount"] == 40
    assert balance(db)["current_payable"] == 100

    # Correct the earlier session downward; the later session now receives 80.
    subsidies.upsert_attendance(db, "bk-early", [{"participant_id": "p1", "attended_minutes": 30}])
    entries = {e["booking_id"]: e for e in subsidies.list_session_entries(db, week_start=MONDAY)["entries"]}
    assert entries["bk-early"]["counted_amount"] == 20
    assert entries["bk-late"]["counted_amount"] == 80
    assert balance(db)["current_payable"] == 100


def test_pending_cannot_be_resent_until_line_is_unpaid(db):
    make_participant(db)
    make_past_booking(db, bid="bk-bob", duration_minutes=60)
    subsidies.upsert_attendance(db, "bk-bob", [{"participant_id": "p1", "attended_minutes": 60}])
    assert balance(db)["sendable_amount"] == 40

    batch, created = subsidies.create_batch(
        db, batch_id="B1", week_start=MONDAY, lines=[{"participant_id": "p1", "amount": 40}]
    )
    assert created is True
    assert [line["line_no"] for line in batch["lines"]] == [1]
    line_no = batch["lines"][0]["line_no"]
    assert balance(db)["sendable_amount"] == 0
    assert balance(db)["pending_amount"] == 40

    replay, created = subsidies.create_batch(
        db, batch_id="B1", week_start=MONDAY, lines=[{"participant_id": "p1", "amount": 40}]
    )
    assert created is False
    assert replay["idempotent_replay"] is True
    assert replay["lines"][0]["id"] == batch["lines"][0]["id"]

    with pytest.raises(ApiError) as exc:
        subsidies.create_batch(
            db, batch_id="B2", week_start=MONDAY, lines=[{"participant_id": "p1", "amount": 40}]
        )
    assert exc.value.code == "subsidy_batch_not_full_sendable_amount"

    with pytest.raises(ApiError) as exc:
        subsidies.create_batch(
            db, batch_id="B1", week_start=MONDAY, lines=[{"participant_id": "p1", "amount": 39}]
        )
    assert exc.value.code == "batch_id_reused_for_different_request"

    confirmed, created = subsidies.confirm_line(
        db, result="unpaid", batch_id="B1", line_no=line_no
    )
    assert created is True
    assert confirmed["status"] == "unpaid"
    assert balance(db)["pending_amount"] == 0
    assert balance(db)["sendable_amount"] == 40

    batch2, created = subsidies.create_batch(
        db, batch_id="B2", week_start=MONDAY, lines=[{"participant_id": "p1", "amount": 40}]
    )
    assert created is True
    assert batch2["lines"][0]["line_no"] == 1


def test_batch_number_cannot_change_request(db):
    make_participant(db)
    make_past_booking(db, duration_minutes=60)
    subsidies.upsert_attendance(db, "bk1", [{"participant_id": "p1", "attended_minutes": 60}])
    subsidies.create_batch(
        db, batch_id="B", week_start=MONDAY, lines=[{"participant_id": "p1", "amount": 40}]
    )
    with pytest.raises(ApiError) as exc:
        subsidies.create_batch(
            db, batch_id="B", week_start=MONDAY, lines=[{"participant_id": "p1", "amount": 30}]
        )
    assert exc.value.code == "batch_id_reused_for_different_request"


def test_refunds_reconcile_overpayment_and_are_idempotent(db):
    make_participant(db)
    make_past_booking(db, bid="early", start="2026-11-23T10:00:00+00:00", duration_minutes=90, rid="r1")
    make_past_booking(db, bid="late", start="2026-11-25T10:00:00+00:00", duration_minutes=180, rid="r2")
    subsidies.upsert_attendance(db, "early", [{"participant_id": "p1", "attended_minutes": 90}])
    subsidies.upsert_attendance(db, "late", [{"participant_id": "p1", "attended_minutes": 120}])
    batch, _ = subsidies.create_batch(
        db, batch_id="PAY", week_start=MONDAY, lines=[{"participant_id": "p1", "amount": 100}]
    )
    line_no = batch["lines"][0]["line_no"]
    subsidies.confirm_line(db, result="paid", batch_id="PAY", line_no=line_no)

    # Move cap allocation in the same way as the HTTP reconciliation scenario,
    # then correct the later session down.  Final payable is 20 + 20 = 40.
    subsidies.upsert_attendance(db, "early", [{"participant_id": "p1", "attended_minutes": 30}])
    subsidies.upsert_attendance(db, "late", [{"participant_id": "p1", "attended_minutes": 30}])
    b = balance(db)
    assert b["current_payable"] == 40
    assert b["net_paid"] == 100
    assert b["overpaid_amount"] == 60
    assert b["sendable_amount"] == 0

    refund, created = subsidies.register_refund(
        db, refund_id="R1", amount=20, batch_id="PAY", line_no=line_no
    )
    assert created is True
    assert refund["amount"] == 20
    b = balance(db)
    assert b["net_paid"] == 80
    assert b["overpaid_amount"] == 40

    replay, created = subsidies.register_refund(
        db, refund_id="R1", amount=20, batch_id="PAY", line_no=line_no
    )
    assert created is False
    assert replay["idempotent_replay"] is True
    assert balance(db)["refunded"] == 20

    with pytest.raises(ApiError) as exc:
        subsidies.register_refund(
            db, refund_id="R1", amount=30, batch_id="PAY", line_no=line_no
        )
    assert exc.value.code == "refund_id_reused_for_different_content"

    with pytest.raises(ApiError) as exc:
        subsidies.register_refund(
            db, refund_id="R2", amount=81, batch_id="PAY", line_no=line_no
        )
    assert exc.value.code == "refund_amount_exceeds_line_payment"

    subsidies.register_refund(db, refund_id="R2", amount=40, batch_id="PAY", line_no=line_no)
    b = balance(db)
    assert b["refunded"] == 60
    assert b["net_paid"] == 40
    assert b["overpaid_amount"] == 0


def test_person_and_week_balances_are_isolated(db):
    make_participant(db, "p1", "s1")
    make_participant(db, "p2", "s2")
    make_past_booking(db, bid="b1", pid="p1", duration_minutes=60, rid="r1")
    make_past_booking(
        db,
        bid="b2",
        pid="p2",
        start="2026-11-16T10:00:00+00:00",
        duration_minutes=120,
        rid="r2",
    )
    subsidies.upsert_attendance(db, "b1", [{"participant_id": "p1", "attended_minutes": 60}])
    subsidies.upsert_attendance(db, "b2", [{"participant_id": "p2", "attended_minutes": 120}])

    assert balance(db, "p1", "2026-11-23")["current_payable"] == 40
    assert balance(db, "p2", "2026-11-16")["current_payable"] == 80
    week1 = subsidies.week_balances(db, "2026-11-23")["balances"]
    assert {x["participant_id"] for x in week1} == {"p1"}


def test_profile_clear_keeps_internal_subsidy_obligation(db):
    make_participant(db, "carol", "sess-carol")
    make_past_booking(db, bid="bk-carol", pid="carol", duration_minutes=60)
    subsidies.upsert_attendance(
        db, "bk-carol", [{"participant_id": "carol", "attended_minutes": 60}]
    )
    services.create_deletion_request(db, "carol")

    row = services.get_participant(db, "carol")
    assert row["status"] == "deletion_requested"
    assert row["display_name"] is None
    assert balance(db, "carol")["current_payable"] == 40

    batch, created = subsidies.create_batch(
        db,
        batch_id="CAROL-PAY",
        week_start=MONDAY,
        lines=[{"participant_id": "carol", "amount": 40}],
    )
    assert created is True
    assert batch["lines"][0]["participant_id"] == "carol"


def test_restart_can_continue_same_list(tmp_path):
    path = tmp_path / "restart.db"
    setup = connect(path)
    init_db(setup)
    services.create_participant(
        setup, participant_id="p1", display_name="A", profile={}, session_token="s1"
    )
    services.create_interview(setup, interview_id="i1", title="I")
    scheduling.create_room(setup, room_id="r1", label="R")
    when = datetime.now(timezone.utc) + timedelta(days=2)
    scheduling.create_booking(
        setup, booking_id="b1", interview_id="i1", room_id="r1",
        participant_ids=["p1"], start_at=when, end_at=when + timedelta(minutes=60),
    )
    setup.execute(
        "UPDATE bookings SET start_at=?, end_at=? WHERE id='b1'",
        (f"{MONDAY}T10:00:00+00:00", f"{MONDAY}T11:00:00+00:00"),
    )
    setup.commit()
    subsidies.upsert_attendance(setup, "b1", [{"participant_id": "p1", "attended_minutes": 60}])
    batch, _ = subsidies.create_batch(
        setup, batch_id="RESTART", week_start=MONDAY,
        lines=[{"participant_id": "p1", "amount": 40}],
    )
    line_no = batch["lines"][0]["line_no"]
    setup.close()

    restarted = connect(path)
    init_db(restarted)
    loaded = subsidies.get_batch(restarted, "RESTART")
    assert loaded["lines"][0]["amount"] == 40
    result, created = subsidies.confirm_line(
        restarted, result="paid", batch_id="RESTART", line_no=line_no
    )
    assert created is True
    assert result["status"] == "paid"
    restarted.close()


def test_concurrent_attendance_corrections_preserve_each_roster_row(tmp_path):
    path = tmp_path / "attendance-race.db"
    setup = connect(path)
    init_db(setup)
    pids = [f"p{i}" for i in range(8)]
    for pid in pids:
        services.create_participant(
            setup, participant_id=pid, display_name=pid, profile={}, session_token=f"s-{pid}"
        )
    services.create_interview(setup, interview_id="i1", title="I")
    scheduling.create_room(setup, room_id="r1", label="R")
    when = datetime.now(timezone.utc) + timedelta(days=2)
    scheduling.create_booking(
        setup, booking_id="b1", interview_id="i1", room_id="r1",
        participant_ids=pids, start_at=when, end_at=when + timedelta(minutes=60),
    )
    setup.execute(
        "UPDATE bookings SET start_at=?, end_at=? WHERE id='b1'",
        (f"{MONDAY}T10:00:00+00:00", f"{MONDAY}T11:00:00+00:00"),
    )
    setup.commit()
    setup.close()

    barrier = threading.Barrier(len(pids))

    def record(i):
        conn = connect(path)
        try:
            barrier.wait()
            return subsidies.upsert_attendance(
                conn, "b1",
                [{"participant_id": f"p{i}", "attended_minutes": 15 * (i % 4 + 1)}],
            )
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=len(pids)) as pool:
        list(pool.map(record, range(len(pids))))

    check = connect(path)
    rows = check.execute(
        "SELECT participant_id, attended_minutes FROM attendance_records ORDER BY participant_id"
    ).fetchall()
    assert len(rows) == len(pids)
    assert {r[0]: r[1] for r in rows} == {
        f"p{i}": 15 * (i % 4 + 1) for i in range(len(pids))
    }
    check.close()


def test_concurrent_mixed_confirmations_have_one_immutable_result(tmp_path):
    path = tmp_path / "confirm-race.db"
    setup = connect(path)
    init_db(setup)
    services.create_participant(
        setup, participant_id="p1", display_name="A", profile={}, session_token="s1"
    )
    services.create_interview(setup, interview_id="i1", title="I")
    scheduling.create_room(setup, room_id="r1", label="R")
    when = datetime.now(timezone.utc) + timedelta(days=2)
    scheduling.create_booking(
        setup, booking_id="b1", interview_id="i1", room_id="r1",
        participant_ids=["p1"], start_at=when, end_at=when + timedelta(minutes=60),
    )
    setup.execute(
        "UPDATE bookings SET start_at=?, end_at=? WHERE id='b1'",
        (f"{MONDAY}T10:00:00+00:00", f"{MONDAY}T11:00:00+00:00"),
    )
    setup.commit()
    subsidies.upsert_attendance(setup, "b1", [{"participant_id": "p1", "attended_minutes": 60}])
    batch, _ = subsidies.create_batch(
        setup, batch_id="CONF", week_start=MONDAY,
        lines=[{"participant_id": "p1", "amount": 40}],
    )
    line_no = batch["lines"][0]["line_no"]
    setup.close()

    barrier = threading.Barrier(16)

    def confirm(i):
        conn = connect(path)
        try:
            barrier.wait()
            result = "paid" if i % 2 == 0 else "unpaid"
            payload, _created = subsidies.confirm_line(
                conn, result=result, batch_id="CONF", line_no=line_no
            )
            return "ok:" + payload["status"]
        except ApiError as exc:
            return "error:" + exc.code
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(confirm, range(16)))

    assert not any(o.startswith("error:") and o != "error:payment_result_already_recorded"
                   for o in outcomes)
    assert any(o in {"ok:paid", "ok:unpaid"} for o in outcomes)
    final = connect(path)
    status = final.execute(
        "SELECT result_status, confirmed_at FROM subsidy_batch_lines WHERE batch_id='CONF'"
    ).fetchone()
    assert f"ok:{status[0]}" in outcomes
    assert status[0] in {"paid", "unpaid"}
    assert status[1] is not None
    final.close()


def test_concurrent_unique_refunds_cannot_exceed_line_payment(tmp_path):
    path = tmp_path / "refund-race.db"
    setup = connect(path)
    init_db(setup)
    services.create_participant(
        setup, participant_id="p1", display_name="A", profile={}, session_token="s1"
    )
    services.create_interview(setup, interview_id="i1", title="I")
    scheduling.create_room(setup, room_id="r1", label="R")
    when = datetime.now(timezone.utc) + timedelta(days=2)
    scheduling.create_booking(
        setup, booking_id="b1", interview_id="i1", room_id="r1",
        participant_ids=["p1"], start_at=when, end_at=when + timedelta(minutes=600),
    )
    setup.execute(
        "UPDATE bookings SET start_at=?, end_at=? WHERE id='b1'",
        (f"{MONDAY}T10:00:00+00:00", f"{MONDAY}T20:00:00+00:00"),
    )
    setup.commit()
    subsidies.upsert_attendance(setup, "b1", [{"participant_id": "p1", "attended_minutes": 600}])
    batch, _ = subsidies.create_batch(
        setup, batch_id="REF", week_start=MONDAY,
        lines=[{"participant_id": "p1", "amount": 100}],
    )
    line_no = batch["lines"][0]["line_no"]
    subsidies.confirm_line(setup, result="paid", batch_id="REF", line_no=line_no)
    setup.close()

    barrier = threading.Barrier(20)

    def refund(i):
        conn = connect(path)
        try:
            barrier.wait()
            return subsidies.register_refund(
                conn, refund_id=f"RF-{i}", amount=10,
                batch_id="REF", line_no=line_no,
            )[1]
        except ApiError as exc:
            return exc.code
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=20) as pool:
        outcomes = list(pool.map(refund, range(20)))

    assert outcomes.count(True) == 10
    assert outcomes.count("refund_amount_exceeds_line_payment") == 10
    check = connect(path)
    total = check.execute("SELECT COALESCE(SUM(amount_fen),0) FROM subsidy_refunds").fetchone()[0]
    assert total == 10000
    check.close()


def test_concurrent_distinct_batches_reserve_only_once(tmp_path):
    path = tmp_path / "race.db"
    setup = connect(path)
    init_db(setup)
    services.create_participant(
        setup, participant_id="p1", display_name="A", profile={}, session_token="s1"
    )
    services.create_interview(setup, interview_id="i1", title="I")
    scheduling.create_room(setup, room_id="r1", label="R")
    when = datetime.now(timezone.utc) + timedelta(days=2)
    scheduling.create_booking(
        setup, booking_id="b1", interview_id="i1", room_id="r1",
        participant_ids=["p1"], start_at=when, end_at=when + timedelta(minutes=60),
    )
    setup.execute(
        "UPDATE bookings SET start_at=?, end_at=? WHERE id='b1'",
        (f"{MONDAY}T10:00:00+00:00", f"{MONDAY}T11:00:00+00:00"),
    )
    setup.commit()
    subsidies.upsert_attendance(setup, "b1", [{"participant_id": "p1", "attended_minutes": 60}])
    setup.close()

    barrier = threading.Barrier(10)

    def attempt(i):
        conn = connect(path)
        try:
            barrier.wait()
            return subsidies.create_batch(
                conn,
                batch_id=f"RACE-{i}",
                week_start=MONDAY,
                lines=[{"participant_id": "p1", "amount": 40}],
            )[1]
        except ApiError as exc:
            return exc.code
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=10) as pool:
        outcomes = list(pool.map(attempt, range(10)))

    assert outcomes.count(True) == 1
    assert outcomes.count("subsidy_batch_not_full_sendable_amount") == 9

    check = connect(path)
    agg = check.execute(
        "SELECT COUNT(*), COALESCE(SUM(amount_fen),0) FROM subsidy_batch_lines WHERE participant_id='p1'"
    ).fetchone()
    assert agg[0] == 1
    assert agg[1] == 4000
    check.close()


def test_concurrent_same_batch_number_creates_one_list(tmp_path):
    path = tmp_path / "same-id.db"
    setup = connect(path)
    init_db(setup)
    services.create_participant(
        setup, participant_id="p1", display_name="A", profile={}, session_token="s1"
    )
    services.create_interview(setup, interview_id="i1", title="I")
    scheduling.create_room(setup, room_id="r1", label="R")
    when = datetime.now(timezone.utc) + timedelta(days=2)
    scheduling.create_booking(
        setup, booking_id="b1", interview_id="i1", room_id="r1",
        participant_ids=["p1"], start_at=when, end_at=when + timedelta(minutes=60),
    )
    setup.execute(
        "UPDATE bookings SET start_at=?, end_at=? WHERE id='b1'",
        (f"{MONDAY}T10:00:00+00:00", f"{MONDAY}T11:00:00+00:00"),
    )
    setup.commit()
    subsidies.upsert_attendance(setup, "b1", [{"participant_id": "p1", "attended_minutes": 60}])
    setup.close()

    barrier = threading.Barrier(12)

    def attempt(_i):
        conn = connect(path)
        try:
            barrier.wait()
            return subsidies.create_batch(
                conn,
                batch_id="SAME",
                week_start=MONDAY,
                lines=[{"participant_id": "p1", "amount": 40}],
            )[1]
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(attempt, range(12)))
    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 11

    check = connect(path)
    assert check.execute("SELECT COUNT(*) FROM subsidy_batches WHERE id='SAME'").fetchone()[0] == 1
    assert check.execute("SELECT COUNT(*) FROM subsidy_batch_lines WHERE batch_id='SAME'").fetchone()[0] == 1
    check.close()
