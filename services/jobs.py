"""Durable submission and short-transaction claims; no background tasks in API."""
from contextlib import asynccontextmanager
import hashlib
import json
from uuid import uuid4
from core.job_settings import positive,lease_seconds,max_attempts
from schemas.jobs import JobCreate
from core.logging import origin_request_id
from services.errors import Conflict,NotFound


def job_row(row):
    value=dict(row)
    for key in ['owner_id','run_token','worker_id','input_key','deadline_at']:value.pop(key,None)
    if isinstance(value['result'],str):value['result']=json.loads(value['result'])
    return value

async def input_key(conn,owner,nid):
    return await conn.fetchval("SELECT id::text||':'||version FROM notification_extractions WHERE owner_id=$1 AND notification_id=$2 ORDER BY created_at DESC,id DESC LIMIT 1",owner,nid) or '0'

class QueueFull(Conflict):status_code=429

async def input_version(conn,owner,nid):
    return await conn.fetchval('SELECT version FROM notification_extractions WHERE owner_id=$1 AND notification_id=$2 ORDER BY created_at DESC,id DESC LIMIT 1',owner,nid) or 0

class JobService:
    def __init__(self,pool,owner,connection=None):self.pool,self.owner,self.connection=pool,owner,connection
    @asynccontextmanager
    async def transaction(self):
        if self.connection:
            async with self.connection.transaction():yield self.connection
        else:
            async with self.pool.acquire() as conn,conn.transaction():yield conn

    async def submit(self,body:JobCreate):
        fingerprint=hashlib.sha256(body.model_dump_json(exclude={'request_id'}).encode()).hexdigest()
        async with self.transaction() as conn:
            # Serialize short submissions for this personal workspace, never model execution.
            await conn.execute('SELECT pg_advisory_xact_lock($1)',470218332)
            replay=await conn.fetchrow('SELECT * FROM job_requests WHERE owner_id=$1 AND request_id=$2',self.owner,body.request_id)
            if replay:
                if replay['fingerprint']!=fingerprint:raise Conflict('请求编号已用于不同内容')
                return job_row(await conn.fetchrow('SELECT * FROM jobs WHERE id=$1',replay['job_id']))
            exists=await conn.fetchval('SELECT 1 FROM notifications WHERE owner_id=$1 AND id=$2',self.owner,body.notification_id)
            if not exists:raise NotFound('通知不存在')
            latest=await conn.fetchrow('SELECT id,version,status FROM notification_extractions WHERE owner_id=$1 AND notification_id=$2 ORDER BY created_at DESC,id DESC LIMIT 1',self.owner,body.notification_id)
            version=latest['version'] if latest else 0
            if body.input_version is not None and body.input_version!=version:raise Conflict('识别结果已变化，请刷新后提交')
            key=str(latest['id'])+':'+str(latest['version']) if latest else '0'
            row=await conn.fetchrow('SELECT * FROM jobs WHERE owner_id=$1 AND notification_id=$2 AND operation=$3 AND input_key=$4',self.owner,body.notification_id,body.operation,key)
            if row is None:
                await capacity(conn,self.owner)
                await check_model_quota(conn,self.owner,body.notification_id,latest)
                row=await conn.fetchrow("""INSERT INTO jobs(id,owner_id,notification_id,operation,input_version,input_key)
                    VALUES($1,$2,$3,$4,$5,$6) RETURNING *""",uuid4(),self.owner,body.notification_id,body.operation,version,key)
            await conn.execute('INSERT INTO job_requests(owner_id,request_id,fingerprint,job_id,http_request_id) VALUES($1,$2,$3,$4,$5)',self.owner,body.request_id,fingerprint,row['id'],origin_request_id())
            return job_row(row)

    async def get(self,jid):
        row=await self.pool.fetchrow('SELECT * FROM jobs WHERE owner_id=$1 AND id=$2',self.owner,jid)
        if row is None:raise NotFound('任务不存在')
        return job_row(row)

    async def list(self,nid,limit,offset):
        where='owner_id=$1 AND ($2::uuid IS NULL OR notification_id=$2)'
        async with self.pool.acquire() as conn:
            rows=await conn.fetch('SELECT * FROM jobs WHERE '+where+' ORDER BY created_at DESC,id DESC LIMIT $3 OFFSET $4',self.owner,nid,limit,offset)
            total=await conn.fetchval('SELECT count(*) FROM jobs WHERE '+where,self.owner,nid)
        return {'items':[job_row(r) for r in rows],'total':total,'limit':limit,'offset':offset}

    async def cancel(self,jid):
        async with self.transaction() as conn:
            row=await conn.fetchrow('SELECT * FROM jobs WHERE owner_id=$1 AND id=$2 FOR UPDATE',self.owner,jid)
            if row is None:raise NotFound('任务不存在')
            if row['status']=='queued':
                await conn.execute("UPDATE jobs SET status='cancelled',cancel_requested=true,finished_at=now() WHERE id=$1",jid)
            elif row['status']=='running':
                await conn.execute('UPDATE jobs SET cancel_requested=true WHERE id=$1',jid)
            elif row['status']=='succeeded':raise Conflict('任务已经完成；已保存业务请使用撤销')
        return await self.get(jid)

    async def retry(self,jid,request_id):
        # Retrying requires the latest input; it does not erase attempts or saved business.
        async with self.transaction() as conn:
            await conn.execute('SELECT pg_advisory_xact_lock($1)',470218332)
            replay=await conn.fetchrow('SELECT * FROM job_requests WHERE owner_id=$1 AND request_id=$2',self.owner,request_id)
            fingerprint='retry:'+str(jid)
            if replay:
                if replay['fingerprint']!=fingerprint:raise Conflict('请求编号已用于不同内容')
                return job_row(await conn.fetchrow('SELECT * FROM jobs WHERE id=$1',replay['job_id']))
            row=await conn.fetchrow('SELECT * FROM jobs WHERE owner_id=$1 AND id=$2 FOR UPDATE',self.owner,jid)
            if row is None:raise NotFound('任务不存在')
            if row['status'] not in {'failed','cancelled'}:raise Conflict('只能重试已失败或已取消的任务')
            latest=await conn.fetchrow('SELECT id,version,status,job_id FROM notification_extractions WHERE owner_id=$1 AND notification_id=$2 ORDER BY created_at DESC,id DESC LIMIT 1',self.owner,row['notification_id'])
            await capacity(conn,self.owner)
            await check_model_quota(conn,self.owner,row['notification_id'],latest)
            key=str(latest['id'])+':'+str(latest['version']) if latest else '0'
            if key!=row['input_key'] and (not latest or latest['job_id']!=jid):raise Conflict('输入已变化，请按最新版本提交新任务')
            await conn.execute("UPDATE jobs SET status='queued',cancel_requested=false,error=NULL,error_kind=NULL,retryable=false,run_token=NULL,lease_until=NULL,available_at=now(),deadline_at=now()+interval '15 minutes',finished_at=NULL WHERE id=$1",jid)
            await conn.execute('INSERT INTO job_requests(owner_id,request_id,fingerprint,job_id,http_request_id) VALUES($1,$2,$3,$4,$5)',self.owner,request_id,fingerprint,jid,origin_request_id())
        return await self.get(jid)

