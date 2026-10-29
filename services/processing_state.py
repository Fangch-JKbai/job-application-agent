"""One PostgreSQL session lock per attempt; the same connection owns its writes."""
from contextlib import asynccontextmanager
import hashlib
import logging

from services.errors import Conflict

logger=logging.getLogger(__name__)


def lock_key(kind,attempt_id):
    return int.from_bytes(hashlib.sha256(f'p8:{kind}:{attempt_id}'.encode()).digest()[:8],signed=True)


@asynccontextmanager
async def attempt_connection(pool,kind,attempt_id):
    async with pool.acquire() as conn:
        terminated=False
        def closed(_):
            nonlocal terminated
            terminated=True
        conn.add_termination_listener(closed)
        key=lock_key(kind,attempt_id)
        acquired=False
        try:
            acquired=await conn.fetchval('SELECT pg_try_advisory_lock($1)',key)
            if not acquired:raise Conflict('这次处理仍在运行，请查询状态后再操作')
            yield conn,lambda: not terminated
        finally:
            # asyncpg invalidates its pool proxy on termination; don't call it then.
            if not terminated:
                if acquired:await conn.execute('SELECT pg_advisory_unlock($1)',key)
                conn.remove_termination_listener(closed)


def usage_summary(trace,in_flight=False):
    calls=[item for item in trace if item['kind'] in {'model','model_error'}]
    known=[item['usage'] for item in calls if item.get('usage') is not None]
    return {'model_calls':len(calls)+int(in_flight),'calls_with_usage':len(known),
        'prompt_tokens':sum(item.get('prompt_tokens',0) or 0 for item in known) if known else None,
        'completion_tokens':sum(item.get('completion_tokens',0) or 0 for item in known) if known else None,
        'total_tokens':sum(item.get('total_tokens',0) or 0 for item in known) if known else None,
        'complete':not in_flight and bool(calls) and len(known)==len(calls) and not any(item['kind']=='model_error' for item in trace)}


def error_info(error):
    from agent.notification_extractor import ExtractionFailure
    import asyncio
    if isinstance(error,asyncio.CancelledError):return 'interrupted',503,'处理已中断，请查看记录后重试'
    if isinstance(error,ExtractionFailure):return 'model_or_validation_error',error.status_code,str(error)
    return 'unexpected_error',500,f'处理发生内部错误（{type(error).__name__}），本次未完成，请查看记录后重试'


def log_event(kind,attempt_id,phase,**values):
    # No original notification, prompt, user answer, tool arguments or credentials.
    logger.info('processing',extra={'fields':{'kind':kind,'attempt_id':str(attempt_id),'phase':phase,**values}})
