from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

TaskStatus = Literal["backlog", "working", "blocked", "done"]
Priority = Literal["low", "normal", "high", "critical"]


class Project(BaseModel):
    id: str = ""
    name: str
    description: str = ""
    color: str = "#2e6fd8"


class ChecklistItem(BaseModel):
    text: str
    done: bool = False


class Note(BaseModel):
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    body: str


class Attachment(BaseModel):
    filename: str
    path: str
    kind: str = "file"
    description: str = ""
    uploaded_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


class TaskActivityEvent(BaseModel):
    id: str = Field(default_factory=lambda: secrets.token_hex(8))
    event_type: Literal["note", "image", "progress_update"]
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    note_text: str | None = None
    image_path: str | None = None
    image_filename: str | None = None
    progress_before: int | None = None
    progress_after: int | None = None


class Task(BaseModel):
    id: str
    title: str
    status: TaskStatus = "backlog"
    priority: Priority = "normal"
    project_id: str = ""
    project: str = ""
    summary: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    start_date: str | None = None
    due_date: str | None = None
    completed_date: str | None = None
    percent_complete: int = 0
    depends_on: list[str] = Field(default_factory=list)
    checklist: list[ChecklistItem] = Field(default_factory=list)
    notes: list[Note] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)
    activity: list[TaskActivityEvent] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)


MilestoneStatus = Literal["planned", "active", "done"]


class Milestone(BaseModel):
    id: str
    title: str
    status: MilestoneStatus = "active"
    color: str = "#3567e0"
    summary: str = ""
    description: str = ""
    projects: list[str] = Field(default_factory=list)
    start_date: str | None = None
    target_date: str | None = None
    task_ids: list[str] = Field(default_factory=list)
    notes: list[Note] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("projects", mode="before")
    @classmethod
    def _coerce_projects(cls, value: Any) -> Any:
        if not value:
            return []
        if isinstance(value, str):
            return [value]
        return list(value)

    @field_validator("task_ids", mode="before")
    @classmethod
    def _coerce_task_ids(cls, value: Any) -> Any:
        if not value:
            return []
        if isinstance(value, str):
            return [value]
        return list(value)
