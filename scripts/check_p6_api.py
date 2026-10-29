"""Confirmation checks against real HTTP/PostgreSQL with explicit fixture models."""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
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
from scripts.check_p5_api import Client,fixture_model,mutation,tool
from services.notifications import NotificationService
from services.sessions import SessionService

REPORT=ROOT/'.work/p6'
SERIAL=0
SUFFIX='请于2026年10月30日18:00前完成线上测评。'


async def fixture_extractor(notification):
    result=normal_result()
    result['company_name']=notification['hints']['company_name']
    result['evidence']['company_name']={'source':'company_hint','quote':result['company_name']}
    mode=notification['source_reference']
    evidence=lambda quote:{'source':'text','quote':quote}
    if mode=='multi':
        other=deepcopy(result['tasks'][0]);other.update(type='material_submission',title='提交材料')
        other['evidence'].update(type=evidence('提交材料'),title=evidence('提交材料'))
        result['tasks'].append(other)
    if mode in {'date','cancel'}:
        result['notification_type']='interview_invitation' if mode=='date' else 'cancellation'
        result['evidence']['notification_type']=evidence('面试' if mode=='date' else '取消面试')
        result['tasks']=[{'type':'interview','title':'参加面试' if mode=='date' else '取消面试','starts_on':'2026-10-30' if mode=='date' else None,
            'time_basis':{'starts_on':'explicit'} if mode=='date' else {},
            'evidence':{'type':evidence('面试'),'title':evidence('面试'),'starts_on':evidence('2026年10月30日')} if mode=='date' else {'type':evidence('面试'),'title':evidence('取消面试')}}]
    if mode=='reminder':result['notification_type']='reminder'
    if mode in {'rejection','offer','accepted'}:
        result['notification_type']={'rejection':'rejection','offer':'offer_received','accepted':'offer_accepted'}[mode]
        result['evidence']['notification_type']=evidence({'rejection':'未通过','offer':'录用通知','accepted':'已接受Offer'}[mode])
        result['tasks']=[]
    return ModelReply(json.dumps(result,ensure_ascii=False),'fixture','fixture-extraction')


async def p6_fixture_model(messages,tools):
    user=[m['content'] for m in messages if m['role']=='user'][-1]
    names=[m['tool_calls'][0]['function']['name'] for m in messages if m['role']=='assistant' and m.get('tool_calls')]
    if names and names[-1]=='get_upcoming_tasks' and user in {'取消事项','保留已完成事项'}:
        for message in messages:
            if message['role']=='tool':
                output=json.loads(message['content'])
                if output.get('items') and 'application_id' in output['items'][0]:
                    return tool('prepare_update',task_actions=[{'draft_index':0,'action':'cancel' if user=='取消事项' else 'skip','task_id':output['items'][0]['id'],'reason':'通知取消' if user=='取消事项' else '已完成者忽略提醒'}])
    return await fixture_model(messages,tools)


def build_test_app():
    from api.main import app
    from api.notifications import get_notification_service
    from api.sessions import get_session_service
    def notices(request:Request):return NotificationService(request.app.state.pool,request.app.state.user_id,fixture_extractor)
    def sessions(request:Request):return SessionService(request.app.state.pool,request.app.state.user_id,p6_fixture_model)
    app.dependency_overrides[get_notification_service]=notices
    app.dependency_overrides[get_session_service]=sessions
    return app


@contextmanager
def server(dsn,owner,label,fixture=True):
    REPORT.mkdir(parents=True,exist_ok=True)
    with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
    with (REPORT/f'{label}.log').open('w') as log:
        target='scripts.check_p6_api:build_test_app' if fixture else 'api.main:app'
        args=[str(ROOT/'.venv/bin/python'),'-m','uvicorn',target,'--host','127.0.0.1','--port',str(port)]
        if fixture:args.append('--factory')
        process=subprocess.Popen(args,cwd=ROOT,env={**os.environ,'DATABASE_URL':dsn,'LOCAL_USER_ID':str(owner)},stdout=log,stderr=subprocess.STDOUT)
        try:
            for _ in range(100):
                if process.poll() is not None:raise AssertionError('See '+str(REPORT/f'{label}.log'))
                try:
                    with urlopen(f'http://127.0.0.1:{port}/health/live',timeout=1):break
                except (OSError,URLError):time.sleep(.1)
            else:raise AssertionError('API startup timeout')
            yield Client(port)
        finally:
            process.terminate();process.wait(timeout=10)
            assert process.returncode in (0,-15)


