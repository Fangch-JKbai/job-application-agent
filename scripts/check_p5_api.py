"""Real HTTP/PostgreSQL, explicitly simulated model tool calls; no dev data changes."""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError,URLError
from urllib.parse import urlsplit,urlunsplit
from urllib.request import Request,urlopen
from uuid import UUID,uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import asyncpg
from fastapi import Request as FastAPIRequest
from agent.notification_extractor import ModelReply,ExtractionFailure
from agent.session_agent import AgentReply
from database.config import database_url
from database.migrate import migrate
from scripts.check_p4_api import normal_result,TEXT
from services.notifications import NotificationService
from services.sessions import SessionService

REPORT=ROOT/'.work/p5'


class Client:
    def __init__(self,port): self.base=f'http://127.0.0.1:{port}'

    def call(self,method,path,body=None,status=200):
        request=Request(self.base+path,method=method,data=json.dumps(body,ensure_ascii=False).encode() if body is not None else None,headers={'Content-Type':'application/json'})
        try:
            with urlopen(request,timeout=240) as response: code,raw=response.status,response.read()
        except HTTPError as error: code,raw=error.code,error.read()
        data=json.loads(raw) if raw.startswith((b'{',b'[')) else raw.decode()
        if status is None: return code,data
        assert code==status,(method,path,code,data)
        return data


@contextmanager
def server(dsn,owner,label,fixture=True):
    REPORT.mkdir(parents=True,exist_ok=True)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',0)); port=probe.getsockname()[1]
    with (REPORT/f'{label}.log').open('w') as log:
        target='scripts.check_p5_api:build_test_app' if fixture else 'api.main:app'
        command=[str(ROOT/'.venv/bin/python'),'-m','uvicorn',target,'--host','127.0.0.1','--port',str(port)]
        if fixture: command.append('--factory')
        process=subprocess.Popen(command,cwd=ROOT,env={**os.environ,'DATABASE_URL':dsn,'LOCAL_USER_ID':str(owner)},stdout=log,stderr=subprocess.STDOUT)
        try:
            for _ in range(100):
                if process.poll() is not None: raise AssertionError('Startup failed: '+str(REPORT/f'{label}.log'))
                try:
                    with urlopen(f'http://127.0.0.1:{port}/health/live',timeout=1): break
                except (OSError,URLError): time.sleep(.1)
            else: raise AssertionError('API startup timeout')
            yield Client(port)
        finally:
            process.terminate(); process.wait(timeout=10)
            assert process.returncode in (0,-15)


def mutation(**values): return {'request_id':str(uuid4()),**values}


def intake(client,mode='normal',text=TEXT):
    notice=client.call('POST','/api/notifications',mutation(text=text,source_reference=mode,sent_at='2026-10-27T09:00:00+08:00',timezone_hint='Asia/Shanghai'),201)
    extraction=client.call('POST',f'/api/notifications/{notice["id"]}/extract',mutation())
    session=client.call('POST','/api/sessions',mutation(notification_id=notice['id'],extraction_id=extraction['id']),201)
    return notice,extraction,session


def send(client,session,text,status=200,**values):
    return client.call('POST',f'/api/sessions/{session["id"]}/messages',mutation(version=session['version'],text=text,**values),status)


async def fixture_extractor(notification):
    result=normal_result()
    mode=notification['source_reference']
    if mode in {'ambiguous','many','batch'}:
        result['position_name']=None; result['evidence'].pop('position_name')
    if mode=='batch': result['batch']=None; result['evidence'].pop('batch')
    if mode=='alias':
        result['company_name']='星河'; result['evidence']['company_name']['quote']='星河'
    if mode=='missing_company':
        result['company_name']=None; result['evidence'].pop('company_name')
    if mode=='missing':
        result['tasks'][0]['due_at']=None
        result['missing_fields']=[{'field':'sent_at','question':'原通知是什么时候发送的？'}]
    if mode=='schedule':
        result['notification_type']='schedule_change'
        result['evidence']['notification_type']={'source':'text','quote':'改期'}
        result['tasks'][0]['type']='interview'
        result['tasks'][0]['title']='面试改期'
        result['tasks'][0]['evidence']['type']['quote']='面试'
        result['tasks'][0]['evidence']['title']['quote']='面试改期'
    return ModelReply(json.dumps(result,ensure_ascii=False),'fixture','fixture-extraction')


