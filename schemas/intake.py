from typing import Literal
from uuid import UUID
from pydantic import Field
from schemas.recruitment import ApplicationFields,ApplicationOut,InputModel,TaskFields,TaskOut

class IntakeConfirmation(InputModel):
    request_id: UUID
    notification_id: UUID
    extraction_id: UUID
    extraction_version: int = Field(ge=1)
    application: ApplicationFields
    tasks: list[TaskFields] = Field(default_factory=list,max_length=20)
    confirmed: Literal[True]

class IntakeSaved(InputModel):
    notification_id: UUID
    application: ApplicationOut
    tasks: list[TaskOut]

class IntakeReceipt(InputModel):
    application_id: UUID | None
