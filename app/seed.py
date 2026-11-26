"""Seed explicit local demo data.

Run:
    python -m app.seed --reset
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .db import connect, get_db_path, init_db
from . import services


MARKER_RETAINED = "MARKER_RETAINED_BODY_7f3a: 我们对上线时间有争议，这段需要保留原文。"
MARKER_PURGED = "MARKER_PURGED_BODY_2c91: Alice 和 Carol 讨论了一次性清除内容。"
MARKER_VISIBLE = "MARKER_VISIBLE_BODY_48bd: Bob 和 Dave 讨论下一轮访谈安排。"
MARKER_CRASH = "MARKER_CRASH_PENDING_61ae: Dave 的片段用于模拟提交后、清除前中断。"


def seed(reset: bool = False) -> Path:
    path = get_db_path()
    if reset:
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(path) + suffix)
            if p.exists():
                p.unlink()

    conn = connect(path)
    try:
        init_db(conn)

        # Use explicit IDs so README/curl and demo verification are stable.
        participants = [
            ("p-alice", "Alice 示例", {"role": "interviewee", "email": "alice@example.invalid"}, "session-alice"),
            ("p-bob", "Bob 示例", {"role": "interviewee", "email": "bob@example.invalid"}, "session-bob"),
            ("p-carol", "Carol 示例", {"role": "interviewee", "email": "carol@example.invalid"}, "session-carol"),
            ("p-dave", "Dave 示例", {"role": "interviewee", "email": "dave@example.invalid"}, "session-dave"),
        ]
        for pid, name, profile, token in participants:
            if not conn.execute("SELECT 1 FROM participants WHERE id=?", (pid,)).fetchone():
                services.create_participant(
                    conn,
                    participant_id=pid,
                    display_name=name,
                    profile=profile,
                    session_token=token,
                )

        if not conn.execute("SELECT 1 FROM interviews WHERE id='int-alpha'").fetchone():
            services.create_interview(conn, interview_id="int-alpha", title="Alpha 用户访谈")
        if not conn.execute("SELECT 1 FROM interviews WHERE id='int-beta'").fetchone():
            services.create_interview(conn, interview_id="int-beta", title="Beta 可用性访谈")

        segments = [
            (
                "seg-retained",
                "int-alpha",
                1,
                MARKER_RETAINED,
                ["p-alice", "p-bob"],
            ),
            (
                "seg-purged",
                "int-alpha",
                2,
                MARKER_PURGED,
                ["p-alice", "p-carol"],
            ),
            (
                "seg-visible",
                "int-alpha",
                3,
                MARKER_VISIBLE,
                ["p-bob", "p-dave"],
            ),
            (
                "seg-crash-dave",
                "int-beta",
                1,
                MARKER_CRASH,
                ["p-dave"],
            ),
        ]
        for sid, iid, ordinal, content, pids in segments:
            if not conn.execute("SELECT 1 FROM segments WHERE id=?", (sid,)).fetchone():
                services.create_segment(
                    conn,
                    segment_id=sid,
                    interview_id=iid,
                    ordinal=ordinal,
                    content=content,
                    participant_ids=pids,
                )

        if not conn.execute("SELECT 1 FROM retentions WHERE id='ret-demo-retained'").fetchone():
            services.create_retention(
                conn,
                "seg-retained",
                reason="项目负责人：该片段涉及未解决的产品决策争议，需保留原文供复核。",
                created_by="owner-demo",
            )
            # Give the demo retention a readable fixed ID. create_retention
            # generates an ID, so replace it while no deletion references it.
            row = conn.execute(
                "SELECT id FROM retentions WHERE segment_id='seg-retained' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row and row["id"] != "ret-demo-retained":
                conn.execute("UPDATE retentions SET id=? WHERE id=?", ("ret-demo-retained", row["id"]))

        if not conn.execute("SELECT 1 FROM export_requests WHERE id='exp-demo-before'").fetchone():
            services.create_export_request(
                conn,
                export_id="exp-demo-before",
                scope="all",
                interview_id=None,
                export_format="json",
            )
        conn.commit()
    finally:
        conn.close()
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed explicit demo data")
    parser.add_argument("--reset", action="store_true", help="Delete the SQLite database before seeding")
    parser.add_argument("--db", help="Override PDC_DB_PATH for this command")
    args = parser.parse_args()
    if args.db:
        os.environ["PDC_DB_PATH"] = args.db
    path = seed(reset=args.reset)
    print(f"Seeded demo database at {path}")
    print("Owner token: demo-owner-token")


if __name__ == "__main__":
    main()
