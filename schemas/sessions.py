"""P5 session requests and the small set of Agent tool arguments."""
from typing import Literal
from uuid import UUID

from pydantic import Field

from schemas.notifications import ExtractionResult
from schemas.recruitment import InputModel,Text


class SessionCreate(InputModel):
    request_id: UUID
    notification_id: UUID
    extraction_id: UUID


class SessionMessage(InputModel):
    request_id: UUID
    version: int = Field(ge=1)
    text: Text = Field(max_length=4000)
    reply_to_question_id: UUID | None = None


class SearchApplications(InputModel):
    offset: int = Field(default=0,ge=0,le=500)


class ApplicationDetail(InputModel):
    application_id: UUID


class ApplicationTasks(InputModel):
    offset: int = Field(default=0,ge=0,le=500)


class UpcomingTasks(InputModel):
    days: int = Field(default=7,ge=1,le=90)


class RecordAnswer(InputModel):
    field: Literal['company_name','position_name','batch','sent_at','timezone_hint']
    value: Text = Field(description='实体保留用户原话；sent_at为带偏移的ISO8601，如2026-10-19T09:00:00+08:00；timezone_hint为Asia/Shanghai等IANA名称。')
    quote: Text


class ReviseExtraction(InputModel):
    result: ExtractionResult


class SelectApplication(InputModel):
    application_id: UUID
    quote: Text | None = None


class ChooseNewApplication(InputModel):
    quote: Text


class Question(InputModel):
    field: Text
    question: Text


class AskUser(InputModel):
    questions: list[Question] = Field(min_length=1,max_length=3)


class TaskAction(InputModel):
    draft_index: int = Field(ge=0)
    action: Literal['create','update','skip','cancel']
    task_id: UUID | None = None
    reason: Text
    user_quote: Text | None = None


class PrepareUpdate(InputModel):
    task_actions: list[TaskAction] = Field(default_factory=list)
