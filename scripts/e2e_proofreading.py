#!/usr/bin/env python3
"""HTTP end-to-end verification for collaborative proofreading (校稿).

Starts a real uvicorn process on a temporary SQLite database and verifies:

* owner credential required on every proofreading route;
* one open proofreading session per interview, selecting visible segments only;
* parallel submissions from different editors, all based on the same draft
  version, merge without overwriting each other;
* a stale proposal conflicts only where edits actually overlap, and a whole
  conflicting proposal is rejected while naming the segment;
* submission-ID idempotency (same payload replays the original merge result;
  the ID cannot be reused for a different proposal);
* an unresolved annotation blocks publish (anchored or lost) but not merging;
  losing/re-anchoring/resolving behaves as specified;
* publish is version-conditional and atomic, and ordinary read/search/export
  only ever see the complete official body before or after publish;
* participant deletion removes the segment from proofreading scope and publish
  range, blocks new proposals for it, and removes its draft/replacement/
  annotation text, while other segments can still be published;
* a real server restart keeps the draft, annotations, versions and results.
"""

from __future__ import annotations

import concurrent.futures
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

TEMP_DIR = Path(tempfile.mkdtemp(prefix="pdc-proof-e2e-"))
DB_PATH = TEMP_DIR / "proof.db"
os.environ["PDC_DB_PATH"] = str(DB_PATH)
os.environ["PDC_OWNER_TOKEN"] = "demo-owner-token"

from app.db import connect, init_db  # noqa: E402

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


def body_of(response: httpx.Response, expected: int | set[int], label: str) -> dict:
    allowed = {expected} if isinstance(expected, int) else set(expected)
    if response.status_code not in allowed:
        raise AssertionError(f"{label}: expected {allowed}, got {response.status_code}: {response.text}")
    return response.json() if response.content else {}


def segment_text(detail: dict, segment_id: str) -> str:
    return next(s["text"] for s in detail["segments"] if s["segment_id"] == segment_id)


def submit(api: httpx.Client, session_id: str, submission_id: str, base_version: int, edits: list[dict]):
    return api.post(
        f"/owner/proofreading/sessions/{session_id}/proposals",
        headers=OWNER_HEADERS,
        json={"submission_id": submission_id, "base_version": base_version, "edits": edits},
    )


def edit(segment_id: str, start: int, end: int, replacement: str) -> dict:
    return {"segment_id": segment_id, "start": start, "end": end, "replacement": replacement}