async def capacity(conn,owner):
    counts=await conn.fetchrow("SELECT count(*) AS global_count,count(*) FILTER(WHERE owner_id=$1) AS owner_count FROM jobs WHERE status IN ('queued','running')",owner)
    if counts['global_count']>=positive('JOB_QUEUE_LIMIT',200) or counts['owner_count']>=positive('JOB_OWNER_QUEUE_LIMIT',100):
        raise QueueFull('后台队列已满，原通知已保留，请稍后重新提交')

async def claim(pool,worker_id='worker'):
    async with pool.acquire() as conn,conn.transaction():
        # Serialize short scheduling transactions to enforce a DB-wide execution bound.
        await conn.execute('SELECT pg_advisory_xact_lock($1)',470218333)
        expired=await conn.fetch("SELECT * FROM jobs WHERE status='running' AND lease_until<=clock_timestamp() FOR UPDATE SKIP LOCKED")
        for row in expired:
            await conn.execute("UPDATE job_attempts SET status='expired',error_kind='lease_expired',finished_at=now() WHERE id=$1 AND status='running'",row['run_token'])
            await conn.execute("UPDATE notification_extractions SET status='failed',phase='interrupted',elapsed_ms=greatest(0,(extract(epoch FROM now()-created_at)*1000)::bigint),error_kind='lease_expired',error_status=503,error_message='执行租约已过期',finished_at=now() WHERE job_id=$1 AND job_token=$2 AND status='processing'",row['id'],row['run_token'])
            terminal=row['cancel_requested'] or row['attempt_count']>=max_attempts() or await conn.fetchval('SELECT $1::timestamptz<=clock_timestamp()',row['deadline_at'])
            status='cancelled' if row['cancel_requested'] else 'failed' if terminal else 'queued'
            await conn.execute("UPDATE jobs SET status=$2,lease_until=NULL,run_token=NULL,error_kind='lease_expired',error='前次执行中断，结果与用量可能未知',finished_at=CASE WHEN $3 THEN now() END WHERE id=$1",row['id'],status,terminal)
        active=await conn.fetchval("SELECT count(*) FROM jobs WHERE status='running' AND lease_until>clock_timestamp()")
        if active>=positive('MODEL_CONCURRENCY_LIMIT',2):return None
        row=await conn.fetchrow("SELECT * FROM jobs WHERE status='queued' AND available_at<=clock_timestamp() ORDER BY available_at,created_at,id FOR UPDATE SKIP LOCKED LIMIT 1")
        if not row:return None
        if await conn.fetchval('SELECT $1::timestamptz<=clock_timestamp()',row['deadline_at']):
            await conn.execute("UPDATE jobs SET status='failed',error_kind='deadline_exceeded',error='任务等待超过整体时间预算，请按需重试',finished_at=now() WHERE id=$1",row['id'])
            return None
        token=uuid4()
        row=await conn.fetchrow("""UPDATE jobs SET status='running',run_token=$2,worker_id=$3,attempt_count=attempt_count+1,
            started_at=now(),finished_at=NULL,lease_until=clock_timestamp()+$4*interval '1 second' WHERE id=$1 RETURNING *""",row['id'],token,worker_id,lease_seconds())
        await conn.execute("INSERT INTO job_attempts(id,job_id,number,worker_id,status) VALUES($1,$2,$3,$4,'running')",token,row['id'],row['attempt_count'],worker_id)
        return row

async def check_model_quota(conn,owner,nid,latest):
    # Admission is a quick rejection only. The authoritative counter is charged
    # in the worker immediately before each actual new model attempt.
    if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM automatic_records WHERE owner_id=$1 AND notification_id=$2 AND status IN ('saved','undone'))",owner,nid):return
    if latest and latest['status']=='completed':return
    used=await conn.fetchval("SELECT count FROM model_admissions WHERE owner_id=$1 AND hour=date_trunc('hour',clock_timestamp())",owner) or 0
    if used>=positive('MODEL_REQUESTS_PER_HOUR',30):
        from core.access import CallLimit
        raise CallLimit('本小时模型处理次数已达到配置上限，请下个整点后再试')
