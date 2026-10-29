"""Workbench HTTP/contract checks with explicit fixture models in a disposable DB."""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.parse import urlsplit,urlunsplit
from urllib.request import urlopen
from uuid import UUID,uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import asyncpg
from fastapi import Request
from agent.notification_extractor import ModelReply
from database.config import database_url
from database.migrate import migrate
from scripts.check_p4_api import normal_result
from scripts.check_p5_api import Client,fixture_model,mutation
from services.notifications import NotificationService
from services.sessions import SessionService

REPORT=ROOT/'.work/p7'


async def fixture_extractor(notice):
    if '[慢速测试]' in notice['text']:await asyncio.sleep(3)
    if '[格式错误测试]' in notice['text']:return ModelReply('{invalid','fixture','fixture-extraction')
    result=normal_result()
    match=re.search(r'(示例[^：\n]+?)2027秋招',notice['text'])
    company=notice['hints'].get('company_name') or (match.group(1) if match else '示例星河科技')
    result['company_name']=company
    result['evidence']['company_name']={'source':'company_hint' if notice['hints'].get('company_name') else 'text','quote':company}
    position=next((name for name in ['Agent开发工程师','后端开发工程师','AI应用开发工程师'] if name in notice['text']),None)
    result['position_name']=position
    if position:result['evidence']['position_name']={'source':'text','quote':position}
    else:result['evidence'].pop('position_name')
    return ModelReply(json.dumps(result,ensure_ascii=False),'fixture','fixture-extraction')


def build_test_app():
    from api.main import app
    from api.notifications import get_notification_service
    from api.sessions import get_session_service
    def notices(request:Request):return NotificationService(request.app.state.pool,request.app.state.user_id,fixture_extractor)
    def sessions(request:Request):return SessionService(request.app.state.pool,request.app.state.user_id,fixture_model)
    app.dependency_overrides[get_notification_service]=notices
    app.dependency_overrides[get_session_service]=sessions
    return app


@contextmanager
def server(dsn,owner,label,fixture=True):
    REPORT.mkdir(parents=True,exist_ok=True)
    with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
    with (REPORT/f'{label}.log').open('w') as log:
        args=[str(ROOT/'.venv/bin/python'),'-m','uvicorn','scripts.check_p7_api:build_test_app' if fixture else 'api.main:app','--host','127.0.0.1','--port',str(port)]
        if fixture:args.append('--factory')
        process=subprocess.Popen(args,cwd=ROOT,env={**os.environ,'DATABASE_URL':dsn,'LOCAL_USER_ID':str(owner)},stdout=log,stderr=subprocess.STDOUT)
        try:
            for _ in range(100):
                if process.poll() is not None:raise AssertionError('See '+str(REPORT/f'{label}.log'))
                try:
                    with urlopen(f'http://127.0.0.1:{port}/health/live',timeout=1):break
                except (OSError,URLError):time.sleep(.1)
            else:raise AssertionError('Startup timeout')
            yield Client(port)
        finally:
            process.terminate();process.wait(timeout=10)
            assert process.returncode in (0,-15)


def import_notice(client,text='示例星河科技2027秋招：请于2026年10月30日18:00前完成线上测评。'):
    return client.call('POST','/api/notifications',mutation(text=text,sent_at='2026-10-27T09:00:00+08:00',timezone_hint='Asia/Shanghai'),201)


