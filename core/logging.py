"""Shared structured logging and correlation IDs, adapted from the learning project."""

from __future__ import annotations

import json
import logging
import uuid
from contextvars import ContextVar
from contextlib import contextmanager
from pathlib import Path
import traceback

# ---------------------------------------------------------------------------
# Correlation ID — propagated across async tasks via contextvars
# ---------------------------------------------------------------------------
_context=ContextVar('operation_context',default=None)

@contextmanager
def log_context(correlation_id=None,**fields):
    token=_context.set({**(_context.get() or {}),**{k:v for k,v in fields.items() if v is not None}})
    cid_token=_correlation_id.set(correlation_id) if correlation_id is not None else None
    try:yield
    finally:
        _context.reset(token)
        if cid_token is not None:_correlation_id.reset(cid_token)

def origin_request_id():
    context=_context.get() or {}
    return context.get('request_id') or context.get('origin_request_id')

def error_fields(error):
    return {'error_type':type(error).__name__,'frames':[{'file':Path(f.filename).name,'line':f.lineno,'function':f.name} for f in traceback.extract_tb(error.__traceback__)[-8:]]}

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")


def get_correlation_id() -> str:
    """Return the current correlation ID (empty string if unset)."""
    return _correlation_id.get()


def set_correlation_id(cid: str | None = None) -> str:
    """Set (or generate) a correlation ID for the current async context."""
    cid = cid or uuid.uuid4().hex
    _correlation_id.set(cid)
    return cid


# ---------------------------------------------------------------------------
# JSON Formatter
# ---------------------------------------------------------------------------
class _JSONFormatter(logging.Formatter):
    """Emit each log record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if hasattr(record,"fields"):
            payload["fields"] = record.fields
        payload.update(_context.get() or {})
        cid = get_correlation_id()
        if cid:
            payload["correlation_id"] = cid
        if record.exc_info and record.exc_info[1] is not None:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


# ---------------------------------------------------------------------------
# Bootstrap — call once at import time
# ---------------------------------------------------------------------------
def _configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(_JSONFormatter())

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Avoid duplicate handlers on re-import
    if not any(isinstance(h, logging.StreamHandler) and isinstance(h.formatter, _JSONFormatter) for h in root.handlers):
        root.addHandler(handler)


_configure_logging()

# The structured middleware replaces raw URL/query-string access logs.
logging.getLogger('uvicorn.access').disabled=True
logging.getLogger('httpx').setLevel(logging.WARNING)
