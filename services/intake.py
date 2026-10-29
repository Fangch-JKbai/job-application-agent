"""An editable new-application form, confirmed atomically without another model call."""
import hashlib
import json
from uuid import UUID,uuid5
from schemas.recruitment import ApplicationCreate,TaskCreate
from services.confirmations import matching_applications,ConfirmationService
from services.errors import Conflict,NotFound,InvalidInput
from services.recruitment import RecruitmentService,snapshot

class IntakeService:
    def __init__(self,pool,owner):self.pool,self.owner=pool,owner

    async def confirm(self,body):
        key=hashlib.sha256(body.model_dump_json(exclude={'request_id'}).encode()).hexdigest()
        async with self.pool.acquire() as conn,conn.transaction():
            service=RecruitmentService(self.pool,self.owner,connection=conn,source_reference=f'intake;notification:{body.notification_id}')
            await service.lock_application_creation(conn)
            prior=await conn.fetchrow('SELECT * FROM intake_receipts WHERE owner_id=$1 AND request_id=$2',self.owner,body.request_id)
            if prior:
                if prior['request_fingerprint']!=key:raise Conflict('该请求编号已用于其他录入内容')
                return json.loads(prior['result'])
            if await conn.fetchval('SELECT EXISTS(SELECT 1 FROM intake_receipts WHERE owner_id=$1 AND notification_id=$2)',self.owner,body.notification_id):
                raise Conflict('这条通知已创建申请，请前往申请记录查看')
            if await conn.fetchval('SELECT EXISTS(SELECT 1 FROM proposal_executions WHERE owner_id=$1 AND notification_id=$2)',self.owner,body.notification_id):
                raise Conflict('这条通知已经通过核对方案保存，请查看已有申请')
            if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM automatic_records WHERE owner_id=$1 AND notification_id=$2 AND status IN ('saved','undone'))",self.owner,body.notification_id):raise Conflict('这条通知已经自动处理，请查看结果')
            extraction=await conn.fetchrow('SELECT * FROM notification_extractions WHERE owner_id=$1 AND notification_id=$2 AND id=$3 FOR SHARE',self.owner,body.notification_id,body.extraction_id)
            if not extraction:raise NotFound('提取记录不存在')
            if extraction['status']!='completed' or extraction['version']!=body.extraction_version:raise Conflict('提取版本已变化，请重新打开录入表单')
            result=json.loads(extraction['reviewed_result'] or extraction['model_result'])
            if result['notification_type'] in {'schedule_change','cancellation','rejection','offer_accepted','conditional_update','reminder'}:
                raise InvalidInput('这类通知请通过通知页匹配已有申请、澄清并核对，不能在新建入口直接创建')
            if await matching_applications(conn,self.owner,body.application.model_dump(mode='json')):
                raise Conflict('已有相同公司和岗位的申请，请通过通知页匹配更新，避免重复录入')
            if body.application.outcome!='active' and body.tasks:raise InvalidInput('已结束的申请不能同时新增待办')
            app=await service.create_application(ApplicationCreate(request_id=uuid5(body.request_id,'application'),**body.application.model_dump()))
            tasks=[]
            for i,task in enumerate(body.tasks):
                row=await service.create_task(TaskCreate(request_id=uuid5(body.request_id,f'task:{i}'),application_id=UUID(str(app['id'])),**task.model_dump()))
                actual=await conn.fetchrow('SELECT * FROM tasks WHERE owner_id=$1 AND id=$2',self.owner,row['id'])
                ConfirmationService._verify(actual,task.model_dump())
                tasks.append(snapshot(actual))
            actual=await conn.fetchrow('SELECT * FROM applications WHERE owner_id=$1 AND id=$2',self.owner,app['id'])
            ConfirmationService._verify(actual,body.application.model_dump())
            saved={'notification_id':str(body.notification_id),'application':snapshot(actual),'tasks':tasks}
            await conn.execute('INSERT INTO intake_receipts(owner_id,request_id,notification_id,request_fingerprint,result) VALUES($1,$2,$3,$4,$5::jsonb)',self.owner,body.request_id,body.notification_id,key,json.dumps(saved,ensure_ascii=False))
        return saved