def main() -> int:
    suffix = os.urandom(4).hex()
    conn = connect(DB_PATH)
    init_db(conn)
    conn.close()

    port = free_port()
    log_path = TEMP_DIR / "uvicorn.log"
    proc, log_handle = start_server(port, log_path)
    base = f"http://127.0.0.1:{port}"
    print(f"Database: {DB_PATH}")
    print(f"Server:   {base}")

    try:
        with httpx.Client(base_url=base, timeout=15.0) as api:
            print("\n[1/8] Explicit scenario: interview, visible segments, editor participants")
            for pid in (f"p-alpha-{suffix}", f"p-beta-{suffix}"):
                body_of(
                    api.post(
                        "/participants",
                        json={
                            "id": pid,
                            "display_name": pid,
                            "session_token": f"sess-{pid}",
                            "profile": {"note": "explicit local data"},
                        },
                    ),
                    201,
                    f"create {pid}",
                )
            iid = f"int-proof-{suffix}"
            body_of(api.post("/interviews", json={"id": iid, "title": "Proofreading E2E"}), 201, "interview")

            seg_a = f"seg-a-{suffix}"
            seg_b = f"seg-b-{suffix}"
            seg_doomed = f"seg-doomed-{suffix}"
            # 20 code points so each editor can safely replace one character.
            base_a = "0123456789ABCDEFGHIJ"
            base_b = "second draft body"
            base_doomed = "eventually deleted body"
            for sid, text, pids in [
                (seg_a, base_a, [f"p-beta-{suffix}"]),
                (seg_b, base_b, [f"p-beta-{suffix}"]),
                (seg_doomed, base_doomed, [f"p-alpha-{suffix}", f"p-beta-{suffix}"]),
            ]:
                body_of(
                    api.post(
                        "/segments",
                        json={"id": sid, "interview_id": iid, "content": text, "participant_ids": pids},
                    ),
                    201,
                    f"segment {sid}",
                )
            export_id = f"exp-proof-{suffix}"
            body_of(
                api.post("/exports", json={"id": export_id, "scope": "interview", "interview_id": iid}),
                201,
                "export request",
            )

            print("\n[2/8] Owner-only session creation; one open session per interview")
            session_id = f"prf-{suffix}"
            unauth = api.post(
                "/owner/proofreading/sessions",
                json={"interview_id": iid, "segment_ids": [seg_a, seg_b, seg_doomed]},
            )
            check("session creation requires owner credential", unauth.status_code == 403, unauth.text)

            created = body_of(
                api.post(
                    "/owner/proofreading/sessions",
                    headers=OWNER_HEADERS,
                    json={"interview_id": iid, "segment_ids": [seg_a, seg_b, seg_doomed]},
                ),
                201,
                "open session",
            )
            session_id = created["id"]
            check("session snapshots version 1", created["current_version"] == 1)
            check("all three segments selected", len(created["segments"]) == 3)
            check(
                "draft equals official body at open",
                segment_text(created, seg_a) == base_a,
            )
            duplicate = api.post(
                "/owner/proofreading/sessions",
                headers=OWNER_HEADERS,
                json={"interview_id": iid, "segment_ids": [seg_b]},
            )
            check("second open session rejected", duplicate.status_code == 409, duplicate.text)
            check(
                "error code says session exists",
                duplicate.json()["error"]["code"] == "proofreading_session_exists",
            )

            print("\n[3/8] Parallel editors all based on version 1: no lost updates")
            # 20 editors concurrently replace one distinct character each.
            def parallel_submission(index: int) -> tuple[int, str]:
                with httpx.Client(base_url=base, timeout=30.0) as client:
                    response = submit(
                        client,
                        session_id,
                        f"sub-{suffix}-{index}",
                        1,
                        [edit(seg_a, index, index + 1, chr(ord("a") + index))],
                    )
                    replay = client.post(
                        f"/owner/proofreading/sessions/{session_id}/proposals",
                        headers=OWNER_HEADERS,
                        json={
                            "submission_id": f"sub-{suffix}-{index}",
                            "base_version": 1,
                            "edits": [edit(seg_a, index, index + 1, chr(ord("a") + index))],
                        },
                    )
                    replayed = replay.json().get("idempotent_replay") is True
                    return response.status_code, f"{response.status_code}/{replay.status_code}:{replayed}"

            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
                results = list(pool.map(parallel_submission, range(len(base_a))))
            check("all 20 parallel submissions merged", all(code == 201 for code, _ in results), str(results))
            check("all retries replayed idempotently", all(":True" in note for _, note in results))

            detail = body_of(
                api.get(f"/owner/proofreading/sessions/{session_id}", headers=OWNER_HEADERS),
                200,
                "draft after parallel merge",
            )
            check(
                "every editor's change survived",
                segment_text(detail, seg_a) == "abcdefghijklmnopqrst",
                segment_text(detail, seg_a),
            )
            check("version advanced once per merge", detail["current_version"] == 21)
            version = detail["current_version"]

            print("\n[4/8] Stale conflicting proposal rejected as a whole (segment named)")
            # Based on v1, overlapping the range already changed by editor 5.
            conflict = submit(
                api,
                session_id,
                f"conflict-{suffix}",
                1,
                [edit(seg_a, 5, 6, "X"), edit(seg_b, 0, 6, "FIRST-")],
            )
            check("conflicting stale proposal -> 409", conflict.status_code == 409, conflict.text)
            details = conflict.json()["error"]["details"]
            check("conflicting segment identified", details["segments"] == [seg_a], str(details))
            detail = body_of(
                api.get(f"/owner/proofreading/sessions/{session_id}", headers=OWNER_HEADERS),
                200,
                "draft after rejection",
            )
            check(
                "non-conflicting segment from the rejected proposal untouched",
                segment_text(detail, seg_b) == base_b,
            )
            check("conflict did not create a version", detail["current_version"] == version)

            # A stale proposal that does not conflict still merges.
            stale_ok = submit(
                api, session_id, f"stale-ok-{suffix}", 1, [edit(seg_b, 0, 6, "MERGEDTEXT")]
            )
            merged = body_of(stale_ok, 201, "stale non-conflicting merge")
            check("stale non-conflicting proposal rebased", merged["rebased"] is True)
            check(
                "stale merge applied to current draft",
                segment_text(merged, seg_b) == "MERGEDTEXT draft body",
                segment_text(merged, seg_b),
            )

            # Same submission ID with different content cannot be reused.
            reused = submit(api, session_id, f"stale-ok-{suffix}", 1, [edit(seg_b, 0, 6, "OTHER")])
            check("submission ID cannot be reused for another proposal", reused.status_code == 409)
            check(
                "reuse error code",
                reused.json()["error"]["code"] == "submission_id_reused",
            )

            print("\n[5/8] Annotations: shift, lose anchor, block publish, re-anchor, resolve")
            detail = body_of(
                api.get(f"/owner/proofreading/sessions/{session_id}", headers=OWNER_HEADERS),
                200,
                "draft before annotations",
            )
            version = detail["current_version"]
            anchor_ok = f"ann-shift-{suffix}"
            # In "abcdefghijklmnopqrst", annotate "ghij" = indices 6..10.
            note = body_of(
                api.post(
                    f"/owner/proofreading/sessions/{session_id}/annotations",
                    headers=OWNER_HEADERS,
                    json={"segment_id": seg_a, "start": 6, "end": 10, "text": "verify ghij"},
                ),
                201,
                "create annotation",
            )
            # A non-conflicting change before the annotated text shifts it.
            shifted = submit(
                api,
                session_id,
                f"shift-{suffix}",
                version,
                [edit(seg_a, 0, 1, "<<")],
            )
            body_of(shifted, 201, "prepend edit")
            detail = body_of(
                api.get(f"/owner/proofreading/sessions/{session_id}", headers=OWNER_HEADERS),
                200,
                "draft after prepend",
            )
            ann = next(a for a in detail["segments"][0]["annotations"] if a["id"] == note["id"])
            check("annotation stays anchored after avoid edit", ann["anchored"] is True)
            check(
                "annotation still points at the same text",
                segment_text(detail, seg_a)[ann["start"] : ann["end"]] == "ghij",
                f"{ann['start']}..{ann['end']}",
            )
            check("unresolved annotation blocks publish", detail["can_publish"] is False)

            version = detail["current_version"]
            publish_blocked = api.post(
                f"/owner/proofreading/sessions/{session_id}/publish",
                headers=OWNER_HEADERS,
                json={"version": version},
            )
            check("publish blocked by unresolved annotation", publish_blocked.status_code == 409)
            check(
                "publish error names annotation block",
                publish_blocked.json()["error"]["code"] == "proofreading_unresolved_annotations",
            )
            # Merging continues despite the unresolved annotation.
            body_of(
                submit(api, session_id, f"during-block-{suffix}", version, [edit(seg_b, 0, 0, "#")]),
                201,
                "merge while annotation pending",
            )

            # Replace a character inside the annotated text: anchor is lost.
            detail = body_of(
                api.get(f"/owner/proofreading/sessions/{session_id}", headers=OWNER_HEADERS),
                200,
                "draft before losing anchor",
            )
            version = detail["current_version"]
            ann = next(a for a in detail["segments"][0]["annotations"] if a["id"] == note["id"])
            body_of(
                submit(
                    api,
                    session_id,
                    f"lose-anchor-{suffix}",
                    version,
                    [edit(seg_a, ann["start"] + 1, ann["start"] + 2, "Z")],
                ),
                201,
                "edit inside annotation",
            )
            lost = body_of(
                api.get(f"/owner/proofreading/sessions/{session_id}", headers=OWNER_HEADERS),
                200,
                "draft after losing anchor",
            )
            ann = next(a for a in lost["segments"][0]["annotations"] if a["id"] == note["id"])
            check("annotation reported as lost", ann["anchored"] is False and ann["anchor_status"] == "lost")
            check("lost annotation has no guessed position", ann["start"] is None and ann["end"] is None)
            check("lost unresolved annotation still blocks", lost["can_publish"] is False)

            # Re-anchor explicitly to the current version, then resolve.
            version = lost["current_version"]
            reanchored = body_of(
                api.post(
                    f"/owner/proofreading/sessions/{session_id}/annotations/{note['id']}/reanchor",
                    headers=OWNER_HEADERS,
                    json={"start": 7, "end": 11},
                ),
                200,
                "reanchor",
            )
            check("re-anchored annotation is anchored", reanchored["anchored"] is True)
            body_of(
                api.post(
                    f"/owner/proofreading/sessions/{session_id}/annotations/{note['id']}/resolve",
                    headers=OWNER_HEADERS,
                ),
                200,
                "resolve",
            )
            final = body_of(
                api.get(f"/owner/proofreading/sessions/{session_id}", headers=OWNER_HEADERS),
                200,
                "draft before publish",
            )
            check("resolved annotation no longer blocks", final["can_publish"] is True)
            version = final["current_version"]
            draft_a_before_publish = segment_text(final, seg_a)
            draft_b_before_publish = segment_text(final, seg_b)

            print("\n[6/8] Publish is version-gated; ordinary entries see the complete new body")
            before_text = api.get(f"/segments/{seg_a}").json()["content"]
            check("official body unchanged before publish", before_text == base_a, before_text)
            check(
                "search cannot find draft-only text before publish",
                api.get("/search", params={"q": "MERGEDTEXT"}).json()["count"] == 0,
            )
            check(
                "export shows old body before publish",
                api.get(f"/exports/{export_id}/content").json()["segments"][0]["content"]
                == base_a,
            )
            stale_publish = api.post(
                f"/owner/proofreading/sessions/{session_id}/publish",
                headers=OWNER_HEADERS,
                json={"version": version - 1},
            )
            check("publish with a stale version rejected", stale_publish.status_code == 409)
            published = body_of(
                api.post(
                    f"/owner/proofreading/sessions/{session_id}/publish",
                    headers=OWNER_HEADERS,
                    json={"version": version},
                ),
                200,
                "publish",
            )
            check(
                "publish reports all three segments atomically",
                set(published["published_segment_ids"]) == {seg_a, seg_b, seg_doomed},
                str(published["published_segment_ids"]),
            )
            official_a = api.get(f"/segments/{seg_a}").json()["content"]
            official_b = api.get(f"/segments/{seg_b}").json()["content"]
            check(
                "official body updated after publish",
                official_a == draft_a_before_publish and "MERGEDTEXT" in official_b,
                f"{official_a!r} vs {draft_a_before_publish!r}",
            )
            check(
                "second segment also updated",
                official_b == draft_b_before_publish,
                f"{official_b!r} vs {draft_b_before_publish!r}",
            )
            check(
                "search now finds published text",
                api.get("/search", params={"q": "MERGEDTEXT"}).json()["count"] == 1,
            )
            interview = api.get(f"/interviews/{iid}").json()
            check(
                "ordinary interview read is internally consistent",
                all(
                    s["content"] == official_a if s["id"] == seg_a else True
                    for s in interview["segments"]
                ),
            )
            check(
                "export now returns the published body",
                api.get(f"/exports/{export_id}/content").json()["segments"][0]["content"]
                == official_a,
            )
            closed = submit(
                api, session_id, f"after-publish-{suffix}", version, [edit(seg_a, 0, 0, "!")]
            )
            check("published session rejects new proposals", closed.status_code == 409)
            check(
                "closed-session error code",
                closed.json()["error"]["code"] == "proofreading_session_closed",
            )

            print("\n[7/8] Participant deletion removes the segment from proofreading scope")
            # A second session is needed because the first interview session is now published.
            new_session = body_of(
                api.post(
                    "/owner/proofreading/sessions",
                    headers=OWNER_HEADERS,
                    json={"interview_id": iid, "segment_ids": [seg_a, seg_b, seg_doomed]},
                ),
                201,
                "reopen session after publish",
            )
            session2 = new_session["id"]
            doomed_ann = body_of(
                api.post(
                    f"/owner/proofreading/sessions/{session2}/annotations",
                    headers=OWNER_HEADERS,
                    json={"segment_id": seg_doomed, "start": 0, "end": 7, "text": "doomed note"},
                ),
                201,
                "annotation on soon-purged segment",
            )
            body_of(
                submit(
                    api,
                    session2,
                    f"secret-draft-{suffix}",
                    new_session["current_version"],
                    [
                        edit(seg_doomed, 0, 7, "SECRET_DRAFT_MARKER_9f2"),
                        edit(seg_b, 0, 0, "SURVIVOR_"),
                    ],
                ),
                201,
                "merge touching doomed segment",
            )
            body_of(api.post(f"/participants/p-alpha-{suffix}/deletion-requests"), 201, "deletion")

            scope = body_of(
                api.get(f"/owner/proofreading/sessions/{session2}", headers=OWNER_HEADERS),
                200,
                "session after purging participant",
            )
            check(
                "purged segment exits visible/publishable scope",
                scope["publishable_segment_ids"] == [seg_a, seg_b],
                str(scope["publishable_segment_ids"]),
            )
            check(
                "annotation on purged segment no longer blocks",
                scope["unresolved_annotation_count"] == 0,
            )
            blocked_new = submit(
                api,
                session2,
                f"post-purge-{suffix}",
                scope["current_version"],
                [edit(seg_doomed, 0, 7, "nope")],
            )
            check("new merge on purged segment fails", blocked_new.status_code == 409)
            check(
                "purged merge error code",
                blocked_new.json()["error"]["code"] == "proofreading_segment_out_of_scope",
            )
            check("ordinary read of purged segment is 404", api.get(f"/segments/{seg_doomed}").status_code == 404)
            surviving = body_of(
                submit(
                    api,
                    session2,
                    f"survivor-{suffix}",
                    scope["current_version"],
                    [edit(seg_a, 0, 2, "~~")],
                ),
                201,
                "merge on surviving segment",
            )
            surviving_text = segment_text(surviving, seg_a)
            check("other segments can still merge", surviving["status"] == "merged")

            db = connect(DB_PATH)
            try:
                check(
                    "draft history for purged segment removed",
                    db.execute(
                        "SELECT COUNT(*) AS c FROM proofreading_segment_contents WHERE segment_id=?",
                        (seg_doomed,),
                    ).fetchone()["c"] == 0,
                )
                check(
                    "replacement text for purged segment removed",
                    db.execute(
                        "SELECT COUNT(*) AS c FROM proofreading_edits WHERE segment_id=?",
                        (seg_doomed,),
                    ).fetchone()["c"] == 0,
                )
                check(
                    "annotation text for purged segment removed",
                    db.execute(
                        "SELECT COUNT(*) AS c FROM proofreading_annotations WHERE segment_id=?",
                        (seg_doomed,),
                    ).fetchone()["c"] == 0,
                )
                stored = db.execute(
                    "SELECT result_json FROM proofreading_submissions WHERE session_id=?",
                    (session2,),
                ).fetchall()
                check(
                    "stored merge results no longer echo purged draft text",
                    all("SECRET_DRAFT_MARKER" not in r["result_json"] for r in stored),
                )
            finally:
                db.close()

            publish_remaining = body_of(
                api.post(
                    f"/owner/proofreading/sessions/{session2}/publish",
                    headers=OWNER_HEADERS,
                    json={"version": surviving["merged_version"]},
                ),
                200,
                "publish remaining segments",
            )
            check(
                "publish after purge updates only surviving segments",
                set(publish_remaining["published_segment_ids"]) == {seg_a, seg_b},
                str(publish_remaining["published_segment_ids"]),
            )
            check(
                "surviving official body reflects published draft",
                api.get(f"/segments/{seg_a}").json()["content"] == surviving_text,
                surviving_text,
            )
            check("purged segment stays unavailable", api.get(f"/segments/{seg_doomed}").status_code == 404)

        print("\n[8/8] Restart the server and confirm persisted draft/results")
        # Open a fresh session whose draft is deliberately not published, then
        # restart and confirm it is still there.
        with httpx.Client(base_url=base, timeout=15.0) as api:
            pending = body_of(
                api.post(
                    "/owner/proofreading/sessions",
                    headers=OWNER_HEADERS,
                    json={"interview_id": iid, "segment_ids": [seg_a]},
                ),
                201,
                "session before restart",
            )
            pending_id = pending["id"]
            pending_merge = body_of(
                submit(
                    api,
                    pending_id,
                    f"restart-{suffix}",
                    pending["current_version"],
                    [edit(seg_a, 0, 2, ">>")],
                ),
                201,
                "merge before restart",
            )
            pending_text = segment_text(pending_merge, seg_a)
            published_before_restart = api.get(f"/segments/{seg_a}").json()["content"]
            check("pre-restart draft exists", pending_text.startswith(">>"), pending_text)
            check(
                "unpublished draft is not yet official",
                published_before_restart == surviving_text,
            )

        stop_server(proc, log_handle)
        port2 = free_port()
        proc, log_handle = start_server(port2, log_path)
        base = f"http://127.0.0.1:{port2}"
        with httpx.Client(base_url=base, timeout=15.0) as api:
            restored = body_of(
                api.get(f"/owner/proofreading/sessions/{pending_id}", headers=OWNER_HEADERS),
                200,
                "session after restart",
            )
            check("draft survives restart", segment_text(restored, seg_a) == pending_text)
            check("version survives restart", restored["current_version"] == 2)
            replay = body_of(
                submit(
                    api,
                    pending_id,
                    f"restart-{suffix}",
                    pending["current_version"],
                    [edit(seg_a, 0, 2, ">>")],
                ),
                200,
                "submission replay after restart",
            )
            check("submission idempotency survives restart", replay["idempotent_replay"] is True)
            check(
                "published state survives restart",
                api.get(f"/segments/{seg_a}").json()["content"] == surviving_text,
            )
            check(
                "published search result survives restart",
                api.get("/search", params={"q": "MERGEDTEXT"}).json()["count"] == 1,
            )
            check(
                "export after restart uses the published body",
                api.get(f"/exports/{export_id}/content").json()["segments"][0]["content"]
                == surviving_text,
            )

        print(f"\nALL {len(CHECKS)} CHECKS PASSED")
        print(f"Temporary database kept for inspection: {DB_PATH}")
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
