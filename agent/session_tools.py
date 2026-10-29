"""Owner-scoped reads and draft preparation. No recruitment mutation tools."""
from contextlib import asynccontextmanager
from datetime import datetime
import re
from uuid import UUID,uuid4

from pydantic import AwareDatetime,TypeAdapter

from agent.notification_extractor import ExtractionFailure
from schemas import sessions as args
from schemas.notifications import ExtractionResult
from schemas.recruitment import ApplicationFields,STAGE_ORDER,TaskFields
from services.errors import Conflict,InvalidInput
from services.notification_validation import validate_evidence
from services.recruitment import snapshot
from services.proposal_state import task_scope


TOOLS = {
    'search_applications':(args.SearchApplications,'按当前已提取/用户确认的公司、岗位、批次搜索申请。每页5条，返回total；无公司不扫描全部申请。'),
    'get_application':(args.ApplicationDetail,'读取已搜索候选的详情和当前数据版本。'),
    'select_application':(args.SelectApplication,'选中候选。唯一匹配可直接选；多候选必须引用本轮用户选择原话quote，不能自行挑一个。'),
    'get_application_tasks':(args.ApplicationTasks,'读取选中申请的已有事项，每页5条，含完成状态和版本。'),
    'get_upcoming_tasks':(args.UpcomingTasks,'读取选中申请近期事项，每次最多5条；不读取其他申请。'),
    'record_answer':(args.RecordAnswer,'记录本轮用户补充的实体、原发送时间或时区，quote逐字引用本轮输入。改变匹配字段后需重新搜索。'),
    'revise_extraction':(args.ReviseExtraction,'根据通知和已保存用户补充更新会话提取草稿。用户新事实用review证据；解决的问题从missing_fields移除。不覆盖P4原结果。'),
    'choose_new_application':(args.ChooseNewApplication,'没有匹配时，引用用户明确新建的原话选择补建；不会创建申请。先搜索，不能自动新建。'),
    'ask_user':(args.AskUser,'提出1至3个具体问题并结束本轮，保存问题编号，下轮继续。'),
    'prepare_update':(args.PrepareUpdate,'目标与问题确定后生成核对方案并结束本轮，不执行。默认新增事项；已有同类型/轮次事项须明确update/skip/cancel及task_id。确为额外新事项时create须user_quote引用用户本轮明确另建的原话。改期更新原事项，不另建。'),
}


def tool_definitions():
    return [{'type':'function','function':{'name':name,'description':description,'parameters':model.model_json_schema()}} for name,(model,description) in TOOLS.items()]


from core.categories import category_key

def normalized(value):
    return re.sub(r'\s+','',value or '').casefold()


def position_key(value):
    return re.sub(r'(工程师|岗位|岗)$','',normalized(value))


