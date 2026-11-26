"""Concurrency invariants for retention creation/release versus purge."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from app.db import connect, init_db
from app.services import (
    ApiError,
    create_deletion_request,
    create_interview,
    create_participant,
    create_retention,
    create_segment,
    release_retention,
)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "concurrency.db"
    conn = connect(path)
    try:
        init_db(conn)
        create_interview(conn, interview_id="int-race", title="Race Interview")
    finally:
        conn.close()
    return path


def test_deletion_versus_retention_commit_order(db_path: Path) -> None:
    iterations = 25

    for i in range(iterations):
        pid = f"p-del-{i}"
        sid = f"seg-del-ret-{i}"
        setup = connect(db_path)
        try:
            create_participant(
                setup,
                participant_id=pid,
                display_name=f"P {i}",
                profile={},
                session_token=f"s-{i}",
            )
            create_segment(
                setup,
                segment_id=sid,
                interview_id="int-race",
                content=f"body {sid}",
                participant_ids=[pid],
            )
        finally:
            setup.close()

        barrier = threading.Barrier(2)
        outcomes: dict[str, object] = {}

        def deletion_worker() -> None:
            barrier.wait()
            conn = connect(db_path)
            try:
                detail, created = create_deletion_request(conn, pid)
                outcomes["deletion"] = detail
                outcomes["deletion_created"] = created
            except Exception as exc:  # capture for assertion in main thread
                outcomes["deletion_error"] = exc
            finally:
                conn.close()

        def retention_worker() -> None:
            barrier.wait()
            conn = connect(db_path)
            try:
                outcomes["retention"] = create_retention(
                    conn, sid, reason=f"concurrent reason {i}", created_by="owner"
                )
            except ApiError as exc:
                outcomes["retention_error"] = exc
            except Exception as exc:  # pragma: no cover - failure path
                outcomes["retention_unexpected"] = exc
            finally:
                conn.close()

        t1 = threading.Thread(target=deletion_worker)
        t2 = threading.Thread(target=retention_worker)
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)
        assert not t1.is_alive() and not t2.is_alive()

        assert "deletion_error" not in outcomes, repr(outcomes.get("deletion_error"))
        assert "retention_unexpected" not in outcomes, repr(outcomes.get("retention_unexpected"))
        assert outcomes.get("deletion_created") is True

        verify = connect(db_path)
        try:
            segment = verify.execute("SELECT id FROM segments WHERE id=?", (sid,)).fetchone()
            item = verify.execute(
                "SELECT status FROM deletion_items WHERE segment_id=?", (sid,)
            ).fetchone()
            active_count = verify.execute(
                "SELECT COUNT(*) AS c FROM retentions WHERE segment_id=? AND released_at IS NULL",
                (sid,),
            ).fetchone()["c"]

            if "retention" in outcomes:
                # A successfully committed retention must have won before the
                # purge and must leave one consistent retained state.
                assert segment is not None, i
                assert item is not None and item["status"] == "retained", i
                assert active_count == 1, i
            else:
                err = outcomes.get("retention_error")
                assert isinstance(err, ApiError), outcomes
                assert err.status_code == 409
                assert err.code == "segment_body_purged_cannot_retain"
                assert segment is None, i
                assert item is not None and item["status"] == "purged", i
                assert active_count == 0, i
        finally:
            verify.close()


def test_release_last_versus_create_retention_commit_order(db_path: Path) -> None:
    iterations = 25

    for i in range(iterations):
        pid = f"p-release-{i}"
        sid = f"seg-release-create-{i}"
        setup = connect(db_path)
        try:
            create_participant(
                setup,
                participant_id=pid,
                display_name=f"Release P {i}",
                profile={},
                session_token=f"sr-{i}",
            )
            create_segment(
                setup,
                segment_id=sid,
                interview_id="int-race",
                content=f"release race body {sid}",
                participant_ids=[pid],
            )
            old_retention = create_retention(
                setup, sid, reason="pre-existing hold", created_by="owner"
            )
            old_id = old_retention["id"]
            create_deletion_request(setup, pid)
        finally:
            setup.close()

        barrier = threading.Barrier(2)
        outcomes: dict[str, object] = {}

        def release_worker() -> None:
            barrier.wait()
            conn = connect(db_path)
            try:
                outcomes["release"] = release_retention(conn, old_id)
            except Exception as exc:
                outcomes["release_error"] = exc
            finally:
                conn.close()

        def create_worker() -> None:
            barrier.wait()
            conn = connect(db_path)
            try:
                outcomes["new_retention"] = create_retention(
                    conn, sid, reason=f"racing new hold {i}", created_by="owner"
                )
            except ApiError as exc:
                outcomes["create_error"] = exc
            except Exception as exc:  # pragma: no cover
                outcomes["create_unexpected"] = exc
            finally:
                conn.close()

        t1 = threading.Thread(target=release_worker)
        t2 = threading.Thread(target=create_worker)
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        assert "release_error" not in outcomes, repr(outcomes.get("release_error"))
        assert "create_unexpected" not in outcomes, repr(outcomes.get("create_unexpected"))

        verify = connect(db_path)
        try:
            segment = verify.execute("SELECT id FROM segments WHERE id=?", (sid,)).fetchone()
            item = verify.execute(
                "SELECT status FROM deletion_items WHERE segment_id=?", (sid,)
            ).fetchone()
            old = verify.execute("SELECT released_at FROM retentions WHERE id=?", (old_id,)).fetchone()
            new_active = verify.execute(
                "SELECT COUNT(*) AS c FROM retentions WHERE segment_id=? AND released_at IS NULL",
                (sid,),
            ).fetchone()["c"]
            assert old["released_at"] is not None

            if "new_retention" in outcomes:
                # New hold committed first; releasing the old hold was not the
                # last active retention, so the body must remain restricted.
                assert segment is not None, i
                assert item["status"] == "retained", i
                assert new_active == 1, i
            else:
                err = outcomes.get("create_error")
                assert isinstance(err, ApiError), outcomes
                assert err.code == "segment_body_purged_cannot_retain"
                # Release was the last hold at its commit point and purged in
                # the same transaction; the racing create must not survive.
                assert segment is None, i
                assert item["status"] == "purged", i
                assert new_active == 0, i
        finally:
            verify.close()
