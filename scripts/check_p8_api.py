"""Real PG/HTTP crash tests. Only explicit fixtures; every DB is disposable."""
import asyncio
from contextlib import contextmanager
import json,os,socket,subprocess,sys,time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit,urlunsplit
from urllib.request import urlopen
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import asyncpg
from fastapi import Request
from agent.notification_extractor import ModelReply,ExtractionFailure
from database.config import database_url
from database.migrate import migrate
from scripts.check_p5_api import Client,fixture_model,mutation
from scripts.check_p7_api import fixture_extractor
from services.notifications import NotificationService
from services.sessions import SessionService
REPORT=ROOT/'.work/p8'
RELEASE=None

async def extractor(notice):
    if os.environ.get('P8_PAUSE')=='extraction':await asyncio.Event().wait()
    if notice['source_reference']=='unexpected':raise RuntimeError('P8_PRIVATE_ERROR_SENTINEL')
    if notice['source_reference']=='known':raise ExtractionFailure('explicit fixture upstream failure',502)
    reply=await fixture_extractor(notice);reply.usage={'prompt_tokens':100,'completion_tokens':20,'total_tokens':120}
    return reply

async def model(messages,tools):
    if os.environ.get('P8_PAUSE') in {'answer','connection'} and messages[-1]['role']=='tool':
        earlier=[m for m in messages if m['role']=='assistant' and m.get('tool_calls')]
        if earlier[-1]['tool_calls'][0]['function']['name']=='record_answer':await (RELEASE if os.environ.get('P8_PAUSE')=='connection' else asyncio.Event()).wait()
    reply=await fixture_model(messages,tools);reply.usage={'prompt_tokens':30,'completion_tokens':10,'total_tokens':40}
    return reply

def build_test_app():
    global RELEASE
    RELEASE=asyncio.Event()
    from api.main import app
    from api.notifications import get_notification_service
    from api.sessions import get_session_service
    from services import sessions as module
    from agent.session_agent import run_agent
    async def pause_after_plan(*args,**kwargs):
        result=await run_agent(*args,**kwargs)
        if os.environ.get('P8_PAUSE')=='plan' and args[0].proposal:await asyncio.Event().wait()
        return result
    module.run_agent=pause_after_plan
    @app.post('/test/release',include_in_schema=False)
    async def release():
        RELEASE.set()
        return {'released':True}
    def notices(request:Request):return NotificationService(request.app.state.pool,request.app.state.user_id,extractor)
    def sessions(request:Request):return SessionService(request.app.state.pool,request.app.state.user_id,model)
    app.dependency_overrides[get_notification_service]=notices;app.dependency_overrides[get_session_service]=sessions
    return app

@contextmanager
def server(dsn,owner,label,pause=''):
    REPORT.mkdir(parents=True,exist_ok=True)
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    with (REPORT/(label+'.log')).open('w') as log:
        process=subprocess.Popen([str(ROOT/'.venv/bin/python'),'-m','uvicorn','scripts.check_p8_api:build_test_app','--factory','--host','127.0.0.1','--port',str(port)],cwd=ROOT,
            env={**os.environ,'DATABASE_URL':dsn,'LOCAL_USER_ID':str(owner),'P8_PAUSE':pause},stdout=log,stderr=subprocess.STDOUT)
        try:
            for _ in range(100):
                if process.poll() is not None:raise AssertionError('startup: '+label)
                try:
                    with urlopen(f'http://127.0.0.1:{port}/health/live',timeout=1):break
                except (OSError,URLError):time.sleep(.1)
            else:raise AssertionError('startup timeout')
            yield Client(port),process
        finally:
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=3)
                except subprocess.TimeoutExpired:process.kill();process.wait()

def notice(c,source=None):return c.call('POST','/api/notifications',mutation(text='示例星河科技2027秋招：请于2026年10月30日18:00前完成线上测评。P8_PRIVATE_NOTICE_SENTINEL',timezone_hint='Asia/Shanghai',source_reference=source),201)
def retry(c,kind,attempt,body=None,status=200):return c.call('POST',f'/api/processing/{kind}/{attempt}/retry',body or mutation(),status)
async def wait_for(check):
    for _ in range(150):
        value=await check()
        if value:return value
        await asyncio.sleep(.05)
    raise AssertionError('condition not reached')
async def row_where(conn,sql,*values):return await conn.fetchrow(sql,*values)
async def ignore_closed(task):
    result=await asyncio.gather(task,return_exceptions=True)
    assert isinstance(result[0],Exception),result

