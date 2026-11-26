"""Collaborative proofreading: merge, conflict, annotations, publish, purge."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from app import proofreading
from app.db import connect, init_db
from app.proofreading import ApiError
from app.services import (
    create_deletion_request,
    create_interview,
    create_participant,
    create_retention,
    create_segment,
    get_interview_normal,
    get_segment_normal,
    search_segments,
)


@pytest.fixture()
def conn(tmp_path: Path):
    path = tmp_path / "proof.db"
    connection = connect(path)
    init_db(connection)
    create_participant(
        connection, participant_id="p1", display_name="One", profile={}, session_token="s1"
    )
    create_participant(
        connection, participant_id="p2", display_name="Two", profile={}, session_token="s2"
    )
    create_interview(connection, interview_id="i1", title="Proof Interview")
    yield connection
    connection.close()


def _segment(conn, sid: str, text: str, participants: list[str] | None = None) -> None:
    create_segment(
        conn,
        segment_id=sid,
        interview_id="i1",
        content=text,
        participant_ids=participants or ["p2"],
    )


def _open(conn, *segment_ids: str) -> str:
    return proofreading.open_session(
        conn, interview_id="i1", segment_ids=list(segment_ids)
    )["id"]


def _edit(sid: str, start: int, end: int, replacement: str) -> dict:
    return {"segment_id": sid, "start": start, "end": end, "replacement": replacement}


def _submit(conn, session_id, submission_id, base_version, edits):
    return proofreading.submit_proposal(
        conn,
        session_id,
        submission_id=submission_id,
        base_version=base_version,
        edits=edits,
    )


def _text(conn, session_id, segment_id) -> str:
    detail = proofreading.session_detail(conn, session_id)
    return next(s["text"] for s in detail["segments"] if s["segment_id"] == segment_id)


# ---------------------------------------------------------------------------
# Selection and one-open-session rule
# ---------------------------------------------------------------------------


def test_one_open_session_per_interview_and_visible_segments_only(conn) -> None:
    _segment(conn, "sg-a", "alpha")
    _segment(conn, "sg-b", "bravo")
    session_id = _open(conn, "sg-a", "sg-b")
    assert proofreading.session_detail(conn, session_id)["current_version"] == 1

    with pytest.raises(ApiError) as exc:
        _open(conn, "sg-a")
    assert exc.value.code == "proofreading_session_exists"

    # After publishing, a new session may be opened; it snapshots the new body.
    _submit(conn, session_id, "m0", 1, [_edit("sg-a", 0, 5, "ALPHA")])
    proofreading.publish(
        conn, session_id, version=proofreading.session_detail(conn, session_id)["current_version"]
    )
    second = _open(conn, "sg-a")
    assert _text(conn, second, "sg-a") == "ALPHA"


def test_invisible_segment_cannot_be_selected(conn) -> None:
    _segment(conn, "sg-hidden", "secret", participants=["p1"])
    create_deletion_request(conn, "p1")
    with pytest.raises(ApiError) as exc:
        _open(conn, "sg-hidden")
    assert exc.value.code == "proofreading_segment_not_selectable"

    _segment(conn, "sg-other", "other")
    create_interview(conn, interview_id="i2", title="Other")
    create_segment(
        conn,
        segment_id="sg-x",
        interview_id="i2",
        content="x",
        participant_ids=["p2"],
    )
    with pytest.raises(ApiError) as exc:
        _open(conn, "sg-x")
    assert exc.value.code == "proofreading_segment_not_selectable"


# ---------------------------------------------------------------------------
# Merge semantics
# ---------------------------------------------------------------------------


def test_insert_delete_and_unicode_code_points(conn) -> None:
    _segment(conn, "sg", "a😀b")
    session_id = _open(conn, "sg")
    # Emoji is one Unicode code point, so b starts at index 2.
    _submit(conn, session_id, "m1", 1, [_edit("sg", 1, 2, "X"), _edit("sg", 2, 2, "Y")])
    assert _text(conn, session_id, "sg") == "aXYb"
    _submit(conn, session_id, "m2", 2, [_edit("sg", 1, 3, "")])
    assert _text(conn, session_id, "sg") == "ab"


def test_empty_replacement_deletes_and_insertion_is_empty_range(conn) -> None:
    _segment(conn, "sg", "0123456789")
    session_id = _open(conn, "sg")
    _submit(conn, session_id, "m1", 1, [_edit("sg", 2, 5, "")])
    assert _text(conn, session_id, "sg") == "0156789"
    _submit(conn, session_id, "m2", 2, [_edit("sg", 0, 0, "AB"), _edit("sg", 7, 7, "Z")])
    assert _text(conn, session_id, "sg") == "AB0156789Z"


def test_out_of_range_rejected(conn) -> None:
    _segment(conn, "sg", "abc")
    session_id = _open(conn, "sg")
    with pytest.raises(ApiError) as exc:
        _submit(conn, session_id, "bad", 1, [_edit("sg", 2, 5, "x")])
    assert exc.value.code == "proofreading_range_invalid"
    with pytest.raises(ApiError) as exc:
        _submit(conn, session_id, "bad2", 1, [_edit("sg", 2, 1, "x")])
    assert exc.value.code == "proofreading_range_invalid"


def test_conflicts_inside_one_proposal_reject_whole_proposal(conn) -> None:
    _segment(conn, "sg-a", "abcdef")
    _segment(conn, "sg-b", "ghijkl")
    session_id = _open(conn, "sg-a", "sg-b")
    # Valid edit on sg-b together with a conflicting pair on sg-a: nothing merges.
    with pytest.raises(ApiError) as exc:
        _submit(
            conn,
            session_id,
            "m-conf",
            1,
            [
                _edit("sg-a", 1, 4, "X"),
                _edit("sg-a", 2, 3, "Y"),
                _edit("sg-b", 0, 1, "G"),
            ],
        )
    assert exc.value.code == "proofreading_merge_conflict"
    assert exc.value.extra["segments"] == ["sg-a"]
    detail = proofreading.session_detail(conn, session_id)
    assert detail["current_version"] == 1
    assert _text(conn, session_id, "sg-b") == "ghijkl"


@pytest.mark.parametrize(
    "first, second",
    [
        ((1, 4, "X"), (4, 4, "Y")),  # insertion at range end endpoint
        ((1, 4, "X"), (1, 1, "Y")),  # insertion at range start endpoint
        ((1, 4, "X"), (0, 1, "Y")),  # adjacent non-empty ranges
    ],
)
def test_endpoint_touching_is_not_a_conflict(conn, first, second) -> None:
    _segment(conn, "sg", "abcdef")
    session_id = _open(conn, "sg")
    _submit(conn, session_id, "m1", 1, [_edit("sg", *first)])
    # Still based on version 1 and only touching an endpoint: must merge.
    result = _submit(conn, session_id, "m2", 1, [_edit("sg", *second)])
    assert result["status"] == "merged"
    assert result["rebased"] is True
    assert proofreading.session_detail(conn, session_id)["current_version"] == 3


def test_insertion_inside_other_range_and_same_point_conflict(conn) -> None:
    _segment(conn, "sg", "abcdef")
    session_id = _open(conn, "sg")
    _submit(conn, session_id, "m1", 1, [_edit("sg", 2, 5, "X")])
    with pytest.raises(ApiError) as exc:
        _submit(conn, session_id, "m2", 1, [_edit("sg", 3, 3, "Y")])
    assert exc.value.code == "proofreading_merge_conflict"
    assert exc.value.extra["segments"] == ["sg"]

    # Insertions at the same point conflict.
    _submit(conn, session_id, "m3", 2, [_edit("sg", 0, 0, "A")])
    with pytest.raises(ApiError) as exc:
        _submit(conn, session_id, "m4", 2, [_edit("sg", 0, 0, "B")])
    assert exc.value.code == "proofreading_merge_conflict"


def test_insertion_at_replacement_endpoint_stays_outside(conn) -> None:
    _segment(conn, "sg", "abcdef")
    session_id = _open(conn, "sg")
    _submit(conn, session_id, "m1", 1, [_edit("sg", 1, 4, "XYZ")])
    # Insert at the start endpoint of the replaced range from the old version.
    _submit(conn, session_id, "m2", 1, [_edit("sg", 1, 1, "<")])
    assert _text(conn, session_id, "sg") == "a<XYZef"
    # Insert at the end endpoint of the same range from the old version.
    _submit(conn, session_id, "m3", 1, [_edit("sg", 4, 4, ">")])
    assert _text(conn, session_id, "sg") == "a<XYZ>ef"


def test_stale_non_conflicting_proposal_rebases_into_current_draft(conn) -> None:
    _segment(conn, "sg", "hello world")
    session_id = _open(conn, "sg")
    _submit(conn, session_id, "m1", 1, [_edit("sg", 0, 5, "HELLO")])
    # Still based on version 1 but touches a disjoint range.
    result = _submit(conn, session_id, "m2", 1, [_edit("sg", 6, 11, "there")])
    assert result["rebased"] is True
    assert result["segments"][0]["text"] == "HELLO there"
    # Both changes survive; nothing was overwritten.
    assert _text(conn, session_id, "sg") == "HELLO there"


def test_stale_conflicting_proposal_rejects_whole_proposal(conn) -> None:
    _segment(conn, "sg-a", "hello world")
    _segment(conn, "sg-b", "second text")
    session_id = _open(conn, "sg-a", "sg-b")
    _submit(conn, session_id, "m1", 1, [_edit("sg-a", 6, 11, "there")])
    with pytest.raises(ApiError) as exc:
        _submit(
            conn,
            session_id,
            "m2",
            1,
            [_edit("sg-a", 8, 9, "X"), _edit("sg-b", 0, 6, "FIRST")],
        )
    assert exc.value.code == "proofreading_merge_conflict"
    assert exc.value.extra["segments"] == ["sg-a"]
    # sg-b untouched because the whole proposal was rejected.
    assert _text(conn, session_id, "sg-b") == "second text"
    assert proofreading.session_detail(conn, session_id)["current_version"] == 2


def test_reverting_text_does_not_erase_historical_conflict(conn) -> None:
    _segment(conn, "sg", "abcdef")
    session_id = _open(conn, "sg")
    _submit(conn, session_id, "m1", 1, [_edit("sg", 1, 2, "X")])  # v2
    _submit(conn, session_id, "m2", 2, [_edit("sg", 1, 2, "b")])  # v3 reverts text
    assert _text(conn, session_id, "sg") == "abcdef"
    # A proposal still based on v1 touching the same range must conflict, even
    # though the visible text is identical again.
    with pytest.raises(ApiError) as exc:
        _submit(conn, session_id, "m3", 1, [_edit("sg", 1, 2, "Y")])
    assert exc.value.code == "proofreading_merge_conflict"


def test_submission_id_idempotency(conn) -> None:
    _segment(conn, "sg", "abcdef")
    session_id = _open(conn, "sg")
    edits = [_edit("sg", 0, 1, "A")]
    first = _submit(conn, session_id, "sub-1", 1, edits)
    assert first["idempotent_replay"] is False
    version_after = proofreading.session_detail(conn, session_id)["current_version"]

    replay = _submit(conn, session_id, "sub-1", 1, edits)
    assert replay["idempotent_replay"] is True
    assert replay["merged_version"] == first["merged_version"]
    # No further version was created.
    assert proofreading.session_detail(conn, session_id)["current_version"] == version_after

    with pytest.raises(ApiError) as exc:
        _submit(conn, session_id, "sub-1", 1, [_edit("sg", 0, 1, "B")])
    assert exc.value.code == "submission_id_reused"


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------


def test_annotation_shifts_when_edit_avoids_text(conn) -> None:
    _segment(conn, "sg", "abcdefghij")
    session_id = _open(conn, "sg")
    # "def" is indices 3..6 in "abcdefghij".
    ann = proofreading.create_annotation(
        conn, session_id, segment_id="sg", start=3, end=6, text="see def"
    )
    _submit(conn, session_id, "m1", 1, [_edit("sg", 0, 2, "XY")])
    _submit(conn, session_id, "m2", 2, [_edit("sg", 10, 10, "!!")])
    detail = proofreading.session_detail(conn, session_id)
    current = detail["segments"][0]
    assert current["text"] == "XYcdefghij!!"
    anchor = next(a for a in current["annotations"] if a["id"] == ann["id"])
    assert anchor["anchored"] is True
    assert current["text"][anchor["start"] : anchor["end"]] == "def"
    assert anchor["start"] == 3 and anchor["end"] == 6


@pytest.mark.parametrize(
    "start, end, replacement",
    [
        (4, 5, "X"),  # replaces part of the annotated text
        (5, 6, ""),  # deletes part of the annotated text
        (6, 6, "Z"),  # inserts strictly inside the annotated text
    ],
)
def test_annotation_lost_on_replace_delete_and_inner_insert(
    conn, start, end, replacement
) -> None:
    # The annotated text is "efgh" = indices 4..8.
    _segment(conn, "sg", "abcdefghij")
    session_id = _open(conn, "sg")
    ann = proofreading.create_annotation(
        conn, session_id, segment_id="sg", start=4, end=8, text="see efgh"
    )
    _submit(conn, session_id, "m1", 1, [_edit("sg", start, end, replacement)])
    anchor = next(
        a
        for a in proofreading.session_detail(conn, session_id)["segments"][0]["annotations"]
        if a["id"] == ann["id"]
    )
    assert anchor["anchored"] is False, (start, end, replacement)
    assert anchor["anchor_status"] == "lost"
    assert anchor["start"] is None and anchor["end"] is None


def test_annotation_survives_boundary_insertions(conn) -> None:
    _segment(conn, "sg", "abcdefghij")
    session_id = _open(conn, "sg")
    ann = proofreading.create_annotation(
        conn, session_id, segment_id="sg", start=3, end=6, text="def"
    )
    # Insert exactly at both endpoints of the annotated text.
    _submit(conn, session_id, "m1", 1, [_edit("sg", 3, 3, "["), _edit("sg", 6, 6, "]")])
    current = proofreading.session_detail(conn, session_id)["segments"][0]
    anchor = next(a for a in current["annotations"] if a["id"] == ann["id"])
    assert anchor["anchored"] is True
    assert current["text"] == "abc[def]ghij"
    assert current["text"][anchor["start"] : anchor["end"]] == "def"


def test_lost_annotation_can_be_reanchored_or_resolved_and_blocks_publish(conn) -> None:
    _segment(conn, "sg", "abcdefghij")
    session_id = _open(conn, "sg")
    ann = proofreading.create_annotation(
        conn, session_id, segment_id="sg", start=2, end=5, text="cde"
    )
    _submit(conn, session_id, "m1", 1, [_edit("sg", 2, 5, "XYZ")])
    detail = proofreading.session_detail(conn, session_id)
    version = detail["current_version"]
    assert detail["unresolved_annotation_count"] == 1
    assert detail["can_publish"] is False
    with pytest.raises(ApiError) as exc:
        proofreading.publish(conn, session_id, version=version)
    assert exc.value.code == "proofreading_unresolved_annotations"
    # Unresolved annotations do not block further merges.
    _submit(conn, session_id, "m2", version, [_edit("sg", 0, 1, "Q")])
    version = proofreading.session_detail(conn, session_id)["current_version"]

    reanchored = proofreading.reanchor_annotation(
        conn, session_id, ann["id"], start=0, end=3
    )
    assert reanchored["anchored"] is True and reanchored["start"] == 0
    proofreading.resolve_annotation(conn, session_id, ann["id"])
    detail = proofreading.session_detail(conn, session_id)
    assert detail["can_publish"] is True
    proofreading.publish(conn, session_id, version=version)


def test_resolved_annotation_does_not_block_and_lost_annotation_status(conn) -> None:
    _segment(conn, "sg", "abcdef")
    session_id = _open(conn, "sg")
    ann = proofreading.create_annotation(
        conn, session_id, segment_id="sg", start=1, end=3, text="bc"
    )
    proofreading.resolve_annotation(conn, session_id, ann["id"])
    _submit(conn, session_id, "m1", 1, [_edit("sg", 1, 3, "ZZ")])
    detail = proofreading.session_detail(conn, session_id)
    anchor = detail["segments"][0]["annotations"][0]
    # Resolved annotations are not re-anchored; they stay resolved.
    assert anchor["resolved"] is True
    assert detail["unresolved_annotation_count"] == 0
    proofreading.publish(conn, session_id, version=detail["current_version"])


# ---------------------------------------------------------------------------
# Publish and ordinary entries
# ---------------------------------------------------------------------------


def test_publish_requires_current_version_and_updates_all_segments(conn) -> None:
    _segment(conn, "sg-a", "alpha")
    _segment(conn, "sg-b", "bravo")
    session_id = _open(conn, "sg-a", "sg-b")
    _submit(
        conn,
        session_id,
        "m1",
        1,
        [_edit("sg-a", 0, 5, "ALPHA"), _edit("sg-b", 0, 5, "BRAVO")],
    )
    version = proofreading.session_detail(conn, session_id)["current_version"]
    with pytest.raises(ApiError) as exc:
        proofreading.publish(conn, session_id, version=version - 1)
    assert exc.value.code == "proofreading_version_mismatch"

    # Ordinary entries still show official bodies before publish.
    assert get_segment_normal(conn, "sg-a")["content"] == "alpha"
    assert get_interview_normal(conn, "i1")["segments"][0]["content"] == "alpha"

    result = proofreading.publish(conn, session_id, version=version)
    assert result["status"] == "published"
    assert set(result["published_segment_ids"]) == {"sg-a", "sg-b"}
    interview = get_interview_normal(conn, "i1")
    assert {s["id"]: s["content"] for s in interview["segments"]} == {
        "sg-a": "ALPHA",
        "sg-b": "BRAVO",
    }
    assert len(search_segments(conn, "ALPHA")) == 1

    with pytest.raises(ApiError) as exc:
        _submit(conn, session_id, "m2", version, [_edit("sg-a", 0, 5, "X")])
    assert exc.value.code == "proofreading_session_closed"


# ---------------------------------------------------------------------------
# Deletion interaction
# ---------------------------------------------------------------------------


def test_purged_segment_leaves_scope_and_text_is_removed(conn) -> None:
    _segment(conn, "sg-doomed", "abcdefghij", participants=["p1", "p2"])
    _segment(conn, "sg-kept", "keepersegment", participants=["p2"])
    session_id = _open(conn, "sg-doomed", "sg-kept")
    proofreading.create_annotation(
        conn, session_id, segment_id="sg-doomed", start=0, end=3, text="doomed note"
    )
    _submit(
        conn,
        session_id,
        "m1",
        1,
        [
            _edit("sg-doomed", 0, 1, "SECRET_DRAFT_MARKER"),
            _edit("sg-kept", 0, 5, "KEPT"),
        ],
    )
    create_deletion_request(conn, "p1")

    detail = proofreading.session_detail(conn, session_id)
    assert detail["publishable_segment_ids"] == ["sg-kept"]
    assert detail["unresolved_annotation_count"] == 0
    # The purged segment's annotation no longer blocks publication.
    assert detail["can_publish"] is True

    with pytest.raises(ApiError) as exc:
        _submit(
            conn,
            session_id,
            "m2",
            detail["current_version"],
            [_edit("sg-doomed", 0, 1, "X")],
        )
    assert exc.value.code == "proofreading_segment_out_of_scope"

    version = detail["current_version"]
    _submit(conn, session_id, "m3", version, [_edit("sg-kept", 0, 4, "KEPT")])
    result = proofreading.publish(
        conn, session_id, version=proofreading.session_detail(conn, session_id)["current_version"]
    )
    assert result["published_segment_ids"] == ["sg-kept"]
    assert get_segment_normal(conn, "sg-kept")["content"].startswith("KEPT")

    # No draft history, replacement text or annotation text remains for the
    # physically purged segment.
    for table in ("proofreading_segment_contents", "proofreading_edits", "proofreading_annotations"):
        count = conn.execute(
            f"SELECT COUNT(*) AS c FROM {table} WHERE segment_id = ?", ("sg-doomed",)
        ).fetchone()["c"]
        assert count == 0, table
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM segments WHERE id = 'sg-doomed'"
    ).fetchone()["c"] == 0


def test_retained_segment_remains_out_of_scope_but_body_is_kept(conn) -> None:
    _segment(conn, "sg-retained", "retain me", participants=["p1", "p2"])
    create_retention(conn, "sg-retained", reason="legal hold", created_by="owner")
    session_id = _open(conn, "sg-retained")
    create_deletion_request(conn, "p1")
    # Retention keeps the official body, but the deletion request hides it from
    # ordinary/proofreading scope.
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM segments WHERE id = 'sg-retained'"
    ).fetchone()["c"] == 1
    detail = proofreading.session_detail(conn, session_id)
    assert detail["segments"] == []
    assert detail["can_publish"] is False
    with pytest.raises(ApiError) as exc:
        proofreading.publish(conn, session_id, version=detail["current_version"])
    assert exc.value.code == "proofreading_no_publishable_segments"


# ---------------------------------------------------------------------------
# Concurrency and restart
# ---------------------------------------------------------------------------


def test_parallel_non_conflicting_submissions_all_merge(tmp_path: Path) -> None:
    path = tmp_path / "parallel.db"
    setup = connect(path)
    init_db(setup)
    create_participant(
        setup, participant_id="p1", display_name="One", profile={}, session_token="s1"
    )
    create_interview(setup, interview_id="i1", title="Parallel")
    base = "0123456789ABCDEFGHIJ"
    create_segment(
        setup,
        segment_id="sg",
        interview_id="i1",
        content=base,
        participant_ids=["p1"],
    )
    session_id = proofreading.open_session(
        setup, interview_id="i1", segment_ids=["sg"]
    )["id"]
    setup.close()

    # Each thread replaces one distinct character, all against version 1 of
    # the same segment.  They must all merge, preserving every change.
    threads: list[threading.Thread] = []
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        thread_conn = connect(path)
        try:
            proofreading.submit_proposal(
                thread_conn,
                session_id,
                submission_id=f"sub-{index}",
                base_version=1,
                edits=[
                    {
                        "segment_id": "sg",
                        "start": index,
                        "end": index + 1,
                        "replacement": chr(ord("a") + index),
                    }
                ],
            )
        except BaseException as exc:  # noqa: BLE001 - collected for assertion
            errors.append(exc)
        finally:
            thread_conn.close()

    for i in range(len(base)):
        thread = threading.Thread(target=worker, args=(i,))
        threads.append(thread)
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert all(not t.is_alive() for t in threads)
    assert errors == []

    verify = connect(path)
    try:
        detail = proofreading.session_detail(verify, session_id)
        assert detail["current_version"] == len(base) + 1
        assert detail["segments"][0]["text"] == "abcdefghijklmnopqrst"
        # Publishing once emits the fully merged draft.
        proofreading.publish(verify, session_id, version=detail["current_version"])
        assert get_segment_normal(verify, "sg")["content"] == "abcdefghijklmnopqrst"
    finally:
        verify.close()


def test_state_survives_reconnect_and_startup_recovery(tmp_path: Path) -> None:
    path = tmp_path / "restart.db"
    conn = connect(path)
    init_db(conn)
    create_participant(
        conn, participant_id="p1", display_name="One", profile={}, session_token="s1"
    )
    create_interview(conn, interview_id="i1", title="Restart")
    create_segment(
        conn,
        segment_id="sg",
        interview_id="i1",
        content="original body",
        participant_ids=["p1"],
    )
    session_id = proofreading.open_session(conn, interview_id="i1", segment_ids=["sg"])["id"]
    proofreading.create_annotation(
        conn, session_id, segment_id="sg", start=0, end=8, text="note"
    )
    proofreading.submit_proposal(
        conn,
        session_id,
        submission_id="m1",
        base_version=1,
        edits=[_edit("sg", 0, 13, "draft body")],
    )
    conn.close()

    # Simulate a service restart: new connection, schema init and recovery.
    from app.services import recover_unfinished

    conn = connect(path)
    try:
        init_db(conn)
        recover_unfinished(conn)
        detail = proofreading.session_detail(conn, session_id)
        assert detail["current_version"] == 2
        assert detail["segments"][0]["text"] == "draft body"
        assert detail["unresolved_annotation_count"] == 1
        # The annotation's 0..8 range was replaced: it lost its anchor.
        assert detail["segments"][0]["annotations"][0]["anchored"] is False
        # Idempotent replay still works after restart.
        replay = proofreading.submit_proposal(
            conn,
            session_id,
            submission_id="m1",
            base_version=1,
            edits=[_edit("sg", 0, 13, "draft body")],
        )
        assert replay["idempotent_replay"] is True
        proofreading.resolve_annotation(
            conn, session_id, detail["segments"][0]["annotations"][0]["id"]
        )
        proofreading.publish(conn, session_id, version=2)
        assert get_segment_normal(conn, "sg")["content"] == "draft body"
    finally:
        conn.close()


def test_concurrent_reads_never_observe_half_published_set(tmp_path: Path) -> None:
    """Publication of many segments is one transaction for ordinary readers."""
    path = tmp_path / "atomic.db"
    setup = connect(path)
    init_db(setup)
    create_interview(setup, interview_id="i1", title="Atomic")
    create_participant(
        setup, participant_id="p-reader", display_name="Reader", profile={}, session_token="s-r"
    )
    segment_ids = [f"sg-{i}" for i in range(12)]
    for i, sid in enumerate(segment_ids):
        create_segment(
            setup,
            segment_id=sid,
            interview_id="i1",
            content=f"OLD-{i}",
            participant_ids=["p-reader"],
            ordinal=i + 1,
        )
    session_id = proofreading.open_session(
        setup, interview_id="i1", segment_ids=segment_ids
    )["id"]
    edits = [_edit(sid, 0, 3, "NEW") for sid in segment_ids]
    proofreading.submit_proposal(
        setup, session_id, submission_id="all", base_version=1, edits=edits
    )
    version = proofreading.session_detail(setup, session_id)["current_version"]
    setup.close()

    stop = threading.Event()
    observed: list[tuple[str, ...]] = []
    errors: list[BaseException] = []

    def reader() -> None:
        reader_conn = connect(path)
        try:
            while not stop.is_set():
                interview = get_interview_normal(reader_conn, "i1")
                observed.append(
                    tuple(
                        s["content"].split("-", 1)[0] for s in interview["segments"]
                    )
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            reader_conn.close()

    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    writer = connect(path)
    try:
        proofreading.publish(writer, session_id, version=version)
    finally:
        writer.close()
        stop.set()
    reader_thread.join(timeout=30)
    assert not reader_thread.is_alive()
    assert errors == []
    assert observed, "reader observed nothing"
    # Every read is entirely old or entirely new -- never a partial mix.
    assert set(observed) <= {("OLD",) * len(segment_ids), ("NEW",) * len(segment_ids)}, set(observed)


def test_annotation_on_retained_segment_does_not_block_other_segments(conn) -> None:
    _segment(conn, "sg-hold", "held body", participants=["p1", "p2"])
    _segment(conn, "sg-free", "free body", participants=["p2"])
    session_id = _open(conn, "sg-hold", "sg-free")
    proofreading.create_annotation(
        conn, session_id, segment_id="sg-hold", start=0, end=4, text="on held body"
    )
    create_retention(conn, "sg-hold", reason="legal hold", created_by="owner")
    create_deletion_request(conn, "p1")

    detail = proofreading.session_detail(conn, session_id)
    # The retained segment is out of scope; its body stays only in the
    # restricted retention entry, and its annotation cannot block publication
    # of the remaining visible segments.
    assert detail["publishable_segment_ids"] == ["sg-free"]
    assert detail["unresolved_annotation_count"] == 0
    assert detail["can_publish"] is True
    version = detail["current_version"]
    _submit(conn, session_id, "m1", version, [_edit("sg-free", 0, 4, "FREE")])
    result = proofreading.publish(
        conn, session_id, version=proofreading.session_detail(conn, session_id)["current_version"]
    )
    assert result["published_segment_ids"] == ["sg-free"]
    # Retention text is untouched and still only available through retention.
    assert conn.execute(
        "SELECT content FROM segments WHERE id = 'sg-hold'"
    ).fetchone()["content"] == "held body"
    # Still hidden from ordinary reads despite the retained body.
    with pytest.raises(ApiError):
        get_segment_normal(conn, "sg-hold")
