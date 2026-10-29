"""Confirm one immutable proposal in one transaction, then return stored readback."""
import hashlib
import json
from uuid import UUID,uuid4,uuid5

from schemas.recruitment import ApplicationCreate,ApplicationFields,ApplicationPatch,TaskCreate,TaskFields,TaskPatch
from services.errors import Conflict,NotFound
from services.notifications import notification_row
from services.proposal_state import proposal_digest,task_scope
from services.recruitment import RecruitmentService,snapshot
from services.sessions import fingerprint,proposal_row,session_row


async def matching_applications(conn,owner,fields):
    values=[owner,fields['company_name'],fields['position_name']]
    clauses=["owner_id=$1",r"(lower(regexp_replace(company_name,'\s+','','g'))=lower(regexp_replace($2::text,'\s+','','g')) OR EXISTS(SELECT 1 FROM unnest(company_aliases) a WHERE lower(regexp_replace(a,'\s+','','g'))=lower(regexp_replace($2::text,'\s+','','g'))))",
        r"regexp_replace(lower(regexp_replace(position_name,'\s+','','g')),'(工程师|岗位|岗)$','')=regexp_replace(lower(regexp_replace($3::text,'\s+','','g')),'(工程师|岗位|岗)$','')"]
    if fields['batch']:
        values.append(fields['batch']);clauses.append(r"recruitment_category(batch)=recruitment_category($4::text)")
    return await conn.fetchval('SELECT count(*) FROM applications WHERE '+' AND '.join(clauses),*values)