class SessionTools:
    def __init__(self,pool,owner_id,session,turn,notification,connection=None):
        self.pool,self.owner_id = pool,owner_id
        self.connection=connection
        self.db=connection if connection is not None else pool
        self.session,self.turn,self.notification = session,turn,notification
        self.state = session['state']
        self.terminal = None
        self.proposal = None

    @asynccontextmanager
    async def read_connection(self):
        if self.connection is not None:yield self.connection
        else:
            async with self.pool.acquire() as conn:yield conn

    def quote(self,quote):
        if quote not in self.turn['user_text']:
            raise InvalidInput('依据必须逐字来自本轮用户输入')
        return {'quote':quote,'turn_id':str(self.turn['id']),'question_id':self.turn['reply_to_question_id']}

    def result(self):
        return ExtractionResult.model_validate(self.state['result'])

    async def search_applications(self,body):
        result = self.result()
        if not result.company_name:
            self.state['candidates'] = {'items':[],'total':0,'needs_company':True}
            return self.state['candidates']
        values = [self.owner_id,normalized(result.company_name)]
        normalize_sql = lambda column: f"lower(regexp_replace({column},'\\s+','','g'))"
        clauses = ["owner_id=$1",f"({normalize_sql('company_name')}=$2 OR EXISTS (SELECT 1 FROM unnest(company_aliases) alias WHERE {normalize_sql('alias')}=$2))"]
        if result.position_name:
            values.append(position_key(result.position_name))
            clauses.append(f"regexp_replace({normalize_sql('position_name')},'(工程师|岗位|岗)$','')=${len(values)}")
        if result.batch:
            values.append(category_key(result.batch))
            clauses.append(f"recruitment_category(batch)=${len(values)}")
        where = ' AND '.join(clauses)
        async with self.read_connection() as conn:
            total = await conn.fetchval(f'SELECT count(*) FROM applications WHERE {where}',*values)
            rows = await conn.fetch(f'''SELECT id,company_name,company_aliases,position_name,batch,stage,outcome,version
                FROM applications WHERE {where} ORDER BY created_at,id LIMIT 5 OFFSET ${len(values)+1}''',*values,body.offset)
        found = {'items':[snapshot(row) for row in rows],'total':total,'limit':5,'offset':body.offset,
                 'criteria':{field:getattr(result,field) for field in ['company_name','position_name','batch']}}
        self.state['candidates'] = found
        return found

    def candidate(self,application_id):
        for item in self.state.get('candidates',{}).get('items',[]):
            if item['id'] == str(application_id):
                return item
        raise InvalidInput('请先搜索并读取该候选所在页')

    async def get_application(self,body):
        self.candidate(body.application_id)
        row = await self.db.fetchrow('''SELECT id,company_name,company_aliases,position_name,batch,stage,outcome,version,
            applied_at,applied_on FROM applications WHERE owner_id=$1 AND id=$2''',self.owner_id,body.application_id)
        if row is None:
            raise InvalidInput('候选已不存在，请重新搜索')
        detail = snapshot(row)
        self.state['application_detail'] = detail
        return detail

    async def select_application(self,body):
        self.candidate(body.application_id)
        if self.state['candidates']['total'] != 1 and body.quote is None:
            raise InvalidInput('有多个候选，必须先向用户询问')
        evidence = self.quote(body.quote) if body.quote else {'kind':'unique_match','criteria':self.state['candidates']['criteria']}
        self.state['selection'] = {'kind':'existing','application_id':str(body.application_id),'evidence':evidence}
        self.state['observed_tasks'] = {}
        self.state['tasks_read'] = False
        return self.state['selection']

    def selected_id(self):
        selection = self.state.get('selection')
        if not selection or selection['kind'] != 'existing':
            raise InvalidInput('请先唯一确定已有申请')
        return UUID(selection['application_id'])

    async def get_application_tasks(self,body):
        application_id = self.selected_id()
        async with self.read_connection() as conn:
            total = await conn.fetchval('SELECT count(*) FROM tasks WHERE owner_id=$1 AND application_id=$2',self.owner_id,application_id)
            rows = await conn.fetch('''SELECT id,application_id,type,title,status,round,opens_at,starts_at,ends_at,due_at,
                starts_on,due_on,source_timezone,time_note,version FROM tasks
                WHERE owner_id=$1 AND application_id=$2 ORDER BY created_at,id LIMIT 5 OFFSET $3''',self.owner_id,application_id,body.offset)
        items = [snapshot(row) for row in rows]
        self.state['observed_tasks'] = {item['id']:item for item in items}
        self.state['tasks_read'] = True
        return {'items':items,'total':total,'limit':5,'offset':body.offset}

    async def get_upcoming_tasks(self,body):
        application_id = self.selected_id()
        rows = await self.db.fetch('''SELECT id,type,title,status,starts_at,due_at,starts_on,due_on,version FROM tasks
            WHERE owner_id=$1 AND application_id=$2 AND status IN ('pending','in_progress') AND (
                starts_at BETWEEN now() AND now()+$3::int*interval '1 day' OR due_at BETWEEN now() AND now()+$3::int*interval '1 day'
                OR starts_on BETWEEN (now() AT TIME ZONE 'Asia/Shanghai')::date AND (now() AT TIME ZONE 'Asia/Shanghai')::date+$3::int
                OR due_on BETWEEN (now() AT TIME ZONE 'Asia/Shanghai')::date AND (now() AT TIME ZONE 'Asia/Shanghai')::date+$3::int)
            ORDER BY LEAST(starts_at,due_at),LEAST(starts_on,due_on),id LIMIT 5''',self.owner_id,application_id,body.days)
        return {'items':[snapshot(row) for row in rows],'limit':5,'days':body.days,'application_id':str(application_id)}

    async def record_answer(self,body):
        evidence = self.quote(body.quote)
        value = body.value
        if body.field in {'company_name','position_name','batch'} and value not in body.quote:
            raise InvalidInput('实体名称必须保留用户原话中的值')
        if body.field == 'sent_at':
            value = TypeAdapter(AwareDatetime).validate_python(value).isoformat()
        if body.field == 'timezone_hint':
            from schemas.notifications import valid_zone
            valid_zone(value)
        self.state['confirmed_fields'][body.field] = {'value':value,**evidence}
        if body.field in {'company_name','position_name','batch'}:
            self.state['result'][body.field] = value
            self.state['result']['evidence'][body.field] = {'source':'review','quote':body.quote}
            self.state['candidates'] = {}
            self.state['selection'] = None
            self.state['application_detail'] = None
            self.state['observed_tasks'] = {}
            self.state['tasks_read'] = False
        else:
            self.state['source_context'][body.field] = value
        self.state['result']['missing_fields'] = [item for item in self.state['result']['missing_fields'] if item['field'] != body.field]
        self.state['review_sources'].append(evidence)
        return {'field':body.field,'confirmed':self.state['confirmed_fields'][body.field]}

    async def revise_extraction(self,body):
        # Only real user quotes may become review evidence, never assistant summaries.
        allowed = [item['quote'] for item in self.state['review_sources']]+[self.turn['user_text']]
        review = '\n'.join(allowed+[self.state.get('initial_review_text') or ''])
        source = {**self.notification,**self.state['source_context']}
        validate_evidence(body.result,source,review)
        old = self.result()
        for field in ['company_name','position_name','batch']:
            if getattr(body.result,field) != getattr(old,field):
                raise InvalidInput('公司、岗位、批次请先通过record_answer记录本轮用户补充')
        for index,task in enumerate(body.result.tasks):
            for field,evidence in task.evidence.items():
                if evidence.source == 'review' and evidence.quote in self.turn['user_text']:
                    fact = {'value':getattr(task,field),'quote':evidence.quote,'turn_id':str(self.turn['id']),'question_id':self.turn['reply_to_question_id']}
                    self.state['confirmed_fields'][f'tasks.{index}.{field}'] = snapshot(fact)
                    self.state['review_sources'].append({key:fact[key] for key in ['quote','turn_id','question_id']})
        self.state['result'] = body.result.model_dump(mode='json')
        return {'result':self.state['result']}

    async def choose_new_application(self,body):
        evidence = self.quote(body.quote)
        candidates = self.state.get('candidates',{})
        if candidates.get('total') != 0 or candidates.get('needs_company'):
            raise InvalidInput('请先明确公司并搜索；已有候选时不能绕过冲突另建申请')
        result = self.result()
        if not result.company_name or not result.position_name:
            raise InvalidInput('补建申请需明确公司和岗位')
        self.state['selection'] = {'kind':'new','evidence':evidence}
        return self.state['selection']

    async def ask_user(self,body):
        questions = [{'id':str(uuid4()),**item.model_dump()} for item in body.questions]
        self.state['questions'] = questions
        self.terminal = ('awaiting_user','\n'.join(item['question'] for item in questions))
        return {'questions':questions}

    async def prepare_update(self,body):
        result = self.result()
        if result.missing_fields:
            raise InvalidInput('提取草稿仍有待解决字段，请先澄清并更新草稿')
        selection = self.state.get('selection')
        if not selection:
            raise InvalidInput('尚未确定申请，不能生成方案')
        if result.notification_type in {'conditional_update','other'}:
            raise InvalidInput('条件信息或含义不明的通知不能形成修改方案，请先澄清')
        application = None
        if selection['kind'] == 'existing':
            application_id = self.selected_id()
            if not self.state.get('application_detail') or self.state['application_detail']['id'] != str(application_id):
                raise InvalidInput('请先读取选中申请详情')
            if not self.state['tasks_read']:
                raise InvalidInput('请先读取选中申请已有待办')
            row = await self.db.fetchrow('SELECT * FROM applications WHERE owner_id=$1 AND id=$2',self.owner_id,application_id)
            application = snapshot(row)
            names = [application['company_name'],*application['company_aliases']]
            if ((result.company_name and normalized(result.company_name) not in {normalized(name) for name in names})
                or (result.position_name and position_key(result.position_name) != position_key(application['position_name']))
                or (result.batch and category_key(result.batch) != category_key(application['batch']))):
                raise InvalidInput('当前申请与通知公司、岗位或批次冲突，请重新匹配或核对事实')
            if application['version'] != self.state['application_detail']['version']:
                raise Conflict('申请已变化，请重新读取后生成方案')
            if application['outcome'] != 'active':
                raise InvalidInput('申请已结束，请先在手动管理中核对并显式纠错，P5不自动恢复或另建')
        elif result.notification_type in {'schedule_change','cancellation'}:
            raise InvalidInput('改期或取消必须关联已有申请及事项')
        actions = {item.draft_index:item for item in body.task_actions}
        if len(actions) != len(body.task_actions) or any(i >= len(result.tasks) for i in actions):
            raise InvalidInput('事项操作序号无效或重复')
        task_changes = []
        used_targets = set()
        for index,draft in enumerate(result.tasks):
            action = actions.get(index)
            kind = action.action if action else 'create'
            target = None
            if kind in {'update','skip','cancel'}:
                if not application or not action.task_id or str(action.task_id) not in self.state['observed_tasks']:
                    raise InvalidInput('请先读取拟更新/跳过事项所在页，并指定task_id')
                row = await self.db.fetchrow('SELECT * FROM tasks WHERE owner_id=$1 AND application_id=$2 AND id=$3',self.owner_id,UUID(application['id']),action.task_id)
                target = snapshot(row)
                if target['id'] in used_targets:
                    raise InvalidInput('同一方案不能对一个已有事项重复操作')
                used_targets.add(target['id'])
                if target['type'] != draft.type or (draft.round is not None and target['round'] != draft.round):
                    raise InvalidInput('目标事项类型或轮次与通知不符')
                if target['version'] != self.state['observed_tasks'][target['id']]['version']:
                    raise Conflict('待办版本变化，请重新读取')
                if kind in {'update','cancel'} and target['status'] in {'completed','cancelled'}:
                    raise InvalidInput('已完成或已取消事项不能由P5重开或修改，请先手动核对')
            if kind == 'create' and application:
                existing = await self.db.fetchval('''SELECT count(*) FROM tasks WHERE owner_id=$1 AND application_id=$2
                    AND type=$3 AND round IS NOT DISTINCT FROM $4''',self.owner_id,UUID(application['id']),draft.type,draft.round)
                if existing and not (action and action.user_quote):
                    raise InvalidInput('已有同类型/轮次事项，先核对是改期、重复还是新事项，再明确选择已有事项')
                if existing:
                    self.quote(action.user_quote)
            if result.notification_type in {'schedule_change','cancellation'} and kind not in {'update','cancel','skip'}:
                raise InvalidInput('改期/取消应定位已有事项，不能新增')
            if result.notification_type == 'cancellation' and kind == 'update':
                raise InvalidInput('取消通知请选择cancel或skip')
            fields = draft.model_dump(mode='json',exclude={'evidence','time_basis'})
            if kind == 'update':
                fields = {key:target[key] for key in TaskFields.model_fields}
                fields.update(draft.model_dump(mode='json',exclude={'evidence','time_basis'},exclude_none=True))
                for precise,day in [('starts_at','starts_on'),('due_at','due_on')]:
                    if getattr(draft,precise) is not None: fields[day] = None
                    if getattr(draft,day) is not None: fields[precise] = None
                fields = TaskFields.model_validate(fields).model_dump(mode='json')
            elif kind == 'cancel':
                fields = {'status':'cancelled'}
            elif kind == 'skip':
                fields = {}
            task_changes.append({'action':kind,'draft_index':index,'task_id':target['id'] if target else None,
                'expected_version':target['version'] if target else None,'before':target,
                'fields':fields,
                'reason':action.reason if action else '通知中的新事项',
                'user_evidence':self.quote(action.user_quote) if action and action.user_quote else None})
        if result.notification_type in {'schedule_change','cancellation'} and not task_changes:
            raise InvalidInput('改期/取消需明确对应的事项')
        stage_map = {'assessment_invitation':'assessment','written_test_invitation':'written_test','interview_invitation':'interview','offer_received':'offer','offer_accepted':'offer'}
        stage = stage_map.get(result.notification_type)
        before_stage = application['stage'] if application else 'submitted'
        changes = {}
        if stage and STAGE_ORDER.index(stage) > STAGE_ORDER.index(before_stage):
            changes['stage'] = stage
        if result.notification_type in {'rejection','offer_accepted'}:
            changes['outcome'] = 'rejected' if result.notification_type == 'rejection' else 'accepted'
        new_fields = None
        if selection['kind'] == 'new':
            new_fields = ApplicationFields(company_name=result.company_name,position_name=result.position_name,batch=result.batch).model_dump(mode='json')
        task_set_digest = None
        if application:
            async with self.read_connection() as conn:
                task_set_digest = await task_scope(conn,self.owner_id,UUID(application['id']))
        payload = {'plan_version':'p6-v1','task_set_digest':task_set_digest,'notification_id':str(self.session['notification_id']),'extraction_id':str(self.session['extraction_id']),
            'extraction_version':self.session['extraction_version'],'session_id':str(self.session['id']),
            'selection':selection,'application_id':application['id'] if application else None,
            'expected_application_version':application['version'] if application else None,
            'application_before':application,'new_application':new_fields,'application_changes':changes,
            'task_actions':task_changes,'extraction_result':self.state['result'],
            'confirmed_fields':self.state['confirmed_fields'],'warnings':list(result.issues),
            'executed':False}
        for draft in result.tasks:
            if draft.due_at and draft.due_at < datetime.now(draft.due_at.tzinfo):
                payload['warnings'].append('含已过去的截止时间，保存前请核对历史事项；不会自动标为完成')
        self.proposal = payload
        self.state['questions'] = []
        self.terminal = ('ready','已生成待核对方案，申请和待办尚未修改。请打开核对页，确认后保存。')
        return payload
