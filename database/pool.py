"""Role-specific pools with bounded acquisition and query waits."""
from contextlib import asynccontextmanager
from time import monotonic
import logging
import os
import asyncpg
from database.config import database_url,load_settings
from core.job_settings import positive,worker_slots
from core.observation import record_wait

logger=logging.getLogger(__name__)

class BoundedPool:
    """Only the asyncpg operations used by this project; acquire is a context manager."""
    def __init__(self,pool,role):
        self.pool,self.role=pool,role
        self.acquire_timeout=positive('DB_ACQUIRE_TIMEOUT',5)
        self.wait_count=0;self.wait_seconds=0.;self.wait_max_seconds=0.
    @asynccontextmanager
    async def acquire(self):
        started=monotonic();success=False
        try:
            conn=await self.pool.acquire(timeout=self.acquire_timeout);success=True
        finally:
            elapsed=monotonic()-started
            record_wait(elapsed,success)
            self.wait_count+=1;self.wait_seconds+=elapsed;self.wait_max_seconds=max(self.wait_max_seconds,elapsed)
        try:yield conn
        finally:await self.pool.release(conn)
    async def fetch(self,*args,**kwargs):
        async with self.acquire() as conn:return await conn.fetch(*args,**kwargs)
    async def fetchrow(self,*args,**kwargs):
        async with self.acquire() as conn:return await conn.fetchrow(*args,**kwargs)
    async def fetchval(self,*args,**kwargs):
        async with self.acquire() as conn:return await conn.fetchval(*args,**kwargs)
    async def execute(self,*args,**kwargs):
        async with self.acquire() as conn:return await conn.execute(*args,**kwargs)
    def get_size(self):return self.pool.get_size()
    def get_idle_size(self):return self.pool.get_idle_size()
    def get_max_size(self):return self.pool.get_max_size()
    def metrics(self):return {'role':self.role,'size':self.get_size(),'idle':self.get_idle_size(),'max':self.get_max_size(),'acquires':self.wait_count,'wait_seconds':round(self.wait_seconds,6),'max_wait_seconds':round(self.wait_max_seconds,6)}
    async def close(self):await self.pool.close()

async def create_pool(*,dsn=None,min_size=1,max_size=None,role='api'):
    load_settings()
    defaults={'api':5,'worker':8,'scheduler':3}
    if role not in defaults:raise ValueError('未知数据库连接角色')
    maximum=max_size if max_size is not None else positive(role.upper()+'_DB_POOL_MAX',defaults[role])
    if role=='worker' and maximum<worker_slots()+2:raise ValueError('worker连接池至少需要WORKER_CONCURRENCY+2条连接')
    pool=await asyncpg.create_pool(database_url(dsn),min_size=min_size,max_size=maximum,
        command_timeout=positive('DB_QUERY_TIMEOUT',30),
        server_settings={'timezone':'UTC','application_name':'job-application-agent:'+role})
    return BoundedPool(pool,role)
