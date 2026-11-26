"""Pydantic request models.  All business text is explicitly supplied locally."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ParticipantCreate(BaseModel):
    id: str | None = Field(default=None, description="Optional caller-defined participant ID")
    display_name: str = Field(min_length=1)
    profile: dict[str, Any] = Field(default_factory=dict)
    session_token: str | None = Field(default=None, description="Optional caller-defined demo session token")


class ProfileUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1)
    profile: dict[str, Any] | None = None


class InterviewCreate(BaseModel):
    id: str | None = None
    title: str = Field(min_length=1)


class SegmentCreate(BaseModel):
    id: str | None = Field(default=None, description="Optional caller-defined segment ID")
    interview_id: str
    ordinal: int | None = Field(default=None, ge=1)
    content: str = Field(min_length=1)
    participant_ids: list[str] = Field(min_length=1)


class RetentionCreate(BaseModel):
    reason: str = Field(min_length=1, description="Explicit reason supplied by the project owner")
    created_by: str = Field(default="project-owner")


class RetentionRelease(BaseModel):
    release_note: str | None = None


class ExportCreate(BaseModel):
    id: str | None = None
    scope: Literal["all", "interview"] = "all"
    interview_id: str | None = None
    format: Literal["json", "markdown"] = "json"


# ---------------------------------------------------------------------------
# Collaborative proofreading (校稿)
# ---------------------------------------------------------------------------


class ProofreadingSessionCreate(BaseModel):
    interview_id: str
    segment_ids: list[str] = Field(
        min_length=1,
        description="Visible segments of this interview to include in proofreading",
    )
    created_by: str = "project-owner"


class ProofreadingEdit(BaseModel):
    segment_id: str
    start: int = Field(ge=0, description="Zero-based Unicode code-point start (inclusive)")
    end: int = Field(ge=0, description="Zero-based Unicode code-point end (exclusive)")
    replacement: str = Field(default="", description="Empty string deletes the range")


class ProofreadingProposal(BaseModel):
    submission_id: str = Field(min_length=1, description="Caller-chosen idempotency ID for this proposal")
    base_version: int = Field(ge=1, description="Draft version the ranges are interpreted against")
    edits: list[ProofreadingEdit] = Field(min_length=1)


class ProofreadingAnnotationCreate(BaseModel):
    segment_id: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    text: str = Field(min_length=1)


class ProofreadingAnnotationReanchor(BaseModel):
    start: int = Field(ge=0)
    end: int = Field(ge=0)


class ProofreadingPublish(BaseModel):
    version: int = Field(ge=1, description="Current draft version being published")


# ---------------------------------------------------------------------------
# Interview room booking and batch rescheduling (访谈预约 / 批量改期)
# ---------------------------------------------------------------------------


class RoomCreate(BaseModel):
    id: str = Field(min_length=1, description="Caller-defined room identifier")
    label: str = Field(min_length=1)


class BookingCreate(BaseModel):
    id: str | None = Field(default=None, description="Optional caller-defined booking ID")
    interview_id: str
    room_id: str
    participant_ids: list[str] = Field(min_length=1)
    start_at: datetime = Field(description="UTC inclusive start; timezone-aware required")
    end_at: datetime = Field(description="UTC exclusive end; timezone-aware required")


class RescheduleItemInput(BaseModel):
    booking_id: str
    room_id: str
    start_at: datetime
    end_at: datetime


class RescheduleOrderCreate(BaseModel):
    id: str | None = Field(default=None, description="Optional caller-defined order ID")
    items: list[RescheduleItemInput] = Field(min_length=1)
    consultation_deadline: datetime = Field(
        description="UTC deadline; later than now and no later than every original/new start"
    )


class RescheduleResponseInput(BaseModel):
    decision: Literal["accepted", "rejected"]
