"""Read only this project's local configuration."""
from pathlib import Path
import os
from urllib.parse import urlsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


def load_settings() -> None:
    load_dotenv(ROOT / '.env')


def database_url(dsn: str | None = None) -> str:
    load_settings()
    value = dsn or os.environ['DATABASE_URL']
    # The learning project has its own database; don't accidentally migrate it.
    # 翻译一下这个注释
    name = urlsplit(value).path.removeprefix('/')
    if not name.startswith('job_application_agent_'):
        raise ValueError('本项目只使用 job_application_agent_ 开头的独立数据库')
    return value
