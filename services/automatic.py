"""Default notice workflow: extract, match, record once; uncertainty stays in the inbox."""
import json,re
from uuid import UUID,uuid5
from schemas.notifications import ExtractionRequest,ExtractionResult
from schemas.recruitment import ApplicationCreate,ApplicationFields,ApplicationPatch,TaskCreate,TaskFields,TaskPatch,STAGE_ORDER
from services.notifications import NotificationService,extraction_row
from services.recruitment import RecruitmentService,snapshot
from services.confirmations import ConfirmationService
from services.errors import Conflict,NotFound
from services.proposal_state import task_scope
from core.categories import category_key

SUBMISSION=re.compile(r'投递成功|投递通知|感谢.{0,20}应聘|感谢.{0,30}投递|简历数据库|简历库|简历.{0,20}入库|收到.{0,15}简历|application.{0,20}received',re.I)
TIMES=['opens_at','starts_at','ends_at','due_at','starts_on','due_on']
STAGES={'assessment_invitation':'assessment','written_test_invitation':'written_test','interview_invitation':'interview','offer_received':'offer','offer_accepted':'offer'}

def output(notification_id,status,message,application_id=None,**extra):
    return {'notification_id':str(notification_id),'status':status,'message':message,'application_id':str(application_id) if application_id else None,**extra}

