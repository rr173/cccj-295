#!/usr/bin/env python3
"""HTTP end-to-end verification for interview attendance subsidies.

The script starts a real uvicorn process with a temporary SQLite database,
backdates bookings through SQL (bookings themselves must be created in the
future by the existing scheduling API), and verifies:

* attendance minute validation and floor billing;
* UTC Monday-week grouping and cap reallocation caused by corrections;
* full-sendable batch validation and fixed idempotent payment lists;
* pending amounts cannot be issued twice;
* immutable paid/unpaid confirmation and idempotent refunds;
* reconciliation after offline results and partial/full returns;
* participant session isolation;
* profile clearing retaining internal subsidy obligations;
* concurrent HTTP batch races;
* real process restart continuation of the same fixed list.

Run:
    .venv/bin/python scripts/e2e_subsidies.py
"""

from __future__ import annotations

import concurrent.futures
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEMP_DIR = Path(tempfile.mkdtemp(prefix="pdc-subsidy-e2e-"))
DB_PATH = TEMP_DIR / "subsidies.db"
os.environ["PDC_DB_PATH"] = str(DB_PATH)
os.environ["PDC_OWNER_TOKEN"] = "demo-owner-token"

OWNER_HEADERS = {"X-Owner-Token": "demo-owner-token"}
WEEK = "2026-11-23"
OTHER_WEEK = "2026-11-16"
CHECKS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    CHECKS.append(name)
    print(f"  ✅ {name}")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_server(port: int, log_path: Path) -> tuple[subprocess.Popen, object]:
    log = log_path.open("ab")
    env = os.environ.copy()
    env["PDC_DB_PATH"] = str(DB_PATH)
    env["PDC_OWNER_TOKEN"] = "demo-owner-token"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    last_error: Exception | None = None
    for _ in range(150):
        if proc.poll() is not None:
            log.flush()
            print(log_path.read_text(errors="replace"))
            raise RuntimeError(f"uvicorn exited early with code {proc.returncode}")
        try:
            response = httpx.get(f"{base_url}/health", timeout=0.5)
            if response.status_code == 200:
                return proc, log
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(0.1)
    raise RuntimeError(f"server did not become ready: {last_error}")


def stop_server(proc: subprocess.Popen, log) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    log.close()


def must(response: httpx.Response, expected: int | set[int], label: str) -> dict:
    allowed = {expected} if isinstance(expected, int) else set(expected)
    if response.status_code not in allowed:
        raise AssertionError(f"{label}: expected {allowed}, got {response.status_code}: {response.text}")
    return response.json() if response.content else {}


def code_of(response: httpx.Response) -> str:
    return str(response.json()["error"]["code"])


def headers(token: str) -> dict[str, str]:
    return {"X-Session-Token": token}


def backdate(bid: str, start: str, duration_minutes: int) -> None:
    start_dt = datetime.fromisoformat(start)
    end_dt = start_dt + timedelta(minutes=duration_minutes)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute(
            "UPDATE bookings SET start_at = ?, end_at = ? WHERE id = ?",
            (start_dt.isoformat(), end_dt.isoformat(), bid),
        )
        conn.commit()
    finally:
        conn.close()


def balance_for(payload: dict, participant_id: str) -> dict:
    return next(x for x in payload["balances"] if x["participant_id"] == participant_id)