def draft(client,mode='normal',existing=True,company=None,stage='submitted'):
    global SERIAL
    SERIAL+=1
    company=company or f'示例P6第{SERIAL}科技'
    app=None;task=None
    if existing:
        app=client.call('POST','/api/applications',mutation(company_name=company,position_name='Agent开发工程师',batch='2027秋招',stage=stage),201)
        if mode in {'cancel','reminder','rejection'}:
            task=client.call('POST','/api/tasks',mutation(application_id=app['id'],type='assessment' if mode=='reminder' else 'interview',title='完成线上测评' if mode=='reminder' else '原面试',starts_at='2026-10-29T09:00:00+08:00',source_timezone='Asia/Shanghai',status='completed' if mode=='reminder' else 'pending'),201)
    suffix={'date':'面试日期2026年10月30日，具体时刻另行通知。','cancel':'取消面试。','multi':SUFFIX+'请提交材料。','rejection':'本次申请未通过。','offer':'这是录用通知。','accepted':'用户已接受Offer。'}.get(mode,SUFFIX)
    notice=client.call('POST','/api/notifications',mutation(text=company+'2027秋招Agent开发工程师：'+suffix,source_reference=mode,hints={'company_name':company},sent_at='2026-10-27T09:00:00+08:00',timezone_hint='Asia/Shanghai'),201)
    extraction=client.call('POST',f'/api/notifications/{notice["id"]}/extract',mutation())
    session=client.call('POST','/api/sessions',mutation(notification_id=notice['id'],extraction_id=extraction['id']),201)
    text={'cancel':'取消事项','reminder':'保留已完成事项'}.get(mode,'开始处理')
    response=client.call('POST',f'/api/sessions/{session["id"]}/messages',mutation(version=1,text=text))
    if not existing:
        response=client.call('POST',f'/api/sessions/{session["id"]}/messages',mutation(version=response['session']['version'],text='新建这条申请'))
    assert response['session']['status']=='ready',response
    proposal=response['proposal']
    review=client.call('GET',f'/api/proposals/{proposal["id"]}/review')
    return {'app':app,'old_task':task,'notice':notice,'extraction':extraction,'session':response['session'],'proposal':proposal,'review':review}


def confirmation(context,**values):return mutation(**context['review']['confirmation'],**values)
def confirm(client,context,body=None,status=200):return client.call('POST',f'/api/proposals/{context["proposal"]["id"]}/confirm',body or confirmation(context),status)


