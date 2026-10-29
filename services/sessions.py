"""Persist conversational facts and draft proposals without changing business rows."""
import asyncio
from copy import deepcopy
import hashlib
import json
from uuid import UUID,uuid4

from agent.notification_extractor import ExtractionFailure
from agent.session_agent import call_model,run_agent
from agent.session_tools import SessionTools
from services.errors import Conflict,NotFound,InvalidInput
from services.notifications import NotificationService
from services.recruitment import snapshot
from services.processing_state import attempt_connection,error_info,log_event,usage_summary


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def session_row(row):
    value = snapshot(row)
    value.pop('request_fingerprint')
    value['state'] = json.loads(row['state'])
    return value


def turn_row(row):
    value = snapshot(row)
    value.pop('request_fingerprint')
    value.pop('response_snapshot')
    value['trace'] = json.loads(row['trace'])
    value['usage'] = json.loads(row['usage']) if row['usage'] is not None else None
    value['cost_basis'] = json.loads(row['cost_basis'])
    value.pop('input_state')
    return value


def proposal_row(row):
    value = snapshot(row)
    value['payload'] = json.loads(row['payload'])
    value['review_url'] = f'/review/proposals/{value["id"]}'
    return value


class SessionService:
    def __init__(self,pool,owner_id,model=call_model):
        self.pool,self.owner_id,self.model = pool,owner_id,model

    async def get(self,session_id):
        row = await self.pool.fetchrow('SELECT * FROM agent_sessions WHERE owner_id=$1 AND id=$2',self.owner_id,session_id)
        if row is None:
            raise NotFound('会话不存在')
        return session_row(row)

    async def create(self,body):
        notices = NotificationService(self.pool,self.owner_id)
        await notices.get_notification(body.notification_id)
        extraction = await notices.get_extraction(body.notification_id,body.extraction_id)
        if extraction['status'] != 'completed':
            raise Conflict('请先完成P4提取或核对')
        notice = await notices.get_notification(body.notification_id)
        state = {'result':extraction['reviewed_result'] or extraction['model_result'],
            'source_context':{'sent_at':notice['sent_at'].isoformat() if notice['sent_at'] else None,'timezone_hint':notice['timezone_hint']},
            'initial_review_text':extraction['review_text'],'confirmed_fields':{},'review_sources':[],
            'candidates':{},'selection':None,'application_detail':None,'observed_tasks':{},'tasks_read':False,'questions':[],'proposal_id':None}
        key = fingerprint(body.model_dump(mode='json',exclude={'request_id'}))
        row = await self.pool.fetchrow('''INSERT INTO agent_sessions(owner_id,notification_id,extraction_id,extraction_version,request_id,request_fingerprint,state)
            VALUES($1,$2,$3,$4,$5,$6,$7::jsonb) ON CONFLICT(owner_id,request_id) DO NOTHING RETURNING *''',
            self.owner_id,body.notification_id,body.extraction_id,extraction['version'],body.request_id,key,json.dumps(state,ensure_ascii=False))
        if row is None:
            row = await self.pool.fetchrow('SELECT * FROM agent_sessions WHERE owner_id=$1 AND request_id=$2',self.owner_id,body.request_id)
            if row['request_fingerprint'] != key:
                raise Conflict('该会话请求编号已用于其他通知/提取')
        return session_row(row)

    async def list(self,limit=20,offset=0,notification_id=None):
        where,values='owner_id=$1',[self.owner_id]
        if notification_id is not None:
            values.append(notification_id);where+=' AND notification_id=$2'
        total=await self.pool.fetchval(f'SELECT count(*) FROM agent_sessions WHERE {where}',*values)
        rows=await self.pool.fetch(f'SELECT * FROM agent_sessions WHERE {where} ORDER BY updated_at DESC,id DESC LIMIT ${len(values)+1} OFFSET ${len(values)+2}',*values,limit,offset)
        return {'items':[session_row(row) for row in rows],'total':total,'limit':limit,'offset':offset}

    async def turns(self,session_id,limit=20,offset=0):
        await self.get(session_id)
        total = await self.pool.fetchval('SELECT count(*) FROM agent_turns WHERE owner_id=$1 AND session_id=$2',self.owner_id,session_id)
        rows = await self.pool.fetch('SELECT * FROM agent_turns WHERE owner_id=$1 AND session_id=$2 ORDER BY created_at,id LIMIT $3 OFFSET $4',self.owner_id,session_id,limit,offset)
        return {'items':[turn_row(row) for row in rows],'total':total,'limit':limit,'offset':offset}

    async def proposal(self,proposal_id):
        row = await self.pool.fetchrow('SELECT * FROM action_proposals WHERE owner_id=$1 AND id=$2',self.owner_id,proposal_id)
        if row is None:
            raise NotFound('方案不存在')
        return proposal_row(row)

    async def message(self,session_id,body,retry_of=None):
        turn_id=uuid4()
        async with attempt_connection(self.pool,'turn',turn_id) as (conn,alive):
            return await self._message(conn,turn_id,session_id,body,retry_of,alive)

    async def _message(self,conn,turn_id,session_id,body,retry_of,alive):
        key = fingerprint({'session_id':str(session_id),**body.model_dump(mode='json',exclude={'request_id'}),**({'retry_of':str(retry_of)} if retry_of else {})})
        async with conn.transaction():
            lock = int.from_bytes(hashlib.sha256(f'p5:{self.owner_id}:{body.request_id}'.encode()).digest()[:8],signed=True)
            await conn.execute('SELECT pg_advisory_xact_lock($1)',lock)
            row = await conn.fetchrow('SELECT * FROM agent_sessions WHERE owner_id=$1 AND id=$2 FOR UPDATE',self.owner_id,session_id)
            if row is None:
                raise NotFound('会话不存在')
            previous = await conn.fetchrow('SELECT * FROM agent_turns WHERE owner_id=$1 AND request_id=$2',self.owner_id,body.request_id)
            if previous:
                if previous['request_fingerprint'] != key:
                    raise Conflict('该消息请求编号已用于其他内容')
                if previous['status'] == 'processing':
                    raise Conflict('该消息仍在处理，请查询会话')
                return json.loads(previous['response_snapshot'])
            session = session_row(row)
            if session['status'] == 'saved':
                raise Conflict('该会话方案已保存，请查看执行结果；后续纠错使用手动管理')
            if session['status'] == 'processing':
                raise Conflict('会话正在处理另一条消息，请等待')
            if session['version'] != body.version:
                raise Conflict('会话版本已变化，请刷新后继续')
            questions = session['state']['questions']
            question_id = str(body.reply_to_question_id) if body.reply_to_question_id else None
            if question_id and question_id not in {item['id'] for item in questions}:
                raise InvalidInput('回复的问题不属于当前待解决问题')
            if not question_id and len(questions) == 1:
                question_id = questions[0]['id']
            if not question_id and len(questions) > 1:
                raise InvalidInput('有多个待解决问题，请填写reply_to_question_id')
            if retry_of:
                original=await conn.fetchrow('SELECT * FROM agent_turns WHERE owner_id=$1 AND id=$2 AND session_id=$3',self.owner_id,retry_of,session_id)
                latest=await conn.fetchval('SELECT id FROM agent_turns WHERE owner_id=$1 AND session_id=$2 ORDER BY created_at DESC,id DESC LIMIT 1',self.owner_id,session_id)
                if original is None or latest!=retry_of or original['status']!='failed' or original['input_state'] is None:
                    raise Conflict('只能重试最新且有输入快照的失败消息；旧记录可在会话中补充信息继续')
                extraction_version=await conn.fetchval('SELECT version FROM notification_extractions WHERE owner_id=$1 AND id=$2',self.owner_id,UUID(session['extraction_id']))
                if extraction_version!=session['extraction_version']:raise Conflict('提取已被重新核对，请从最新版本继续')
                session['state']=json.loads(original['input_state'])
                questions=session['state']['questions']
                question_id=str(body.reply_to_question_id) if body.reply_to_question_id else None
            input_state=deepcopy(session['state'])
            input_state['proposal_id']=None
            turn = await conn.fetchrow('''INSERT INTO agent_turns(id,owner_id,session_id,request_id,request_fingerprint,user_text,reply_to_question_id,status,phase,retry_of,input_state)
                VALUES($1,$2,$3,$4,$5,$6,$7,'processing','starting',$8,$9::jsonb) RETURNING *''',turn_id,self.owner_id,session_id,body.request_id,key,body.text,UUID(question_id) if question_id else None,retry_of,json.dumps(input_state,ensure_ascii=False))
            await conn.execute("UPDATE action_proposals SET status='superseded' WHERE owner_id=$1 AND session_id=$2 AND status='draft'",self.owner_id,session_id)
            session['state']['proposal_id'] = None
            await conn.execute("UPDATE agent_sessions SET status='processing',version=version+1,state=$3::jsonb,updated_at=now() WHERE owner_id=$1 AND id=$2",self.owner_id,session_id,json.dumps(session['state'],ensure_ascii=False))

        # A connection owns the liveness lock, but no transaction spans model I/O.
        trace=[]
        async def checkpoint(phase):
            await conn.execute('UPDATE agent_turns SET phase=$3,trace=$4::jsonb,usage=$5::jsonb WHERE owner_id=$1 AND id=$2',
                self.owner_id,turn_id,phase,json.dumps(trace,ensure_ascii=False,default=str),json.dumps(usage_summary(trace,in_flight=phase=='model')))
            log_event('turn',turn_id,phase,tool_calls=sum(item['kind']=='tool' for item in trace))
        runtime=None
        try:
            notice=await NotificationService(conn,self.owner_id).get_notification(UUID(session['notification_id']))
            history_rows=await conn.fetch("""SELECT user_text,assistant_text FROM agent_turns
                WHERE owner_id=$1 AND session_id=$2 AND status='completed' ORDER BY created_at DESC,id DESC LIMIT 3""",self.owner_id,session_id)
            history=[]
            for item in reversed(history_rows):
                history.extend([{'role':'user','content':item['user_text']},{'role':'assistant','content':item['assistant_text']}])
            runtime=SessionTools(self.pool,self.owner_id,session,turn_row(turn),notice,connection=conn)
            status,assistant_text=await run_agent(runtime,history,trace,self.model,checkpoint)
            response=await self._finish(conn,session_id,turn_id,status,assistant_text,trace,runtime.state,runtime.proposal)
        except (Exception,asyncio.CancelledError) as error:
            if not alive():raise Conflict('执行连接已断开；请查看处理记录恢复，不写入旧结果') from None
            kind,code,message=error_info(error)
            response=await self._finish(conn,session_id,turn_id,'failed',message,trace,input_state,None,kind,code,message)
            log_event('turn',turn_id,kind,error_type=type(error).__name__)
            if isinstance(error,asyncio.CancelledError):raise
        return response

    async def _finish(self,conn,session_id,turn_id,status,assistant_text,trace,state,prepared,error_kind=None,error_status=None,error_message=None):
        async with conn.transaction():
            await conn.fetchrow('SELECT id FROM agent_sessions WHERE owner_id=$1 AND id=$2 FOR UPDATE',self.owner_id,session_id)
            proposal=None
            if prepared is not None:
                row=await conn.fetchrow("""INSERT INTO action_proposals(owner_id,session_id,turn_id,payload)
                    VALUES($1,$2,$3,$4::jsonb) RETURNING *""",self.owner_id,session_id,turn_id,json.dumps(prepared,ensure_ascii=False,default=str))
                proposal=proposal_row(row);state['proposal_id']=proposal['id']
            row=await conn.fetchrow("""UPDATE agent_sessions SET status=$3,state=$4::jsonb,version=version+1,updated_at=now()
                WHERE owner_id=$1 AND id=$2 RETURNING *""",self.owner_id,session_id,status,json.dumps(state,ensure_ascii=False,default=str))
            saved=await conn.fetchrow("""UPDATE agent_turns SET status=$3,phase=$4,assistant_text=$5,trace=$6::jsonb,usage=$7::jsonb,
                error_kind=$8,error_status=$9,error_message=$10,finished_at=now(),
                elapsed_ms=greatest(0,(extract(epoch FROM now()-created_at)*1000)::bigint) WHERE owner_id=$1 AND id=$2 RETURNING *""",
                self.owner_id,turn_id,'failed' if status=='failed' else 'completed',error_kind or status,assistant_text,
                json.dumps(trace,ensure_ascii=False,default=str),json.dumps(usage_summary(trace)),error_kind,error_status,error_message)
            response={'session':session_row(row),'turn':turn_row(saved),'proposal':proposal}
            await conn.execute('UPDATE agent_turns SET response_snapshot=$3::jsonb WHERE owner_id=$1 AND id=$2',self.owner_id,turn_id,json.dumps(response,ensure_ascii=False))
        log_event('turn',turn_id,status,elapsed_ms=saved['elapsed_ms'],usage=usage_summary(trace))
        return response