class ConfirmationService:
    def __init__(self,pool,owner_id):
        self.pool,self.owner_id=pool,owner_id

    async def _proposal(self,conn,proposal_id,lock=False):
        row=await conn.fetchrow('SELECT * FROM action_proposals WHERE owner_id=$1 AND id=$2'+(' FOR UPDATE' if lock else ''),self.owner_id,proposal_id)
        if row is None:raise NotFound('方案不存在')
        return proposal_row(row)

    async def _execution(self,conn,proposal_id):
        row=await conn.fetchrow('SELECT result FROM proposal_executions WHERE owner_id=$1 AND proposal_id=$2',self.owner_id,proposal_id)
        return json.loads(row['result']) if row else None

    async def execution(self,proposal_id):
        async with self.pool.acquire() as conn:
            await self._proposal(conn,proposal_id)
            result=await self._execution(conn,proposal_id)
            if result is None:raise NotFound('方案尚未执行')
            return result

    async def _inspect(self,conn,proposal,session,lock=False):
        payload=proposal['payload']
        blockers=[]
        if proposal['status']!='draft':blockers.append('该方案已保存或已被新消息替代')
        if session['status']!='ready' or session['state']['proposal_id']!=proposal['id']:
            blockers.append('该方案不是当前会话的待确认方案，请重新生成')
        if payload.get('plan_version')!='p6-v1':blockers.append('这是P6之前的方案，请在会话中重新生成后核对')
        extraction_version=await conn.fetchval('SELECT version FROM notification_extractions WHERE owner_id=$1 AND id=$2'+(' FOR SHARE' if lock else ''),self.owner_id,UUID(payload['extraction_id']))
        if extraction_version!=payload['extraction_version']:blockers.append('提取结果已被重新核对，请基于最新版本生成方案')
        used=await conn.fetchval('SELECT proposal_id FROM proposal_executions WHERE owner_id=$1 AND notification_id=$2',self.owner_id,UUID(payload['notification_id']))
        intake=await conn.fetchval('SELECT EXISTS(SELECT 1 FROM intake_receipts WHERE owner_id=$1 AND notification_id=$2)',self.owner_id,UUID(payload['notification_id']))
        if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM automatic_records WHERE owner_id=$1 AND notification_id=$2 AND status IN ('saved','undone'))",self.owner_id,UUID(payload['notification_id'])):blockers.append('这条通知已自动处理，请在结果页查看或编辑')
        if intake:blockers.append('这条通知已通过录入表单保存，请查看申请并手动纠错')
        if used:blockers.append('这条通知已经保存，后续纠错请使用手动管理')
        application=None
        if payload['application_id']:
            row=await conn.fetchrow('SELECT * FROM applications WHERE owner_id=$1 AND id=$2'+(' FOR UPDATE' if lock else ''),self.owner_id,UUID(payload['application_id']))
            if row is None:
                blockers.append('目标申请已不存在')
            else:
                application=snapshot(row)
                if application['version']!=payload['expected_application_version']:blockers.append('申请版本已变化，请重新生成方案并核对')
                if application['outcome']!='active':blockers.append('申请已结束，请先显式纠错，不自动恢复')
                digest=await task_scope(conn,self.owner_id,UUID(application['id']),lock)
                if digest!=payload.get('task_set_digest'):blockers.append('申请的事项已变化，请重新生成方案并核对')
                for action in payload['task_actions']:
                    if action['task_id']:
                        task=await conn.fetchrow('SELECT version FROM tasks WHERE owner_id=$1 AND application_id=$2 AND id=$3',self.owner_id,UUID(application['id']),UUID(action['task_id']))
                        if task is None or task['version']!=action['expected_version']:
                            blockers.append('拟操作事项版本已变化，请重新核对')
                    elif action['action']=='create' and not action.get('user_evidence'):
                        fields=action['fields']
                        existing=await conn.fetchval('SELECT count(*) FROM tasks WHERE owner_id=$1 AND application_id=$2 AND type=$3 AND round IS NOT DISTINCT FROM $4',self.owner_id,UUID(application['id']),fields['type'],fields.get('round'))
                        if existing:blockers.append('已有同类型/轮次事项，不能重复创建，请重新核对')
        elif payload['new_application']:
            if await matching_applications(conn,self.owner_id,payload['new_application']):
                blockers.append('生成方案后已出现同公司/岗位/批次申请，请重新匹配')
        else:
            blockers.append('方案没有目标申请')
        return application,list(dict.fromkeys(blockers))

    async def review(self,proposal_id):
        async with self.pool.acquire() as conn,conn.transaction(isolation='repeatable_read',readonly=True):
            proposal=await self._proposal(conn,proposal_id)
            session=session_row(await conn.fetchrow('SELECT * FROM agent_sessions WHERE owner_id=$1 AND id=$2',self.owner_id,UUID(proposal['session_id'])))
            application,blockers=await self._inspect(conn,proposal,session)
            notice=notification_row(await conn.fetchrow('SELECT * FROM notifications WHERE owner_id=$1 AND id=$2',self.owner_id,UUID(proposal['payload']['notification_id'])))
            execution=await self._execution(conn,proposal_id)
        payload=proposal['payload']
        before=payload['application_before']
        after={**(before or payload['new_application']),**payload['application_changes']}
        fields={key:{'before':before.get(key) if before else None,'after':value} for key,value in after.items() if key in ApplicationFields.model_fields and (before is None or before.get(key)!=value)}
        return {'proposal':proposal,'notification':notice,'application_current':application,
            'application_diff':fields,'task_actions':payload['task_actions'],'warnings':payload['warnings'],
            'blockers':blockers,'can_confirm':not blockers,'execution':execution,
            'confirmation':{'proposal_digest':proposal_digest(payload),'session_version':session['version'],'confirmed':True},
            'review_url':f'/review/proposals/{proposal_id}'}

    @staticmethod
    def _verify(actual,expected):
        if any(actual[key]!=value for key,value in expected.items()):
            raise RuntimeError('数据库回读与确认方案不一致，事务已停止')

    async def confirm(self,proposal_id,body):
        key=fingerprint({'proposal_id':str(proposal_id),**body.model_dump(mode='json',exclude={'request_id'})})
        async with self.pool.acquire() as conn,conn.transaction():
            request_lock=int.from_bytes(hashlib.sha256(f'confirm:{self.owner_id}:{body.request_id}'.encode()).digest()[:8],signed=True)
            await conn.execute('SELECT pg_advisory_xact_lock($1)',request_lock)
            initial=await self._proposal(conn,proposal_id)
            # Same order as message admission: session first, then its proposal.
            session=session_row(await conn.fetchrow('SELECT * FROM agent_sessions WHERE owner_id=$1 AND id=$2 FOR UPDATE',self.owner_id,UUID(initial['session_id'])))
            proposal=await self._proposal(conn,proposal_id,lock=True)
            previous=await conn.fetchrow('''SELECT r.request_fingerprint,e.result FROM confirmation_requests r
                JOIN proposal_executions e ON e.owner_id=r.owner_id AND e.id=r.execution_id
                WHERE r.owner_id=$1 AND r.request_id=$2''',self.owner_id,body.request_id)
            if previous:
                if previous['request_fingerprint']!=key:raise Conflict('该确认请求编号已用于其他方案或内容')
                return json.loads(previous['result'])
            if body.proposal_digest!=proposal_digest(proposal['payload']):
                raise Conflict('确认内容与方案不一致，请重新打开核对页')
            executed=await self._execution(conn,proposal_id)
            if executed:
                await self._remember_request(conn,body.request_id,key,UUID(executed['execution_id']))
                return executed
            if body.session_version!=session['version']:
                raise Conflict('会话版本已变化，请重新打开核对页')
            service=RecruitmentService(self.pool,self.owner_id,connection=conn,
                source_reference=f'proposal:{proposal_id};notification:{proposal["payload"]["notification_id"]}')
            await service.lock_application_creation(conn)
            _,blockers=await self._inspect(conn,proposal,session,lock=True)
            if blockers:raise Conflict('；'.join(blockers))
            payload=proposal['payload']
            operations=[]
            if payload['application_id']:
                application_id=UUID(payload['application_id'])
            else:
                operation=uuid5(proposal_id,'application.create')
                values={**payload['new_application'],**{key:value for key,value in payload['application_changes'].items() if key=='stage'}}
                application=await service.create_application(ApplicationCreate(request_id=operation,**values))
                application_id=UUID(str(application['id']));operations.append(operation)
            task_ids=[]
            expected_tasks={}
            task_results=[]
            for index,action in enumerate(payload['task_actions']):
                kind=action['action']
                operation=uuid5(proposal_id,f'task.{index}.{kind}')
                if kind=='create':
                    typed=TaskCreate(request_id=operation,application_id=application_id,**action['fields'])
                    task=await service.create_task(typed)
                    expected_tasks[UUID(str(task['id']))]=typed.model_dump(exclude={'request_id','application_id'})
                elif kind in {'update','cancel'}:
                    typed=TaskPatch(request_id=operation,version=action['expected_version'],**action['fields'])
                    task=await service.update_task(UUID(action['task_id']),typed)
                    expected_tasks[UUID(str(task['id']))]=TaskFields.model_validate({**{key:action['before'][key] for key in TaskFields.model_fields},**action['fields']}).model_dump()
                else:
                    task=dict(await conn.fetchrow('SELECT * FROM tasks WHERE owner_id=$1 AND id=$2',self.owner_id,UUID(action['task_id'])))
                    expected_tasks[UUID(str(task['id']))]=TaskFields.model_validate({key:action['before'][key] for key in TaskFields.model_fields}).model_dump()
                task_id=UUID(str(task['id']));task_ids.append(task_id)
                task_results.append({'action':kind,'task_id':str(task_id),'reason':action['reason']})
                if kind!='skip':operations.append(operation)
            changes=payload['application_changes']
            current=await conn.fetchrow('SELECT * FROM applications WHERE owner_id=$1 AND id=$2',self.owner_id,application_id)
            actual_changes={key:value for key,value in changes.items() if current[key]!=value}
            if actual_changes:
                operation=uuid5(proposal_id,'application.update')
                await service.update_application(application_id,ApplicationPatch(request_id=operation,version=current['version'],**actual_changes))
                operations.append(operation)
            # Re-query all writes before recording success; mismatch aborts everything.
            app_row=await conn.fetchrow('SELECT * FROM applications WHERE owner_id=$1 AND id=$2',self.owner_id,application_id)
            app_expected=ApplicationFields.model_validate({**{key:payload['application_before'][key] for key in ApplicationFields.model_fields},**changes}).model_dump() if payload['application_before'] else None
            if payload['new_application']:
                app_expected=ApplicationFields.model_validate({**payload['new_application'],**changes}).model_dump()
            self._verify(app_row,app_expected)
            tasks=[]
            for task_id in task_ids:
                task=await conn.fetchrow('SELECT * FROM tasks WHERE owner_id=$1 AND application_id=$2 AND id=$3',self.owner_id,application_id,task_id)
                if task_id in expected_tasks:self._verify(task,expected_tasks[task_id])
                tasks.append(snapshot(task))
            events=[]
            for event in await conn.fetch('SELECT * FROM application_events WHERE owner_id=$1 AND operation_id=ANY($2::uuid[]) ORDER BY created_at,id',self.owner_id,operations):
                item=snapshot(event)
                for field in ['before_data','after_data']:item[field]=json.loads(event[field]) if event[field] is not None else None
                events.append(item)
            if len(events)!=len(operations):raise RuntimeError('变更事件回读数量不符，事务已停止')
            execution_id=uuid4()
            result={'execution_id':str(execution_id),'proposal_id':str(proposal_id),'notification_id':payload['notification_id'],
                'status':'saved','executed':True,'application':snapshot(app_row),'tasks':tasks,'task_results':task_results,
                'events':events,'saved_at':(await conn.fetchval('SELECT clock_timestamp()')).isoformat()}
            await conn.execute('''INSERT INTO proposal_executions(id,owner_id,proposal_id,notification_id,application_id,result)
                VALUES($1,$2,$3,$4,$5,$6::jsonb)''',execution_id,self.owner_id,proposal_id,UUID(payload['notification_id']),application_id,json.dumps(result,ensure_ascii=False))
            await self._remember_request(conn,body.request_id,key,execution_id)
            await conn.execute("UPDATE action_proposals SET status='confirmed' WHERE owner_id=$1 AND id=$2",self.owner_id,proposal_id)
            session['state']['execution_id']=str(execution_id)
            await conn.execute("UPDATE agent_sessions SET status='saved',version=version+1,state=$3::jsonb,updated_at=now() WHERE owner_id=$1 AND id=$2",self.owner_id,UUID(proposal['session_id']),json.dumps(session['state'],ensure_ascii=False))
        # This second read happens after commit; no model reports success for us.
        return await self.execution(proposal_id)

    async def _remember_request(self,conn,request_id,key,execution_id):
        await conn.execute('INSERT INTO confirmation_requests(owner_id,request_id,execution_id,request_fingerprint) VALUES($1,$2,$3,$4)',self.owner_id,request_id,execution_id,key)
