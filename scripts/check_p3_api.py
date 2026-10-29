"""Exercise real HTTP requests against real PostgreSQL in one temporary database."""
import asyncio
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError,URLError
from urllib.parse import urlencode,urlsplit,urlunsplit
from urllib.request import Request,urlopen
from uuid import uuid4
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import asyncpg
from database.config import database_url
from database.migrate import migrate

REPORT = ROOT / '.work/p3'


class Client:
    def __init__(self,port):
        self.base = f'http://127.0.0.1:{port}'

    def call(self,method,path,body=None,status=200,headers=None):
        request = Request(self.base+path,data=json.dumps(body,ensure_ascii=False).encode() if body is not None else None,method=method,headers={'Content-Type':'application/json',**(headers or {})})
        try:
            with urlopen(request,timeout=10) as response:
                code,raw = response.status,response.read()
        except HTTPError as error:
            code,raw = error.code,error.read()
        text = raw.decode()
        data = json.loads(text) if text.startswith(('{','[')) else text
        assert code == status,(method,path,code,data)
        return data


@contextmanager
def running_server(dsn,owner,label):
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',0))
        port = probe.getsockname()[1]
    env = {**os.environ,'DATABASE_URL':dsn,'LOCAL_USER_ID':str(owner)}
    log_path = REPORT / f'{label}.log'
    with log_path.open('w') as log:
        process = subprocess.Popen([str(ROOT/'.venv/bin/python'),'-m','uvicorn','api.main:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT)
        try:
            client = Client(port)
            for _ in range(100):
                if process.poll() is not None:
                    raise AssertionError('API startup failed; see '+str(log_path))
                try:
                    with urlopen(client.base+'/health/live',timeout=1) as response:
                        assert response.status == 200
                    break
                except (URLError,OSError):
                    time.sleep(.1)
            else:
                raise AssertionError('API startup timeout')
            yield client
        finally:
            process.terminate()
            process.wait(timeout=10)
            log.flush()
            assert process.returncode in (0,-15)
            if 'Manual management API started' in log_path.read_text():
                assert 'Manual management API stopped' in log_path.read_text()


def mutation(**fields):
    return {'request_id':str(uuid4()),**fields}


async def checks():
    REPORT.mkdir(parents=True,exist_ok=True)
    base = database_url()
    name = 'job_application_agent_test_p3_'+uuid4().hex[:10]
    dsn = urlunsplit(urlsplit(base)._replace(path='/'+name))
    admin = await asyncpg.connect(base)
    await admin.execute(f'CREATE DATABASE "{name}"')
    results = []
    owner_a,owner_b = uuid4(),uuid4()

    def passed(name):
        results.append(name)

    try:
        await migrate(dsn)
        conn = await asyncpg.connect(dsn)
        try:
            with running_server(dsn,owner_a,'server-a') as a,running_server(dsn,owner_b,'server-b') as b:
                assert 'P3' in a.call('GET','/api/project')['completed_milestones']
                schema = a.call('GET','/openapi.json')
                assert all(path in schema['paths'] for path in ['/api/applications','/api/applications/{application_id}/events','/api/tasks/upcoming'])
                assert a.call('GET','/api/applications')['total'] == 0
                passed('real_http_startup_and_openapi')

                body = mutation(company_name='示例青石科技',position_name='Agent开发工程师',batch='2027秋招',company_aliases=['示例青石'])
                app = a.call('POST','/api/applications',body,201)
                assert app['owner_id'] == str(owner_a) and app['version'] == 1 and app['data_origin'] == 'manual'
                assert a.call('POST','/api/applications',body,201) == app
                assert a.call('GET',f'/api/applications/{app["id"]}/events')['total'] == 1
                passed('application_create_exact_retry_has_one_record_and_event')
                a.call('POST','/api/applications',{**body,'company_name':'另一家公司'},409)
                passed('request_id_reuse_with_other_payload_conflicts')
                concurrent_body = mutation(company_name='示例青石科技',position_name='前端开发工程师',batch='2027实习')
                concurrent = await asyncio.gather(*[asyncio.to_thread(a.call,'POST','/api/applications',concurrent_body,201) for _ in range(2)])
                assert concurrent[0] == concurrent[1]
                assert a.call('GET',f'/api/applications/{concurrent[0]["id"]}/events')['total'] == 1
                passed('concurrent_identical_create_is_replayed_once')
                a.call('POST','/api/applications',mutation(company_name=' ',position_name='岗位'),422)
                a.call('POST','/api/applications',mutation(company_name='测试',position_name='岗位',owner_id=str(owner_b)),422)
                passed('required_fields_and_client_owner_override_rejected')
                page = a.call('GET','/api/applications?'+urlencode({'company':'青石','position':'Agent','stage':'submitted','outcome':'active','limit':1}))
                assert page['total'] == 1 and page['items'][0]['id'] == app['id']
                assert a.call('GET','/api/applications?limit=1&offset=1')['total'] == 2
                passed('application_filters_and_pagination')

                edit = mutation(version=app['version'],notes='准备面试')
                app = a.call('PATCH',f'/api/applications/{app["id"]}',edit)
                assert app['version'] == 2 and app['notes'] == '准备面试'
                assert a.call('PATCH',f'/api/applications/{app["id"]}',edit) == app
                a.call('PATCH',f'/api/applications/{app["id"]}',mutation(version=1,notes='旧版本'),409)
                passed('edit_retry_and_stale_version_conflict')
                app = a.call('PATCH',f'/api/applications/{app["id"]}',mutation(version=app['version'],notes=None))
                assert app['notes'] is None
                a.call('PATCH',f'/api/applications/{app["id"]}',mutation(version=app['version'],company_name=None),422)
                a.call('PATCH',f'/api/applications/{app["id"]}',mutation(version=app['version']),422)
                passed('explicit_null_clear_and_empty_patch_semantics')
                app = a.call('PATCH',f'/api/applications/{app["id"]}',mutation(version=app['version'],stage='interview'))
                a.call('PATCH',f'/api/applications/{app["id"]}',mutation(version=app['version'],stage='assessment'),422)
                app = a.call('PATCH',f'/api/applications/{app["id"]}',mutation(version=app['version'],stage='assessment',correction_reason='上一轮手动填错阶段'))
                events = a.call('GET',f'/api/applications/{app["id"]}/events')
                correction = next(e for e in events['items'] if e['reason'] == '上一轮手动填错阶段')
                assert correction['event_type'] == 'user_correction' and correction['before_data']['stage'] == 'interview' and correction['after_data']['stage'] == 'assessment'
                passed('explicit_correction_preserves_before_after_and_reason')
                app = a.call('PATCH',f'/api/applications/{app["id"]}',mutation(version=app['version'],outcome='rejected'))
                a.call('POST','/api/tasks',mutation(application_id=app['id'],type='assessment',title='不可静默恢复申请'),409)
                a.call('PATCH',f'/api/applications/{app["id"]}',mutation(version=app['version'],outcome='active'),422)
                app = a.call('PATCH',f'/api/applications/{app["id"]}',mutation(version=app['version'],outcome='active',correction_reason='已核对，原拒绝记录有误'))
                app = a.call('PATCH',f'/api/applications/{app["id"]}',mutation(version=app['version'],stage='interview'))
                passed('ended_application_requires_explicit_correction_before_new_task')

                now = datetime.now(timezone.utc)
                future = (now+timedelta(days=1)).isoformat()
                past = (now-timedelta(days=1)).isoformat()
                local_today = now.astimezone(ZoneInfo('Asia/Shanghai')).date()
                day = (local_today+timedelta(days=2)).isoformat()
                task_body = mutation(application_id=app['id'],type='assessment',title='补充测评',due_at=future,source_timezone='UTC')
                task = a.call('POST','/api/tasks',task_body,201)
                assert a.call('POST','/api/tasks',task_body,201) == task
                assert a.call('GET',f'/api/applications/{app["id"]}')['stage'] == 'interview'
                passed('task_create_retry_and_no_implicit_application_stage_change')
                date_task = a.call('POST','/api/tasks',mutation(application_id=app['id'],type='interview',title='时间待定的面试',starts_on=day),201)
                assert date_task['starts_at'] is None and date_task['starts_on'] == day
                passed('date_only_task_has_no_invented_midnight')
                for fields in [
                    {'due_at':future},
                    {'due_at':future,'source_timezone':'Mars/Unknown'},
                    {'due_at':'2026-10-30T18:00:00','source_timezone':'Asia/Shanghai'},
                    {'starts_at':future,'ends_at':past,'source_timezone':'UTC'},
                    {'starts_at':future,'starts_on':day,'source_timezone':'UTC'},
                    {'status':'overdue'},
                ]:
                    a.call('POST','/api/tasks',mutation(application_id=app['id'],type='assessment',title='错误时间案例',**fields),422)
                passed('aware_time_timezone_precision_order_and_status_validation')
                a.call('PATCH',f'/api/tasks/{task["id"]}',mutation(version=task['version'],due_on=day),422)
                task = a.call('PATCH',f'/api/tasks/{task["id"]}',mutation(version=task['version'],due_at=None,due_on=day))
                assert task['due_at'] is None and task['due_on'] == day
                passed('partial_time_change_validates_merged_record')
                completed_body = mutation(version=task['version'],status='completed')
                task = a.call('PATCH',f'/api/tasks/{task["id"]}',completed_body)
                assert a.call('PATCH',f'/api/tasks/{task["id"]}',completed_body) == task
                a.call('PATCH',f'/api/tasks/{task["id"]}',mutation(version=task['version'],status='pending'),422)
                task = a.call('PATCH',f'/api/tasks/{task["id"]}',mutation(version=task['version'],status='pending',correction_reason='完成状态误点'))
                passed('completion_retry_and_explicit_reopening')

                overdue = a.call('POST','/api/tasks',mutation(application_id=app['id'],type='assessment',title='已过期的测评',due_at=past,source_timezone='UTC'),201)
                assert overdue['is_overdue'] is True and overdue['status'] == 'pending'
                done_overdue = a.call('PATCH',f'/api/tasks/{overdue["id"]}',mutation(version=overdue['version'],status='completed'))
                assert done_overdue['is_overdue'] is False
                unscheduled = a.call('POST','/api/tasks',mutation(application_id=app['id'],type='material_submission',title='补充材料，未给时间'),201)
                past_date = a.call('POST','/api/tasks',mutation(application_id=app['id'],type='other',title='仅有过去日期',due_on=(local_today-timedelta(days=2)).isoformat()),201)
                assert past_date['is_overdue'] is False
                passed('overdue_is_derived_without_changing_status_or_guessing_date_precision')
                cancelled = a.call('POST','/api/tasks',mutation(application_id=app['id'],type='interview',title='已取消的面试',starts_on=day,status='cancelled'),201)
                exact = a.call('POST','/api/tasks',mutation(application_id=app['id'],type='written_test',title='明天笔试',starts_at=future,source_timezone='UTC'),201)
                upcoming = a.call('GET','/api/tasks/upcoming?days=7')
                ids = {item['id'] for item in upcoming['items']}
                assert {task['id'],date_task['id'],exact['id']} <= ids
                assert {overdue['id'],unscheduled['id'],cancelled['id'],past_date['id']}.isdisjoint(ids)
                filtered = a.call('GET','/api/tasks?'+urlencode({'application_id':app['id'],'type':'assessment','status':'pending'}))
                assert [item['id'] for item in filtered['items']] == [task['id']]
                passed('task_filters_and_upcoming_calendar_window')

                other_app = b.call('POST','/api/applications',mutation(company_name='另一用户的公司',position_name='AI应用'),201)
                other_task = b.call('POST','/api/tasks',mutation(application_id=other_app['id'],type='interview',title='另一用户的面试',starts_on=day),201)
                a.call('GET',f'/api/applications/{other_app["id"]}',status=404,headers={'X-User-ID':str(owner_b)})
                a.call('PATCH',f'/api/applications/{other_app["id"]}',mutation(version=1,notes='错误归属'),404)
                a.call('GET',f'/api/applications/{other_app["id"]}/events',status=404)
                a.call('GET',f'/api/tasks/{other_task["id"]}',status=404)
                a.call('PATCH',f'/api/tasks/{other_task["id"]}',mutation(version=1,status='completed'),404)
                a.call('POST','/api/tasks',mutation(application_id=other_app['id'],type='other',title='错误挂载'),404)
                assert other_task['id'] not in {item['id'] for item in a.call('GET','/api/tasks/upcoming')['items']}
                assert a.call('GET','/api/applications?company='+urlencode({'q':'另一用户'})[2:])['total'] == 0
                passed('two_server_identities_isolate_reads_writes_events_and_headers')
                a.call('GET','/api/applications/not-a-uuid',status=422)
                a.call('GET',f'/api/applications/{uuid4()}',status=404)
                a.call('GET','/api/tasks?limit=0',status=422)
                passed('invalid_identifiers_missing_records_and_page_parameters')

                await conn.execute('''CREATE FUNCTION fail_p3_event() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
                    IF NEW.after_data->>'title' = 'P3强制回滚' THEN RAISE EXCEPTION 'intentional test event failure'; END IF;
                    RETURN NEW; END; $$;
                    CREATE TRIGGER fail_p3_event BEFORE INSERT ON application_events FOR EACH ROW EXECUTE FUNCTION fail_p3_event();''')
                previous_count = await conn.fetchval('SELECT count(*) FROM tasks')
                a.call('POST','/api/tasks',mutation(application_id=app['id'],type='other',title='P3强制回滚'),500)
                assert await conn.fetchval('SELECT count(*) FROM tasks') == previous_count
                assert await conn.fetchval("SELECT count(*) FROM application_events WHERE after_data->>'title'='P3强制回滚'") == 0
                await conn.execute('DROP TRIGGER fail_p3_event ON application_events; DROP FUNCTION fail_p3_event()')
                a.call('POST','/api/tasks',mutation(application_id=app['id'],type='other',title='回滚后正常提交'),201)
                passed('http_event_failure_rolls_back_business_and_pool_remains_usable')

                current = a.call('GET',f'/api/applications/{app["id"]}')
                events_before = a.call('GET',f'/api/applications/{app["id"]}/events')['total']
                def competing_edit(note):
                    try:
                        value = a.call('PATCH',f'/api/applications/{app["id"]}',mutation(version=current['version'],notes=note))
                        return value
                    except AssertionError as error:
                        assert error.args[0][2] == 409
                        return None
                outcomes = await asyncio.gather(*[asyncio.to_thread(competing_edit,note) for note in ['并发编辑A','并发编辑B']])
                assert sum(item is not None for item in outcomes) == 1
                app = a.call('GET',f'/api/applications/{app["id"]}')
                assert app['version'] == current['version']+1
                assert a.call('GET',f'/api/applications/{app["id"]}/events')['total'] == events_before+1
                passed('real_http_competing_edits_have_one_commit_and_one_conflict')
                historical = a.call('PATCH',f'/api/applications/{app["id"]}',edit)
                assert historical['version'] == 2 and historical['notes'] == '准备面试'
                assert a.call('GET',f'/api/applications/{app["id"]}')['version'] == app['version']
                passed('old_request_replay_returns_original_result_without_overwriting_new_state')
                timeline = a.call('GET',f'/api/applications/{app["id"]}/events?limit=2')
                assert len(timeline['items']) == 2 and timeline['total'] > 2
                assert all(isinstance(item['after_data'],dict) and 'request_fingerprint' not in item for item in timeline['items'])
                passed('event_timeline_pagination_and_structured_snapshots')
            with running_server(dsn,owner_a,'server-a-restarted') as restarted:
                stored = restarted.call('GET',f'/api/applications/{app["id"]}')
                assert stored['version'] == app['version'] and stored['notes'] == app['notes']
                assert restarted.call('GET',f'/api/tasks/{task["id"]}')['status'] == 'pending'
            passed('http_restart_preserves_application_and_task_state')
        finally:
            await conn.close()
        return {'checked_on':'2026-10-27','mode':'real HTTP + PostgreSQL','checks_passed':len(results),'checks':results,'servers_stopped':True,'scope':'Manual API behavior only; no model, Agent, UI, email or production deployment.'}
    finally:
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


if __name__ == '__main__':
    result = asyncio.run(checks())
    result['temporary_database_removed'] = True
    (REPORT/'validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))