class AutomaticService:
    def __init__(self,pool,owner,extractor=None,execution=None):
        self.pool,self.owner,self.extractor,self.execution=pool,owner,extractor,execution
    async def get(self,nid):
        await NotificationService(self.pool,self.owner).get_notification(nid)
        raw=await self.pool.fetchval('SELECT result FROM automatic_records WHERE owner_id=$1 AND notification_id=$2',self.owner,nid)
        return json.loads(raw) if raw else output(nid,'pending','尚未自动记录')
    async def process(self,nid):
        notices=NotificationService(self.pool,self.owner,self.extractor,execution=self.execution) if self.extractor else NotificationService(self.pool,self.owner,execution=self.execution)
        notice=await notices.get_notification(nid)
        existing=await self.get(nid)
        if existing['status'] in {'saved','undone'}:return existing
        latest=await self.pool.fetchrow('SELECT * FROM notification_extractions WHERE owner_id=$1 AND notification_id=$2 ORDER BY created_at DESC,id DESC LIMIT 1',self.owner,nid)
        if self.execution:
            from services.jobs import input_key
            async with self.pool.acquire() as check,check.transaction():
                await self.execution.guard(check)
                key=await input_key(check,self.owner,nid)
                owned=latest and latest['job_id']==self.execution.job['id'] and latest['version']==1
                if key!=self.execution.job['input_key'] and not owned:raise Conflict('识别结果已变化，请按最新输入重新提交')
                if latest and latest['status']=='failed' and (owned or self.execution.job['attempt_count']>1):latest=None
        if latest is None:
            from core.access import admit_model
            await admit_model(self.pool,self.owner)
            ex=await notices.extract(nid,ExtractionRequest(request_id=uuid5(self.execution.job['run_token'],'extract') if self.execution else uuid5(nid,'automatic.extract')))
        else:ex=extraction_row(latest)
        if ex['status']!='completed':return output(nid,'failed' if ex['status']=='failed' else 'processing',ex['error_message'] or '提取仍在进行，请查看处理记录')
        data=ex['reviewed_result'] or ex['model_result']
        # Honour already-supplied conversational facts instead of asking for them again.
        row=await self.pool.fetchrow('SELECT state FROM agent_sessions WHERE owner_id=$1 AND notification_id=$2 AND extraction_id=$3 AND extraction_version=$4 ORDER BY updated_at DESC LIMIT 1',self.owner,nid,UUID(str(ex['id'])),ex['version'])
        if row:
            state=json.loads(row['state'])
            if state.get('confirmed_fields'):data=state['result']
        result=ExtractionResult.model_validate(data)
        async with self.pool.acquire() as conn,conn.transaction():
            if self.execution:
                await self.execution.guard(conn)
                current=await conn.fetchrow('SELECT id,version FROM notification_extractions WHERE owner_id=$1 AND notification_id=$2 ORDER BY created_at DESC,id DESC LIMIT 1 FOR UPDATE',self.owner,nid)
                if current['id']!=ex['id'] or current['version']!=ex['version']:raise Conflict('识别结果已变化，本次不写入业务')
            service=RecruitmentService(self.pool,self.owner,connection=conn,source_reference=f'automatic:{nid};notification:{nid}')
            await service.lock_application_creation(conn)
            raw=await conn.fetchval('SELECT result FROM automatic_records WHERE owner_id=$1 AND notification_id=$2',self.owner,nid)
            if raw and json.loads(raw)['status'] in {'saved','undone'}:
                value=json.loads(raw)
                if self.execution:await self.execution.finish(conn,value)
                return value
            # Old manual intake and confirmed proposals must not be replayed as new automatic work.
            legacy=await conn.fetchval('SELECT application_id FROM proposal_executions WHERE owner_id=$1 AND notification_id=$2',self.owner,nid)
            if not legacy:
                raw_intake=await conn.fetchval("SELECT result->'application'->>'id' FROM intake_receipts WHERE owner_id=$1 AND notification_id=$2",self.owner,nid)
                legacy=UUID(raw_intake) if raw_intake else None
            if legacy:
                value=output(nid,'saved','这条通知已有保存记录',legacy,can_undo=False)
                await conn.execute("INSERT INTO automatic_records(owner_id,notification_id,extraction_version,status,application_id,result) VALUES($1,$2,$3,'saved',$4,$5::jsonb) ON CONFLICT(owner_id,notification_id) DO NOTHING",self.owner,nid,ex['version'],legacy,json.dumps(value,ensure_ascii=False))
                if self.execution:await self.execution.finish(conn,value)
                return value
            async def remember(status,message,aid=None,**extra):
                value=output(nid,status,message,aid,**extra)
                await conn.execute('''INSERT INTO automatic_records(owner_id,notification_id,extraction_version,status,application_id,result)
                    VALUES($1,$2,$3,$4,$5,$6::jsonb) ON CONFLICT(owner_id,notification_id) DO UPDATE SET extraction_version=excluded.extraction_version,status=excluded.status,application_id=excluded.application_id,result=excluded.result,updated_at=now()''',self.owner,nid,ex['version'],status,aid,json.dumps(value,ensure_ascii=False))
                if self.execution:await self.execution.finish(conn,value)
                return value
            kind=result.notification_type
            if kind=='other' and SUBMISSION.search(notice['text']):kind='application_received'
            if kind in {'other','conditional_update'}:
                return await remember('ignored','原文已保留；没有明确的申请进度或安排，未改动业务记录')
            if not result.company_name:return await remember('unresolved','原文已保留，公司尚不明确；可以稍后修正提取结果再处理')
            values=[self.owner,result.company_name]
            clauses=["owner_id=$1",r"(lower(regexp_replace(company_name,'\s+','','g'))=lower(regexp_replace($2::text,'\s+','','g')) OR EXISTS(SELECT 1 FROM unnest(company_aliases) a WHERE lower(regexp_replace(a,'\s+','','g'))=lower(regexp_replace($2::text,'\s+','','g'))))"]
            if result.position_name:
                values.append(result.position_name);clauses.append(r"(position_name IS NULL OR regexp_replace(regexp_replace(lower(regexp_replace(position_name,'\s+','','g')),'^实习',''),'(工程师|岗位|岗)$','')=regexp_replace(regexp_replace(lower(regexp_replace($3::text,'\s+','','g')),'^实习',''),'(工程师|岗位|岗)$',''))")
            if result.batch:
                values.append(category_key(result.batch));clauses.append(f'(batch IS NULL OR recruitment_category(batch)=${len(values)})')
            matches=await conn.fetch('SELECT * FROM applications WHERE '+' AND '.join(clauses)+' ORDER BY id FOR UPDATE',*values)
            if len(matches)>1:return await remember('unresolved','找到多个可能的申请，原通知已保留；可稍后补充岗位或类别，不会反复追问',candidates=[{'id':str(r['id']),'company_name':r['company_name'],'position_name':r['position_name']} for r in matches])
            before=dict(matches[0]) if matches else None
            if not before and kind in {'schedule_change','cancellation'}:return await remember('unresolved','没有找到改期或取消所对应的原申请，通知已保留')
            if before and before['outcome']!='active':return await remember('saved','申请已结束，已保留原通知和现有记录，不自动重开',before['id'],can_undo=False)
            tasks=[dict(r) for r in await conn.fetch('SELECT * FROM tasks WHERE owner_id=$1 AND application_id=$2 FOR UPDATE',self.owner,before['id'])] if before else []
            plans=[];used=set();warnings=list(result.issues)
            for draft in result.tasks:
                typed=TaskFields(**draft.model_dump(exclude={'evidence','time_basis'}))
                candidates=[t for t in tasks if t['type']==typed.type and (typed.round is None or t['round']==typed.round)]
                if kind in {'schedule_change','cancellation'}:
                    if len(candidates)!=1:return await remember('unresolved','无法唯一定位变更的事项，通知已保留',before['id'])
                    target=candidates[0]
                    if target['id'] in used:return await remember('unresolved','同一通知包含无法区分的事项，已保留原文',before['id'])
                    used.add(target['id'])
                    if target['status'] in {'completed','cancelled'}:continue
                    manual=await conn.fetchval("SELECT EXISTS(SELECT 1 FROM application_events WHERE owner_id=$1 AND task_id=$2 AND source_kind IN ('manual','user_correction') AND event_type IN ('task_updated','task_cancelled','user_correction'))",self.owner,target['id'])
                    if manual:return await remember('unresolved','目标待办有手动修改，已保留你的修改；可以在待办页自行调整',before['id'])
                    fields={'status':'cancelled'} if kind=='cancellation' else draft.model_dump(exclude={'evidence','time_basis'},exclude_none=True)
                    for precise,day in [('starts_at','starts_on'),('due_at','due_on')]:
                        if fields.get(precise) is not None:fields[day]=None
                        if fields.get(day) is not None:fields[precise]=None
                    plans.append(('update',target,fields))
                elif candidates:
                    same=[t for t in candidates if all(getattr(typed,k) is None or getattr(typed,k)==t[k] for k in TIMES)]
                    if same or kind=='reminder':continue
                    return await remember('unresolved','已有同类型事项但时间不同，原通知保留供稍后整理',before['id'])
                else:plans.append(('create',None,typed.model_dump()))
            if kind in {'schedule_change','cancellation'} and not result.tasks:return await remember('unresolved','原通知未明确变更事项，已保留')
            changes={}
            if STAGES.get(kind) and STAGE_ORDER.index(STAGES[kind])>STAGE_ORDER.index(before['stage'] if before else 'submitted'):changes['stage']=STAGES[kind]
            if kind in {'rejection','offer_accepted'}:changes['outcome']='rejected' if kind=='rejection' else 'accepted'
            if before:
                if before['position_name'] is None and result.position_name:changes['position_name']=result.position_name
                if before['batch'] is None and result.batch:changes['batch']=category_key(result.batch)
                # Preserve explicit manual stage/outcome corrections; automatic progress does not erase them.
                for field in list(changes):
                    protected=await conn.fetchval("SELECT EXISTS(SELECT 1 FROM application_events WHERE owner_id=$1 AND application_id=$2 AND task_id IS NULL AND source_kind IN ('manual','user_correction') AND before_data->$3 IS DISTINCT FROM after_data->$3 AND before_data IS NOT NULL)",self.owner,before['id'],field)
                    if protected:warnings.append('保留手动修改的'+field);changes.pop(field)
                app=before
            else:
                app=await service.create_application(ApplicationCreate(request_id=uuid5(nid,'automatic.application'),company_name=result.company_name,position_name=result.position_name,batch=category_key(result.batch) or None,stage=STAGES.get(kind,'submitted')))
            aid=app['id'];operations=[]
            for index,(action,target,fields) in enumerate(plans):
                op=uuid5(nid,f'automatic.task.{index}')
                if action=='create':actual=await service.create_task(TaskCreate(request_id=op,application_id=aid,**fields))
                else:actual=await service.update_task(target['id'],TaskPatch(request_id=op,version=target['version'],**fields))
                actual=dict(await conn.fetchrow('SELECT * FROM tasks WHERE owner_id=$1 AND id=$2',self.owner,actual['id']))
                expected=TaskFields(**({**{k:target[k] for k in TaskFields.model_fields},**fields} if target else fields)).model_dump()
                ConfirmationService._verify(actual,expected)
                operations.append({'created':target is None,'before':snapshot(target) if target else None,'after':snapshot(actual)})
            changes={k:v for k,v in changes.items() if app[k]!=v}
            if changes:app=await service.update_application(aid,ApplicationPatch(request_id=uuid5(nid,'automatic.progress'),version=app['version'],**changes))
            actual=dict(await conn.fetchrow('SELECT * FROM applications WHERE owner_id=$1 AND id=$2',self.owner,aid))
            ConfirmationService._verify(actual,ApplicationFields(**{k:app[k] for k in ApplicationFields.model_fields}).model_dump())
            return await remember('saved','已自动记录，可编辑或撤销本次变更',aid,can_undo=True,application_before=snapshot(before) if before else None,application=snapshot(actual),task_changes=operations,task_scope=await task_scope(conn,self.owner,aid),warnings=warnings,notification_type=kind)

    async def undo(self,nid):
        async with self.pool.acquire() as conn,conn.transaction():
            service=RecruitmentService(self.pool,self.owner,connection=conn,source_reference=f'undo;notification:{nid}')
            await service.lock_application_creation(conn)
            row=await conn.fetchrow('SELECT * FROM automatic_records WHERE owner_id=$1 AND notification_id=$2 FOR UPDATE',self.owner,nid)
            if not row:raise NotFound('自动记录不存在')
            value=json.loads(row['result'])
            if value['status']=='undone':return value
            if value['status']!='saved' or not value.get('can_undo'):raise Conflict('这条通知没有可撤销的业务变更')
            aid=row['application_id'];app=await conn.fetchrow('SELECT * FROM applications WHERE owner_id=$1 AND id=$2 FOR UPDATE',self.owner,aid)
            if app['version']!=value['application']['version'] or await task_scope(conn,self.owner,aid)!=value['task_scope']:raise Conflict('申请或待办已被后续修改，保留最新内容；请在详情页编辑')
            for i,change in enumerate(value['task_changes']):
                after=change['after'];old=change['before']
                fields=({k:old[k] for k in TaskFields.model_fields} if old else {'status':'cancelled'})
                await service.update_task(UUID(after['id']),TaskPatch(request_id=uuid5(nid,f'undo.task.{i}'),version=after['version'],correction_reason='撤销自动记录',**fields))
            old=value['application_before']
            fields={k:old[k] for k in ApplicationFields.model_fields} if old else {'outcome':'withdrawn'}
            if any(app[k]!=fields[k] for k in fields):await service.update_application(aid,ApplicationPatch(request_id=uuid5(nid,'undo.application'),version=app['version'],correction_reason='撤销自动记录',**fields))
            value.update(status='undone',can_undo=False,message='已撤销：新建申请标为已撤回、新待办标为已取消；已有记录恢复原值，历史保留')
            await conn.execute("UPDATE automatic_records SET status='undone',result=$3::jsonb,updated_at=now() WHERE owner_id=$1 AND notification_id=$2",self.owner,nid,json.dumps(value,ensure_ascii=False))
            return value