def tool(name,**arguments):
    return AgentReply({'role':'assistant','tool_calls':[{'id':'call_'+uuid4().hex,'type':'function','function':{'name':name,'arguments':json.dumps(arguments,ensure_ascii=False)}}]},'fixture','fixture-tools')


async def fixture_model(messages,tools):
    context=json.loads(messages[1]['content'].split('\n',1)[1])
    text=[m['content'] for m in messages if m['role']=='user'][-1]
    outputs=[json.loads(m['content']) for m in messages if m['role']=='tool']
    names=[m['tool_calls'][0]['function']['name'] for m in messages if m['role']=='assistant' and m.get('tool_calls')]
    state=context['state']
    if text=='上游失败': raise ExtractionFailure('测试替身上游失败',502)
    if text=='慢速处理': await asyncio.sleep(.12)
    if text=='无限循环': return tool('search_applications')
    if text=='尝试越权写入' and not names: return tool('create_application',company_name='不应执行')
    if text.startswith('越界详情:') and not names: return tool('get_application',application_id=text.split(':',1)[1])
    if text=='无证据补充' and not names: return tool('record_answer',field='position_name',value='伪造岗位',quote='原文没有')
    if text=='多问题' and not names:
        return tool('ask_user',questions=[{'field':'company_name','question':'哪个公司？'},{'field':'position_name','question':'什么岗位？'}])
    if outputs and 'error' in outputs[-1]:
        return tool('ask_user',questions=[{'field':'conflict','question':'请核对：'+outputs[-1]['error']}])
    if text=='新建这条申请' and not names: return tool('choose_new_application',quote=text)
    if text=='新建这条申请' and names[-1]=='choose_new_application': return tool('prepare_update')
    if text=='Agent开发岗' and not names: return tool('record_answer',field='position_name',value=text,quote=text)
    if text=='2027秋招' and not names: return tool('record_answer',field='batch',value=text,quote=text)
    if text=='原通知发送于2026-10-27T09:00:00+08:00' and not names:
        return tool('record_answer',field='sent_at',value='2026-10-27T09:00:00+08:00',quote=text)
    if names and names[-1]=='record_answer' and outputs[-1]['field']=='sent_at':
        result=deepcopy(state['result']); result['missing_fields']=[]
        result['tasks'][0]['due_at']='2026-10-30T18:00:00+08:00'
        return tool('revise_extraction',result=result)
    if state['result']['missing_fields'] and not names:
        return tool('ask_user',questions=state['result']['missing_fields'])
    if not names or names[-1] in {'record_answer','revise_extraction'}: return tool('search_applications')
    if names[-1]=='search_applications':
        found=outputs[-1]
        if text=='强选多候选' and found['items']: return tool('select_application',application_id=found['items'][0]['id'])
        if found['total']!=1:
            return tool('ask_user',questions=[{'field':'position_name' if found['total'] else 'selection','question':'请确认岗位/批次。' if found['total'] else '没有候选，要新建还是重新匹配？'}])
        return tool('select_application',application_id=found['items'][0]['id'])
    if names[-1]=='select_application': return tool('get_application',application_id=outputs[-1]['application_id'])
    if names[-1]=='get_application': return tool('get_application_tasks')
    if names[-1]=='get_application_tasks': return tool('get_upcoming_tasks',days=7)
    if names[-1]=='get_upcoming_tasks':
        if text=='改期处理':
            tasks=next(output['items'] for name,output in zip(names,outputs) if name=='get_application_tasks')
            return tool('prepare_update',task_actions=[{'draft_index':0,'action':'update','task_id':tasks[0]['id'],'reason':'通知明确改期'}])
        return tool('prepare_update')
    raise AssertionError((text,names))


def build_test_app():
    from api.main import app
    from api.notifications import get_notification_service
    from api.sessions import get_session_service
    def notice_service(request:FastAPIRequest): return NotificationService(request.app.state.pool,request.app.state.user_id,fixture_extractor)
    def session_service(request:FastAPIRequest): return SessionService(request.app.state.pool,request.app.state.user_id,fixture_model)
    app.dependency_overrides[get_notification_service]=notice_service
    app.dependency_overrides[get_session_service]=session_service
    return app


