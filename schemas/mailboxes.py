from typing import Literal
from uuid import UUID
from pydantic import Field,SecretStr,model_validator
from schemas.recruitment import InputModel

HOSTS={'qq':'imap.qq.com','163':'imap.163.com','126':'imap.126.com'}
DOMAINS={'qq':'qq.com','163':'163.com','126':'126.com'}
class MailboxBind(InputModel):
    provider: Literal['qq','163','126']
    address: str = Field(max_length=254)
    authorization_code: SecretStr
    automatic: bool = True
    @model_validator(mode='after')
    def valid_account(self):
        self.address=self.address.strip().lower()
        if self.address.count('@')!=1 or self.address.split('@')[1]!=DOMAINS[self.provider] or not self.address.split('@')[0]:
            raise ValueError('邮箱地址与所选服务商不一致，仅支持qq.com、163.com和126.com个人邮箱')
        if not 6<=len(self.authorization_code.get_secret_value())<=128:raise ValueError('请填写邮箱客户端授权码')
        return self
class MailboxSetting(InputModel):
    automatic: bool
class MailboxImport(InputModel):
    message_id: UUID

from pydantic import AwareDatetime
class MailboxOut(InputModel):
    id: UUID
    address: str
    provider: str
    enabled: bool
    created_at: AwareDatetime
    last_sync_at: AwareDatetime | None
    last_error: str | None
class MailboxList(InputModel):
    items: list[MailboxOut]
    configured: bool
class MailMessageOut(InputModel):
    id: UUID
    subject: str
    sender: str
    sent_at: AwareDatetime | None
    is_candidate: bool
    notification_id: UUID | None
    auto_state: str
    processing_error: str | None
    imported_at: AwareDatetime
class MailMessageList(InputModel):
    items: list[MailMessageOut]
class ImportedMessage(InputModel):
    notification_id: UUID
