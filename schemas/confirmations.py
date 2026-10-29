"""The user confirms an exact server-side proposal, never arbitrary update fields."""
from typing import Literal
from uuid import UUID

from pydantic import Field
from schemas.recruitment import InputModel


class ProposalConfirmation(InputModel):
    request_id: UUID
    proposal_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    session_version: int = Field(ge=1)
    confirmed: Literal[True]
