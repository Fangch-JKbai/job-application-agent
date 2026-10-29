"""Fencing is checked while holding the job row, inside each write transaction."""
from contextlib import asynccontextmanager
import json
from services.errors import Conflict

class LeaseLost(Conflict):pass
class JobCancelled(Conflict):pass

class Execution:
    def __init__(self,job):self.job=job
    async def guard(self,conn,allow_cancel=False):
        row=await conn.fetchrow('SELECT *,lease_until>clock_timestamp() AS live FROM jobs WHERE id=$1 FOR UPDATE',self.job['id'])
        if not row or row['status']!='running' or row['run_token']!=self.job['run_token'] or not row['live']:
            raise LeaseLost('任务执行权已失效，丢弃旧执行结果')
        if row['cancel_requested'] and not allow_cancel:raise JobCancelled('任务已请求取消')
        return row
    @asynccontextmanager
    async def transaction(self,conn):
        async with conn.transaction():
            await self.guard(conn)
            yield
    async def finish(self,conn,result):
        await self.guard(conn)
        await conn.execute("UPDATE jobs SET status='succeeded',result=$2::jsonb,error=NULL,error_kind=NULL,retryable=false,finished_at=now(),lease_until=NULL WHERE id=$1",self.job['id'],json.dumps(result,ensure_ascii=False,default=str))
        await conn.execute("UPDATE job_attempts SET status='succeeded',finished_at=now() WHERE id=$1",self.job['run_token'])

@asynccontextmanager
async def write_transaction(conn,execution=None):
    async with conn.transaction():
        if execution:await execution.guard(conn)
        yield