async def checks():
    REPORT.mkdir(parents=True,exist_ok=True)
    base=database_url(); name='job_application_agent_test_p5_'+uuid4().hex[:10]
    dsn=urlunsplit(urlsplit(base)._replace(path='/'+name))
    admin=await asyncpg.connect(base)
    await admin.execute(f'CREATE DATABASE "{name}"')
    checks=[]
    passed=lambda name:checks.append(name)
    owner=uuid4()
    try:
        await migrate(dsn)
        conn=await asyncpg.connect(dsn)
        try:
            with server(dsn,owner,'fixture-a') as a,server(dsn,uuid4(),'fixture-b') as b:
                app=a.call('POST','/api/applications',mutation(company_name='示例星河科技',company_aliases=['星河'],position_name='Agent开发工程师',batch='2027秋招'),201)
                _,_,session=intake(a)
                duplicate=a.call('POST','/api/sessions',{'request_id':session['request_id'],'notification_id':session['notification_id'],'extraction_id':session['extraction_id']},201)
                assert duplicate==session
                passed('session_import_is_idempotent_and_snapshots_extraction_version')
                body=mutation(version=session['version'],text='开始处理')
                ready=a.call('POST',f'/api/sessions/{session["id"]}/messages',body)
                assert ready['session']['status']=='ready' and ready['proposal']['payload']['application_id']==app['id']
                assert ready['proposal']['payload']['application_changes']=={'stage':'assessment'}
                assert ready['proposal']['payload']['expected_application_version']==1
                passed('unique_match_uses_read_tools_and_produces_versioned_draft')
                assert a.call('POST',f'/api/sessions/{session["id"]}/messages',body)==ready
                a.call('POST',f'/api/sessions/{session["id"]}/messages',{**body,'text':'换内容'},409)
                send(a,session,'旧版本',409)
                passed('message_replay_returns_original_snapshot_and_rejects_conflicting_requests')
                passed('stale_session_version_rejected')
                assert all(t['provider']=='fixture' for t in ready['turn']['trace'] if t['kind']=='model')
                assert a.call('GET',f'/api/proposals/{ready["proposal"]["id"]}')['payload']['executed'] is False
                passed('raw_tool_calls_usage_and_proposal_are_readable')
                other=a.call('POST','/api/applications',mutation(company_name='示例星河科技',position_name='后端开发工程师',batch='2027秋招'),201)
                _,_,ambiguous=intake(a,'ambiguous',TEXT.replace('Agent开发工程师',''))
                asked=send(a,ambiguous,'开始处理')
                assert asked['session']['status']=='awaiting_user' and asked['session']['state']['candidates']['total']==2 and asked['proposal'] is None
                passed('multiple_candidates_ask_before_selecting')
                resumed=asked['session']
                reply=send(a,resumed,'Agent开发岗')
                assert reply['session']['status']=='ready',reply
                assert reply['proposal']['payload']['application_id']==app['id']
                fact=reply['session']['state']['confirmed_fields']['position_name']
                assert fact['value']=='Agent开发岗' and fact['question_id']==resumed['state']['questions'][0]['id']
                passed('short_followup_is_bound_to_question_and_narrows_position')
                assert a.call('GET',f'/api/sessions/{ambiguous["id"]}')['state']['confirmed_fields']==reply['session']['state']['confirmed_fields']
                passed('confirmed_facts_are_persisted_outside_conversation_summary')
                _,_,forced=intake(a,'ambiguous',TEXT.replace('Agent开发工程师',''))
                denied=send(a,forced,'强选多候选')
                assert denied['session']['status']=='awaiting_user' and any('多个候选' in str(t.get('output')) for t in denied['turn']['trace'])
                passed('server_rejects_automatic_choice_among_multiple_matches')
                _,_,empty=intake(a)
                # Remove fixture-owned rows only through a separate company, never the dev database.
                await conn.execute("UPDATE applications SET company_name='另一公司' WHERE owner_id=$1",owner)
                empty_reply=send(a,empty,'开始处理')
                assert empty_reply['session']['state']['candidates']['total']==0
                new=send(a,empty_reply['session'],'新建这条申请')
                assert new['session']['status']=='ready' and new['proposal']['payload']['new_application']['company_name']=='示例星河科技'
                assert await conn.fetchval('SELECT count(*) FROM applications')==2
                passed('no_match_requires_explicit_user_choice_and_only_drafts_new_application')
                await conn.execute("UPDATE applications SET company_name='示例星河科技' WHERE owner_id=$1",owner)
                _,_,missing=intake(a,'missing')
                missing_reply=send(a,missing,'开始处理')
                fixed=send(a,missing_reply['session'],'原通知发送于2026-10-27T09:00:00+08:00')
                assert fixed['session']['status']=='ready',fixed
                assert fixed['session']['state']['confirmed_fields']['sent_at']['value'].startswith('2026-10-27')
                passed('missing_source_time_can_be_clarified_without_rewriting_p4_extraction')
                _,_,bad=intake(a)
                invalid=send(a,bad,'无证据补充')
                assert not invalid['session']['state']['confirmed_fields']
                assert any('逐字' in str(t.get('output')) for t in invalid['turn']['trace'])
                passed('user_facts_must_quote_current_message')
                _,_,attack=intake(a)
                attempt=send(a,attack,'尝试越权写入')
                assert any('未提供该工具' in str(t.get('output')) for t in attempt['turn']['trace'])
                passed('unknown_business_write_tool_is_never_dispatched')
                _,_,multiq=intake(a)
                multi_reply=send(a,multiq,'多问题')
                send(a,multi_reply['session'],'回答但没有问题编号',422)
                send(a,multi_reply['session'],'回答错误问题',422,reply_to_question_id=str(uuid4()))
                passed('multiple_questions_require_valid_explicit_reply_target')
                _,_,failure=intake(a)
                failed_body=mutation(version=1,text='上游失败')
                failed=a.call('POST',f'/api/sessions/{failure["id"]}/messages',failed_body,502)
                assert failed['session']['status']=='failed'
                assert a.call('POST',f'/api/sessions/{failure["id"]}/messages',failed_body,502)==failed
                again=send(a,failed['session'],'开始处理')
                assert again['session']['status']=='ready'
                passed('known_failure_saved_replayed_and_explicit_new_turn_can_continue')
                _,_,loop=intake(a)
                stopped=send(a,loop,'无限循环',422)
                assert len([t for t in stopped['turn']['trace'] if t['kind']=='model'])==10
                passed('model_tool_loop_has_explicit_turn_limit')
                _,_,slow=intake(a)
                slow_body=mutation(version=1,text='慢速处理')
                pending=asyncio.create_task(asyncio.to_thread(a.call,'POST',f'/api/sessions/{slow["id"]}/messages',slow_body))
                for _ in range(30):
                    if a.call('GET',f'/api/sessions/{slow["id"]}')['status']=='processing': break
                    await asyncio.sleep(.02)
                a.call('POST',f'/api/sessions/{slow["id"]}/messages',slow_body,409)
                send(a,slow,'另一个消息',409)
                assert (await pending)['session']['status']=='ready'
                passed('processing_visible_and_concurrent_turns_rejected_without_open_network_transaction')
                b.call('GET',f'/api/sessions/{session["id"]}',status=404)
                b.call('GET',f'/api/sessions/{session["id"]}/messages',status=404)
                b.call('GET',f'/api/proposals/{ready["proposal"]["id"]}',status=404)
                send(b,session,'开始处理',404)
                passed('sessions_messages_proposals_and_queries_are_owner_scoped')
                invalidated=send(a,ready['session'],'开始处理')
                assert a.call('GET',f'/api/proposals/{ready["proposal"]["id"]}')['status']=='superseded'
                passed('new_turn_supersedes_previous_draft_before_processing')
                oldtask=a.call('POST','/api/tasks',mutation(application_id=app['id'],type='interview',title='原面试',starts_at='2026-10-29T09:00:00+08:00',due_at='2026-10-29T10:00:00+08:00',source_timezone='Asia/Shanghai'),201)
                schedule_text=TEXT+' 面试改期'
                _,_,schedule=intake(a,'schedule',schedule_text)
                schedule_reply=send(a,schedule,'改期处理')
                assert schedule_reply['session']['status']=='ready',schedule_reply
                action=schedule_reply['proposal']['payload']['task_actions'][0]
                assert action['action']=='update' and action['task_id']==oldtask['id'] and action['expected_version']==1
                assert action['fields']['starts_at']==oldtask['starts_at']
                passed('schedule_change_targets_existing_task_and_preserves_unmentioned_fields')
                _,_,duplicate_task=intake(a,'schedule',schedule_text)
                duplicate_reply=send(a,duplicate_task,'开始处理')
                assert duplicate_reply['session']['status']=='awaiting_user' and duplicate_reply['proposal'] is None
                passed('schedule_change_cannot_create_a_duplicate_task')
                assert await conn.fetchval('SELECT count(*) FROM applications')==2
                assert await conn.fetchval('SELECT count(*) FROM tasks')==1
                assert await conn.fetchval('SELECT count(*) FROM application_events')==3
                passed('all_agent_turns_leave_application_task_event_rows_unchanged')
                _,_,alias=intake(a,'alias',TEXT.replace('示例星河科技','星河'))
                alias_reply=send(a,alias,'开始处理')
                assert alias_reply['session']['status']=='ready' and alias_reply['proposal']['payload']['application_id']==app['id']
                passed('maintained_company_alias_matches_without_guessing_new_aliases')
                _,_,no_company=intake(a,'missing_company',TEXT.replace('示例星河科技',''))
                no_company_reply=send(a,no_company,'开始处理')
                assert no_company_reply['session']['state']['candidates'].get('needs_company')
                assert no_company_reply['session']['state']['candidates']['items']==[]
                passed('missing_company_does_not_scan_unrelated_applications')
                _,_,scope=intake(a)
                foreign=send(a,scope,'越界详情:'+str(uuid4()))
                assert any('候选所在页' in str(t.get('output')) for t in foreign['turn']['trace'])
                passed('tool_detail_reads_are_limited_to_current_candidate_ids')
                await conn.execute("UPDATE applications SET outcome='rejected' WHERE id=$1",UUID(app['id']))
                _,_,closed=intake(a)
                closed_reply=send(a,closed,'开始处理')
                assert closed_reply['session']['status']=='awaiting_user' and closed_reply['proposal'] is None
                assert await conn.fetchval('SELECT outcome FROM applications WHERE id=$1',UUID(app['id']))=='rejected'
                passed('closed_application_cannot_be_reopened_or_bypassed_by_agent')
                await conn.execute("UPDATE applications SET outcome='active',stage='interview' WHERE id=$1",UUID(app['id']))
                _,_,advanced=intake(a)
                advanced_reply=send(a,advanced,'开始处理')
                assert advanced_reply['proposal']['payload']['application_changes']=={}
                passed('supplementary_assessment_never_regresses_interview_stage')
                a.call('POST','/api/applications',mutation(company_name='示例星河科技',position_name='Agent开发工程师',batch='2027实习'),201)
                _,_,batch=intake(a,'batch',TEXT.replace('Agent开发工程师','').replace('2027秋招',''))
                batch_first=send(a,batch,'开始处理')
                batch_second=send(a,batch_first['session'],'2027秋招')
                assert batch_second['session']['status']=='awaiting_user'
                batch_third=send(a,batch_second['session'],'Agent开发岗')
                assert batch_third['session']['status']=='ready' and batch_third['proposal']['payload']['application_id']==app['id']
                assert set(batch_third['session']['state']['confirmed_fields'])=={'batch','position_name'}
                passed('batch_and_position_questions_preserve_facts_across_three_turns')
                # Many-candidate page must never masquerade as a unique result.
                for index in range(6):
                    a.call('POST','/api/applications',mutation(company_name='示例星河科技',position_name=f'测试岗位{index}',batch='2027秋招'),201)
                _,_,many=intake(a,'many',TEXT.replace('Agent开发工程师',''))
                page=send(a,many,'开始处理')['session']['state']['candidates']
                assert len(page['items'])==5 and page['total']==8
                passed('candidate_context_is_bounded_and_total_preserves_hidden_ambiguity')
                # Freeze extraction even if the original is reviewed afterwards.
                assert a.call('GET',f'/api/sessions/{session["id"]}')['extraction_version']==1
                persisted=reply['session']
            with server(dsn,owner,'fixture-restarted') as restarted:
                saved=restarted.call('GET',f'/api/sessions/{persisted["id"]}')
                assert saved['state']['confirmed_fields']['position_name']['value']=='Agent开发岗'
                assert restarted.call('GET',f'/api/sessions/{persisted["id"]}/messages')['total']==2
                passed('restart_preserves_questions_answers_and_tool_history')
            assert await conn.fetchval("SELECT count(*) FROM agent_sessions WHERE status='processing'")==0
        finally: await conn.close()
        return {'mode':'real HTTP/PostgreSQL with fixture model','checks_passed':len(checks),'checks':checks,'real_model_called':False,'temporary_database_removed':True}
    finally:
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)'); await admin.close()


if __name__=='__main__':
    report=asyncio.run(checks())
    (REPORT/'api-validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False,indent=2))
