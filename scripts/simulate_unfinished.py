#!/usr/bin/env python3
"""Commit a deletion request without its purge pass, simulating a crash.

The request/profile tombstone and deletion_items are committed first.  This
helper deliberately exits before process_deletion_request(), so the segment is
durable but only hidden_pending_purge, not purged.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import connect, get_db_path, init_db  # noqa: E402
from app.services import create_deletion_request  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("participant_id", help="Existing participant, e.g. p-dave")
    parser.add_argument("--db", help="Optional SQLite DB path; defaults to PDC_DB_PATH or data/deletion_demo.db")
    args = parser.parse_args()

    if args.db:
        import os

        os.environ["PDC_DB_PATH"] = args.db

    conn = connect(get_db_path())
    try:
        init_db(conn)
        detail, created = create_deletion_request(conn, args.participant_id, process_now=False)
    finally:
        conn.close()

    print(f"database={get_db_path()}")
    print(f"request_id={detail['id']}")
    print(f"created={created}")
    print(f"task_status={detail['task_status']}")
    print(f"summary={detail['summary']}")
    print("The transaction was left before purge processing. Restart FastAPI to run recovery.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
