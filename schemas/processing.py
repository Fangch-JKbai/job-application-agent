"""Owner-scoped processing history; recovery never calls a model."""
from typing import Any,Literal
from uuid import UUID
from pydantic import BaseModel
from schemas.recruitment import InputModel
from schemas.notifications import ExtractionOut
from schemas.workbench import MessageView

Kind=Literal['extraction','turn']

class RetryRequest(InputModel):
    request_id: UUID

class AttemptView(BaseModel):
    kind: Kind
    id: str
    notification_id: str
    session_id: str | None
    status: str
    phase: str
    error_kind: str | None
    error_message: str | None
    elapsed_ms: int | None
    retry_of: str | None
    created_at: str
    finished_at: str | None
    usage: dict[str,Any] | None
    cost_basis: dict[str,Any]
    active: bool
    recoverable: bool
    can_retry: bool

class AttemptDetail(BaseModel):
    attempt: AttemptView
    trace: list[dict[str,Any]]

class RetryResponse(BaseModel):
    kind: Kind
    extraction: ExtractionOut | None
    message: MessageView | None
