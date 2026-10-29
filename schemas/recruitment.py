"""P3 request/response contracts. Only manual management is implemented."""
from datetime import date, datetime, timezone
from typing import Annotated, ClassVar, Generic, Literal, TypeVar
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, computed_field, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Stage = Literal['submitted','screening','assessment','written_test','interview','offer']
Outcome = Literal['active','rejected','withdrawn','accepted','closed']
TaskType = Literal['assessment','written_test','interview','material_submission','offer_response','other']
TaskStatus = Literal['pending','in_progress','completed','cancelled']
STAGE_ORDER = ['submitted','screening','assessment','written_test','interview','offer']
MUTATION_FIELDS = {'request_id','version','correction_reason'}


class InputModel(BaseModel):
    model_config = ConfigDict(extra='forbid',json_schema_serialization_defaults_required=True)


class ApplicationFields(InputModel):
    company_name: Text
    company_aliases: list[Text] = Field(default_factory=list)
    position_name: Text | None = None
    batch: Text | None = None
    stage: Stage = 'submitted'
    outcome: Outcome = 'active'
    applied_at: AwareDatetime | None = None
    applied_on: date | None = None
    notes: str | None = None

    @model_validator(mode='after')
    def applied_precision(self):
        if self.applied_at is not None and self.applied_on is not None:
            raise ValueError('投递时间请选择具体时刻或仅日期，不要同时填写')
        return self


class ApplicationCreate(ApplicationFields):
    request_id: UUID


class PatchBase(InputModel):
    request_id: UUID
    version: int = Field(ge=1)
    correction_reason: Text | None = None
    non_nullable: ClassVar[set[str]] = set()

    @model_validator(mode='after')
    def changes_present(self):
        changes = self.model_fields_set - MUTATION_FIELDS
        if not changes:
            raise ValueError('请至少填写一个要修改的字段')
        for name in changes & self.non_nullable:
            if getattr(self,name) is None:
                raise ValueError(f'{name} 不能设为空')
        return self


class ApplicationPatch(PatchBase):
    company_name: Text | None = None
    company_aliases: list[Text] | None = None
    position_name: Text | None = None
    batch: Text | None = None
    stage: Stage | None = None
    outcome: Outcome | None = None
    applied_at: AwareDatetime | None = None
    applied_on: date | None = None
    notes: str | None = None
    non_nullable = {'company_name','company_aliases','stage','outcome'}


class TaskFields(InputModel):
    type: TaskType
    title: Text
    status: TaskStatus = 'pending'
    round: int | None = Field(default=None,ge=1)
    opens_at: AwareDatetime | None = None
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None
    due_at: AwareDatetime | None = None
    starts_on: date | None = None
    due_on: date | None = None
    source_timezone: Text | None = None
    time_note: str | None = None
    notes: str | None = None

    @model_validator(mode='after')
    def time_fields(self):
        if self.starts_at is not None and self.starts_on is not None:
            raise ValueError('开始时间请选择具体时刻或仅日期')
        if self.due_at is not None and self.due_on is not None:
            raise ValueError('截止时间请选择具体时刻或仅日期')
        if self.starts_at and self.ends_at and self.starts_at > self.ends_at:
            raise ValueError('结束时间不能早于开始时间')
        if self.opens_at and self.due_at and self.opens_at > self.due_at:
            raise ValueError('截止时间不能早于开放时间')
        if any([self.opens_at,self.starts_at,self.ends_at,self.due_at]) and not self.source_timezone:
            raise ValueError('具体时刻需要填写来源时区')
        if self.source_timezone:
            try:
                ZoneInfo(self.source_timezone)
            except ZoneInfoNotFoundError as error:
                raise ValueError('来源时区请使用 Asia/Shanghai、UTC 等有效名称') from error
        return self


class TaskCreate(TaskFields):
    request_id: UUID
    application_id: UUID


class TaskPatch(PatchBase):
    type: TaskType | None = None
    title: Text | None = None
    status: TaskStatus | None = None
    round: int | None = Field(default=None,ge=1)
    opens_at: AwareDatetime | None = None
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None
    due_at: AwareDatetime | None = None
    starts_on: date | None = None
    due_on: date | None = None
    source_timezone: Text | None = None
    time_note: str | None = None
    notes: str | None = None
    non_nullable = {'type','title','status'}


class RowMetadata(BaseModel):
    id: UUID
    owner_id: UUID
    version: int
    data_origin: Literal['manual','synthetic']
    sample_case_id: str | None
    sample_key: str | None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class ApplicationOut(ApplicationFields,RowMetadata):
    pass


class TaskOut(TaskFields,RowMetadata):
    application_id: UUID

    @computed_field
    @property
    def is_overdue(self) -> bool:
        return self.status in {'pending','in_progress'} and ((self.due_at is not None and self.due_at < datetime.now(timezone.utc)) or (self.due_on is not None and self.due_on < datetime.now(ZoneInfo('Asia/Shanghai')).date()))


class EventOut(BaseModel):
    id: UUID
    owner_id: UUID
    application_id: UUID
    task_id: UUID | None
    event_type: str
    actor_type: str
    source_kind: str
    source_reference: str | None
    reason: str | None
    before_data: dict | None
    after_data: dict
    operation_id: UUID
    event_index: int
    created_at: AwareDatetime


T = TypeVar('T')


class Page(BaseModel,Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int
