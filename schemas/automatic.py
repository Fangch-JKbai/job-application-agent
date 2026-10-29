from uuid import UUID
from pydantic import Field,ConfigDict
from schemas.recruitment import InputModel,ApplicationOut
class AutomaticOut(InputModel):
    model_config=ConfigDict(extra='ignore',json_schema_serialization_defaults_required=True)
    notification_id: UUID
    status: str
    message: str
    application_id: UUID | None = None
    application: ApplicationOut | None = None
    can_undo: bool = False
    warnings: list[str] = Field(default_factory=list)
