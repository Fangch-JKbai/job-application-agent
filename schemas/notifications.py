"""Notification intake, extraction evidence and editable review contracts."""
from datetime import date
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError

from pydantic import AwareDatetime,Field,computed_field,field_validator,model_validator

from schemas.recruitment import InputModel,TaskFields,TaskType,Text

NotificationType = Literal['assessment_invitation','written_test_invitation','interview_invitation','schedule_change','rejection','offer_received','offer_accepted','cancellation','reminder','conditional_update','material_submission','application_received','other']
TimeName = Literal['opens_at','starts_at','ends_at','due_at','starts_on','due_on']
FactName = Literal['type','title','round','opens_at','starts_at','ends_at','due_at','starts_on','due_on','source_timezone','time_note']
EntityName = Literal['company_name','position_name','batch','notification_type']


def valid_zone(value):
    if value:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError('时区请使用 Asia/Shanghai、UTC 等有效名称') from error
    return value


class EntityHints(InputModel):
    company_name: Text | None = None
    position_name: Text | None = None
    batch: Text | None = None


class NotificationImport(InputModel):
    request_id: UUID
    text: str = Field(max_length=60000)
    source: Literal['manual','email','sms','other'] = 'manual'
    source_reference: str | None = None
    sent_at: AwareDatetime | None = None
    timezone_hint: Text | None = None
    calendar_year: int | None = Field(default=None,ge=1900,le=2200)
    hints: EntityHints = Field(default_factory=EntityHints)

    @field_validator('text')
    @classmethod
    def original_text(cls,value):
        if not value.strip():
            raise ValueError('通知正文不能为空')
        return value  # Keep original whitespace and line breaks.

    @field_validator('timezone_hint')
    @classmethod
    def timezone_name(cls,value):
        return valid_zone(value)


class NotificationOut(InputModel):
    id: UUID
    owner_id: UUID
    request_id: UUID
    text: str = Field(max_length=60000)
    source: str
    source_reference: str | None
    sent_at: AwareDatetime | None
    imported_at: AwareDatetime
    timezone_hint: str | None
    calendar_year: int | None
    hints: EntityHints


class Evidence(InputModel):
    source: Literal['text','company_hint','position_hint','batch_hint','timezone_hint','review']
    quote: Text


class MissingField(InputModel):
    field: Text
    question: Text


class TaskDraft(InputModel):
    type: TaskType
    title: Text
    round: int | None = Field(default=None,ge=1)
    opens_at: AwareDatetime | None = None
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None
    due_at: AwareDatetime | None = None
    starts_on: date | None = None
    due_on: date | None = None
    source_timezone: Text | None = None
    time_note: str | None = None
    time_basis: dict[TimeName,Literal['explicit','relative','conflicting']] = Field(default_factory=dict)
    evidence: dict[FactName,Evidence] = Field(default_factory=dict)

    @model_validator(mode='after')
    def coherent_time_fields(self):
        data = self.model_dump(exclude={'time_basis','evidence'})
        TaskFields(**data)
        return self


class ExtractionResult(InputModel):
    company_name: Text | None = None
    position_name: Text | None = None
    batch: Text | None = None
    notification_type: NotificationType = 'other'
    tasks: list[TaskDraft] = Field(default_factory=list)
    evidence: dict[EntityName,Evidence] = Field(default_factory=dict)
    missing_fields: list[MissingField] = Field(default_factory=list)
    issues: list[Text] = Field(default_factory=list)


class ExtractionRequest(InputModel):
    request_id: UUID


class ExtractionReview(InputModel):
    version: int = Field(ge=1)
    review_text: Text
    result: ExtractionResult


class ExtractionOut(InputModel):
    id: UUID
    owner_id: UUID
    notification_id: UUID
    request_id: UUID
    status: Literal['processing','completed','failed']
    provider: str | None
    model: str | None
    prompt_version: str
    phase: str
    error_kind: str | None
    elapsed_ms: int | None
    retry_of: UUID | None
    cost_basis: dict
    raw_output: str | None
    model_result: ExtractionResult | None
    reviewed_result: ExtractionResult | None
    review_text: str | None
    usage: dict | None
    error_status: int | None
    error_message: str | None
    version: int
    created_at: AwareDatetime
    finished_at: AwareDatetime | None
    reviewed_at: AwareDatetime | None

    @computed_field
    @property
    def result(self) -> ExtractionResult | None:
        return self.reviewed_result or self.model_result
