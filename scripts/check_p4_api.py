"""Real HTTP/PostgreSQL checks with an explicitly simulated model response."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit,urlunsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import asyncpg
from fastapi import Request
from agent.notification_extractor import ExtractionFailure,ModelReply
from database.config import database_url
from database.migrate import migrate
from scripts.check_p3_api import running_server
from services.notifications import NotificationService

COUNTS = {}
TEXT = '示例星河科技2027秋招Agent开发工程师：请于2026年10月30日18:00前完成线上测评。'


def normal_result():
    evidence = lambda quote: {'source':'text','quote':quote}
    return {
        'company_name':'示例星河科技','position_name':'Agent开发工程师','batch':'2027秋招','notification_type':'assessment_invitation',
        'evidence':{'company_name':evidence('示例星河科技'),'position_name':evidence('Agent开发工程师'),'batch':evidence('2027秋招'),'notification_type':evidence('线上测评')},
        'tasks':[{'type':'assessment','title':'完成线上测评','due_at':'2026-10-30T18:00:00+08:00','source_timezone':'Asia/Shanghai','time_basis':{'due_at':'explicit'},'evidence':{'type':evidence('线上测评'),'title':evidence('完成线上测评'),'due_at':evidence('2026年10月30日18:00'),'source_timezone':{'source':'timezone_hint','quote':'Asia/Shanghai'}}}],
        'missing_fields':[],'issues':[],
    }


async def fixture_extractor(notification):
    mode = notification['source_reference'] or 'normal'
    COUNTS[mode] = COUNTS.get(mode,0)+1
    if mode == 'upstream_failure':
        raise ExtractionFailure('测试替身：上游不可用',502)
    if mode == 'slow':
        await asyncio.sleep(.5)
    result = normal_result()
    if mode == 'bad_quote':
        result['evidence']['company_name']['quote'] = '原文没有这句话'
    if mode == 'bad_timezone_quote':
        result['tasks'][0]['evidence']['source_timezone']['quote'] = 'UTC'
    if mode == 'relative_guess':
        result['tasks'][0]['time_basis']['due_at'] = 'relative'
    if mode == 'unknown_relative':
        result['tasks'][0]['due_at'] = None
        result['tasks'][0]['evidence']['due_at']['quote'] = '本周五18:00'
        result['tasks'][0]['time_basis']['due_at'] = 'relative'
        result['missing_fields'] = [{'field':'sent_at','question':'原通知是哪一天发出的？'}]
    if mode == 'date_only':
        result['notification_type']='interview_invitation'
        result['evidence']['notification_type']['quote']='面试'
        result['tasks']=[{'type':'interview','title':'参加面试','starts_on':'2026-10-30','source_timezone':None,'time_note':'具体时刻另行通知','time_basis':{'starts_on':'explicit'},'evidence':{'type':{'source':'text','quote':'面试'},'title':{'source':'text','quote':'面试'},'starts_on':{'source':'text','quote':'2026年10月30日'},'time_note':{'source':'text','quote':'具体时刻另行通知'}}}]
    if mode == 'hint':
        result['company_name']='示例星河科技'
        result['evidence']['company_name']={'source':'company_hint','quote':'示例星河科技'}
    if mode == 'bad_owner_field':
        result['owner_id']=str(uuid4())
    text = json.dumps(result,ensure_ascii=False)
    if mode == 'bad_json':
        text='{bad json'
    if mode == 'empty':
        text=''
    return ModelReply(text,'fixture','simulated-model',None,'length' if mode=='truncated' else 'stop')


def build_test_app():
    from api.main import app
    from api.notifications import get_notification_service

    def service(request:Request):
        return NotificationService(request.app.state.pool,request.app.state.user_id,fixture_extractor)

    app.dependency_overrides[get_notification_service]=service

    @app.get('/testing/fixture-counts')
    async def counts():
        return COUNTS
    return app


async def checks(report_dir=None):
    # Reuse the HTTP harness but point uvicorn at this test-only app factory.
    from contextlib import contextmanager
    import os,socket,subprocess,time
    from urllib.request import urlopen
    from urllib.error import URLError
    from scripts.check_p3_api import Client
    report = report_dir or ROOT/'.work/p4'
    report.mkdir(parents=True,exist_ok=True)
    base=database_url()
    name='job_application_agent_test_p4_'+uuid4().hex[:10]
    dsn=urlunsplit(urlsplit(base)._replace(path='/'+name))
    admin=await asyncpg.connect(base)
    await admin.execute(f'CREATE DATABASE "{name}"')
    results=[]

    @contextmanager
    def server(owner,label):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0)); port=probe.getsockname()[1]
        path=report/f'{label}.log'
        with path.open('w') as log:
            process=subprocess.Popen([str(ROOT/'.venv/bin/python'),'-m','uvicorn','scripts.check_p4_api:build_test_app','--factory','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env={**os.environ,'DATABASE_URL':dsn,'LOCAL_USER_ID':str(owner)},stdout=log,stderr=subprocess.STDOUT)
            try:
                for _ in range(100):
                    if process.poll() is not None: raise AssertionError('See '+str(path))
                    try:
                        with urlopen(f'http://127.0.0.1:{port}/health/live',timeout=1): break
                    except (URLError,OSError): time.sleep(.1)
                else: raise AssertionError('Test API startup timeout')
                yield Client(port)
            finally:
                process.terminate(); process.wait(timeout=10)
                assert process.returncode in (0,-15)

    def passed(value): results.append(value)
    def intake(client,mode='normal',text=TEXT,**values):
        return client.call('POST','/api/notifications',{'request_id':str(uuid4()),'text':text,'source_reference':mode,'sent_at':'2026-10-27T09:00:00+08:00','timezone_hint':'Asia/Shanghai',**values},201)
    def extract(client,notice,key=None,status=200):
        return client.call('POST',f'/api/notifications/{notice["id"]}/extract',{'request_id':key or str(uuid4())},status)

    try:
        await migrate(dsn)
        conn=await asyncpg.connect(dsn)
        try:
            with server(uuid4(),'fixture-a') as a,server(uuid4(),'fixture-b') as b:
                body={'request_id':str(uuid4()),'text':' \n'+TEXT+'\n ','source':'email','sent_at':'2026-10-27T09:00:00+08:00','timezone_hint':'Asia/Shanghai'}
                original=a.call('POST','/api/notifications',body,201)
                assert original['text']==body['text']
                assert a.call('POST','/api/notifications',body,201)==original
                a.call('POST','/api/notifications',{**body,'text':'另一份通知'},409)
                a.call('POST','/api/notifications',{'request_id':str(uuid4()),'text':'  '},422)
                a.call('POST','/api/notifications',{**body,'request_id':str(uuid4()),'owner_id':str(uuid4())},422)
                passed('original_whitespace_import_retry_conflict_and_input_validation')
                assert a.call('GET',f'/api/notifications/{original["id"]}')['text']==body['text']
                assert a.call('GET','/api/notifications?limit=1')['total']==1
                passed('notification_original_readback_and_pagination')
                result=extract(a,original)
                assert result['status']=='completed' and result['provider']=='fixture'
                assert result['result']['tasks'][0]['due_at'] is not None and result['raw_output']
                assert result['result']==result['model_result'] and result['reviewed_result'] is None
                passed('structured_result_evidence_raw_output_and_metadata_saved')
                key=str(uuid4()); notice=intake(a)
                first=extract(a,notice,key)
                count=a.call('GET','/testing/fixture-counts')['normal']
                assert extract(a,notice,key)['id']==first['id']
                assert a.call('GET','/testing/fixture-counts')['normal']==count
                another=intake(a)
                extract(a,another,key,409)
                passed('extract_request_retry_does_not_call_model_again')
                slow=intake(a,'slow'); slow_key=str(uuid4())
                in_flight=asyncio.create_task(asyncio.to_thread(extract,a,slow,slow_key))
                await asyncio.sleep(.2)
                history=a.call('GET',f'/api/notifications/{slow["id"]}/extractions')
                assert history['items'][0]['status']=='processing'
                extract(a,slow,slow_key,409)
                assert (await in_flight)['status']=='completed'
                assert a.call('GET','/testing/fixture-counts')['slow']==1
                passed('processing_is_visible_and_duplicate_inflight_call_is_rejected')
                for mode,status in [('bad_json',422),('empty',422),('bad_quote',422),('bad_timezone_quote',422),('relative_guess',422),('bad_owner_field',422),('truncated',422),('upstream_failure',502)]:
                    bad=intake(a,mode,sent_at=None if mode=='relative_guess' else '2026-10-27T09:00:00+08:00')
                    failure=extract(a,bad,status=status)
                    assert failure['status']=='failed' and failure['error_status']==status
                    assert a.call('GET',f'/api/notifications/{bad["id"]}')['text']==TEXT
                    if mode!='upstream_failure': assert failure['raw_output'] is not None
                    same_key=failure['request_id']; before=a.call('GET','/testing/fixture-counts')[mode]
                    assert extract(a,bad,same_key,status)['id']==failure['id']
                    assert a.call('GET','/testing/fixture-counts')[mode]==before
                passed('known_model_failures_preserve_original_raw_output_and_failed_attempt')
                passed('failed_request_retry_returns_failure_without_hidden_repair_or_rebilling')
                relative_text=TEXT.replace('2026年10月30日18:00','本周五18:00')
                relative=intake(a,'unknown_relative',relative_text,sent_at=None)
                relative_result=extract(a,relative)['result']
                assert relative_result['tasks'][0]['due_at'] is None and relative_result['missing_fields'][0]['field']=='sent_at'
                passed('unknown_original_date_can_remain_null_with_a_question')
                date_text='示例星河科技2027秋招Agent开发工程师：面试日期2026年10月30日，具体时刻另行通知。'
                dated=intake(a,'date_only',date_text)
                dated_result=extract(a,dated)['result']['tasks'][0]
                assert dated_result['starts_on']=='2026-10-30' and dated_result['starts_at'] is None
                passed('date_only_extraction_does_not_invent_a_clock_time')
                hinted=intake(a,'hint',TEXT.replace('示例星河科技',''),hints={'company_name':'示例星河科技'})
                hinted_result=extract(a,hinted)['result']
                assert hinted_result['evidence']['company_name']['source']=='company_hint'
                passed('user_hint_evidence_is_distinct_from_original_text')

                original_model=deepcopy(first['model_result'])
                revised=deepcopy(original_model)
                revised['tasks'][0]['due_at']='2026-10-31T12:00:00+08:00'
                revised['tasks'][0]['evidence']['due_at']={'source':'review','quote':'2026年10月31日12:00'}
                review_body={'version':first['version'],'review_text':'已向HR核对，实际截止为2026年10月31日12:00。','result':revised}
                reviewed=a.call('PATCH',f'/api/notifications/{notice["id"]}/extractions/{first["id"]}',review_body)
                assert reviewed['model_result']==original_model and reviewed['result']['tasks'][0]['due_at'].startswith('2026-10-31T12:00:00')
                assert reviewed['version']==2 and reviewed['raw_output']==first['raw_output']
                a.call('PATCH',f'/api/notifications/{notice["id"]}/extractions/{first["id"]}',review_body,409)
                again=a.call('PATCH',f'/api/notifications/{notice["id"]}/extractions/{first["id"]}',{'version':2,'review_text':'继续保留上述核对结果。','result':reviewed['result']})
                assert '实际截止' in again['review_text'] and again['version']==3
                passed('manual_review_preserves_model_result_raw_text_and_review_evidence')
                passed('review_version_conflicts_and_previous_review_evidence_is_retained')
                history=a.call('GET',f'/api/notifications/{notice["id"]}/extractions?limit=1')
                assert history['total']==1 and history['items'][0]['version']==3
                passed('extraction_history_and_review_readback')
                b.call('GET',f'/api/notifications/{notice["id"]}',status=404)
                b.call('GET',f'/api/notifications/{notice["id"]}/extractions',status=404)
                extract(b,notice,status=404)
                b.call('GET',f'/api/notifications/{notice["id"]}/extractions/{first["id"]}',status=404)
                b.call('PATCH',f'/api/notifications/{notice["id"]}/extractions/{first["id"]}',review_body,404)
                passed('notification_extraction_and_review_are_owner_scoped')
                assert await conn.fetchval('SELECT count(*) FROM applications')==0
                assert await conn.fetchval('SELECT count(*) FROM tasks')==0
                assert await conn.fetchval('SELECT count(*) FROM application_events')==0
                passed('extracting_and_reviewing_never_updates_applications_tasks_or_events')
                try:
                    await conn.execute("UPDATE notifications SET text='改写原文' WHERE id=$1",__import__('uuid').UUID(notice['id']))
                except asyncpg.ObjectNotInPrerequisiteStateError:
                    pass
                else:
                    raise AssertionError('Original notification was mutable')
                passed('database_prevents_original_notification_rewrite')
            assert await conn.fetchval("SELECT count(*) FROM notification_extractions WHERE status='processing'")==0
            passed('completed_and_failed_attempts_are_persisted_after_server_shutdown')
        finally:
            await conn.close()
        return {'checked_on':'2026-10-27','mode':'real HTTP/PostgreSQL with fixture model','checks_passed':len(results),'checks':results,'real_model_called':False,'temporary_database_removed':True}
    finally:
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


if __name__=='__main__':
    result=asyncio.run(checks())
    (ROOT/'.work/p4/api-validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))