def main() -> int:
    suffix = os.urandom(4).hex()
    port = free_port()
    log_path = TEMP_DIR / "uvicorn.log"
    proc, log_handle = start_server(port, log_path)
    base = f"http://127.0.0.1:{port}"
    print(f"Database: {DB_PATH}")
    print(f"Server:   {base}")

    try:
        with httpx.Client(base_url=base, timeout=10.0) as api:
            print("\n[1/10] Setup participants, interviews, room and bookings")
            people: dict[str, tuple[str, str]] = {}
            for short in ("alice", "bob", "carol", "dave", "erin"):
                pid = f"p-{short}-{suffix}"
                token = f"sess-{short}-{suffix}"
                must(api.post("/participants", json={
                    "id": pid, "display_name": short, "session_token": token, "profile": {}
                }), 201, f"create {short}")
                people[short] = (pid, token)
            alice, alice_token = people["alice"]
            bob, bob_token = people["bob"]
            carol, carol_token = people["carol"]
            dave, _ = people["dave"]
            erin, _ = people["erin"]

            must(api.post("/owner/rooms", headers=OWNER_HEADERS,
                          json={"id": f"room-{suffix}", "label": "Subsidy Room"}),
                 201, "create room")
            room = f"room-{suffix}"

            booking_specs = [
                ("early", alice, 180, 90),
                ("late", alice, 181, 180),
                ("bob", bob, 182, 60),
                ("carol", carol, 183, 60),
                ("dave", dave, 184, 60),
                ("erin", erin, 185, 60),
                ("other-week", alice, 186, 120),
                ("future", alice, 187, 60),
            ]
            now = datetime.now(timezone.utc)
            for short, pid, day_offset, duration in booking_specs:
                bid = f"bk-{short}-{suffix}"
                iid = f"int-{short}-{suffix}"
                start = (now + timedelta(days=day_offset)).replace(microsecond=0)
                must(api.post("/interviews", json={"id": iid, "title": short}), 201, f"interview {short}")
                must(api.post("/owner/bookings", headers=OWNER_HEADERS, json={
                    "id": bid,
                    "interview_id": iid,
                    "room_id": room,
                    "participant_ids": [pid],
                    "start_at": start.isoformat(),
                    "end_at": (start + timedelta(minutes=duration)).isoformat(),
                }), 201, f"booking {short}")

            def bid(short: str) -> str:
                return f"bk-{short}-{suffix}"

            backdate(bid("early"), f"{WEEK}T10:00:00+00:00", 90)
            backdate(bid("late"), f"{WEEK}T12:00:00+00:00", 180)
            backdate(bid("bob"), f"{WEEK}T10:00:00+00:00", 60)
            backdate(bid("carol"), f"{WEEK}T11:00:00+00:00", 60)
            backdate(bid("dave"), f"{WEEK}T14:00:00+00:00", 60)
            backdate(bid("erin"), f"{WEEK}T15:00:00+00:00", 60)
            backdate(bid("other-week"), f"{OTHER_WEEK}T10:00:00+00:00", 120)

            print("\n[2/10] Authentication and attendance validation")
            check("owner token required",
                  api.get(f"/owner/bookings/{bid('bob')}/attendance").status_code == 403)
            check("participant session required", api.get("/me/subsidies").status_code == 401)

            bad = api.put(f"/owner/bookings/{bid('early')}/attendance",
                          headers=OWNER_HEADERS,
                          json={"records": [{"participant_id": alice, "attended_minutes": 91}]})
            check("minutes over booking duration rejected",
                  bad.status_code == 422 and code_of(bad) == "attended_minutes_exceed_booking_duration",
                  bad.text)

            outsider = api.put(f"/owner/bookings/{bid('early')}/attendance",
                               headers=OWNER_HEADERS,
                               json={"records": [{"participant_id": bob, "attended_minutes": 10}]})
            check("non-roster participant rejected",
                  outsider.status_code == 422 and code_of(outsider) == "participant_not_in_booking",
                  outsider.text)

            not_ended = api.put(f"/owner/bookings/{bid('future')}/attendance",
                                headers=OWNER_HEADERS,
                                json={"records": [{"participant_id": alice, "attended_minutes": 10}]})
            check("future booking attendance rejected",
                  not_ended.status_code == 409 and code_of(not_ended) == "booking_not_ended",
                  not_ended.text)

            for short, pid, minutes in [
                ("early", alice, 90), ("late", alice, 120), ("bob", bob, 60),
                ("carol", carol, 60), ("dave", dave, 60), ("erin", erin, 60),
                ("other-week", alice, 120),
            ]:
                must(api.put(f"/owner/bookings/{bid(short)}/attendance",
                             headers=OWNER_HEADERS,
                             json={"records": [{"participant_id": pid, "attended_minutes": minutes}]}),
                     200, f"attendance {short}")

            early_view = must(api.get(f"/owner/bookings/{bid('early')}/attendance",
                                      headers=OWNER_HEADERS), 200, "early attendance")
            early_record = early_view["records"][0]
            check("floor billing: 90 minutes = 6 units = 60",
                  early_record["billable_units"] == 6 and early_record["raw_amount"] == 60)

            week_view = must(api.get(f"/owner/subsidies/weeks/{WEEK}",
                                     headers=OWNER_HEADERS), 200, "week view")
            alice_balance = balance_for(week_view, alice)
            check("weekly cap sequentially leaves later session at 40",
                  alice_balance["current_payable"] == 100
                  and {e["booking_id"]: e["counted_amount"] for e in week_view["session_entries"]
                       if e["participant_id"] == alice}[bid("late")] == 40)

            print("\n[3/10] Full-sendable batch, fixed rows and idempotent retry")
            batch_body = {
                "batch_id": f"B-ALICE-{suffix}",
                "week_start": WEEK,
                "lines": [{"participant_id": alice, "expected_amount": 100}],
            }
            alice_batch = must(api.post("/owner/subsidy-batches", headers=OWNER_HEADERS,
                                        json=batch_body), 201, "create Alice batch")
            check("fixed line number and amount delivered",
                  alice_batch["lines"][0]["line_no"] == 1
                  and alice_batch["lines"][0]["amount"] == 100
                  and alice_batch["lines"][0]["status"] == "pending")
            alice_batch_id = alice_batch["batch_id"]

            replay = must(api.post("/owner/subsidy-batches", headers=OWNER_HEADERS,
                                  json=batch_body), 200, "retry Alice batch")
            check("same batch number/request replays same line id",
                  replay.get("idempotent_replay") is True
                  and replay["lines"][0]["id"] == alice_batch["lines"][0]["id"])

            duplicate = api.post("/owner/subsidy-batches", headers=OWNER_HEADERS, json={
                "batch_id": f"B-ALICE-RESEND-{suffix}",
                "week_start": WEEK,
                "lines": [{"participant_id": alice, "amount": 100}],
            })
            check("pending amount cannot be sent again",
                  duplicate.status_code == 409
                  and code_of(duplicate) == "subsidy_batch_not_full_sendable_amount",
                  duplicate.text)

            changed = api.post("/owner/subsidy-batches", headers=OWNER_HEADERS, json={
                **batch_body,
                "lines": [{"participant_id": alice, "amount": 90}],
            })
            check("batch number cannot be reused for changed request",
                  changed.status_code == 409 and code_of(changed) == "batch_id_reused_for_different_request",
                  changed.text)

            print("\n[4/10] Participant session visibility and isolation")
            me_alice = must(api.get(f"/me/subsidies?week={WEEK}", headers=headers(alice_token)),
                            200, "Alice settlement")
            check("session user sees own reserved balance",
                  me_alice["participant_id"] == alice
                  and balance_for(me_alice, alice)["pending_amount"] == 100
                  and balance_for(me_alice, alice)["sendable_amount"] == 0)
            check("Alice response contains no Bob data",
                  all(x["participant_id"] == alice for x in me_alice["balances"])
                  and all(e["participant_id"] == alice for e in me_alice["session_entries"]))
            me_bob = must(api.get(f"/me/subsidies/{WEEK}", headers=headers(bob_token)),
                          200, "Bob settlement")
            check("Bob sees own 40 and no Alice batch",
                  balance_for(me_bob, bob)["current_payable"] == 40
                  and all(x["participant_id"] == bob for x in me_bob["balances"]))
            check("forged participant path is not available under /me",
                  api.get(f"/participants/{alice}/subsidies").status_code in {404, 403})

            print("\n[5/10] Correction changes cross-session amount but not delivered list")
            must(api.put(f"/owner/bookings/{bid('early')}/attendance", headers=OWNER_HEADERS,
                         json={"records": [{"participant_id": alice, "attended_minutes": 30}]}),
                 200, "correct early attendance")
            corrected = must(api.get(f"/owner/subsidies/weeks/{WEEK}", headers=OWNER_HEADERS),
                             200, "corrected week")
            entries = {e["booking_id"]: e["counted_amount"]
                       for e in corrected["session_entries"] if e["participant_id"] == alice}
            check("early correction reallocates cap: 20 then 80",
                  entries[bid("early")] == 20 and entries[bid("late")] == 80)
            check("delivered batch remains fixed at 100",
                  must(api.get(f"/owner/subsidy-batches/{alice_batch_id}",
                               headers=OWNER_HEADERS), 200, "fixed batch")["total_amount"] == 100)

            print("\n[6/10] Offline paid result, immutable verdict and refunds")
            paid = must(api.post(
                f"/owner/subsidy-batches/{alice_batch_id}/lines/1/confirm-paid",
                headers=OWNER_HEADERS), {200, 201}, "confirm paid")
            check("paid confirmation recorded", paid["status"] == "paid")
            paid_retry = must(api.post(
                f"/owner/subsidy-batches/{alice_batch_id}/lines/1/confirm",
                headers=OWNER_HEADERS, json={"result": "paid"}), 200, "retry paid")
            check("same paid result is idempotent", paid_retry.get("idempotent_replay") is True)
            overturn = api.post(
                f"/owner/subsidy-batches/{alice_batch_id}/lines/1/confirm-unpaid",
                headers=OWNER_HEADERS)
            check("paid verdict cannot be changed to unpaid",
                  overturn.status_code == 409 and code_of(overturn) == "payment_result_already_recorded")

            must(api.put(f"/owner/bookings/{bid('late')}/attendance", headers=OWNER_HEADERS,
                         json={"records": [{"participant_id": alice, "attended_minutes": 30}]}),
                 200, "correct late downward")
            recon = must(api.get(f"/owner/subsidies/weeks/{WEEK}", headers=OWNER_HEADERS),
                         200, "reconciliation week")
            ab = balance_for(recon, alice)
            check("correction after payment shows 40 payable and 60 overpaid",
                  ab["current_payable"] == 40 and ab["net_paid"] == 100
                  and ab["overpaid_amount"] == 60 and ab["sendable_amount"] == 0,
                  str(ab))

            refund_body = {"refund_id": f"R1-{suffix}", "amount": 20}
            r1 = must(api.post(f"/owner/subsidy-batches/{alice_batch_id}/lines/1/refunds",
                               headers=OWNER_HEADERS, json=refund_body), {200, 201}, "R1")
            check("first partial refund recorded", r1["amount"] == 20)
            must(api.post(f"/owner/subsidy-batches/{alice_batch_id}/lines/1/refunds",
                          headers=OWNER_HEADERS, json=refund_body), 200, "R1 retry")
            r1_changed = api.post(f"/owner/subsidy-batches/{alice_batch_id}/lines/1/refunds",
                                  headers=OWNER_HEADERS,
                                  json={"refund_id": refund_body["refund_id"], "amount": 21})
            check("refund number cannot change content",
                  r1_changed.status_code == 409 and code_of(r1_changed) == "refund_id_reused_for_different_content")
            over_refund = api.post(f"/owner/subsidy-batches/{alice_batch_id}/lines/1/refunds",
                                   headers=OWNER_HEADERS,
                                   json={"refund_id": f"R-BIG-{suffix}", "amount": 81})
            check("cumulative refund cannot exceed line payment",
                  over_refund.status_code == 409
                  and code_of(over_refund) == "refund_amount_exceeds_line_payment")
            must(api.post(f"/owner/subsidy-batches/{alice_batch_id}/lines/1/refunds",
                          headers=OWNER_HEADERS,
                          json={"refund_id": f"R2-{suffix}", "amount": 40}),
                 {200, 201}, "R2")
            ab = balance_for(must(api.get(f"/owner/subsidies/weeks/{WEEK}",
                                          headers=OWNER_HEADERS), 200, "after refunds"), alice)
            check("refunds reconcile net paid to current payable",
                  ab["refunded_amount"] == 60 and ab["net_paid"] == 40
                  and ab["overpaid_amount"] == 0, str(ab))

            print("\n[7/10] Unpaid result releases money for a new complete list")
            bob_batch_1 = must(api.post(f"/owner/subsidy-weeks/{WEEK}/batches",
                                        headers=OWNER_HEADERS, json={
                "batch_id": f"B-BOB-1-{suffix}",
                "lines": [{"participant_id": bob, "amount": 40}],
            }), 201, "Bob batch 1")
            bob_line = bob_batch_1["lines"][0]["line_no"]
            bob_dup = api.post(f"/owner/subsidy-weeks/{WEEK}/batches",
                               headers=OWNER_HEADERS, json={
                "batch_id": f"B-BOB-DUP-{suffix}",
                "lines": [{"participant_id": bob, "amount": 40}],
            })
            check("Bob pending line blocks resend",
                  bob_dup.status_code == 409
                  and code_of(bob_dup) == "subsidy_batch_not_full_sendable_amount")
            must(api.post(f"/owner/subsidy-batches/{bob_batch_1['batch_id']}/lines/{bob_line}/confirm-unpaid",
                          headers=OWNER_HEADERS), {200, 201}, "Bob unpaid")
            bob_week = must(api.get(f"/owner/subsidies/weeks/{WEEK}", headers=OWNER_HEADERS),
                            200, "Bob released")
            check("unpaid confirmation releases full 40",
                  balance_for(bob_week, bob)["sendable_amount"] == 40
                  and balance_for(bob_week, bob)["pending_amount"] == 0)
            bob_batch_2 = must(api.post("/owner/subsidy-batches", headers=OWNER_HEADERS, json={
                "batch_id": f"B-BOB-2-{suffix}",
                "week": WEEK,
                "recipients": [{"participant_id": bob, "expected_amount": 40}],
            }), 201, "Bob batch 2 using aliases")
            check("released amount can be put on a new fixed list",
                  bob_batch_2["lines"][0]["amount"] == 40
                  and bob_batch_2["lines"][0]["line_no"] == 1)

            print("\n[8/10] Week isolation and profile clearing")
            other = must(api.get(f"/owner/subsidies/weeks/{OTHER_WEEK}", headers=OWNER_HEADERS),
                         200, "other week")
            check("other UTC week has separate 80 payable",
                  balance_for(other, alice)["current_payable"] == 80)
            check("selected week does not mix other-week Alice balance",
                  balance_for(must(api.get(f"/owner/subsidies/weeks/{WEEK}",
                                           headers=OWNER_HEADERS), 200, "week"),
                              alice)["current_payable"] == 40)

            must(api.post(f"/participants/{carol}/deletion-requests"), {200, 201}, "clear Carol profile")
            check("ordinary participant profile remains unavailable",
                  api.get(f"/participants/{carol}").status_code == 410)
            carol_week = must(api.get(f"/owner/subsidies/weeks/{WEEK}", headers=OWNER_HEADERS),
                              200, "Carol obligations")
            check("internal subsidy obligation survives profile clearing",
                  balance_for(carol_week, carol)["current_payable"] == 40)
            carol_batch = must(api.post("/owner/subsidy-batches", headers=OWNER_HEADERS, json={
                "batch_id": f"B-CAROL-{suffix}",
                "week_start": WEEK,
                "lines": [{"participant_id": carol, "amount": 40}],
            }), 201, "Carol batch")
            must(api.delete(f"/participants/{carol}/session"), 200, "revoke Carol session")
            check("subsidy API does not restore revoked participant entry",
                  api.get("/me/subsidies", headers=headers(carol_token)).status_code == 401)
            check("owner can still process Carol by internal id",
                  must(api.get(f"/owner/subsidy-batches/{carol_batch['batch_id']}",
                               headers=OWNER_HEADERS), 200, "Carol batch")["total_amount"] == 40)

            print("\n[9/10] Concurrent HTTP batch issuance")

            def unique_dave(i: int) -> int:
                with httpx.Client(base_url=base, timeout=10.0) as client:
                    response = client.post("/owner/subsidy-batches", headers=OWNER_HEADERS, json={
                        "batch_id": f"B-DAVE-{suffix}-{i}",
                        "week_start": WEEK,
                        "lines": [{"participant_id": dave, "amount": 40}],
                    })
                    return response.status_code

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                statuses = list(pool.map(unique_dave, range(8)))
            check("exactly one distinct concurrent batch is created",
                  statuses.count(201) == 1 and statuses.count(409) == 7, str(statuses))
            dave_balance = balance_for(must(api.get(f"/owner/subsidies/weeks/{WEEK}",
                                                     headers=OWNER_HEADERS), 200, "Dave"), dave)
            check("concurrent races reserve Dave amount exactly once",
                  dave_balance["pending_amount"] == 40 and dave_balance["sendable_amount"] == 0)

            barrier = threading.Barrier(8)

            def same_erin(_i: int) -> int:
                barrier.wait()
                with httpx.Client(base_url=base, timeout=10.0) as client:
                    response = client.post("/owner/subsidy-batches", headers=OWNER_HEADERS, json={
                        "batch_id": f"B-ERIN-SAME-{suffix}",
                        "week_start": WEEK,
                        "lines": [{"participant_id": erin, "amount": 40}],
                    })
                    return response.status_code

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                same_statuses = list(pool.map(same_erin, range(8)))
            check("same batch number has one create and seven replays",
                  same_statuses.count(201) == 1 and same_statuses.count(200) == 7, str(same_statuses))

        print("\n[10/10] Real restart continues original lists")
        stop_server(proc, log_handle)
        port2 = free_port()
        proc, log_handle = start_server(port2, log_path)
        base = f"http://127.0.0.1:{port2}"
        with httpx.Client(base_url=base, timeout=10.0) as api:
            bob_after = must(api.get(f"/owner/subsidy-batches/B-BOB-2-{suffix}",
                                     headers=OWNER_HEADERS), 200, "Bob list after restart")
            check("restart preserves fixed Bob row",
                  bob_after["lines"][0]["line_no"] == 1
                  and bob_after["lines"][0]["amount"] == 40
                  and bob_after["lines"][0]["status"] == "pending")
            must(api.post(f"/owner/subsidy-batches/B-BOB-2-{suffix}/lines/1/confirm-paid",
                          headers=OWNER_HEADERS), {200, 201}, "pay Bob after restart")
            bob_week = must(api.get(f"/owner/subsidies/weeks/{WEEK}", headers=OWNER_HEADERS),
                            200, "Bob after restart")
            check("offline confirmation after restart reconciles",
                  balance_for(bob_week, bob)["net_paid"] == 40
                  and balance_for(bob_week, bob)["sendable_amount"] == 0)

            carol_after = must(api.post(
                f"/owner/subsidy-batches/B-CAROL-{suffix}/lines/1/confirm-paid",
                headers=OWNER_HEADERS), {200, 201}, "Carol after restart")
            check("Carol list can also be confirmed after restart", carol_after["status"] == "paid")

            alice_after = must(api.get(f"/owner/subsidy-batches/B-ALICE-{suffix}",
                                       headers=OWNER_HEADERS), 200, "Alice after restart")
            check("Alice payment/refund history survives restart",
                  alice_after["paid_amount"] == 100
                  and alice_after["refunded_amount"] == 60
                  and len(alice_after["lines"][0]["refunds"]) == 2)

    finally:
        if proc.poll() is None:
            stop_server(proc, log_handle)

    print(f"\n✅ e2e_subsidies: {len(CHECKS)} checks passed")
    print(f"Temporary database retained at: {DB_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
