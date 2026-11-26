"""FastAPI entry point for the verifiable personal-data deletion service."""

from __future__ import annotations

import hmac
import os
import sqlite3
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.responses import JSONResponse

from . import services
from .db import connect, init_db
from .schemas import (
    ExportCreate,
    InterviewCreate,
    ParticipantCreate,
    ProfileUpdate,
    RetentionCreate,
    RetentionRelease,
    SegmentCreate,
)


def owner_token_expected() -> str:
    return os.environ.get("PDC_OWNER_TOKEN", "demo-owner-token")


def require_owner(
    x_owner_token: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    expected = owner_token_expected()
    token = x_owner_token
    if not token and authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            token = value.strip()
    if not token or not hmac.compare_digest(token, expected):
        raise services.ApiError(403, "owner_token_required", "A valid X-Owner-Token or Bearer token is required for this restricted entry")
    return token


def get_db():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


Db = Annotated[object, Depends(get_db)]
Owner = Annotated[str, Depends(require_owner)]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Restart recovery runs before the API starts serving.  It does not mark
    # hidden rows purged; it physically purges only segments with no active
    # retention and then records the same committed transition.
    conn = connect()
    try:
        init_db(conn)
        app.state.startup_recovery = services.recover_unfinished(conn)
        # At restart there is no traffic yet; flush/truncate WAL frames so
        # already-committed purges are not left in auxiliary WAL storage.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    yield


app = FastAPI(
    title="Interview Collaboration - Verifiable Deletion Service",
    version="1.0.0",
    description=(
        "Participants request deletion; hidden shared segments disappear from normal "
        "reads/search/exports; project owners may place reason-backed retentions."
    ),
    lifespan=lifespan,
)


@app.exception_handler(services.ApiError)
async def api_error_handler(_request: Request, exc: services.ApiError):
    content: dict[str, object] = {
        "error": {
            "code": exc.code,
            "message": exc.message,
        }
    }
    if exc.extra:
        content["error"]["details"] = exc.extra
    return JSONResponse(status_code=exc.status_code, content=content)


@app.get("/health")
def health():
    return {"status": "ok", "service": "verifiable-deletion", "owner_header": "X-Owner-Token"}


# ---------------------------------------------------------------------------
# Participants and profiles
# ---------------------------------------------------------------------------


@app.post("/participants", status_code=201)
def create_participant(body: ParticipantCreate, db=Depends(get_db)):
    return services.create_participant(
        db,
        participant_id=body.id,
        display_name=body.display_name,
        profile=body.profile,
        session_token=body.session_token,
    )


@app.get("/participants")
def list_participants(db=Depends(get_db)):
    return {"participants": services.list_participants(db)}


@app.get("/participants/{participant_id}")
def get_participant(participant_id: str, db=Depends(get_db)):
    return services.get_participant_normal(db, participant_id)


@app.api_route(
    "/participants/{participant_id}/profile",
    methods=["PATCH", "PUT", "POST"],
)
def update_participant_profile(participant_id: str, body: ProfileUpdate, db=Depends(get_db)):
    return services.update_profile(
        db, participant_id, display_name=body.display_name, profile=body.profile
    )


@app.delete("/sessions/{session_token}")
def revoke_session(session_token: str, db=Depends(get_db)):
    # Session revocation/logout never revokes a committed deletion request.
    return services.revoke_session(db, session_token)


@app.delete("/participants/{participant_id}/session")
def revoke_participant_session(participant_id: str, db=Depends(get_db)):
    return services.revoke_participant_session(db, participant_id)


# ---------------------------------------------------------------------------
# Interviews, segments and ordinary search/read entries
# ---------------------------------------------------------------------------


@app.post("/interviews", status_code=201)
def create_interview(body: InterviewCreate, db=Depends(get_db)):
    return services.create_interview(db, interview_id=body.id, title=body.title)


@app.get("/interviews")
def list_interviews(db=Depends(get_db)):
    return {"interviews": services.list_interviews(db)}


@app.get("/interviews/{interview_id}")
def get_interview(interview_id: str, db=Depends(get_db)):
    return services.get_interview_normal(db, interview_id)


@app.post("/segments", status_code=201)
def create_segment(body: SegmentCreate, db=Depends(get_db)):
    return services.create_segment(
        db,
        segment_id=body.id,
        interview_id=body.interview_id,
        ordinal=body.ordinal,
        content=body.content,
        participant_ids=body.participant_ids,
    )


@app.get("/segments/{segment_id}")
def get_segment(segment_id: str, db=Depends(get_db)):
    return services.get_segment_normal(db, segment_id)


@app.get("/search")
def search(q: str = Query(min_length=1), limit: int = Query(default=50, ge=1, le=200), db=Depends(get_db)):
    results = services.search_segments(db, q, limit=limit)
    return {"query": q, "count": len(results), "segments": results}


@app.get("/search/sessions")
def search_sessions_alias(
    q: str = Query(min_length=1),
    limit: int = Query(default=50, ge=1, le=200),
    db=Depends(get_db),
):
    """Alias for conversation/session search used by the interview product."""
    results = services.search_segments(db, q, limit=limit)
    return {"query": q, "count": len(results), "segments": results}


# ---------------------------------------------------------------------------
# Deletion preview, execution and verifiable status queries
# ---------------------------------------------------------------------------


@app.get("/participants/{participant_id}/deletion-preview")
def preview_deletion(participant_id: str, db=Depends(get_db)):
    return services.preview_deletion(db, participant_id)


@app.post("/participants/{participant_id}/deletion-requests", status_code=201)
def request_deletion(
    participant_id: str,
    db=Depends(get_db),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    try:
        detail, created = services.create_deletion_request(
            db, participant_id, idempotency_key=idempotency_key
        )
    except sqlite3.IntegrityError:
        # Concurrent retries can race the participant unique constraint.  The
        # winner created the single task; return that same durable request.
        detail = services.get_participant_deletion_request(db, participant_id)
        created = False
    return JSONResponse(status_code=201 if created else 200, content={"created": created, "request": detail})


@app.get("/participants/{participant_id}/deletion-request")
def get_participant_deletion_request(participant_id: str, db=Depends(get_db)):
    return services.get_participant_deletion_request(db, participant_id)


@app.get("/deletion-requests")
def list_deletion_requests(db=Depends(get_db)):
    return {"deletion_requests": services.list_deletion_requests(db)}


@app.get("/deletion-requests/{request_id}")
def get_deletion_request(request_id: str, db=Depends(get_db)):
    return services.get_deletion_request(db, request_id)


# ---------------------------------------------------------------------------
# Ordinary export.  The export request stores scope only; content is built at
# response time against current visibility state.
# ---------------------------------------------------------------------------


@app.post("/exports", status_code=201)
def create_export(body: ExportCreate, db=Depends(get_db)):
    return services.create_export_request(
        db,
        export_id=body.id,
        scope=body.scope,
        interview_id=body.interview_id,
        export_format=body.format,
    )


@app.get("/exports/{export_id}")
def get_export(export_id: str, db=Depends(get_db)):
    row = services._require_export(db, export_id)  # noqa: SLF001 - thin route wrapper
    return dict(row)


@app.get("/exports/{export_id}/content")
def export_content(
    export_id: str,
    db=Depends(get_db),
    format: Literal["json", "markdown"] | None = Query(default=None),
):
    text, media_type, _payload = services.build_export_content(db, export_id, export_format=format)
    return Response(content=text, media_type=media_type)


# ---------------------------------------------------------------------------
# Project-owner restricted retention entries
# ---------------------------------------------------------------------------


@app.post("/owner/segments/{segment_id}/retentions", status_code=201)
def create_retention(segment_id: str, body: RetentionCreate, db=Depends(get_db), owner: Owner = None):
    return services.create_retention(db, segment_id, reason=body.reason, created_by=body.created_by)


@app.get("/owner/retentions")
def list_retentions(
    db=Depends(get_db),
    owner: Owner = None,
    segment_id: str | None = Query(default=None),
    active_only: bool = Query(default=False),
):
    return {"retentions": services.list_retentions(db, segment_id=segment_id, active_only=active_only)}


@app.get("/owner/retentions/{retention_id}")
def get_retention(retention_id: str, db=Depends(get_db), owner: Owner = None):
    return services.get_retention_restricted(db, retention_id)


@app.post("/owner/retentions/{retention_id}/release")
def release_retention(
    retention_id: str,
    body: RetentionRelease | None = None,
    db=Depends(get_db),
    owner: Owner = None,
):
    note = body.release_note if body is not None else None
    return services.release_retention(db, retention_id, release_note=note)


@app.get("/owner/deletion-requests")
def owner_list_deletion_requests(db=Depends(get_db), owner: Owner = None):
    return {"deletion_requests": services.list_deletion_requests(db)}


@app.get("/owner/audit-events")
def owner_audit_events(limit: int = Query(default=200, ge=1, le=1000), db=Depends(get_db), owner: Owner = None):
    return {"events": services.list_audit_events(db, limit=limit)}
