from datetime import datetime
from typing import Literal
from uuid import UUID
from schemas.recruitment import InputModel
from schemas.automatic import AutomaticOut
from pydantic import Field

class JobCreate(InputModel):
    request_id: UUID
    notification_id: UUID
    operation: Literal['process_notification'] = 'process_notification'
    input_version: int | None = Field(default=None,ge=0)

class JobOut(InputModel):
    id: UUID
    notification_id: UUID
    operation: str
    input_version: int
    status: Literal['queued','running','succeeded','failed','cancelled']
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    result: AutomaticOut | None

    attempt_count: int
    cancel_requested: bool
    available_at: datetime
    lease_until: datetime | None
    error_kind: str | None
    retryable: bool

class JobRetry(InputModel):
    request_id: UUID

class JobAttemptOut(InputModel):
    id: UUID
    number: int
    status: str
    started_at: datetime
    finished_at: datetime | None
    error_kind: str | None
