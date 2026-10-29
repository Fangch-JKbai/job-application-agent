"""Public workbench contracts; internal Agent facts remain preserved as extras."""
from typing import Any,Literal
from pydantic import BaseModel,ConfigDict,Field
from schemas.notifications import ExtractionResult


class PendingQuestion(BaseModel):
    id: str
    field: str
    question: str


class ConfirmedFact(BaseModel):
    model_config=ConfigDict(extra='allow',json_schema_serialization_defaults_required=True)
    value: Any
    quote: str
    turn_id: str
    question_id: str | None = None


class Candidate(BaseModel):
    id: str
    company_name: str
    position_name: str | None
    batch: str | None
    stage: str
    outcome: str
    version: int
    company_aliases: list[str]


class CandidateSet(BaseModel):
    model_config=ConfigDict(extra='allow',json_schema_serialization_defaults_required=True)
    items: list[Candidate]=Field(default_factory=list)
    total: int=0


class Selection(BaseModel):
    model_config=ConfigDict(extra='allow',json_schema_serialization_defaults_required=True)
    kind: Literal['new','existing']
    application_id: str | None = None


class SessionState(BaseModel):
    model_config=ConfigDict(extra='allow',json_schema_serialization_defaults_required=True)
    result: ExtractionResult
    questions: list[PendingQuestion]
    candidates: CandidateSet
    confirmed_fields: dict[str,ConfirmedFact]
    selection: Selection | None
    proposal_id: str | None


class SessionView(BaseModel):
    model_config=ConfigDict(extra='allow',json_schema_serialization_defaults_required=True)
    id: str
    notification_id: str
    extraction_id: str
    extraction_version: int
    status: Literal['idle','processing','awaiting_user','ready','failed','saved']
    state: SessionState
    version: int
    created_at: str
    updated_at: str


class TurnView(BaseModel):
    model_config=ConfigDict(extra='allow',json_schema_serialization_defaults_required=True)
    id: str
    user_text: str
    assistant_text: str | None
    status: Literal['processing','completed','failed']
    error_message: str | None
    reply_to_question_id: str | None
    created_at: str
    trace: list[dict[str,Any]]
    phase: str
    error_kind: str | None
    elapsed_ms: int | None
    retry_of: str | None
    usage: dict[str,Any] | None
    cost_basis: dict[str,Any]


class ProposalView(BaseModel):
    model_config=ConfigDict(extra='allow',json_schema_serialization_defaults_required=True)
    id: str
    status: Literal['draft','superseded','confirmed']
    payload: dict[str,Any]
    review_url: str


class MessageView(BaseModel):
    session: SessionView
    turn: TurnView
    proposal: ProposalView | None


class InboxItem(BaseModel):
    id: str
    text: str
    source: str
    imported_at: str
    company_name: str | None
    position_name: str | None
    session_id: str | None
    session_status: str | None
    extraction_id: str | None
    extraction_status: str | None

    intake_application_id: str | None = None
    automatic_status: str | None = None