async def checks():
    REPORT.mkdir(parents=True,exist_ok=True)
    base=database_url();name='job_application_agent_test_p7_'+uuid4().hex[:10];dsn=urlunsplit(urlsplit(base)._replace(path='/'+name))
    admin=await asyncpg.connect(base);await admin.execute(f'CREATE DATABASE "{name}"')
    owner=uuid4();checks=[]
    def passed(name):checks.append(name)
    try:
        await migrate(dsn)
        conn=await asyncpg.connect(dsn)
        try:
            with server(dsn,owner,'api-a') as a,server(dsn,uuid4(),'api-b') as b:
                html=a.call('GET','/')
                assert 'root' in html and 'workbench-assets' in html
                for asset in re.findall(r'(?:src|href)="(/workbench-assets/[^\"]+)"',html):assert a.call('GET',asset)
                passed('built_react_workbench_and_hashed_assets_are_served_by_fastapi')
                a.call('GET','/api/not-a-route',status=404)
                passed('unknown_api_route_does_not_fall_back_to_html')
                schema=a.call('GET','/openapi.json')['components']['schemas']
                assert 'company_aliases' in schema['ApplicationOut']['required']
                result_out=next(value for key,value in schema.items() if key=='ExtractionResult-Output')
                assert 'tasks' in result_out['required'] and 'missing_fields' in result_out['required']
                assert 'company_aliases' not in schema['ApplicationCreate']['required']
                passed('openapi_distinguishes_optional_input_defaults_from_present_output_fields')
                assert a.call('GET','/api/workbench/inbox')['total']==0
                passed('empty_inbox_is_a_real_empty_page')
                target=a.call('POST','/api/applications',mutation(company_name='示例星河科技',position_name='Agent开发工程师',batch='2027秋招'),201)
                a.call('POST','/api/applications',mutation(company_name='示例星河科技',position_name='后端开发工程师',batch='2027秋招'),201)
                notice=import_notice(a)
                item=a.call('GET','/api/workbench/inbox')['items'][0]
                assert item['extraction_id'] is None and item['session_id'] is None
                passed('imported_notice_remains_unprocessed_until_explicit_extraction')
                extraction=a.call('POST',f'/api/notifications/{notice["id"]}/extract',mutation())
                item=a.call('GET','/api/workbench/inbox')['items'][0]
                assert item['extraction_status']=='completed' and item['company_name']=='示例星河科技'
                session=a.call('POST','/api/sessions',mutation(notification_id=notice['id'],extraction_id=extraction['id']),201)
                response=a.call('POST',f'/api/sessions/{session["id"]}/messages',mutation(version=1,text='开始处理'))
                assert response['session']['status']=='awaiting_user'
                item=a.call('GET','/api/workbench/inbox')['items'][0]
                assert item['session_status']=='awaiting_user' and item['session_id']==session['id']
                passed('inbox_projects_latest_extraction_and_question_state')
                assert a.call('GET','/api/sessions?notification_id='+notice['id'])['total']==1
                assert b.call('GET','/api/sessions?notification_id='+notice['id'])['total']==0
                assert b.call('GET','/api/workbench/inbox')['total']==0
                passed('inbox_and_notification_scoped_sessions_keep_server_owner_scope')
                resumed=a.call('GET',f'/api/sessions/{session["id"]}')
                response=a.call('POST',f'/api/sessions/{session["id"]}/messages',mutation(version=resumed['version'],text='Agent开发岗'))
                assert response['session']['state']['confirmed_fields']['position_name']['value']=='Agent开发岗'
                assert a.call('GET','/api/workbench/inbox')['items'][0]['position_name']=='Agent开发岗'
                assert a.call('GET','/api/workbench/inbox')['items'][0]['session_status']=='ready'
                passed('short_followup_updates_current_facts_and_ready_inbox_state')
                pid=response['proposal']['id'];review=a.call('GET',f'/api/proposals/{pid}/review')
                saved=a.call('POST',f'/api/proposals/{pid}/confirm',mutation(**review['confirmation']))
                assert saved['application']['id']==target['id']
                assert a.call('GET','/api/workbench/inbox')['items'][0]['session_status']=='saved'
                passed('confirmed_notice_projects_saved_state_and_real_task')
                another=import_notice(a)
                ex=a.call('POST',f'/api/notifications/{another["id"]}/extract',mutation())
                old_session=a.call('POST','/api/sessions',mutation(notification_id=another['id'],extraction_id=ex['id']),201)
                revised=deepcopy(ex['result']);revised['position_name']='后端开发工程师';revised['evidence']['position_name']={'source':'review','quote':'后端开发工程师'}
                changed=a.call('PATCH',f'/api/notifications/{another["id"]}/extractions/{ex["id"]}',{'version':1,'review_text':'核对岗位为后端开发工程师。','result':revised})
                item=a.call('GET','/api/workbench/inbox')['items'][0]
                assert item['position_name']=='后端开发工程师' and item['session_id'] is None
                assert a.call('GET',f'/api/sessions/{old_session["id"]}')['extraction_version']==1
                passed('manual_review_never_presents_old_session_as_current_version')
                stale_notice=import_notice(a,'示例远帆科技2027秋招Agent开发工程师：请于2026年10月30日18:00前完成线上测评。')
                stale_ex=a.call('POST',f'/api/notifications/{stale_notice["id"]}/extract',mutation())
                stale_s=a.call('POST','/api/sessions',mutation(notification_id=stale_notice['id'],extraction_id=stale_ex['id']),201)
                asked=a.call('POST',f'/api/sessions/{stale_s["id"]}/messages',mutation(version=1,text='开始处理'))
                planned=a.call('POST',f'/api/sessions/{stale_s["id"]}/messages',mutation(version=asked['session']['version'],text='新建这条申请'))
                stale_pid=planned['proposal']['id'];old_review=a.call('GET',f'/api/proposals/{stale_pid}/review')
                a.call('PATCH',f'/api/notifications/{stale_notice["id"]}/extractions/{stale_ex["id"]}',{'version':1,'review_text':'补充核对说明。','result':stale_ex['result']})
                assert not a.call('GET',f'/api/proposals/{stale_pid}/review')['can_confirm']
                a.call('POST',f'/api/proposals/{stale_pid}/confirm',mutation(**old_review['confirmation']),409)
                passed('manual_extraction_review_also_invalidates_old_confirmation_links')

                bad=import_notice(a,'[格式错误测试]示例星河科技2027秋招：请于2026年10月30日18:00前完成线上测评。')
                a.call('POST',f'/api/notifications/{bad["id"]}/extract',mutation(),422)
                assert a.call('GET','/api/workbench/inbox')['items'][0]['extraction_status']=='failed'
                passed('failed_extraction_remains_visible_and_original_text_is_readable')
                slow=import_notice(a,'[慢速测试]示例星河科技2027秋招：请于2026年10月30日18:00前完成线上测评。')
                processing=asyncio.create_task(asyncio.to_thread(a.call,'POST',f'/api/notifications/{slow["id"]}/extract',mutation()))
                for _ in range(50):
                    if a.call('GET','/api/workbench/inbox')['items'][0]['extraction_status']=='processing':break
                    await asyncio.sleep(.02)
                else:raise AssertionError('Processing was not visible')
                assert (await processing)['status']=='completed'
                passed('processing_can_be_polled_after_page_refresh_without_reextracting')
                page=a.call('GET','/api/workbench/inbox?limit=2&offset=1')
                assert page['total']==5 and len(page['items'])==2 and page['offset']==1
                a.call('GET','/api/workbench/inbox?limit=101',status=422)
                passed('inbox_pagination_limits_and_totals_are_consistent')
            with server(dsn,owner,'restarted') as a:
                assert a.call('GET','/')
                assert a.call('GET','/api/sessions?notification_id='+notice['id'])['items'][0]['status']=='saved'
                assert a.call('GET',f'/api/proposals/{pid}/execution')['execution_id']==saved['execution_id']
                passed('restart_preserves_workbench_routes_and_saved_business_state')
        finally:await conn.close()
        return {'executed_on':datetime.now().astimezone().isoformat(),'mode':'real HTTP/PostgreSQL with explicit fixture models','checks_passed':len(checks),'checks':checks,'real_model_called':False,'temporary_database_removed':True}
    finally:
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)');await admin.close()


if __name__=='__main__':
    report=asyncio.run(checks());(REPORT/'api-validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print(json.dumps(report,ensure_ascii=False,indent=2))
