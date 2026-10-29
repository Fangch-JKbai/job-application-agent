"""Independent durable IMAP scheduler. No model calls in this process."""
import asyncio
from contextlib import suppress
import logging
import os
import signal
from uuid import uuid4
from core.job_settings import positive
from core.logging import origin_request_id,log_context
from database.pool import create_pool
from services.errors import Conflict,NotFound
from services.mailboxes import MailboxService
from services.mail_transport import read_mail
import core.logging

logger=logging.getLogger(__name__)

async def request_sync(pool,owner,account_id,only_if_due=False):
    async with pool.acquire() as conn,conn.transaction():
        account=await conn.fetchrow('SELECT *,next_sync_at<=clock_timestamp() AS due FROM mailbox_accounts WHERE owner_id=$1 AND id=$2 FOR UPDATE',owner,account_id)
        if not account:raise NotFound('邮箱账户不存在')
        if only_if_due and (not account['enabled'] or not account['due']):return None
        row=await conn.fetchrow("""INSERT INTO mail_sync_jobs(owner_id,account_id,origin_request_id) VALUES($1,$2,$3)
            ON CONFLICT(account_id) WHERE status IN ('queued','running') DO NOTHING RETURNING id,status""",owner,account_id,origin_request_id())
        if row is None:row=await conn.fetchrow("SELECT id,status FROM mail_sync_jobs WHERE account_id=$1 AND status IN ('queued','running')",account_id)
        await conn.execute("UPDATE mailbox_accounts SET next_sync_at=clock_timestamp()+$2*interval '1 second' WHERE id=$1",account_id,positive('MAIL_POLL_SECONDS',43200))
        return {'id':str(row['id']),'status':row['status']}

async def claim_sync(pool):
    async with pool.acquire() as conn,conn.transaction():
        row=await conn.fetchrow("""SELECT * FROM mail_sync_jobs WHERE
          (status='queued' AND available_at<=clock_timestamp()) OR (status='running' AND lease_until<=clock_timestamp())
          ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1""")
        if not row:return None
        return await conn.fetchrow("UPDATE mail_sync_jobs SET status='running',run_token=$2,lease_until=clock_timestamp()+$3*interval '1 second' WHERE id=$1 RETURNING *",row['id'],uuid4(),positive('MAIL_SYNC_LEASE_SECONDS',120))

async def _perform(pool,job,transport=read_mail):
    async def heartbeat(conn):
        value=await conn.fetchval("UPDATE mail_sync_jobs SET lease_until=clock_timestamp()+$3*interval '1 second' WHERE id=$1 AND run_token=$2 AND status='running' AND lease_until>clock_timestamp() RETURNING id",job['id'],job['run_token'],positive('MAIL_SYNC_LEASE_SECONDS',120))
        if not value:raise Conflict('邮箱同步执行权已变化')
    status='succeeded';error=None;again=False
    task=asyncio.create_task(MailboxService(pool,job['owner_id'],transport=transport).sync(job['account_id'],heartbeat=heartbeat))
    async def keepalive():
        try:
            while not task.done():
                await asyncio.sleep(max(.2,positive('MAIL_SYNC_LEASE_SECONDS',120)/3))
                async with pool.acquire() as conn:await heartbeat(conn)
        except Exception:
            task.cancel()
            raise
    beat=asyncio.create_task(keepalive())
    try:await task
    except (Conflict,asyncio.CancelledError):again=True;status='queued'
    except Exception as exc:
        status='failed';error='邮箱同步失败：'+type(exc).__name__+'，请检查邮箱配置或连接'
        logger.warning('mail_sync_failed',extra={'fields':{'sync_id':str(job['id']),'error_type':type(exc).__name__}})
    finally:
        beat.cancel()
        with suppress(asyncio.CancelledError,Conflict):await beat
    await pool.execute("""UPDATE mail_sync_jobs SET status=$3,error=$4,lease_until=NULL,
        available_at=clock_timestamp()+interval '1 second',finished_at=CASE WHEN $5 THEN NULL ELSE now() END
        WHERE id=$1 AND run_token=$2 AND status='running'""",job['id'],job['run_token'],status,error,again)

async def perform(pool,job,transport=read_mail):
    with log_context(origin_request_id=job['origin_request_id'],mail_sync_id=str(job['id'])):
        return await _perform(pool,job,transport)

async def tick(pool,transport=read_mail):
    # The database clock is the shared wakeup signal for every Web process.
    accounts=await pool.fetch('SELECT owner_id,id FROM mailbox_accounts WHERE enabled AND next_sync_at<=clock_timestamp()')
    for row in accounts:
        try:await request_sync(pool,row['owner_id'],row['id'],only_if_due=True)
        except NotFound:pass
    job=await claim_sync(pool)
    if job:await perform(pool,job,transport)
    rows=await pool.fetch("SELECT id,owner_id FROM mailbox_accounts WHERE enabled")
    for row in rows:
        try:await MailboxService(pool,row['owner_id'],transport=transport).enqueue_cached(row['id'])
        except Exception as exc:logger.warning('mail_enqueue_failed',extra={'fields':{'error_type':type(exc).__name__}})
    await pool.execute("""UPDATE mailbox_messages m SET auto_state=CASE WHEN j.status='succeeded' THEN 'completed'
      WHEN j.status IN ('failed','cancelled') THEN 'failed' ELSE 'processing' END,
      processing_error=CASE WHEN j.status IN ('failed','cancelled') THEN coalesce(j.error,'后台处理未完成，请打开通知查看') ELSE NULL END
      FROM jobs j WHERE m.processing_job_id=j.id AND (m.auto_state IS DISTINCT FROM CASE WHEN j.status='succeeded' THEN 'completed'
      WHEN j.status IN ('failed','cancelled') THEN 'failed' ELSE 'processing' END OR m.processing_error IS DISTINCT FROM j.error)""")
    return job is not None

async def run(stop=None,transport=read_mail):
    from database.config import load_settings
    load_settings()
    stop=stop or asyncio.Event()
    loop=asyncio.get_running_loop()
    for sig in (signal.SIGINT,signal.SIGTERM):loop.add_signal_handler(sig,stop.set)
    if os.environ.get('MAIL_POLL_ENABLED','true').lower()!='true':
        await stop.wait()
        return
    pool=await create_pool(role='scheduler')
    from services.runtime_observation import runtime
    try:
      from core.runtime_settings import validate
      from core.recovery_guard import require_active
      validate()
      await require_active(pool)
      async with runtime(pool,'scheduler'):
        while not stop.is_set():
            busy=await tick(pool,transport)
            if not busy:
                try:await asyncio.wait_for(stop.wait(),timeout=positive('MAIL_SCHEDULER_TICK_SECONDS',5))
                except TimeoutError:pass
    finally:await pool.close()

if __name__=='__main__':asyncio.run(run())
