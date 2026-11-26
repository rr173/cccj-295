#!/usr/bin/env python3
"""Cross-entry HTTP verification and restart recovery demo.

This script starts a real uvicorn process on a temporary SQLite database,
seeds explicit demo data, creates an isolated scenario, and checks:

* normal read/search/export hiding,
* owner-only retention viewing,
* multiple simultaneous retentions and last-release purge,
* idempotent deletion retry and write blocking,
* rejection of retention after physical purge,
* session revocation not undoing deletion,
* hidden_pending_purge != purged across a real process restart.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEMP_DIR = Path(tempfile.mkdtemp(prefix="pdc-e2e-"))
DB_PATH = TEMP_DIR / "e2e.db"
os.environ["PDC_DB_PATH"] = str(DB_PATH)
os.environ["PDC_OWNER_TOKEN"] = "demo-owner-token"

from app.db import connect  # noqa: E402
from app.seed import seed  # noqa: E402
from app import services  # noqa: E402

OWNER_HEADERS = {"X-Owner-Token": "demo-owner-token"}
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
    if response.status_code == 204 or not response.content:
        return {}
    return response.json()


def main() -> int:
    suffix = os.urandom(4).hex()
    seed(reset=True)
    port = free_port()
    log_path = TEMP_DIR / "uvicorn.log"
    proc, log_handle = start_server(port, log_path)
    base = f"http://127.0.0.1:{port}"
    print(f"Database: {DB_PATH}")
    print(f"Server:   {base}")

    try:
        with httpx.Client(base_url=base, timeout=10.0) as api:
            print("\n[1/8] Create explicit isolated scenario")
            a = f"p-e2e-a-{suffix}"
            b = f"p-e2e-b-{suffix}"
            c = f"p-e2e-c-{suffix}"
            d = f"p-e2e-d-{suffix}"
            token_a = f"sess-e2e-a-{suffix}"
            display_a = f"E2E Alice {suffix}"

            for pid, name, token in [
                (a, display_a, token_a),
                (b, f"E2E Bob {suffix}", f"sess-b-{suffix}"),
                (c, f"E2E Carol {suffix}", f"sess-c-{suffix}"),
                (d, f"E2E Dave {suffix}", f"sess-d-{suffix}"),
            ]:
                body = must(
                    api.post(
                        "/participants",
                        json={"id": pid, "display_name": name, "session_token": token, "profile": {"note": "local explicit data"}},
                    ),
                    201,
                    "create participant",
                )
                check(f"created {pid}", body["id"] == pid)

            iid = f"int-e2e-{suffix}"
            must(api.post("/interviews", json={"id": iid, "title": "E2E Interview"}), 201, "interview")

            seg_multi = f"seg-multi-{suffix}"
            seg_purge = f"seg-purge-{suffix}"
            seg_open = f"seg-open-{suffix}"
            seg_late = f"seg-late-{suffix}"
            marker_multi = f"E2E_MULTI_{suffix}_body"
            marker_purge = f"E2E_PURGE_{suffix}_body"
            marker_open = f"E2E_OPEN_{suffix}_body"
            marker_late = f"E2E_LATE_{suffix}_body"

            for sid, marker, pids in [
                (seg_multi, marker_multi, [a, b]),
                (seg_purge, marker_purge, [a, c]),
                (seg_open, marker_open, [b, c]),
            ]:
                must(
                    api.post("/segments", json={"id": sid, "interview_id": iid, "content": marker, "participant_ids": pids}),
                    201,
                    f"segment {sid}",
                )

            retention_reason_1 = f"E2E reason one {suffix}: legal/dispute hold"
            retention_reason_2 = f"E2E reason two {suffix}: owner review hold"
            ret1 = must(
                api.post(
                    f"/owner/segments/{seg_multi}/retentions",
                    headers=OWNER_HEADERS,
                    json={"reason": retention_reason_1, "created_by": "owner-1"},
                ),
                201,
                "retention 1",
            )["id"]
            ret2 = must(
                api.post(
                    f"/owner/segments/{seg_multi}/retentions",
                    headers=OWNER_HEADERS,
                    json={"reason": retention_reason_2, "created_by": "owner-2"},
                ),
                201,
                "retention 2",
            )["id"]
            check("two independent retention IDs", ret1 != ret2)

            export_id = f"exp-e2e-{suffix}"
            must(
                api.post("/exports", json={"id": export_id, "scope": "interview", "interview_id": iid, "format": "json"}),
                201,
                "old export request",
            )

            print("\n[2/8] Preview, then add a late segment before execution")
            preview = must(api.get(f"/participants/{a}/deletion-preview"), 200, "preview")
            preview_ids = {item["segment_id"] for item in preview["segments"]}
            check("preview sees current two A segments", preview_ids == {seg_multi, seg_purge}, str(preview_ids))
            predicted = {item["segment_id"]: item["predicted_action"] for item in preview["segments"]}
            check("preview predicts retention", predicted.get(seg_multi) == "restricted_retention")
            check("preview predicts purge", predicted.get(seg_purge) == "purge_body")

            must(
                api.post(
                    "/segments",
                    json={"id": seg_late, "interview_id": iid, "content": marker_late, "participant_ids": [a, d]},
                ),
                201,
                "late segment",
            )

            print("\n[3/8] Execute and retry idempotently")
            idem_key = f"idem-{suffix}"
            first = must(
                api.post(
                    f"/participants/{a}/deletion-requests",
                    headers={"Idempotency-Key": idem_key},
                ),
                201,
                "first deletion request",
            )
            request_id = first["request"]["id"]
            check("first call reports created", first["created"] is True)
            summary = first["request"]["summary"]
            check("three current segments included, including late one", summary["total"] == 3, str(summary))
            check("one retained", summary["retained"] == 1, str(summary))
            check("two purged", summary["purged"] == 2, str(summary))

            retry_same_key = must(
                api.post(
                    f"/participants/{a}/deletion-requests",
                    headers={"Idempotency-Key": idem_key},
                ),
                200,
                "retry same key",
            )
            check("same request on key retry", retry_same_key["request"]["id"] == request_id)
            check("retry reports not created", retry_same_key["created"] is False)
            retry_no_key = must(api.post(f"/participants/{a}/deletion-requests"), 200, "retry no key")
            check("same request on participant retry", retry_no_key["request"]["id"] == request_id)

            db = connect(DB_PATH)
            try:
                count = db.execute("SELECT COUNT(*) AS c FROM deletion_requests WHERE participant_id=?", (a,)).fetchone()["c"]
                check("exactly one task in SQLite", count == 1, f"count={count}")
            finally:
                db.close()

            print("\n[4/8] New writes are blocked")
            check(
                "profile write after request -> 409",
                api.patch(f"/participants/{a}/profile", json={"profile": {"x": 1}}).status_code == 409,
            )
            blocked = api.post(
                "/segments",
                json={"interview_id": iid, "content": "should be rejected", "participant_ids": [a, b]},
            )
            check("new segment after request -> 409", blocked.status_code == 409, blocked.text)

            print("\n[5/8] Ordinary reads, search and old export hide all A segments")
            gone = api.get(f"/participants/{a}")
            check("participant ordinary read is 410 tombstone", gone.status_code == 410, gone.text)
            check("erased display name absent from error", display_a not in gone.text)
            participants_response = api.get("/participants")
            must(participants_response, 200, "list participants")
            check("requested profile absent from list", display_a not in participants_response.text)

            for sid in [seg_multi, seg_purge, seg_late]:
                response = api.get(f"/segments/{sid}")
                check(f"direct segment {sid} unavailable", response.status_code == 404, response.text)

            interview_response = api.get(f"/interviews/{iid}")
            interview = must(interview_response, 200, "interview after deletion")
            visible_ids = {s["id"] for s in interview["segments"]}
            check("interview keeps only unaffected segment", visible_ids == {seg_open}, str(visible_ids))
            check("retained body absent from ordinary interview", marker_multi not in interview_response.text)

            for marker in [marker_multi, marker_purge, marker_late]:
                result = must(api.get("/search", params={"q": marker.rsplit("_", 1)[0]}), 200, "search hidden marker")
                check(f"search hides {marker[:24]}", result["count"] == 0, str(result))
            open_search = must(api.get("/search/sessions", params={"q": f"OPEN_{suffix}"}), 200, "search open")
            check("unaffected segment still searchable", open_search["count"] == 1)

            export_response = api.get(f"/exports/{export_id}/content")
            must(export_response, 200, "old export content")
            export_text = export_response.text
            check("old export validates current state and hides multi", marker_multi not in export_text)
            check("old export hides purged", marker_purge not in export_text and marker_late not in export_text)
            check("old export still contains unaffected", marker_open in export_text)

            print("\n[6/8] Owner restricted retention entry and queryable reasons")
            check("retention endpoint requires owner token", api.get(f"/owner/retentions/{ret1}").status_code == 403)
            hold1 = must(api.get(f"/owner/retentions/{ret1}", headers=OWNER_HEADERS), 200, "owner ret1")
            hold2 = must(api.get(f"/owner/retentions/{ret2}", headers=OWNER_HEADERS), 200, "owner ret2")
            check("restricted entry ret1 shows body", hold1.get("segment", {}).get("content") == marker_multi)
            check("restricted entry ret2 shows same indivisible body", hold2.get("segment", {}).get("content") == marker_multi)
            check("ordinary direct endpoint still 404", api.get(f"/segments/{seg_multi}").status_code == 404)

            status = must(api.get(f"/deletion-requests/{request_id}"), 200, "request status")
            multi_item = next(x for x in status["items"] if x["segment_id"] == seg_multi)
            check("multi item retained", multi_item["status"] == "retained")
            check("multi body still present", multi_item["body_present_in_business_storage"] is True)
            reasons = set(multi_item["active_retention_reasons"])
            check("both reasons queryable", {retention_reason_1, retention_reason_2} <= reasons, str(reasons))

            print("\n[7/8] Release one, then last retention; purge and no revival")
            released1 = must(
                api.post(f"/owner/retentions/{ret1}/release", headers=OWNER_HEADERS, json={"release_note": "first hold lifted"}),
                200,
                "release ret1",
            )
            check("released retention no longer exposes body itself", released1["body_accessible_through_retention"] is False)
            status = must(api.get(f"/deletion-requests/{request_id}"), 200, "status after ret1")
            multi_item = next(x for x in status["items"] if x["segment_id"] == seg_multi)
            check("still retained with one hold", multi_item["status"] == "retained" and len(multi_item["active_retentions"]) == 1)
            hold2_after = must(api.get(f"/owner/retentions/{ret2}", headers=OWNER_HEADERS), 200, "ret2 still works")
            check("remaining hold still shows body", hold2_after["segment"]["content"] == marker_multi)

            released2 = must(
                api.post(f"/owner/retentions/{ret2}/release", headers=OWNER_HEADERS, json={}),
                200,
                "release ret2",
            )
            check("last release purges in same transaction", released2["reconciliation"]["outcome"] == "purged")
            status = must(api.get(f"/deletion-requests/{request_id}"), 200, "status after last release")
            check("all items purged", status["summary"].get("purged") == 3 and status["summary"].get("retained", 0) == 0)
            multi_item = next(x for x in status["items"] if x["segment_id"] == seg_multi)
            check("purged item has timestamp and no body flag", bool(multi_item["purged_at"]) and multi_item["body_present_in_business_storage"] is False)

            db = connect(DB_PATH)
            try:
                row = db.execute("SELECT COUNT(*) AS c FROM segments WHERE id=?", (seg_multi,)).fetchone()["c"]
                check("SQLite business segment row physically removed", row == 0)
                leftover = db.execute(
                    "SELECT COUNT(*) AS c FROM deletion_items WHERE segment_id=? AND status='purged'", (seg_multi,)
                ).fetchone()["c"]
                check("content-free processing record remains", leftover >= 1)
            finally:
                db.close()

            cannot_retain = api.post(
                f"/owner/segments/{seg_multi}/retentions",
                headers=OWNER_HEADERS,
                json={"reason": "late attempt to revive purged body"},
            )
            check("retention after purge -> 409", cannot_retain.status_code == 409, cannot_retain.text)
            check(
                "error code says body purged",
                cannot_retain.json()["error"]["code"] == "segment_body_purged_cannot_retain",
            )

            revoked = must(api.delete(f"/sessions/{token_a}"), 200, "revoke session")
            check("session revoked", revoked["revoked"] is True)
            after_logout = must(api.get(f"/deletion-requests/{request_id}"), 200, "request after logout")
            check("session revocation does not undo purge", after_logout["summary"].get("purged") == 3)

            print("\n[8/8] Hidden unfinished state, then real restart recovery")
            unfinished_conn = connect(DB_PATH)
            try:
                detail, created = services.create_deletion_request(
                    unfinished_conn, "p-dave", process_now=False
                )
                check("demo unfinished request created", created is True and bool(detail["id"]))
            finally:
                unfinished_conn.close()

            dave = must(api.get("/participants/p-dave/deletion-request"), 200, "dave hidden")
            dave_item = next(item for item in dave["items"] if item["segment_id"] == "seg-crash-dave")
            check("before restart status is hidden_pending_purge", dave_item["status"] == "hidden_pending_purge", str(dave_item))
            check("hidden is not reported as purged", dave_item["body_present_in_business_storage"] is True and not dave_item["purged_at"])
            check("normal entry still hides it", api.get("/segments/seg-crash-dave").status_code == 404)
            db = connect(DB_PATH)
            try:
                raw = db.execute("SELECT content FROM segments WHERE id='seg-crash-dave'").fetchone()
                check("raw body is still present while merely hidden", raw is not None and "MARKER_CRASH" in raw["content"])
            finally:
                db.close()

        print("\nRestarting uvicorn to verify startup recovery...")
        stop_server(proc, log_handle)
        port2 = free_port()
        proc, log_handle = start_server(port2, log_path)
        base = f"http://127.0.0.1:{port2}"
        with httpx.Client(base_url=base, timeout=10.0) as api:
            dave_after = must(api.get("/participants/p-dave/deletion-request"), 200, "dave after restart")
            dave_item = next(item for item in dave_after["items"] if item["segment_id"] == "seg-crash-dave")
            check("restart completes purge", dave_item["status"] == "purged", str(dave_item))
            check("restarted request task processed", dave_after["task_status"] == "processed")
            check("purged body absent flag", dave_item["body_present_in_business_storage"] is False and bool(dave_item["purged_at"]))
            check("normal segment remains unavailable", api.get("/segments/seg-crash-dave").status_code == 404)
            db = connect(DB_PATH)
            try:
                count = db.execute("SELECT COUNT(*) AS c FROM segments WHERE id='seg-crash-dave'").fetchone()["c"]
                check("restart physically removed unfinished body", count == 0)
            finally:
                db.close()

        print(f"\nALL {len(CHECKS)} CHECKS PASSED")
        print(f"Temporary demo database retained for inspection: {DB_PATH}")
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
