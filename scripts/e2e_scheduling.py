#!/usr/bin/env python3
"""HTTP end-to-end verification for interview booking and batch rescheduling.

Starts a real uvicorn process on a temporary SQLite database and exercises the
full HTTP surface, including the three headline scenarios requested for the
feature:

1. **Original-slot swap** - two confirmed interviews exchange rooms in one
   consultation; the proposed arrangement is rejected while either original or
   proposed slot is still held, and only unanimous acceptance applies the swap.
2. **Capacity recovery after a failed consultation** - a rejected/expired order
   releases its proposed slots so the room/participant can be booked again.
3. **Calendar effect of the final move** - participant calendar queries show
   the original arrangement during consultation and the new arrangement only
   after the whole batch commits.

Also covered: owner/session authentication, overlap and back-to-back rules,
immutable one-time participant answers, owner withdrawal, cancellation of an
underlying booking invalidating the order, participant purge integration,
concurrent booking races and real restart recovery of expired orders.
"""

from __future__ import annotations

import concurrent.futures
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEMP_DIR = Path(tempfile.mkdtemp(prefix="pdc-sched-e2e-"))
DB_PATH = TEMP_DIR / "scheduling.db"
os.environ["PDC_DB_PATH"] = str(DB_PATH)
os.environ["PDC_OWNER_TOKEN"] = "demo-owner-token"

OWNER_HEADERS = {"X-Owner-Token": "demo-owner-token"}
CHECKS: list[str] = []
BASE_TIME = datetime.now(timezone.utc).replace(microsecond=0)


def t(minutes_from_now: float) -> str:
    return (BASE_TIME + timedelta(minutes=minutes_from_now)).isoformat()


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
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    last_error = None
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


