"""Store first, extract once, then review. Never call application write services."""
import asyncio
from uuid import uuid4
import hashlib
import json

from pydantic import ValidationError

from agent.notification_extractor import ExtractionFailure,PROMPT_VERSION,extract_notification
from schemas.notifications import ExtractionResult
from services.errors import Conflict,NotFound
from services.processing_state import attempt_connection,error_info,log_event
from services.notification_validation import validate_evidence
from services.job_execution import write_transaction


def notification_row(record):
    value = dict(record)
    value.pop('request_fingerprint')
    value['hints'] = json.loads(value['hints'])
    return value


def extraction_row(record):
    value = dict(record)
    for key in ['job_id','job_token','retryable','retry_after_seconds']:value.pop(key,None)
    for field in ['model_result','reviewed_result','usage','cost_basis']:
        value[field] = json.loads(value[field]) if value[field] is not None else None
    return value


class NotificationService:
    def __init__(self,pool,owner_id,extractor=extract_notification,execution=None):
        self.execution=execution
        self.pool,self.owner_id,self.extractor = pool,owner_id,extractor

    async def import_notification(self,body):
        data = body.model_dump(mode='json',exclude={'request_id'})
        fingerprint = hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        row = await self.pool.fetchrow('''INSERT INTO notifications(
            owner_id,request_id,request_fingerprint,text,source,source_reference,sent_at,timezone_hint,hints,calendar_year)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10)
            ON CONFLICT(owner_id,request_id) DO NOTHING RETURNING *''',
            self.owner_id,body.request_id,fingerprint,body.text,body.source,body.source_reference,body.sent_at,body.timezone_hint,json.dumps(data['hints'],ensure_ascii=False),body.calendar_year)
        if row is None:
            row = await self.pool.fetchrow('SELECT * FROM notifications WHERE owner_id=$1 AND request_id=$2',self.owner_id,body.request_id)
            if row['request_fingerprint'] != fingerprint:
                raise Conflict('该导入请求编号已用于其他通知内容')
        return notification_row(row)

    async def get_notification(self,notification_id):
        row = await self.pool.fetchrow('SELECT * FROM notifications WHERE id=$1 AND owner_id=$2',notification_id,self.owner_id)
        if row is None:
            raise NotFound('通知不存在')
        return notification_row(row)

    async def list_notifications(self,limit=50,offset=0):
        total = await self.pool.fetchval('SELECT count(*) FROM notifications WHERE owner_id=$1',self.owner_id)
        rows = await self.pool.fetch('SELECT * FROM notifications WHERE owner_id=$1 ORDER BY imported_at DESC,id DESC LIMIT $2 OFFSET $3',self.owner_id,limit,offset)
        return {'items':[notification_row(row) for row in rows],'total':total,'limit':limit,'offset':offset}

    async def get_extraction(self,notification_id,extraction_id):
        row = await self.pool.fetchrow('SELECT * FROM notification_extractions WHERE owner_id=$1 AND notification_id=$2 AND id=$3',self.owner_id,notification_id,extraction_id)
        if row is None:
            raise NotFound('提取记录不存在')
        return extraction_row(row)

    async def list_extractions(self,notification_id,limit=50,offset=0):
        await self.get_notification(notification_id)
        total = await self.pool.fetchval('SELECT count(*) FROM notification_extractions WHERE owner_id=$1 AND notification_id=$2',self.owner_id,notification_id)
        rows = await self.pool.fetch('SELECT * FROM notification_extractions WHERE owner_id=$1 AND notification_id=$2 ORDER BY created_at DESC,id DESC LIMIT $3 OFFSET $4',self.owner_id,notification_id,limit,offset)
        return {'items':[extraction_row(row) for row in rows],'total':total,'limit':limit,'offset':offset}

    async def extract(self,notification_id,body,retry_of=None):
        notification=await self.get_notification(notification_id)
        attempt_id=uuid4()
        async with attempt_connection(self.pool,'extraction',attempt_id) as (conn,alive):
            async with write_transaction(conn,self.execution):
                await conn.fetchrow('SELECT id FROM notifications WHERE owner_id=$1 AND id=$2 FOR UPDATE',self.owner_id,notification_id)
                previous=await conn.fetchrow('SELECT * FROM notification_extractions WHERE owner_id=$1 AND request_id=$2',self.owner_id,body.request_id)
                if previous:
                    if previous['notification_id']!=notification_id or previous['retry_of']!=retry_of:
                        raise Conflict('该提取编号已用于其他通知或重试来源')
                    if previous['status']=='processing':raise Conflict('该请求仍在提取中，请查询记录，不要重复提交')
                    return extraction_row(previous)
                latest=await conn.fetchrow('SELECT * FROM notification_extractions WHERE owner_id=$1 AND notification_id=$2 ORDER BY created_at DESC,id DESC LIMIT 1',self.owner_id,notification_id)
                if latest and latest['status']=='processing':raise Conflict('这条通知仍在提取；中断后请先解除中断状态')
                if retry_of and (not latest or latest['id']!=retry_of or latest['status']!='failed'):
                    raise Conflict('只能重试最新的失败提取，请刷新状态')
                if retry_of and await conn.fetchval('SELECT EXISTS(SELECT 1 FROM proposal_executions WHERE owner_id=$1 AND notification_id=$2)',self.owner_id,notification_id):
                    raise Conflict('这条通知已确认保存，后续纠错请使用手动管理')
                await conn.execute("""INSERT INTO notification_extractions(id,owner_id,notification_id,request_id,status,prompt_version,phase,retry_of)
                    VALUES($1,$2,$3,$4,'processing',$5,'model',$6)""",attempt_id,self.owner_id,notification_id,body.request_id,PROMPT_VERSION,retry_of)
                if self.execution:
                    await conn.execute('UPDATE notification_extractions SET job_id=$2,job_token=$3 WHERE id=$1',attempt_id,self.execution.job['id'],self.execution.job['run_token'])
            log_event('extraction',attempt_id,'model')
            try:
                reply=await self.extractor(notification)
                async with write_transaction(conn,self.execution):
                    await conn.execute("""UPDATE notification_extractions SET phase='validating',raw_output=$3,provider=$4,model=$5,usage=$6::jsonb
                        WHERE owner_id=$1 AND id=$2""",self.owner_id,attempt_id,reply.text,reply.provider,reply.model,json.dumps(reply.usage) if reply.usage is not None else None)
                if reply.finish_reason!='stop':raise ExtractionFailure('模型输出未正常结束，请检查输出长度或服务响应')
                try:result=ExtractionResult.model_validate_json(reply.text)
                except ValidationError as error:
                    first=error.errors()[0]
                    raise ExtractionFailure(f'提取格式错误：{first["loc"]} {first["msg"]}') from error
                validate_evidence(result,notification)
                async with write_transaction(conn,self.execution):
                    saved=await conn.fetchrow("""UPDATE notification_extractions SET status='completed',phase='completed',model_result=$3::jsonb,
                        finished_at=now(),elapsed_ms=greatest(0,(extract(epoch FROM now()-created_at)*1000)::bigint)
                        WHERE owner_id=$1 AND id=$2 RETURNING *""",self.owner_id,attempt_id,result.model_dump_json())
            except (Exception,asyncio.CancelledError) as error:
                if not alive():raise Conflict('执行连接已断开；请查看处理记录恢复，不写入旧结果') from None
                kind,code,message=error_info(error)
                async with write_transaction(conn,self.execution):
                    saved=await conn.fetchrow("""UPDATE notification_extractions SET status='failed',phase=$3,error_kind=$3,error_status=$4,error_message=$5,
                        finished_at=now(),elapsed_ms=greatest(0,(extract(epoch FROM now()-created_at)*1000)::bigint)
                        WHERE owner_id=$1 AND id=$2 RETURNING *""",self.owner_id,attempt_id,kind,code,message)
                    await conn.execute('UPDATE notification_extractions SET retryable=$2,retry_after_seconds=$3 WHERE id=$1',attempt_id,bool(getattr(error,'retryable',False)),getattr(error,'retry_after_seconds',None))
                log_event('extraction',attempt_id,kind,error_type=type(error).__name__)
                if isinstance(error,asyncio.CancelledError):raise
            else:log_event('extraction',attempt_id,'completed',elapsed_ms=saved['elapsed_ms'])
            return extraction_row(saved)

    async def review(self,notification_id,extraction_id,body):
        notification = await self.get_notification(notification_id)
        previous = await self.get_extraction(notification_id,extraction_id)
        if previous['status'] != 'completed':
            raise Conflict('只有成功提取的记录可以核对，失败记录请另发提取请求')
        combined = previous['review_text']+'\n\n'+body.review_text if previous['review_text'] else body.review_text
        validate_evidence(body.result,notification,combined)
        row = await self.pool.fetchrow('''UPDATE notification_extractions SET reviewed_result=$1::jsonb,
            review_text=$2,reviewed_at=now(),version=version+1 WHERE owner_id=$3 AND notification_id=$4 AND id=$5 AND version=$6 RETURNING *''',
            body.result.model_dump_json(),combined,self.owner_id,notification_id,extraction_id,body.version)
        if row is None:
            raise Conflict('核对记录已变化，请读取最新版本后再修改')
        return extraction_row(row)