async def checks():
    REPORT.mkdir(parents=True,exist_ok=True)
    base=database_url();name='job_application_agent_test_p6_'+uuid4().hex[:10]
    dsn=urlunsplit(urlsplit(base)._replace(path='/'+name))
    admin=await asyncpg.connect(base);await admin.execute(f'CREATE DATABASE "{name}"')
    results=[];owner=uuid4()
    def passed(name):
        results.append(name)
        (REPORT/'api-progress.json').write_text(json.dumps({'checks_completed':results},ensure_ascii=False,indent=2)+'\n')
    try:
        await migrate(dsn)
        conn=await asyncpg.connect(dsn)
        async def counts():return {table:await conn.fetchval(f'SELECT count(*) FROM {table}') for table in ['applications','tasks','application_events','proposal_executions','confirmation_requests']}
        try:
            with server(dsn,owner,'api-a') as a,server(dsn,uuid4(),'api-b') as b:
                context=draft(a)
                review=context['review'];pid=context['proposal']['id']
                assert review['can_confirm'] and review['notification']['text']==context['notice']['text']
                assert review['application_diff']['stage']=={'before':'submitted','after':'assessment'}
                assert review['task_actions'][0]['fields']['due_at'].startswith('2026-10-30T18:00:00')
                assert await conn.fetchval('SELECT count(*) FROM tasks')==0
                passed('review_has_target_before_after_time_and_original_without_business_writes')
                assert a.call('GET','/review/proposals/'+pid).startswith('<!doctype html>')
                assert 'textContent' in a.call('GET','/review/assets/review.js')
                passed('dedicated_review_page_and_assets_are_served')
                before=await counts();body=confirmation(context)
                a.call('POST',f'/api/proposals/{pid}/confirm',{key:value for key,value in body.items() if key!='confirmed'},422)
                a.call('POST',f'/api/proposals/{pid}/confirm',{**body,'confirmed':False},422)
                a.call('POST',f'/api/proposals/{pid}/confirm',{**body,'fields':{'stage':'offer'}},422)
                assert await counts()==before
                passed('confirmation_requires_explicit_true_and_rejects_arbitrary_update_fields')
                a.call('POST',f'/api/proposals/{pid}/confirm',{**body,'proposal_digest':'0'*64},409)
                a.call('POST',f'/api/proposals/{pid}/confirm',{**body,'session_version':999},409)
                assert await counts()==before
                passed('confirmation_is_bound_to_exact_payload_digest_and_session_version')
                saved=confirm(a,context,body)
                assert saved['executed'] and saved['status']=='saved' and saved['application']['stage']=='assessment'
                assert len(saved['tasks'])==1 and len(saved['events'])==2
                assert all(event['source_kind']=='notification' and pid in event['source_reference'] for event in saved['events'])
                assert a.call('GET',f'/api/proposals/{pid}/execution')==saved
                assert a.call('GET',f'/api/tasks/{saved["tasks"][0]["id"]}')['title']==saved['tasks'][0]['title']
                passed('confirmation_atomically_saves_application_task_notification_events_and_readback')
                assert a.call('GET',f'/api/sessions/{context["session"]["id"]}')['status']=='saved'
                assert not a.call('GET',f'/api/proposals/{pid}/review')['can_confirm']
                a.call('POST',f'/api/sessions/{context["session"]["id"]}/messages',mutation(version=context['session']['version']+1,text='再处理一次'),409)
                passed('saved_session_cannot_generate_another_implicit_write')
                stable=await counts()
                assert confirm(a,context,body)==saved
                alias_body=confirmation(context)
                assert confirm(a,context,alias_body)==saved
                assert confirm(a,context,alias_body)==saved
                after=await counts();assert {k:v for k,v in after.items() if k!='confirmation_requests'}=={k:v for k,v in stable.items() if k!='confirmation_requests'}
                passed('same_and_new_confirmation_request_ids_replay_one_execution')
                a.call('PATCH',f'/api/applications/{saved["application"]["id"]}',mutation(version=saved['application']['version'],notes='确认后的手动备注'))
                assert confirm(a,context,body)==saved
                passed('replay_returns_saved_snapshot_without_overwriting_later_manual_edits')
                other=draft(a)
                confirm(a,other,body,409)
                passed('reusing_confirmation_request_id_for_another_proposal_is_rejected')
                b.call('GET',f'/api/proposals/{pid}/review',status=404)
                b.call('GET',f'/api/proposals/{pid}/execution',status=404)
                b.call('GET',f'/review/proposals/{pid}',status=404)
                confirm(b,context,body,404)
                passed('review_confirmation_and_execution_are_owner_scoped')
                concurrent=draft(a)
                first,second=await asyncio.gather(asyncio.to_thread(confirm,a,concurrent),asyncio.to_thread(confirm,a,concurrent))
                assert first['execution_id']==second['execution_id']
                assert await conn.fetchval('SELECT count(*) FROM proposal_executions WHERE proposal_id=$1',UUID(concurrent['proposal']['id']))==1
                passed('concurrent_confirmation_commits_exactly_one_execution')
                stale=draft(a)
                a.call('PATCH',f'/api/applications/{stale["app"]["id"]}',mutation(version=1,notes='别处修改'))
                before=await counts();confirm(a,stale,status=409);assert await counts()==before
                assert not a.call('GET',f'/api/proposals/{stale["proposal"]["id"]}/review')['can_confirm']
                passed('changed_application_version_requires_a_new_review_without_partial_write')
                stale_tasks=draft(a,'cancel')
                a.call('PATCH',f'/api/tasks/{stale_tasks["old_task"]["id"]}',mutation(version=1,title='手动改名'))
                before=await counts();confirm(a,stale_tasks,status=409);assert await counts()==before
                passed('changed_task_version_invalidates_old_proposal')
                added=draft(a)
                a.call('POST','/api/tasks',mutation(application_id=added['app']['id'],type='other',title='后来新增的事项'),201)
                before=await counts();confirm(a,added,status=409);assert await counts()==before
                passed('new_task_is_detected_even_when_application_version_did_not_change')
                superseded=draft(a)
                new_turn=a.call('POST',f'/api/sessions/{superseded["session"]["id"]}/messages',mutation(version=superseded['session']['version'],text='开始处理'))
                before=await counts();confirm(a,superseded,status=409);assert await counts()==before
                assert new_turn['proposal']['id']!=superseded['proposal']['id']
                passed('superseded_proposal_cannot_be_confirmed')
                same_notice=draft(a)
                session=a.call('POST','/api/sessions',mutation(notification_id=same_notice['notice']['id'],extraction_id=same_notice['extraction']['id']),201)
                another=a.call('POST',f'/api/sessions/{session["id"]}/messages',mutation(version=1,text='开始处理'))
                another_context={'proposal':another['proposal'],'review':a.call('GET',f'/api/proposals/{another["proposal"]["id"]}/review')}
                confirm(a,same_notice)
                before=await counts();confirm(a,another_context,status=409);assert await counts()==before
                passed('same_notification_cannot_be_applied_again_from_another_session')
                new=draft(a,existing=False)
                before=await counts();created=confirm(a,new);after=await counts()
                assert after['applications']==before['applications']+1 and after['tasks']==before['tasks']+1
                assert created['application']['stage']=='assessment'
                assert {event['event_type'] for event in created['events']}=={'application_created','task_created'}
                passed('confirmed_new_application_and_first_task_are_created_together')
                race_company='示例同一目标新建科技'
                race1=draft(a,existing=False,company=race_company);race2=draft(a,existing=False,company=race_company)
                def raw_confirm(context):return a.call('POST',f'/api/proposals/{context["proposal"]["id"]}/confirm',confirmation(context),None)
                race_results=await asyncio.gather(asyncio.to_thread(raw_confirm,race1),asyncio.to_thread(raw_confirm,race2))
                assert sorted(item[0] for item in race_results)==[200,409]
                assert await conn.fetchval('SELECT count(*) FROM applications WHERE company_name=$1',race_company)==1
                passed('competing_new_application_proposals_recheck_matches_inside_transaction')
                cancel=draft(a,'cancel',stage='interview');cancelled=confirm(a,cancel)
                assert cancelled['tasks'][0]['id']==cancel['old_task']['id'] and cancelled['tasks'][0]['status']=='cancelled'
                assert datetime.fromisoformat(cancelled['tasks'][0]['starts_at'])==datetime.fromisoformat(cancel['old_task']['starts_at'])
                assert cancelled['events'][0]['event_type']=='task_cancelled'
                passed('cancel_updates_original_task_and_preserves_time_and_history')
                skip=draft(a,'reminder');before=await counts();skipped=confirm(a,skip);after=await counts()
                assert after['tasks']==before['tasks'] and after['application_events']==before['application_events']
                assert skipped['tasks'][0]['status']=='completed' and skipped['tasks'][0]['version']==1
                passed('completed_reminder_is_skipped_without_reopening_or_duplicate_events')
                dated=draft(a,'date');dated_saved=confirm(a,dated)
                assert dated_saved['tasks'][0]['starts_on']=='2026-10-30' and dated_saved['tasks'][0]['starts_at'] is None
                passed('date_only_precision_survives_confirmation_and_database_readback')
                offer=confirm(a,draft(a,'offer'))
                assert offer['application']['stage']=='offer' and offer['application']['outcome']=='active'
                accepted=confirm(a,draft(a,'accepted'))
                assert accepted['application']['outcome']=='accepted'
                passed('received_offer_is_separate_from_user_reported_acceptance')
                rejected=draft(a,'rejection',stage='interview');rejection=confirm(a,rejected)
                assert rejection['application']['stage']=='interview' and rejection['application']['outcome']=='rejected'
                assert a.call('GET',f'/api/tasks/{rejected["old_task"]["id"]}')['status']=='pending'
                passed('rejection_preserves_stage_and_does_not_silently_cancel_other_tasks')
                try:
                    await conn.execute("UPDATE action_proposals SET payload=payload || '{\"arbitrary\":true}'::jsonb WHERE id=$1",UUID(other['proposal']['id']))
                except asyncpg.ObjectNotInPrerequisiteStateError:pass
                else:raise AssertionError('Payload was mutable')
                passed('database_prevents_changing_the_reviewed_proposal_payload')
                # Inject failure at the event write, after new application/task insert.
                failed=draft(a,existing=False)
                fail_body=confirmation(failed)
                await conn.execute("""CREATE FUNCTION p6_fail_event() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
                    IF NEW.source_kind='notification' AND NEW.event_type='task_created' THEN RAISE EXCEPTION 'test event failure'; END IF; RETURN NEW; END; $$;
                    CREATE TRIGGER p6_fail BEFORE INSERT ON application_events FOR EACH ROW EXECUTE FUNCTION p6_fail_event()""")
                before=await counts();confirm(a,failed,fail_body,500);assert await counts()==before
                assert a.call('GET',f'/api/proposals/{failed["proposal"]["id"]}/review')['can_confirm']
                passed('event_failure_rolls_back_application_task_execution_and_confirmation_request')
                await conn.execute('DROP TRIGGER p6_fail ON application_events; DROP FUNCTION p6_fail_event()')
                retried=confirm(a,failed,fail_body)
                assert retried['executed'] and confirm(a,failed,fail_body)==retried
                passed('same_confirmation_can_retry_after_rolled_back_failure_without_duplicates')
                multi=draft(a,'multi')
                await conn.execute("""CREATE FUNCTION p6_fail_task() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
                    IF NEW.title='提交材料' THEN RAISE EXCEPTION 'test second task failure'; END IF; RETURN NEW; END; $$;
                    CREATE TRIGGER p6_fail BEFORE INSERT ON tasks FOR EACH ROW EXECUTE FUNCTION p6_fail_task()""")
                before=await counts();confirm(a,multi,status=500);assert await counts()==before
                assert a.call('GET',f'/api/applications/{multi["app"]["id"]}')['stage']=='submitted'
                passed('failure_in_second_task_rolls_back_first_task_and_all_events')
                await conn.execute('DROP TRIGGER p6_fail ON tasks; DROP FUNCTION p6_fail_task()')
                mismatch=draft(a)
                await conn.execute("""CREATE FUNCTION p6_rewrite_task() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
                    NEW.title='unexpected database rewrite'; RETURN NEW; END; $$;
                    CREATE TRIGGER p6_rewrite BEFORE INSERT ON tasks FOR EACH ROW EXECUTE FUNCTION p6_rewrite_task()""")
                before=await counts();confirm(a,mismatch,status=500);assert await counts()==before
                passed('readback_mismatch_aborts_instead_of_reporting_success')
                await conn.execute('DROP TRIGGER p6_rewrite ON tasks; DROP FUNCTION p6_rewrite_task()')
                changed_by_trigger=draft(a,'cancel')
                await conn.execute("""CREATE FUNCTION p6_rewrite_update() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
                    NEW.title='unreviewed task title'; RETURN NEW; END; $$;
                    CREATE TRIGGER p6_rewrite BEFORE UPDATE ON tasks FOR EACH ROW EXECUTE FUNCTION p6_rewrite_update()""")
                before=await counts();confirm(a,changed_by_trigger,status=500);assert await counts()==before
                assert a.call('GET',f'/api/tasks/{changed_by_trigger["old_task"]["id"]}')['status']=='pending'
                passed('readback_also_protects_unchanged_fields_during_cancel')
                await conn.execute('DROP TRIGGER p6_rewrite ON tasks; DROP FUNCTION p6_rewrite_update()')
                saved_for_restart=(context,body,saved)
            with server(dsn,owner,'api-restarted') as restarted:
                context,body,saved=saved_for_restart
                assert confirm(restarted,context,body)==saved
                passed('restart_preserves_confirmed_execution_and_exact_replay')
        finally:await conn.close()
        return {'mode':'real HTTP/PostgreSQL with fixture P4/P5 models','checks_passed':len(results),'checks':results,'real_model_called':False,'temporary_database_removed':True}
    finally:
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)');await admin.close()


if __name__=='__main__':
    result=asyncio.run(checks())
    (REPORT/'api-validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))
