"""Manual application/task services, shared with future Agent tools."""
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from uuid import UUID
from zoneinfo import ZoneInfo

import asyncpg
from pydantic import ValidationError

from schemas.recruitment import (
    ApplicationCreate,ApplicationFields,ApplicationPatch,
    TaskCreate,TaskFields,TaskPatch,MUTATION_FIELDS,STAGE_ORDER,
)
from services.errors import Conflict,InvalidInput,NotFound


def snapshot(row) -> dict:
    def encode(value):
        if isinstance(value,(date,datetime)):
            return value.isoformat()
        return str(value)
    return json.loads(json.dumps(dict(row),default=encode,ensure_ascii=False))


def request_fingerprint(action, target, body) -> str:
    value = {'action':action,'target':str(target) if target else None,'body':body.model_dump(mode='json',exclude={'request_id'},exclude_unset=True)}
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


class RecruitmentService:
    def __init__(self,pool:asyncpg.Pool,owner_id:UUID,connection=None,source_reference=None):
        self.pool = pool
        self.owner_id = owner_id
        self.connection = connection
        self.source_reference = source_reference

    @asynccontextmanager
    async def _transaction(self):
        if self.connection is not None:
            yield self.connection
        else:
            async with self.pool.acquire() as conn,conn.transaction():
                yield conn

    async def lock_application_creation(self,conn):
        # P3 manual creation and P6 confirmation share this short transaction lock.
        lock = int.from_bytes(hashlib.sha256(f'application-create:{self.owner_id}'.encode()).digest()[:8],signed=True)
        await conn.execute('SELECT pg_advisory_xact_lock($1)',lock)

    async def _previous_request(self,conn,request_id,fingerprint):
        lock = int.from_bytes(hashlib.sha256(f'{self.owner_id}:{request_id}'.encode()).digest()[:8],signed=True)
        await conn.execute('SELECT pg_advisory_xact_lock($1)',lock)
        row = await conn.fetchrow('SELECT request_fingerprint,after_data FROM application_events WHERE owner_id=$1 AND operation_id=$2 AND event_index=0',self.owner_id,request_id)
        if row:
            if row['request_fingerprint'] != fingerprint:
                raise Conflict('该请求编号已经用于另一项操作，请为新操作生成新编号')
            return json.loads(row['after_data'])
        return None

    async def _event(self,conn,request_id,fingerprint,event_type,after,before=None,reason=None,task=False):
        await conn.execute('''INSERT INTO application_events(
            owner_id,application_id,task_id,event_type,actor_type,source_kind,reason,
            before_data,after_data,operation_id,request_fingerprint,source_reference)
            VALUES($1,$2,$3,$4,$12,$5,$6,$7::jsonb,$8::jsonb,$9,$10,$11)''',
            self.owner_id,after['application_id'] if task else after['id'],after['id'] if task else None,
            'user_correction' if reason else event_type,'notification' if self.source_reference else ('user_correction' if reason else 'manual'),reason,
            json.dumps(snapshot(before),ensure_ascii=False) if before is not None else None,
            json.dumps(snapshot(after),ensure_ascii=False),request_id,fingerprint,self.source_reference,'system' if self.source_reference and self.source_reference.startswith('automatic:') else 'local_user')

    async def _application(self,conn,application_id,lock=False):
        row = await conn.fetchrow('SELECT * FROM applications WHERE id=$1 AND owner_id=$2' + (' FOR UPDATE' if lock else ''),application_id,self.owner_id)
        if row is None:
            raise NotFound('申请不存在')
        return dict(row)

    async def _task(self,conn,task_id):
        row = await conn.fetchrow('SELECT * FROM tasks WHERE id=$1 AND owner_id=$2',task_id,self.owner_id)
        if row is None:
            raise NotFound('待办不存在')
        return dict(row)

    async def create_application(self,body:ApplicationCreate):
        fingerprint = request_fingerprint('application.create',None,body)
        async with self._transaction() as conn:
            replay = await self._previous_request(conn,body.request_id,fingerprint)
            if replay is not None:
                return replay
            await self.lock_application_creation(conn)
            values = body.model_dump(exclude={'request_id'})
            columns = list(values)
            placeholders = ','.join(f'${n}' for n in range(2,len(values)+2))
            row = dict(await conn.fetchrow(f'INSERT INTO applications(owner_id,{",".join(columns)}) VALUES($1,{placeholders}) RETURNING *',self.owner_id,*values.values()))
            await self._event(conn,body.request_id,fingerprint,'application_created',row)
            return row

    async def update_application(self,application_id,body:ApplicationPatch):
        fingerprint = request_fingerprint('application.update',application_id,body)
        async with self._transaction() as conn:
            replay = await self._previous_request(conn,body.request_id,fingerprint)
            if replay is not None:
                return replay
            before = await self._application(conn,application_id)
            if before['version'] != body.version:
                raise Conflict('申请已被修改，请刷新后使用最新版本')
            values = {name:before[name] for name in ApplicationFields.model_fields}
            values.update(body.model_dump(exclude_unset=True,exclude=MUTATION_FIELDS))
            try:
                values = ApplicationFields(**values).model_dump()
            except ValidationError as error:
                raise InvalidInput(error.errors()[0]['msg']) from error
            backwards = STAGE_ORDER.index(values['stage']) < STAGE_ORDER.index(before['stage'])
            reopening = before['outcome'] != 'active' and values['outcome'] != before['outcome']
            if (backwards or reopening) and not body.correction_reason:
                raise InvalidInput('回退阶段或更改已结束的申请，请填写 correction_reason')
            assignments = ','.join(f'{name}=${n}' for n,name in enumerate(values,4))
            row = await conn.fetchrow(f'UPDATE applications SET {assignments},version=version+1,updated_at=now() WHERE id=$1 AND owner_id=$2 AND version=$3 RETURNING *',application_id,self.owner_id,body.version,*values.values())
            if row is None:
                raise Conflict('申请版本已变化，请刷新后再编辑')
            row = dict(row)
            await self._event(conn,body.request_id,fingerprint,'application_updated',row,before,body.correction_reason)
            return row

    async def create_task(self,body:TaskCreate):
        fingerprint = request_fingerprint('task.create',body.application_id,body)
        async with self._transaction() as conn:
            replay = await self._previous_request(conn,body.request_id,fingerprint)
            if replay is not None:
                return replay
            application = await self._application(conn,body.application_id,lock=True)
            if application['outcome'] != 'active':
                raise Conflict('申请已经结束，请先核对并更正申请结果')
            values = body.model_dump(exclude={'request_id'})
            columns = list(values)
            placeholders = ','.join(f'${n}' for n in range(2,len(values)+2))
            row = dict(await conn.fetchrow(f'INSERT INTO tasks(owner_id,{",".join(columns)}) VALUES($1,{placeholders}) RETURNING *',self.owner_id,*values.values()))
            await self._event(conn,body.request_id,fingerprint,'task_created',row,task=True)
            return row

    async def update_task(self,task_id,body:TaskPatch):
        fingerprint = request_fingerprint('task.update',task_id,body)
        async with self._transaction() as conn:
            replay = await self._previous_request(conn,body.request_id,fingerprint)
            if replay is not None:
                return replay
            before = await self._task(conn,task_id)
            if before['version'] != body.version:
                raise Conflict('待办已被修改，请刷新后使用最新版本')
            values = {name:before[name] for name in TaskFields.model_fields}
            values.update(body.model_dump(exclude_unset=True,exclude=MUTATION_FIELDS))
            try:
                values = TaskFields(**values).model_dump()
            except ValidationError as error:
                raise InvalidInput(error.errors()[0]['msg']) from error
            if before['status'] in {'completed','cancelled'} and values['status'] != before['status'] and not body.correction_reason:
                raise InvalidInput('更改已完成或已取消的待办，请填写 correction_reason')
            assignments = ','.join(f'{name}=${n}' for n,name in enumerate(values,4))
            row = await conn.fetchrow(f'UPDATE tasks SET {assignments},version=version+1,updated_at=now() WHERE id=$1 AND owner_id=$2 AND version=$3 RETURNING *',task_id,self.owner_id,body.version,*values.values())
            if row is None:
                raise Conflict('待办版本已变化，请刷新后再编辑')
            row = dict(row)
            event_type = 'task_cancelled' if values['status'] == 'cancelled' else 'task_updated'
            await self._event(conn,body.request_id,fingerprint,event_type,row,before,body.correction_reason,task=True)
            return row

    async def get_application(self,application_id):
        async with self.pool.acquire() as conn:
            return await self._application(conn,application_id)

    async def get_task(self,task_id):
        async with self.pool.acquire() as conn:
            return await self._task(conn,task_id)

    async def list_applications(self,company=None,position=None,stage=None,outcome=None,limit=50,offset=0):
        clauses,values = ['owner_id=$1'],[self.owner_id]
        for column,value,partial in [('company_name',company,True),('position_name',position,True),('stage',stage,False),('outcome',outcome,False)]:
            if value is not None:
                values.append(value)
                clauses.append(f'strpos(lower({column}),lower(${len(values)}))>0' if partial else f'{column}=${len(values)}')
        where = ' AND '.join(clauses)
        async with self.pool.acquire() as conn:
            total = await conn.fetchval(f'SELECT count(*) FROM applications WHERE {where}',*values)
            rows = await conn.fetch(f'SELECT * FROM applications WHERE {where} ORDER BY created_at DESC,id DESC LIMIT ${len(values)+1} OFFSET ${len(values)+2}',*values,limit,offset)
        return {'items':[dict(row) for row in rows],'total':total,'limit':limit,'offset':offset}

    async def list_tasks(self,application_id=None,status=None,type=None,limit=50,offset=0):
        clauses,values = ['owner_id=$1'],[self.owner_id]
        for column,value in [('application_id',application_id),('status',status),('type',type)]:
            if value is not None:
                values.append(value)
                clauses.append(f'{column}=${len(values)}')
        where = ' AND '.join(clauses)
        async with self.pool.acquire() as conn:
            total = await conn.fetchval(f'SELECT count(*) FROM tasks WHERE {where}',*values)
            rows = await conn.fetch(f'SELECT * FROM tasks WHERE {where} ORDER BY created_at DESC,id DESC LIMIT ${len(values)+1} OFFSET ${len(values)+2}',*values,limit,offset)
        return {'items':[dict(row) for row in rows],'total':total,'limit':limit,'offset':offset}

    async def upcoming_tasks(self,days,limit=50,offset=0):
        # Calendar-day window in the local user's display timezone; no invented time is stored.
        display_zone = ZoneInfo('Asia/Shanghai')
        now = datetime.now(timezone.utc)
        today = now.astimezone(display_zone).date()
        end_date = today + timedelta(days=days)
        end_at = datetime.combine(end_date,time.min,tzinfo=display_zone)
        where = '''owner_id=$1 AND status IN ('pending','in_progress') AND (
            (starts_at >= $2 AND starts_at < $3) OR (due_at >= $2 AND due_at < $3)
            OR (starts_on >= $4 AND starts_on < $5) OR (due_on >= $4 AND due_on < $5))'''
        values = [self.owner_id,now,end_at,today,end_date]
        async with self.pool.acquire() as conn:
            total = await conn.fetchval(f'SELECT count(*) FROM tasks WHERE {where}',*values)
            rows = await conn.fetch(f'''SELECT * FROM tasks WHERE {where}
                ORDER BY LEAST(starts_on,due_on,(starts_at AT TIME ZONE 'Asia/Shanghai')::date,(due_at AT TIME ZONE 'Asia/Shanghai')::date),
                    LEAST(starts_at,due_at),id LIMIT $6 OFFSET $7''',*values,limit,offset)
        return {'items':[dict(row) for row in rows],'total':total,'limit':limit,'offset':offset}

    async def list_events(self,application_id,limit=50,offset=0):
        async with self.pool.acquire() as conn:
            await self._application(conn,application_id)
            total = await conn.fetchval('SELECT count(*) FROM application_events WHERE owner_id=$1 AND application_id=$2',self.owner_id,application_id)
            rows = await conn.fetch('SELECT * FROM application_events WHERE owner_id=$1 AND application_id=$2 ORDER BY created_at DESC,id DESC LIMIT $3 OFFSET $4',self.owner_id,application_id,limit,offset)
        items = []
        for record in rows:
            item = dict(record)
            item['before_data'] = json.loads(item['before_data']) if item['before_data'] is not None else None
            item['after_data'] = json.loads(item['after_data'])
            items.append(item)
        return {'items':items,'total':total,'limit':limit,'offset':offset}
