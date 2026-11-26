"""Shared segments and later requests from another participant."""

from __future__ import annotations

from pathlib import Path

from app.db import connect, init_db
from app.services import (
    create_deletion_request,
    create_interview,
    create_participant,
    create_segment,
    preview_deletion,
)


def test_later_participant_request_discovers_already_purged_shared_segment(tmp_path: Path) -> None:
    db_path = tmp_path / "shared.db"
    conn = connect(db_path)
    init_db(conn)
    create_interview(conn, interview_id="int-shared", title="Shared")
    create_participant(conn, participant_id="p1", display_name="One", profile={}, session_token="s1")
    create_participant(conn, participant_id="p2", display_name="Two", profile={}, session_token="s2")
    create_segment(
        conn,
        segment_id="seg-shared",
        interview_id="int-shared",
        content="shared secret body",
        participant_ids=["p1", "p2"],
    )

    first, created = create_deletion_request(conn, "p1")
    assert created is True
    assert first["summary"]["purged"] == 1

    # The body row is gone, but IDs-only impact records remain.
    assert conn.execute("SELECT COUNT(*) AS c FROM segments WHERE id='seg-shared'").fetchone()["c"] == 0

    preview = preview_deletion(conn, "p2")
    assert preview["segment_count"] == 1
    assert preview["segments"][0]["predicted_action"] == "already_purged"

    second, created = create_deletion_request(conn, "p2")
    assert created is True
    assert second["summary"]["total"] == 1
    assert second["summary"]["purged"] == 1
    assert second["items"][0]["segment_id"] == "seg-shared"

    # A later request must not somehow leave a pending/hidden item.
    assert second["summary"].get("hidden_pending_purge", 0) == 0
    conn.close()
