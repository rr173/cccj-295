"""Pydantic request models.  All business text is explicitly supplied locally."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


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


# ---------------------------------------------------------------------------
# Interview attendance subsidies (访谈补贴结算)
# ---------------------------------------------------------------------------


class AttendanceItemInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    participant_id: str = Field(min_length=1)
    attended_minutes: int | None = Field(default=None, ge=0)
    minutes: int | None = Field(default=None, ge=0)

    def normalized_minutes(self) -> int:
        if self.attended_minutes is not None:
            return self.attended_minutes
        if self.minutes is not None:
            return self.minutes
        raise ValueError("attended_minutes is required")


class AttendanceUpsertInput(BaseModel):
    booking_id: str | None = None
    participant_id: str | None = None
    attended_minutes: int | None = Field(default=None, ge=0)
    minutes: int | None = Field(default=None, ge=0)
    records: list[AttendanceItemInput] | None = None
    attendance: list[AttendanceItemInput] | None = None
    items: list[AttendanceItemInput] | None = None
    minutes_by_participant: dict[str, int] | None = None

    def normalized_records(self) -> list[dict[str, Any]]:
        source = self.records or self.attendance or self.items
        if source is not None:
            return [
                {"participant_id": item.participant_id, "attended_minutes": item.normalized_minutes()}
                for item in source
            ]
        if self.minutes_by_participant is not None:
            return [
                {"participant_id": pid, "attended_minutes": minutes}
                for pid, minutes in self.minutes_by_participant.items()
            ]
        if self.participant_id is not None:
            minutes = self.attended_minutes if self.attended_minutes is not None else self.minutes
            if minutes is None:
                raise ValueError("attended_minutes is required")
            return [{"participant_id": self.participant_id, "attended_minutes": minutes}]
        raise ValueError("records, attendance, items or minutes_by_participant is required")


class AttendanceMinutesInput(BaseModel):
    attended_minutes: int | None = Field(default=None, ge=0)
    minutes: int | None = Field(default=None, ge=0)

    def normalized_minutes(self) -> int:
        if self.attended_minutes is not None:
            return self.attended_minutes
        if self.minutes is not None:
            return self.minutes
        raise ValueError("attended_minutes is required")


class SubsidyBatchLineInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    participant_id: str = Field(min_length=1)
    amount: Any = Field(
        default=None,
        validation_alias=AliasChoices(
            "amount", "expected_amount", "sendable_amount", "available_amount", "amount_yuan"
        ),
    )
    amount_fen: int | None = None


class SubsidyBatchCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    batch_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("batch_id", "id"),
    )
    week_start: str | None = Field(
        default=None,
        validation_alias=AliasChoices("week_start", "week", "week_date"),
    )
    lines: list[SubsidyBatchLineInput] | None = Field(
        default=None,
        validation_alias=AliasChoices("lines", "items", "recipients", "payments"),
    )
    # Path-supplied week is merged by the route when the body omits week_start.
    path_week: str | None = None

    def normalized_lines(self) -> list[dict[str, Any]]:
        if not self.lines:
            raise ValueError("at least one payment line is required")
        result: list[dict[str, Any]] = []
        for line in self.lines:
            if line.amount_fen is not None:
                result.append(
                    {"participant_id": line.participant_id, "amount_fen": line.amount_fen}
                )
            elif line.amount is not None:
                result.append({"participant_id": line.participant_id, "amount": line.amount})
            else:
                raise ValueError("each line requires amount/expected_amount")
        return result

    def normalized_week(self) -> str:
        week = self.week_start or self.path_week
        if not week:
            raise ValueError("week_start is required")
        return week


class SubsidyLineConfirm(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    result: Literal["paid", "unpaid"] | None = Field(
        default=None,
        validation_alias=AliasChoices("result", "status", "outcome", "payment_result"),
    )
    paid: bool | None = None
    note: str | None = None

    def normalized_result(self, default: str | None = None) -> str:
        if self.result is not None:
            return self.result
        if self.paid is True:
            return "paid"
        if self.paid is False:
            return "unpaid"
        if default is not None:
            return default
        raise ValueError("result must be paid or unpaid")


class SubsidyRefundCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    refund_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("refund_id", "id", "return_id"),
    )
    amount: Any = Field(
        default=None,
        validation_alias=AliasChoices("amount", "refund_amount", "amount_yuan"),
    )
    amount_fen: int | None = Field(default=None, ge=1)
    line_id: str | None = None
    batch_id: str | None = None
    line_no: int | None = Field(default=None, ge=1)
    note: str | None = None
