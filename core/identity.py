"""Server-owned personal workspace ID; the optional P10 gate does not accept a client owner ID."""
import os
from uuid import UUID

from database.config import load_settings


def current_user_id() -> UUID:
    load_settings()
    return UUID(os.environ['LOCAL_USER_ID'])