def session_headers(token: str) -> dict[str, str]:
    return {"X-Session-Token": token}


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
            print("\n[1/9] Setup: participants, interviews, rooms")
            participants = {}
            for short in ("alice", "bob", "carol", "dave", "erin"):
                pid = f"p-{short}-{suffix}"
                token = f"sess-{short}-{suffix}"
                must(
                    api.post(
                        "/participants",
                        json={"id": pid, "display_name": short, "session_token": token, "profile": {}},
                    ),
                    201,
                    f"create {short}",
                )
                participants[short] = (pid, token)
            a, token_a = participants["alice"]
            b, token_b = participants["bob"]
            c, token_c = participants["carol"]
            d, token_d = participants["dave"]
            e, token_e = participants["erin"]

            interviews = {}
            for short in ("one", "two", "three", "four", "five",
                          "six", "seven", "eight", "nine", "ten", "eleven"):
                iid = f"int-{short}-{suffix}"
                must(api.post("/interviews", json={"id": iid, "title": f"Interview {short}"}),
                     201, f"create interview {short}")
                interviews[short] = iid

            rooms = {}
            for short in ("r1", "r2", "r3"):
                rid = f"room-{short}-{suffix}"
                must(api.post("/owner/rooms", headers=OWNER_HEADERS,
                              json={"id": rid, "label": f"Room {short}"}), 201, f"create room {short}")
                rooms[short] = rid

            print("\n[2/9] Booking validation and overlap rules")
            check("owner token required for bookings",
                  api.post("/owner/bookings", json={}).status_code == 403)

            bk_one = f"bk-one-{suffix}"
            must(
                api.post(
                    "/owner/bookings",
                    headers=OWNER_HEADERS,
                    json={
                        "id": bk_one, "interview_id": interviews["one"], "room_id": rooms["r1"],
                        "participant_ids": [a, b], "start_at": t(100), "end_at": t(200),
                    },
                ),
                201,
                "create booking one",
            )

            # One active booking per interview.
            dup = api.post(
                "/owner/bookings",
                headers=OWNER_HEADERS,
                json={
                    "interview_id": interviews["one"], "room_id": rooms["r2"],
                    "participant_ids": [c], "start_at": t(300), "end_at": t(400),
                },
            )
            check("second active booking for interview -> 409", dup.status_code == 409, dup.text)
            check("error code active_booking_exists",
                  dup.json()["error"]["code"] == "active_booking_exists")

            # Room overlap blocked; back-to-back allowed.
            overlap = api.post(
                "/owner/bookings",
                headers=OWNER_HEADERS,
                json={
                    "interview_id": interviews["two"], "room_id": rooms["r1"],
                    "participant_ids": [c], "start_at": t(150), "end_at": t(250),
                },
            )
            check("overlapping room booking -> 409 slot_conflict",
                  overlap.status_code == 409 and overlap.json()["error"]["code"] == "slot_conflict",
                  overlap.text)

            bk_two = f"bk-two-{suffix}"
            must(
                api.post(
                    "/owner/bookings",
                    headers=OWNER_HEADERS,
                    json={
                        "id": bk_two, "interview_id": interviews["two"], "room_id": rooms["r1"],
                        "participant_ids": [c], "start_at": t(200), "end_at": t(300),
                    },
                ),
                201,
                "back-to-back room booking allowed",
            )

            # Participant overlap in a different room.
            person_clash = api.post(
                "/owner/bookings",
                headers=OWNER_HEADERS,
                json={
                    "interview_id": interviews["three"], "room_id": rooms["r2"],
                    "participant_ids": [a, d], "start_at": t(120), "end_at": t(180),
                },
            )
            check("participant overlap -> 409 slot_conflict",
                  person_clash.status_code == 409, person_clash.text)

            owner_list = must(api.get("/owner/bookings", headers=OWNER_HEADERS), 200, "owner list bookings")
            check("owner sees both confirmed bookings",
                  len(owner_list["bookings"]) == 2, str(owner_list))

            print("\n[3/9] Scenario A: original-slot swap via one consultation")
            ro_swap = f"ro-swap-{suffix}"
            must(
                api.post(
                    "/owner/reschedule-orders",
                    headers=OWNER_HEADERS,
                    json={
                        "id": ro_swap,
                        "consultation_deadline": t(50),
                        "items": [
                            # bk_one moves into room r2 at its own time; bk_two
                            # moves into the room r1 slot bk_one vacates.
                            {"booking_id": bk_one, "room_id": rooms["r2"],
                             "start_at": t(100), "end_at": t(200)},
                            {"booking_id": bk_two, "room_id": rooms["r1"],
                             "start_at": t(100), "end_at": t(200)},
                        ],
                    },
                ),
                201,
                "create swap order",
            )

            swap_detail = must(api.get(f"/owner/reschedule-orders/{ro_swap}",
                                       headers=OWNER_HEADERS), 200, "get swap order")
            check("swap order pending", swap_detail["status"] == "pending")
            check("swap order has two items", len(swap_detail["items"]) == 2)
            check("three distinct affected participants",
                  set(swap_detail["participant_ids"]) == {a, b, c},
                  str(swap_detail["participant_ids"]))

            # During consultation the original arrangement is still confirmed.
            cal_a = must(api.get("/me/calendar", headers=session_headers(token_a)), 200, "Alice calendar")
            a_booking = next(x for x in cal_a["confirmed_bookings"] if x["id"] == bk_one)
            check("calendar keeps original room during consultation",
                  a_booking["room_id"] == rooms["r1"])
            check("calendar shows the pending consultation",
                  any(o["id"] == ro_swap and o["status"] == "pending"
                      for o in cal_a["reschedule_orders"]))

            # Proposed slots are held: an outside booking cannot take room r2 at 100-200.
            held = api.post(
                "/owner/bookings",
                headers=OWNER_HEADERS,
                json={
                    "interview_id": interviews["four"], "room_id": rooms["r2"],
                    "participant_ids": [e], "start_at": t(100), "end_at": t(200),
                },
            )
            check("proposed slot held against outside booking",
                  held.status_code == 409 and held.json()["error"]["code"] == "slot_conflict",
                  held.text)
            # The same held slot back-to-back (starting exactly at the end) is allowed.
            touch = must(
                api.post(
                    "/owner/bookings",
                    headers=OWNER_HEADERS,
                    json={
                        "interview_id": interviews["four"], "room_id": rooms["r2"],
                        "participant_ids": [e], "start_at": t(200), "end_at": t(220),
                    },
                ),
                201,
                "back-to-back against held slot allowed",
            )
            check("back-to-back booking confirmed", touch["status"] == "confirmed")

            # Authentication and authorization on participant entries.
            check("session token required", api.get("/me/calendar").status_code == 401)
            check("invalid session rejected",
                  api.get("/me/calendar", headers=session_headers("nope")).status_code == 401)
            check("outsider cannot view the order",
                  api.get(f"/me/reschedule-orders/{ro_swap}",
                          headers=session_headers(token_e)).status_code == 403)
            check("outsider cannot answer the order",
                  api.post(f"/me/reschedule-orders/{ro_swap}/responses",
                           headers=session_headers(token_e),
                           json={"decision": "accepted"}).status_code == 403)

            # Each participant answers once; first answer is immutable.
            first = must(
                api.post(f"/me/reschedule-orders/{ro_swap}/responses",
                         headers=session_headers(token_a), json={"decision": "accepted"}),
                201, "Alice accepts",
            )
            check("still pending after first acceptance", first["status"] == "pending")
            replay = must(
                api.post(f"/me/reschedule-orders/{ro_swap}/responses",
                         headers=session_headers(token_a), json={"decision": "accepted"}),
                200, "Alice replay",
            )
            check("repeated answer is idempotent replay", replay.get("idempotent_replay") is True)
            change = api.post(f"/me/reschedule-orders/{ro_swap}/responses",
                              headers=session_headers(token_a), json={"decision": "rejected"})
            check("first answer cannot be changed", change.status_code == 409, change.text)

            must(
                api.post(f"/me/reschedule-orders/{ro_swap}/responses",
                         headers=session_headers(token_b), json={"decision": "accepted"}),
                201, "Bob accepts",
            )
            applied = must(
                api.post(f"/me/reschedule-orders/{ro_swap}/responses",
                         headers=session_headers(token_c), json={"decision": "accepted"}),
                201, "Carol accepts - unanimous",
            )
            check("unanimous acceptance approves order", applied["status"] == "approved")

            # Scenario A result + scenario C: calendar reflects the final move.
            cal_a_after = must(api.get("/me/calendar", headers=session_headers(token_a)),
                               200, "Alice calendar after swap")
            moved_one = next(x for x in cal_a_after["confirmed_bookings"] if x["id"] == bk_one)
            check("calendar shows booking one in new room r2",
                  moved_one["room_id"] == rooms["r2"])
            cal_c = must(api.get("/me/calendar", headers=session_headers(token_c)),
                         200, "Carol calendar after swap")
            moved_two = next(x for x in cal_c["confirmed_bookings"] if x["id"] == bk_two)
            check("calendar shows booking two in new room r1",
                  moved_two["room_id"] == rooms["r1"])
            check("approved order visible in calendar",
                  any(o["id"] == ro_swap and o["status"] == "approved"
                      for o in cal_a_after["reschedule_orders"]))

            # The original room r1 at 100-200 is now occupied by bk_two (not free),
            # but room r3 is independently bookable back-to-back.
            must(
                api.post(
                    "/owner/bookings",
                    headers=OWNER_HEADERS,
                    json={
                        "interview_id": interviews["nine"], "room_id": rooms["r3"],
                        "participant_ids": [e], "start_at": t(220), "end_at": t(240),
                    },
                ),
                201,
                "new booking after swap in free room",
            )

            print("\n[4/9] Scenario B: rejection fails the consultation and restores capacity")
            bk_rej = f"bk-rej-{suffix}"
            must(
                api.post(
                    "/owner/bookings",
                    headers=OWNER_HEADERS,
                    json={
                        "id": bk_rej, "interview_id": interviews["six"], "room_id": rooms["r3"],
                        "participant_ids": [d, e], "start_at": t(300), "end_at": t(400),
                    },
                ),
                201,
                "create booking for rejection scenario",
            )
            ro_rej = f"ro-rej-{suffix}"
            must(
                api.post(
                    "/owner/reschedule-orders",
                    headers=OWNER_HEADERS,
                    json={
                        "id": ro_rej,
                        "consultation_deadline": t(50),
                        "items": [
                            {"booking_id": bk_rej, "room_id": rooms["r2"],
                             "start_at": t(500), "end_at": t(600)},
                        ],
                    },
                ),
                201,
                "create rejection order",
            )
            # While pending, proposed room r2 500-600 cannot be taken.
            blocked = api.post(
                "/owner/bookings",
                headers=OWNER_HEADERS,
                json={
                    "interview_id": interviews["seven"], "room_id": rooms["r2"],
                    "participant_ids": [a], "start_at": t(550), "end_at": t(560),
                },
            )
            check("proposed slot blocked while pending", blocked.status_code == 409, blocked.text)

            must(
                api.post(f"/me/reschedule-orders/{ro_rej}/responses",
                         headers=session_headers(token_e), json={"decision": "rejected"}),
                201, "Erin rejects",
            )
            rej_detail = must(api.get(f"/owner/reschedule-orders/{ro_rej}",
                                      headers=OWNER_HEADERS), 200, "rejected order detail")
            check("order marked rejected", rej_detail["status"] == "rejected")
            unchanged = must(api.get(f"/owner/bookings/{bk_rej}", headers=OWNER_HEADERS),
                             200, "booking unchanged after rejection")
            check("original booking stays in room r3", unchanged["room_id"] == rooms["r3"])

            # Capacity restored: the rejected proposal's slot is now bookable.
            recovered = must(
                api.post(
                    "/owner/bookings",
                    headers=OWNER_HEADERS,
                    json={
                        "interview_id": interviews["seven"], "room_id": rooms["r2"],
                        "participant_ids": [a], "start_at": t(500), "end_at": t(600),
                    },
                ),
                201,
                "capacity restored after rejection",
            )
            check("recovered slot booking confirmed", recovered["status"] == "confirmed")

            # Late answers cannot apply a finished order.
            late = api.post(f"/me/reschedule-orders/{ro_rej}/responses",
                            headers=session_headers(token_d), json={"decision": "accepted"})
            check("late answer on rejected order -> 409", late.status_code == 409, late.text)

            print("\n[5/9] Owner withdrawal also releases proposed slots")
            bk_wd = f"bk-wd-{suffix}"
            must(
                api.post(
                    "/owner/bookings",
                    headers=OWNER_HEADERS,
                    json={
                        "id": bk_wd, "interview_id": interviews["eight"], "room_id": rooms["r3"],
                        "participant_ids": [a], "start_at": t(700), "end_at": t(800),
                    },
                ),
                201,
                "create booking for withdrawal",
            )
            ro_wd = f"ro-wd-{suffix}"
            must(
                api.post(
                    "/owner/reschedule-orders",
                    headers=OWNER_HEADERS,
                    json={
                        "id": ro_wd,
                        "consultation_deadline": t(50),
                        "items": [
                            {"booking_id": bk_wd, "room_id": rooms["r1"],
                             "start_at": t(700), "end_at": t(800)},
                        ],
                    },
                ),
                201,
                "create withdrawal order",
            )
            must(api.post(f"/owner/reschedule-orders/{ro_wd}/withdraw",
                          headers=OWNER_HEADERS), 200, "owner withdraws")
            must(
                api.post(
                    "/owner/bookings",
                    headers=OWNER_HEADERS,
                    json={
                        "interview_id": interviews["ten"], "room_id": rooms["r1"],
                        "participant_ids": [e], "start_at": t(700), "end_at": t(800),
                    },
                ),
                201,
                "withdrawn proposed slot reusable",
            )

            print("\n[6/9] Cancelling an underlying booking invalidates the whole order")
            bk_cx = f"bk-cx-{suffix}"
            must(
                api.post(
                    "/owner/bookings",
                    headers=OWNER_HEADERS,
                    json={
                        "id": bk_cx, "interview_id": interviews["three"], "room_id": rooms["r2"],
                        "participant_ids": [b], "start_at": t(900), "end_at": t(1000),
                    },
                ),
                201,
                "create booking for cancellation scenario",
            )
            ro_cx = f"ro-cx-{suffix}"
            must(
                api.post(
                    "/owner/reschedule-orders",
                    headers=OWNER_HEADERS,
                    json={
                        "id": ro_cx,
                        "consultation_deadline": t(50),
                        "items": [
                            {"booking_id": bk_cx, "room_id": rooms["r3"],
                             "start_at": t(900), "end_at": t(1000)},
                        ],
                    },
                ),
                201,
                "create cancellation order",
            )
            must(api.post(f"/owner/bookings/{bk_cx}/cancel",
                          headers=OWNER_HEADERS, params={"reason": "owner decision"}),
                 200, "cancel underlying booking")
            cx_detail = must(api.get(f"/owner/reschedule-orders/{ro_cx}",
                                     headers=OWNER_HEADERS), 200, "order after cancellation")
            check("order invalidated by booking cancellation",
                  cx_detail["status"] == "cancelled_booking", str(cx_detail["status"]))

            print("\n[7/9] Participant purge cancels bookings and ends consultations")
            bk_purge = f"bk-purge-{suffix}"
            must(
                api.post(
                    "/owner/bookings",
                    headers=OWNER_HEADERS,
                    json={
                        "id": bk_purge, "interview_id": interviews["eleven"], "room_id": rooms["r3"],
                        "participant_ids": [d, a], "start_at": t(1100), "end_at": t(1200),
                    },
                ),
                201,
                "create booking involving Dave",
            )
            ro_purge = f"ro-purge-{suffix}"
            must(
                api.post(
                    "/owner/reschedule-orders",
                    headers=OWNER_HEADERS,
                    json={
                        "id": ro_purge,
                        "consultation_deadline": t(50),
                        "items": [
                            {"booking_id": bk_purge, "room_id": rooms["r2"],
                             "start_at": t(1100), "end_at": t(1200)},
                        ],
                    },
                ),
                201,
                "consultation on Dave booking",
            )
            must(api.post(f"/participants/{d}/deletion-requests"), 201, "Dave requests purge")
            purge_booking = must(api.get(f"/owner/bookings/{bk_purge}",
                                         headers=OWNER_HEADERS), 200, "Dave booking after purge")
            check("Dave's not-started booking cancelled",
                  purge_booking["status"] == "cancelled")
            purge_order = must(api.get(f"/owner/reschedule-orders/{ro_purge}",
                                       headers=OWNER_HEADERS), 200, "order after purge")
            check("Dave's consultation invalidated",
                  purge_order["status"] == "cancelled_booking")
            # Session revocation is independent of the purge: the credential is
            # only invalidated by an explicit session deletion.
            dave_cal = api.get("/me/calendar", headers=session_headers(token_d))
            check("purge alone does not revoke the session",
                  dave_cal.status_code == 200)
            must(api.delete(f"/sessions/sess-dave-{suffix}"), 200, "explicitly revoke Dave session")
            check("explicit session revocation rejected afterwards",
                  api.get("/me/calendar", headers=session_headers(token_d)).status_code == 401)

            print("\n[8/9] Concurrent booking races have exactly one winner")
            race_iid = f"int-race-{suffix}"
            must(api.post("/interviews", json={"id": race_iid, "title": "Race"}), 201, "race interview")

            def race_booking(i: int) -> httpx.Response:
                return api.post(
                    "/owner/bookings",
                    headers=OWNER_HEADERS,
                    json={
                        "id": f"bk-race-{i}-{suffix}",
                        "interview_id": f"int-race-{suffix}",
                        "room_id": rooms["r1"],
                        "participant_ids": [e],
                        "start_at": t(1300 + i),
                        "end_at": t(1400 + i),
                    },
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                responses = list(pool.map(race_booking, range(8)))
            codes = sorted(r.status_code for r in responses)
            winners = [r for r in responses if r.status_code == 201]
            check("exactly one concurrent booking wins", len(winners) == 1, str(codes))
            check("all losing races report slot/active conflicts",
                  all(r.json()["error"]["code"] in {"slot_conflict", "active_booking_exists"}
                      for r in responses if r.status_code == 409))

            print("\n[9/9] Restart: an order expiring while down releases slots on recovery")
            # Create a fresh pending consultation before stopping the server.
            bk_restart = f"bk-restart-{suffix}"
            ro_restart = f"ro-restart-{suffix}"
            must(
                api.post(
                    "/owner/bookings",
                    headers=OWNER_HEADERS,
                    json={
                        "id": bk_restart, "interview_id": interviews["eleven"],
                        "room_id": rooms["r2"], "participant_ids": [e],
                        "start_at": t(1500), "end_at": t(1600),
                    },
                ),
                201,
                "create booking for restart-expiry scenario",
            )
            must(
                api.post(
                    "/owner/reschedule-orders",
                    headers=OWNER_HEADERS,
                    json={
                        "id": ro_restart,
                        "consultation_deadline": t(50),
                        "items": [
                            {"booking_id": bk_restart, "room_id": rooms["r3"],
                             "start_at": t(1500), "end_at": t(1600)},
                        ],
                    },
                ),
                201,
                "create order that will expire while down",
            )
            # While pending, the proposed room r3 1500-1600 slot is held.
            held_before = api.post(
                "/owner/bookings",
                headers=OWNER_HEADERS,
                json={
                    "interview_id": interviews["six"], "room_id": rooms["r3"],
                    "participant_ids": [a], "start_at": t(1500), "end_at": t(1600),
                },
            )
            check("proposed slot held before restart",
                  held_before.status_code == 409, held_before.text)

        # Move the deadline into the past while no server is running, so expiry
        # can only happen through startup recovery.
        from app.db import connect  # noqa: E402

        offline = connect(DB_PATH)
        try:
            offline.execute(
                "UPDATE reschedule_orders SET consultation_deadline = ? WHERE id = ?",
                (t(-1), ro_restart),
            )
            offline.commit()
            pending = offline.execute(
                "SELECT status FROM reschedule_orders WHERE id = ?", (ro_restart,)
            ).fetchone()
            check("order still pending in SQLite while server is down",
                  pending["status"] == "pending")
        finally:
            offline.close()

        stop_server(proc, log_handle)
        port2 = free_port()
        proc, log_handle = start_server(port2, log_path)
        base = f"http://127.0.0.1:{port2}"
        with httpx.Client(base_url=base, timeout=10.0) as api:
            health = must(api.get("/health"), 200, "restart health")
            check("service healthy after restart", health["status"] == "ok")

            # Startup recovery expired the order and released its proposed slot.
            restart_order = must(api.get(f"/owner/reschedule-orders/{ro_restart}",
                                         headers=OWNER_HEADERS), 200, "restart order detail")
            check("startup recovery expired the down-time order",
                  restart_order["status"] == "expired", str(restart_order["status"]))
            must(
                api.post(
                    "/owner/bookings",
                    headers=OWNER_HEADERS,
                    json={
                        "interview_id": interviews["six"], "room_id": rooms["r3"],
                        "participant_ids": [a], "start_at": t(1500), "end_at": t(1600),
                    },
                ),
                201,
                "expired proposal slot reusable after restart",
            )
            late = api.post(f"/me/reschedule-orders/{ro_restart}/responses",
                            headers=session_headers(token_e), json={"decision": "accepted"})
            check("late answer cannot apply the expired order",
                  late.status_code == 409, late.text)

            # Earlier consultations keep their committed terminal states.
            orders = must(api.get("/owner/reschedule-orders", headers=OWNER_HEADERS),
                          200, "orders after restart")
            statuses = {o["id"]: o["status"] for o in orders["orders"]}
            check("swap order still approved after restart",
                  statuses.get(ro_swap) == "approved")
            check("rejected order still rejected after restart",
                  statuses.get(ro_rej) == "rejected")
            check("cancelled order still invalidated after restart",
                  statuses.get(ro_cx) == "cancelled_booking")
            # The applied swap survived the restart.
            bk_one_after = must(api.get(f"/owner/bookings/{bk_one}",
                                        headers=OWNER_HEADERS), 200, "swap survives restart")
            check("applied room swap persisted across restart",
                  bk_one_after["room_id"] == rooms["r2"])
            # Explicit sweep now reports nothing pending.
            sweep = must(api.post("/owner/reschedule-orders/sweep-expired",
                                  headers=OWNER_HEADERS), 200, "post-restart sweep")
            check("no stale pending order holds slots after restart", sweep["count"] == 0)

        print(f"\nALL {len(CHECKS)} CHECKS PASSED")
        print(f"Temporary database retained for inspection: {DB_PATH}")
        print(f"Server log: {log_path}")
        return 0
    except Exception:
        print("\nServer log tail:")
        try:
            sys.stdout.write(log_path.read_text(errors="replace")[-4000:])
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        try:
            stop_server(proc, log_handle)
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