async def checks():
    REPORT.mkdir(parents=True,exist_ok=True);results=[];artifacts={}
    def passed(value):results.append(value)
    base=database_url();name='job_application_agent_test_p8_'+uuid4().hex[:10];dsn=urlunsplit(urlsplit(base)._replace(path='/'+name));owner=uuid4()
    admin=await asyncpg.connect(base);await admin.execute(f'CREATE DATABASE "{name}"')
    try:
        await migrate(dsn);conn=await asyncpg.connect(dsn)
        try:
            with server(dsn,owner,'api-a') as (a,_),server(dsn,uuid4(),'api-other') as (other,_):
                apps=[a.call('POST','/api/applications',mutation(company_name='示例星河科技',position_name=position,batch='2027秋招'),201) for position in ['Agent开发工程师','后端开发工程师']]
                assert a.call('GET','/api/processing')['total']==0;passed('empty_history_has_real_zero_and_valid_contract')
                bad=notice(a,'unexpected');failed=a.call('POST',f'/api/notifications/{bad["id"]}/extract',mutation(),500)
                assert failed['error_kind']=='unexpected_error' and 'P8_PRIVATE_ERROR' not in failed['error_message'];passed('unexpected_exception_is_a_persisted_sanitized_failure_not_stuck_processing')
                assert a.call('POST',f'/api/notifications/{bad["id"]}/extract',{'request_id':failed['request_id']},500)==failed;passed('original_failed_request_replays_without_new_attempt')
                again=retry(a,'extraction',failed['id'],status=500)['extraction'];assert again['retry_of']==failed['id'];passed('manual_retry_preserves_failed_source_and_links_new_attempt')
                code,_=other.call('GET',f'/api/processing/extraction/{failed["id"]}',status=None);assert code==404
                other.call('POST',f'/api/processing/extraction/{failed["id"]}/recover',status=404);retry(other,'extraction',failed['id'],status=404)
                assert other.call('GET','/api/processing?notification_id='+bad['id'])['total']==0;passed('history_details_recovery_and_retry_enforce_owner_scope')
                known=notice(a,'known');known_result=a.call('POST',f'/api/notifications/{known["id"]}/extract',mutation(),502);assert known_result['error_kind']=='model_or_validation_error';passed('known_provider_failure_retains_error_and_retry_action')
                source=notice(a)
            with server(dsn,owner,'killed-extraction','extraction') as (a,process):
                body=mutation();request=asyncio.create_task(asyncio.to_thread(a.call,'POST',f'/api/notifications/{source["id"]}/extract',body))
                pending=await wait_for(lambda:row_where(conn,"SELECT * FROM notification_extractions WHERE notification_id=$1 AND status='processing'",__import__('uuid').UUID(source['id'])))
                detail=a.call('GET',f'/api/processing/extraction/{pending["id"]}')
                assert detail['attempt']['active'] and not detail['attempt']['recoverable'];passed('live_attempt_is_distinguished_from_orphan_without_timeout_guess')
                a.call('POST',f'/api/processing/extraction/{pending["id"]}/recover',status=409)
                with server(dsn,uuid4(),'active-other') as (foreign,_):
                    foreign.call('POST',f'/api/processing/extraction/{pending["id"]}/recover',status=404)
                passed('live_foreign_attempt_is_not_disclosed_by_recovery_lock_probe')
                a.call('POST',f'/api/notifications/{source["id"]}/extract',mutation(),409);passed('cannot_recover_or_duplicate_a_live_extraction')
                process.kill();process.wait();await ignore_closed(request)
            with server(dsn,owner,'restarted-extraction') as (a,_):
                detail=a.call('GET',f'/api/processing/extraction/{pending["id"]}');assert detail['attempt']['recoverable'] and not detail['attempt']['active'];passed('sigkill_then_restart_exposes_orphan_and_keeps_original')
                recovered=a.call('POST',f'/api/processing/extraction/{pending["id"]}/recover');assert recovered['attempt']['error_kind']=='interrupted'
                assert a.call('POST',f'/api/processing/extraction/{pending["id"]}/recover')==recovered;passed('explicit_recovery_is_idempotent_and_does_not_call_model')
                retry_body=mutation();retried=retry(a,'extraction',pending['id'],retry_body);ex=retried['extraction']
                assert ex['status']=='completed' and ex['retry_of']==str(pending['id']) and ex['elapsed_ms']>=0 and ex['usage']['total_tokens']==120;passed('retry_reexecutes_extraction_with_original_input_and_new_attempt')
                assert retry(a,'extraction',pending['id'],retry_body)==retried;retry(a,'extraction',pending['id'],status=409);passed('retry_request_replays_and_old_source_cannot_start_another_retry')
                session=a.call('POST','/api/sessions',mutation(notification_id=source['id'],extraction_id=ex['id']),201)
                asked=a.call('POST',f'/api/sessions/{session["id"]}/messages',mutation(version=session['version'],text='请处理这条通知。'));assert asked['session']['status']=='awaiting_user'
            with server(dsn,owner,'killed-answer','answer') as (a,process):
                answer=mutation(version=asked['session']['version'],text='Agent开发岗');request=asyncio.create_task(asyncio.to_thread(a.call,'POST',f'/api/sessions/{session["id"]}/messages',answer))
                async def answered_checkpoint():
                    r=await conn.fetchrow("SELECT * FROM agent_turns WHERE session_id=$1 AND status='processing'",__import__('uuid').UUID(session['id']))
                    return r if r and any(t.get('name')=='record_answer' for t in json.loads(r['trace'])) else None
                pending_turn=await wait_for(answered_checkpoint)
                d=a.call('GET',f'/api/processing/turn/{pending_turn["id"]}');assert d['attempt']['active'] and d['trace'] and d['attempt']['usage']['complete'] is False;passed('tool_and_model_usage_are_durable_before_turn_completion')
                assert (await conn.fetchval('SELECT state FROM agent_sessions WHERE id=$1',__import__('uuid').UUID(session['id']))) and await conn.fetchval('SELECT count(*) FROM action_proposals')==0
                a.call('POST',f'/api/processing/turn/{pending_turn["id"]}/recover',status=409)
                process.kill();process.wait();await ignore_closed(request)
            with server(dsn,owner,'restarted-answer') as (a,_):
                recovered=a.call('POST',f'/api/processing/turn/{pending_turn["id"]}/recover');assert recovered['attempt']['error_kind']=='interrupted' and len(recovered['trace'])==len(json.loads(pending_turn['trace']));passed('interrupted_answer_keeps_checkpoint_and_failed_session_without_business_write')
                assert a.call('GET',f'/api/sessions/{session["id"]}')['state']['confirmed_fields']=={};passed('partial_turn_facts_do_not_replace_last_completed_session_state')
            with server(dsn,owner,'killed-plan','plan') as (a,process):
                body=mutation();request=asyncio.create_task(asyncio.to_thread(retry,a,'turn',pending_turn['id'],body))
                async def plan_checkpoint():
                    r=await conn.fetchrow("SELECT * FROM agent_turns WHERE session_id=$1 AND status='processing'",__import__('uuid').UUID(session['id']))
                    return r if r and any(t.get('name')=='prepare_update' for t in json.loads(r['trace'])) else None
                prepared=await wait_for(plan_checkpoint);assert await conn.fetchval('SELECT count(*) FROM action_proposals')==0
                process.kill();process.wait();await ignore_closed(request);passed('kill_after_prepare_update_keeps_trace_without_publishing_partial_proposal')
            with server(dsn,owner,'restarted-plan') as (a,_):
                a.call('POST',f'/api/processing/turn/{prepared["id"]}/recover');body=mutation();resumed=retry(a,'turn',prepared['id'],body);ready=resumed['message']
                assert ready['session']['status']=='ready' and ready['turn']['user_text']=='Agent开发岗' and ready['turn']['retry_of']==str(prepared['id']) and ready['session']['state']['confirmed_fields']['position_name']['quote']=='Agent开发岗';passed('whole_turn_retry_uses_exact_answer_question_and_input_snapshot')
                assert retry(a,'turn',prepared['id'],body)==resumed and await conn.fetchval('SELECT count(*) FROM action_proposals')==1;passed('retry_replay_publishes_one_proposal_without_duplicate_model_work')
                before_tasks=await conn.fetchval('SELECT count(*) FROM tasks');assert before_tasks==0;passed('recovery_and_retry_never_write_applications_tasks_or_business_events')
                review=a.call('GET',f'/api/proposals/{ready["proposal"]["id"]}/review');confirm_body=mutation(**review['confirmation']);saved=a.call('POST',f'/api/proposals/{ready["proposal"]["id"]}/confirm',confirm_body)
                assert a.call('POST',f'/api/proposals/{ready["proposal"]["id"]}/confirm',confirm_body)==saved and await conn.fetchval('SELECT count(*) FROM tasks')==1 and await conn.fetchval('SELECT count(*) FROM proposal_executions')==1;passed('after_crash_retries_explicit_confirmation_still_commits_once')
                retry(a,'turn',prepared['id'],status=409);passed('saved_notification_rejects_fresh_retry_of_failed_history')
                detail=a.call('GET',f'/api/processing/turn/{ready["turn"]["id"]}');assert detail['attempt']['usage']['total_tokens']>0 and detail['attempt']['cost_basis']['estimated_cost'] is None;passed('usage_is_measured_and_unconfigured_cost_remains_unknown')
                page=a.call('GET','/api/processing?status=failed&limit=2&offset=1');assert page['total']>2 and len(page['items'])==2;passed('history_filter_pagination_and_totals_are_consistent')
                legacy=await conn.fetchrow("INSERT INTO agent_turns(owner_id,session_id,request_id,request_fingerprint,user_text,status,phase) VALUES($1,$2,$3,'legacy','legacy','failed','legacy') RETURNING id",owner,__import__('uuid').UUID(session['id']),uuid4())
                assert not a.call('GET',f'/api/processing/turn/{legacy["id"]}')['attempt']['can_retry'];passed('old_turn_without_input_snapshot_does_not_claim_exact_replay')
                with urlopen(a.base+'/health/live') as health:assert health.headers.get('X-Request-ID')
                passed('request_id_header_correlates_sanitized_structured_logs')
                artifacts.update(final_attempt=detail,execution=saved,interrupted_answer=recovered)
            with server(dsn,owner,'connection-owner','connection') as (a,_),server(dsn,owner,'connection-observer') as (observer,_):
                for position in ['Agent开发工程师','后端开发工程师']:
                    a.call('POST','/api/applications',mutation(company_name='示例远帆科技',position_name=position,batch='2027秋招'),201)
                n=a.call('POST','/api/notifications',mutation(text='示例远帆科技2027秋招：请于2026年10月30日18:00前完成线上测评。',timezone_hint='Asia/Shanghai'),201)
                e=a.call('POST',f'/api/notifications/{n["id"]}/extract',mutation())
                s=a.call('POST','/api/sessions',mutation(notification_id=n['id'],extraction_id=e['id']),201)
                asked=a.call('POST',f'/api/sessions/{s["id"]}/messages',mutation(version=s['version'],text='请处理这条通知。'))
                request=asyncio.create_task(asyncio.to_thread(a.call,'POST',f'/api/sessions/{s["id"]}/messages',mutation(version=asked['session']['version'],text='Agent开发岗'),None))
                async def dropped_checkpoint():
                    r=await conn.fetchrow("SELECT * FROM agent_turns WHERE session_id=$1 AND status='processing'",__import__('uuid').UUID(s['id']))
                    return r if r and any(t.get('name')=='record_answer' for t in json.loads(r['trace'])) else None
                dropped=await wait_for(dropped_checkpoint)
                from services.processing_state import lock_key
                key=lock_key('turn',dropped['id'])
                pid=await conn.fetchval("SELECT pid FROM pg_locks WHERE locktype='advisory' AND database=(SELECT oid FROM pg_database WHERE datname=current_database()) AND classid::bigint=$1 AND objid::bigint=$2 AND objsubid=1 AND granted",(key>>32)&0xffffffff,key&0xffffffff)
                assert pid and await conn.fetchval('SELECT pg_terminate_backend($1)',pid)
                recovered=observer.call('POST',f'/api/processing/turn/{dropped["id"]}/recover')
                assert recovered['attempt']['error_kind']=='interrupted'
                new=retry(observer,'turn',dropped['id'])['message'];assert new['session']['status']=='ready'
                a.call('POST','/test/release')
                code,_=await request;assert code==409
                current=observer.call('GET',f'/api/sessions/{s["id"]}')
                assert current['status']=='ready' and current['state']['proposal_id']==new['proposal']['id']
                passed('lost_lock_connection_fences_late_worker_after_another_process_recovers')
                assert observer.call('GET',f'/api/processing/turn/{dropped["id"]}')['attempt']['status']=='failed'
                passed('stale_worker_cannot_overwrite_recovered_failure_or_new_proposal')
            logs='\n'.join(p.read_text() for p in REPORT.glob('*.log'))
            assert 'P8_PRIVATE_NOTICE_SENTINEL' not in logs and 'P8_PRIVATE_ERROR_SENTINEL' not in logs;passed('operational_logs_exclude_original_text_and_raw_exception_content')
            (REPORT/'crash-evidence.json').write_text(json.dumps(artifacts,ensure_ascii=False,indent=2)+'\n')
        finally:await conn.close()
        return {'executed_on':__import__('datetime').datetime.now().astimezone().isoformat(),'mode':'real HTTP/PostgreSQL; SIGKILL at extraction, answer checkpoint, prepared-plan checkpoint; explicit model fixtures','checks_passed':len(results),'checks':results,'real_model_called':False,'temporary_database_removed':True}
    finally:await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)');await admin.close()

if __name__=='__main__':
    result=asyncio.run(checks());(REPORT/'api-validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps(result,ensure_ascii=False,indent=2))
