"""Pydantic request models.  All business text is explicitly supplied locally."""

from __future__ import annotations

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
