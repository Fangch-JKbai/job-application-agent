"""Explicit orphan recovery and whole-attempt retry; no timer or automatic model call."""
import json
from uuid import UUID
from services.errors import Conflict,NotFound
from services.processing_state import attempt_connection,lock_key,log_event
from services.recruitment import snapshot

TABLES={'extraction':'notification_extractions','turn':'agent_turns'}

class ProcessingService:
    def __init__(self,pool,owner_id):self.pool,self.owner_id=pool,owner_id

    async def _row(self,conn,kind,attempt_id):
        row=await conn.fetchrow(f'SELECT * FROM {TABLES[kind]} WHERE owner_id=$1 AND id=$2',self.owner_id,attempt_id)
        if row is None:raise NotFound('处理记录不存在')
        return row

    async def _view(self,conn,kind,row):
        value=snapshot(row)
        session=None
        if kind=='turn':session=await conn.fetchrow('SELECT * FROM agent_sessions WHERE owner_id=$1 AND id=$2',self.owner_id,row['session_id'])
        notification_id=row['notification_id'] if kind=='extraction' else session['notification_id']
        active=False
        if row['status']=='processing':
            free=await conn.fetchval('SELECT pg_try_advisory_lock($1)',lock_key(kind,row['id']))
            if free:await conn.execute('SELECT pg_advisory_unlock($1)',lock_key(kind,row['id']))
            active=not free
        if kind=='extraction':latest=await conn.fetchval('SELECT id FROM notification_extractions WHERE owner_id=$1 AND notification_id=$2 ORDER BY created_at DESC,id DESC LIMIT 1',self.owner_id,notification_id)
        else:latest=await conn.fetchval('SELECT id FROM agent_turns WHERE owner_id=$1 AND session_id=$2 ORDER BY created_at DESC,id DESC LIMIT 1',self.owner_id,row['session_id'])
        saved=await conn.fetchval('SELECT EXISTS(SELECT 1 FROM proposal_executions WHERE owner_id=$1 AND notification_id=$2)',self.owner_id,notification_id)
        can_retry=row['status']=='failed' and row['id']==latest and not saved
        if kind=='turn':
            version=await conn.fetchval('SELECT version FROM notification_extractions WHERE owner_id=$1 AND id=$2',self.owner_id,session['extraction_id'])
            can_retry=can_retry and row['input_state'] is not None and session['status']=='failed' and session['extraction_version']==version
        return {**{k:value[k] for k in ['id','status','phase','error_kind','error_message','elapsed_ms','retry_of','created_at','finished_at']},
            'kind':kind,'notification_id':str(notification_id),'session_id':str(row['session_id']) if kind=='turn' else None,
            'usage':json.loads(row['usage']) if row['usage'] else None,'cost_basis':json.loads(row['cost_basis']),
            'active':active,'recoverable':row['status']=='processing' and not active,'can_retry':can_retry}

    async def list(self,notification_id=None,limit=20,offset=0,status=None):
        async with self.pool.acquire() as conn:
            values=[self.owner_id];where='owner_id=$1'
            if notification_id is not None:values.append(notification_id);where+=f' AND notification_id=${len(values)}'
            if status is not None:values.append(status);where+=f' AND status=${len(values)}'
            union="""SELECT 'extraction' kind,id,owner_id,notification_id,status,created_at FROM notification_extractions
                UNION ALL SELECT 'turn',t.id,t.owner_id,s.notification_id,t.status,t.created_at FROM agent_turns t JOIN agent_sessions s ON s.owner_id=t.owner_id AND s.id=t.session_id"""
            total=await conn.fetchval(f'SELECT count(*) FROM ({union}) p WHERE {where}',*values)
            rows=await conn.fetch(f'SELECT kind,id FROM ({union}) p WHERE {where} ORDER BY created_at DESC,id DESC LIMIT ${len(values)+1} OFFSET ${len(values)+2}',*values,limit,offset)
            items=[await self._view(conn,row['kind'],await self._row(conn,row['kind'],row['id'])) for row in rows]
        return {'items':items,'total':total,'limit':limit,'offset':offset}

    async def detail(self,kind,attempt_id):
        async with self.pool.acquire() as conn:
            row=await self._row(conn,kind,attempt_id)
            trace=json.loads(row['trace']) if kind=='turn' else []
            return {'attempt':await self._view(conn,kind,row),'trace':trace}

    async def recover(self,kind,attempt_id):
        # Check ownership before probing a live execution lock.
        await self._row(self.pool,kind,attempt_id)
        # A live worker owns this same lock. A killed worker's PG session releases it.
        async with attempt_connection(self.pool,kind,attempt_id) as (conn,alive):
            async with conn.transaction():
                row=await self._row(conn,kind,attempt_id)
                if row['status']!='processing':return {'attempt':await self._view(conn,kind,row),'trace':json.loads(row['trace']) if kind=='turn' else []}
                if kind=='turn':
                    from services.sessions import session_row,turn_row
                    await conn.fetchrow('SELECT id FROM agent_sessions WHERE owner_id=$1 AND id=$2 FOR UPDATE',self.owner_id,row['session_id'])
                row=await conn.fetchrow(f"""UPDATE {TABLES[kind]} SET status='failed',phase='interrupted',error_kind='interrupted',error_status=503,
                    error_message='执行进程已中断；已完成步骤保留在记录中，可明确重试整次处理',finished_at=now(),
                    elapsed_ms=greatest(0,(extract(epoch FROM now()-created_at)*1000)::bigint)
                    WHERE owner_id=$1 AND id=$2 AND status='processing' RETURNING *""",self.owner_id,attempt_id)
                if kind=='turn':
                    session=await conn.fetchrow("UPDATE agent_sessions SET status='failed',version=version+1,updated_at=now() WHERE owner_id=$1 AND id=$2 RETURNING *",self.owner_id,row['session_id'])
                    response={'session':session_row(session),'turn':turn_row(row),'proposal':None}
                    await conn.execute('UPDATE agent_turns SET response_snapshot=$3::jsonb WHERE owner_id=$1 AND id=$2',self.owner_id,attempt_id,json.dumps(response,ensure_ascii=False))
            log_event(kind,attempt_id,'interrupted')
        return await self.detail(kind,attempt_id)

    async def retry(self,kind,attempt_id,body,extractor,model):
        from schemas.notifications import ExtractionRequest
        from schemas.sessions import SessionMessage
        from services.notifications import NotificationService,extraction_row
        from services.sessions import SessionService
        async with self.pool.acquire() as conn:
            source=await self._row(conn,kind,attempt_id)
            previous=await conn.fetchrow(f'SELECT * FROM {TABLES[kind]} WHERE owner_id=$1 AND request_id=$2',self.owner_id,body.request_id)
            if previous:
                if previous['retry_of']!=attempt_id:raise Conflict('该请求编号已用于其他处理')
                if previous['status']=='processing':raise Conflict('该重试仍在处理，请查询状态')
                if kind=='extraction':return {'kind':kind,'extraction':extraction_row(previous),'message':None}
                return {'kind':kind,'extraction':None,'message':json.loads(previous['response_snapshot'])}
            view=await self._view(conn,kind,source)
            if not view['can_retry']:raise Conflict('该记录当前不能重试；运行中请等待，中断请先解除状态，旧记录请从最新处理继续')
            if kind=='turn':session=await conn.fetchrow('SELECT * FROM agent_sessions WHERE owner_id=$1 AND id=$2',self.owner_id,source['session_id'])
        if kind=='extraction':
            result=await NotificationService(self.pool,self.owner_id,extractor).extract(source['notification_id'],ExtractionRequest(request_id=body.request_id),attempt_id)
            return {'kind':kind,'extraction':result,'message':None}
        message=SessionMessage(request_id=body.request_id,version=session['version'],text=source['user_text'],reply_to_question_id=source['reply_to_question_id'])
        result=await SessionService(self.pool,self.owner_id,model).message(source['session_id'],message,attempt_id)
        return {'kind':kind,'extraction':None,'message':result}
