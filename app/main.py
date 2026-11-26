"""FastAPI entry point for the verifiable personal-data deletion service."""

from __future__ import annotations

import hmac
import os
import sqlite3
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.responses import JSONResponse

from . import proofreading, scheduling, services
from .db import connect, init_db
from .schemas import (
    BookingCreate,
    ExportCreate,
    InterviewCreate,
    ParticipantCreate,
    ProfileUpdate,
    ProofreadingAnnotationCreate,
    ProofreadingAnnotationReanchor,
    ProofreadingProposal,
    ProofreadingPublish,
    ProofreadingSessionCreate,
    RescheduleOrderCreate,
    RescheduleResponseInput,
    RetentionCreate,
    RetentionRelease,
    RoomCreate,
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


def require_participant_session(
    x_session_token: Annotated[str | None, Header(alias="X-Session-Token")] = None,
    authorization: Annotated[str | None, Header()] = None,
    db=Depends(get_db),
) -> dict[str, object]:
    """Authenticate a participant via the existing demo session credential."""
    token = x_session_token
    if not token and authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            token = value.strip()
    if not token:
        raise services.ApiError(
            401, "session_required", "A valid X-Session-Token or Bearer session token is required"
        )
    row = scheduling.authenticate_session(db, token)
    return {"id": row["id"], "session_token": token, "status": row["status"]}


Session = Annotated[dict[str, object], Depends(require_participant_session)]
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
        # Expired reschedule consultations must release their proposed slots
        # after a restart, even if no request arrives at the deadline.
        app.state.reschedule_recovery = scheduling.recover_rescheduling(conn)
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


# ---------------------------------------------------------------------------
# Collaborative proofreading (校稿).  Every route uses the existing owner
# credential.  Draft text is never visible through ordinary read/search/export
# entries until it is published into segments.content.
# ---------------------------------------------------------------------------


@app.post("/owner/proofreading/sessions", status_code=201)
def open_proofreading_session(
    body: ProofreadingSessionCreate, db=Depends(get_db), owner: Owner = None
):
    return proofreading.open_session(
        db,
        interview_id=body.interview_id,
        segment_ids=body.segment_ids,
        created_by=body.created_by,
    )


@app.get("/owner/proofreading/sessions")
def list_proofreading_sessions(
    db=Depends(get_db),
    owner: Owner = None,
    interview_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
):
    return {
        "sessions": proofreading.list_sessions(
            db, interview_id=interview_id, status=status
        )
    }


@app.get("/owner/proofreading/sessions/{session_id}")
def get_proofreading_session(
    session_id: str,
    db=Depends(get_db),
    owner: Owner = None,
    version: int | None = Query(default=None, ge=1),
):
    return proofreading.session_detail(db, session_id, version=version)


@app.get("/owner/proofreading/sessions/{session_id}/versions")
def list_proofreading_versions(
    session_id: str, db=Depends(get_db), owner: Owner = None
):
    return {"versions": proofreading.list_versions(db, session_id)}


@app.get("/owner/proofreading/sessions/{session_id}/versions/{version}")
def get_proofreading_version(
    session_id: str, version: int, db=Depends(get_db), owner: Owner = None
):
    return proofreading.get_version_draft(db, session_id, version)


@app.post("/owner/proofreading/sessions/{session_id}/proposals", status_code=201)
def submit_proofreading_proposal(
    session_id: str,
    body: ProofreadingProposal,
    db=Depends(get_db),
    owner: Owner = None,
):
    edits = [
        {
            "segment_id": e.segment_id,
            "start": e.start,
            "end": e.end,
            "replacement": e.replacement,
        }
        for e in body.edits
    ]
    result = proofreading.submit_proposal(
        db,
        session_id,
        submission_id=body.submission_id,
        base_version=body.base_version,
        edits=edits,
    )
    payload = dict(result)
    payload["idempotent_replay"] = result.get("idempotent_replay", False)
    return JSONResponse(
        status_code=200 if payload["idempotent_replay"] else 201, content=payload
    )


@app.get("/owner/proofreading/sessions/{session_id}/annotations")
def list_proofreading_annotations(
    session_id: str,
    db=Depends(get_db),
    owner: Owner = None,
    include_out_of_scope: bool = Query(default=False),
):
    return {
        "annotations": proofreading.list_annotations(
            db, session_id, include_out_of_scope=include_out_of_scope
        )
    }


@app.post("/owner/proofreading/sessions/{session_id}/annotations", status_code=201)
def create_proofreading_annotation(
    session_id: str,
    body: ProofreadingAnnotationCreate,
    db=Depends(get_db),
    owner: Owner = None,
):
    return proofreading.create_annotation(
        db,
        session_id,
        segment_id=body.segment_id,
        start=body.start,
        end=body.end,
        text=body.text,
    )


@app.post(
    "/owner/proofreading/sessions/{session_id}/annotations/{annotation_id}/reanchor"
)
def reanchor_proofreading_annotation(
    session_id: str,
    annotation_id: str,
    body: ProofreadingAnnotationReanchor,
    db=Depends(get_db),
    owner: Owner = None,
):
    return proofreading.reanchor_annotation(
        db, session_id, annotation_id, start=body.start, end=body.end
    )


@app.post(
    "/owner/proofreading/sessions/{session_id}/annotations/{annotation_id}/resolve"
)
def resolve_proofreading_annotation(
    session_id: str, annotation_id: str, db=Depends(get_db), owner: Owner = None
):
    return proofreading.resolve_annotation(db, session_id, annotation_id)


@app.post("/owner/proofreading/sessions/{session_id}/publish")
def publish_proofreading_session(
    session_id: str,
    body: ProofreadingPublish,
    db=Depends(get_db),
    owner: Owner = None,
):
    return proofreading.publish(db, session_id, version=body.version)


# ---------------------------------------------------------------------------
# Interview room booking and batch rescheduling (访谈预约 / 批量改期).
# Owner entries manage rooms, bookings and consultations; participants use
# their existing session token for the calendar/response entries.
# ---------------------------------------------------------------------------


@app.post("/owner/rooms", status_code=201)
def create_room(body: RoomCreate, db=Depends(get_db), owner: Owner = None):
    return scheduling.create_room(db, room_id=body.id, label=body.label)


@app.get("/owner/rooms")
def list_rooms(db=Depends(get_db), owner: Owner = None):
    return {"rooms": scheduling.list_rooms(db)}


@app.post("/owner/bookings", status_code=201)
def create_booking(body: BookingCreate, db=Depends(get_db), owner: Owner = None):
    return scheduling.create_booking(
        db,
        booking_id=body.id,
        interview_id=body.interview_id,
        room_id=body.room_id,
        participant_ids=body.participant_ids,
        start_at=body.start_at,
        end_at=body.end_at,
    )


@app.get("/owner/bookings")
def owner_list_bookings(
    db=Depends(get_db),
    owner: Owner = None,
    status: str | None = Query(default=None),
    room_id: str | None = Query(default=None),
    interview_id: str | None = Query(default=None),
):
    return {
        "bookings": scheduling.list_bookings(
            db, status=status, room_id=room_id, interview_id=interview_id
        )
    }


@app.get("/owner/bookings/{booking_id}")
def owner_get_booking(booking_id: str, db=Depends(get_db), owner: Owner = None):
    return scheduling.get_booking(db, booking_id)


@app.post("/owner/bookings/{booking_id}/cancel")
def owner_cancel_booking(
    booking_id: str,
    db=Depends(get_db),
    owner: Owner = None,
    reason: str | None = Query(default=None),
):
    return scheduling.cancel_booking(db, booking_id, reason=reason)


@app.post("/owner/reschedule-orders", status_code=201)
def create_reschedule_order(body: RescheduleOrderCreate, db=Depends(get_db), owner: Owner = None):
    items = [
        {
            "booking_id": item.booking_id,
            "room_id": item.room_id,
            "start_at": item.start_at,
            "end_at": item.end_at,
        }
        for item in body.items
    ]
    return scheduling.create_reschedule_order(
        db,
        order_id=body.id,
        items=items,
        consultation_deadline=body.consultation_deadline,
    )


@app.get("/owner/reschedule-orders")
def owner_list_reschedule_orders(
    db=Depends(get_db),
    owner: Owner = None,
    status: str | None = Query(default=None),
):
    return {"orders": scheduling.list_orders(db, status=status)}


@app.get("/owner/reschedule-orders/{order_id}")
def owner_get_reschedule_order(order_id: str, db=Depends(get_db), owner: Owner = None):
    return scheduling.order_detail(db, order_id)


@app.post("/owner/reschedule-orders/{order_id}/withdraw")
def owner_withdraw_reschedule_order(order_id: str, db=Depends(get_db), owner: Owner = None):
    return scheduling.withdraw_order(db, order_id)


@app.post("/owner/reschedule-orders/sweep-expired")
def owner_sweep_expired_orders(db=Depends(get_db), owner: Owner = None):
    return scheduling.sweep_expired_orders(db)


# --- Participant session entries -------------------------------------------


@app.get("/me/calendar")
def my_calendar(db=Depends(get_db), session: Session = None):
    return scheduling.participant_calendar(db, str(session["id"]))


@app.get("/me/reschedule-orders")
def my_reschedule_orders(db=Depends(get_db), session: Session = None):
    return {"orders": scheduling.list_orders(db, participant_id=str(session["id"]))}


@app.get("/me/reschedule-orders/{order_id}")
def my_reschedule_order(order_id: str, db=Depends(get_db), session: Session = None):
    detail = scheduling.order_detail(db, order_id)
    if str(session["id"]) not in detail["participant_ids"]:
        raise services.ApiError(
            403,
            "not_reschedule_participant",
            "This consultation does not involve the authenticated participant",
        )
    return detail


@app.post("/me/reschedule-orders/{order_id}/responses", status_code=201)
def respond_my_reschedule_order(
    order_id: str,
    body: RescheduleResponseInput,
    db=Depends(get_db),
    session: Session = None,
):
    result = scheduling.respond_to_order(
        db,
        order_id=order_id,
        participant_id=str(session["id"]),
        decision=body.decision,
    )
    replay = bool(result.get("idempotent_replay", False))
    result["idempotent_replay"] = replay
    return JSONResponse(status_code=200 if replay else 201, content=result)
