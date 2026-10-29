"""Lease-bound workers; model execution and business writes use fenced attempts."""
import asyncio
from contextlib import suppress
import json
import logging
import os
import signal
import socket
from uuid import uuid4
from agent.notification_extractor import extract_notification
from core.job_settings import lease_seconds,max_attempts,worker_slots,positive
from core.logging import log_context,error_fields
from core.observation import measure_waits
from database.pool import create_pool
from services.automatic import AutomaticService
from services.errors import ServiceError
from services.jobs import claim
from services.job_execution import Execution,LeaseLost,JobCancelled

logger=logging.getLogger(__name__)

async def _execute(pool,job,extractor):
    execution=Execution(job)
    result=None;error=None;kind=None;retryable=False;delay=None;cancelled=False
    logger.info('job_started',extra={'fields':{'job_id':str(job['id']),'attempt_id':str(job['run_token']),'worker_id':job['worker_id']}})
    try:
        async with asyncio.timeout(positive('JOB_EXECUTION_TIMEOUT',90)):
            result=await AutomaticService(pool,job['owner_id'],extractor,execution=execution).process(job['notification_id'])
        if result['status'] not in {'saved','unresolved','ignored','undone'}:
            error=result['message'];kind='extraction_failed'
            ex=await pool.fetchrow('SELECT retryable,retry_after_seconds,error_kind FROM notification_extractions WHERE job_id=$1 ORDER BY created_at DESC,id DESC LIMIT 1',job['id'])
            if ex:retryable=ex['retryable'];delay=ex['retry_after_seconds'];kind=ex['error_kind']
    except LeaseLost:return
    except JobCancelled:
        cancelled=True;kind='cancelled';error='已取消；已发生的模型用量可能未知'
    except asyncio.CancelledError:
        kind='interrupted';error='执行已中断，用量可能未知';retryable=True
    except TimeoutError:
        error='本次执行超时，用量可能未知';kind='timeout';retryable=True
    except ServiceError as exc:
        error=str(exc);kind='admission_limit' if exc.status_code==429 else 'input_or_service_error'
        retryable=False  # Local admission/config/input errors need a deliberate later retry.
    except Exception as exc:
        error='处理异常，请按任务编号检查服务日志';kind='internal_error'
        logger.error('job_execution_failed',extra={'fields':{'job_id':str(job['id']),**error_fields(exc)}})
    try:
        async with pool.acquire() as conn,conn.transaction():
            current=await conn.fetchrow('SELECT status,run_token FROM jobs WHERE id=$1 FOR UPDATE',job['id'])
            # The business transaction may already have atomically completed the job.
            if current['status']=='succeeded':return
            row=await execution.guard(conn,allow_cancel=True)
            cancelled=cancelled or row['cancel_requested']
            if not error and not cancelled:
                await execution.finish(conn,result)
                return
            delay=max(1,float(delay or min(30,2**job['attempt_count'])))
            deadline=await conn.fetchval("SELECT clock_timestamp()+$1*interval '1 second'<$2::timestamptz",delay,row['deadline_at'])
            again=retryable and not cancelled and job['attempt_count']<max_attempts() and deadline
            if cancelled:
                await conn.execute("UPDATE notification_extractions SET status='failed',phase='interrupted',elapsed_ms=greatest(0,(extract(epoch FROM now()-created_at)*1000)::bigint),error_kind='cancelled',error_status=409,error_message='任务已取消',finished_at=now() WHERE job_id=$1 AND job_token=$2 AND status='processing'",job['id'],job['run_token'])
            status='cancelled' if cancelled else 'queued' if again else 'failed'
            await conn.execute("""UPDATE jobs SET status=$2,error=$3,error_kind=$4,retryable=$5,result=$6::jsonb,
                available_at=clock_timestamp()+$7*interval '1 second',lease_until=NULL,run_token=NULL,
                finished_at=CASE WHEN $2='queued' THEN NULL ELSE now() END WHERE id=$1""",job['id'],status,error,kind,retryable,json.dumps(result,default=str,ensure_ascii=False) if result else None,delay)
            await conn.execute("UPDATE job_attempts SET status=$2,error_kind=$3,finished_at=now() WHERE id=$1",job['run_token'],'cancelled' if cancelled else 'failed',kind)
            logger.info('job_finished',extra={'fields':{'job_id':str(job['id']),'attempt_id':str(job['run_token']),'status':status,'error_kind':kind}})
    except LeaseLost:return

async def execute(pool,job,extractor):
    with log_context(correlation_id=str(job['id']),job_id=str(job['id']),job_attempt_id=str(job['run_token']),notification_id=str(job['notification_id']),worker_id=job['worker_id']),measure_waits() as waits:
        origin=await pool.fetchval('SELECT http_request_id FROM job_requests WHERE job_id=$1 AND http_request_id IS NOT NULL ORDER BY created_at,request_id LIMIT 1',job['id'])
        with log_context(origin_request_id=origin):
            try:return await _execute(pool,job,extractor)
            finally:
                try:
                    status=await pool.fetchval('SELECT status FROM job_attempts WHERE id=$1',job['run_token'])
                    logger.info('job_attempt_observed',extra={'fields':{'status':status,'db_wait':waits}})
                except Exception as exc:logger.warning('job_observation_failed',extra={'fields':error_fields(exc)})

async def heartbeat(pool,job,task):
    try:
        while not task.done():
            await asyncio.sleep(max(.2,lease_seconds()/3))
            row=await pool.fetchrow("""UPDATE jobs SET lease_until=clock_timestamp()+$3*interval '1 second'
                WHERE id=$1 AND run_token=$2 AND status='running' AND lease_until>clock_timestamp() RETURNING cancel_requested""",job['id'],job['run_token'],lease_seconds())
            if row is None or row['cancel_requested']:
                task.cancel();return
    except Exception:
        task.cancel()
        raise

async def run(stop=None,extractor=extract_notification):
    stop=stop or asyncio.Event()
    loop=asyncio.get_running_loop()
    for sig in (signal.SIGINT,signal.SIGTERM):loop.add_signal_handler(sig,stop.set)
    pool=await create_pool(role='worker')
    worker_id=socket.gethostname()+':'+str(os.getpid())+':'+uuid4().hex[:6]
    async def slot(number):
        while not stop.is_set():
            job=await claim(pool,worker_id+':'+str(number))
            if job:
                task=asyncio.create_task(execute(pool,job,extractor))
                beat=asyncio.create_task(heartbeat(pool,job,task))
                try:await task
                finally:
                    beat.cancel()
                    with suppress(asyncio.CancelledError):await beat
            else:
                try:await asyncio.wait_for(stop.wait(),timeout=.25)
                except TimeoutError:pass
    from services.runtime_observation import runtime
    try:
      from core.runtime_settings import validate
      from core.recovery_guard import require_active
      validate()
      await require_active(pool)
      async with runtime(pool,'worker',worker_key=worker_id):
        async with asyncio.TaskGroup() as group:
            for i in range(worker_slots()):group.create_task(slot(i))
    finally:await pool.close()

if __name__=='__main__':asyncio.run(run())
