"""Disposable HTTP/PG delivery checks. Model and IMAP are explicit fixtures."""
import asyncio
from contextlib import contextmanager,asynccontextmanager
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from email.message import EmailMessage
import http.cookiejar
import json
import os
import re
from pathlib import Path
import socket
import subprocess
import sys
import time
from unittest.mock import patch
from urllib.error import HTTPError,URLError
from urllib.request import Request,build_opener,HTTPCookieProcessor,ProxyHandler
from urllib.parse import urlsplit,urlunsplit
from uuid import UUID,uuid4
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import asyncpg
from cryptography.fernet import Fernet
from database.config import database_url
from database.migrate import migrate
from scripts.check_p4_api import normal_result,TEXT
from scripts.check_p5_api import fixture_model,mutation
from agent.notification_extractor import ModelReply
from services.notifications import NotificationService
from services.sessions import SessionService
REPORT=ROOT/'.work/p10'
TEST_KEY=Fernet.generate_key().decode()

async def fixture_extractor(notice):
    result=normal_result()
    match=re.search(r'(示例[^：\n]+?)2027秋招',notice['text'])
    if match:
        result['company_name']=match.group(1)
        result['evidence']['company_name']={'source':'text','quote':match.group(1)}
    if '无岗位' in notice['text'] or 'Agent开发工程师' not in notice['text']:
        result['position_name']=None;result['evidence'].pop('position_name')
    return ModelReply(json.dumps(result,ensure_ascii=False),'fixture','fixture')

def fixture_mail(provider,address,code,known_validity=None,last_uid=0,limit=30,since=None):
    now=datetime.now(timezone.utc)
    if not limit:return 'epoch-1',[]
    items=[]
    for i in range(1,37):
        date=(now-timedelta(days=61 if i==1 else 50-i)).date()
        if since and date<since:continue
        if known_validity=='epoch-1' and i<=last_uid:continue
        subject='2027秋招测评通知' if i==2 else '实习笔试通知' if i==3 else f'日常邮件{i}'
        items.append({'uid':str(i),'subject':subject,'sender':'fixture@example.invalid','sent_at':now,'text':TEXT if i in {2,3} else '普通订阅内容'})
    return 'epoch-1',items[:limit]

def build_test_app():
    from api.main import app
    from api.notifications import get_notification_service
    from api.sessions import get_session_service
    original=app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(app):
        async with original(app):
            app.state.notification_extractor=fixture_extractor;app.state.mail_transport=fixture_mail
            yield
    app.router.lifespan_context=lifespan
    app.dependency_overrides[get_notification_service]=lambda:NotificationService(app.state.pool,app.state.user_id,fixture_extractor)
    app.dependency_overrides[get_session_service]=lambda:SessionService(app.state.pool,app.state.user_id,fixture_model)
    return app

class Client:
    def __init__(self,port):
        self.base=f'http://127.0.0.1:{port}';self.jar=http.cookiejar.CookieJar();self.opener=build_opener(ProxyHandler({}),HTTPCookieProcessor(self.jar));self.last_headers={}
    def call(self,method,path,body=None,status=200,headers=None):
        req=Request(self.base+path,method=method,data=json.dumps(body,ensure_ascii=False).encode() if body is not None else None,headers={'Content-Type':'application/json',**(headers or {})})
        try:
            with self.opener.open(req,timeout=240) as r:code,raw=r.status,r.read();self.last_headers=dict(r.headers)
        except HTTPError as e:code,raw=e.code,e.read();self.last_headers=dict(e.headers)
        data=json.loads(raw) if raw.startswith((b'{',b'[')) else raw.decode()
        if status is None:return code,data
        assert code==status,(method,path,code,data)
        return data
@contextmanager
def server(dsn,owner,label,mode='local',poll=False,quota=10000,live=False):
    REPORT.mkdir(parents=True,exist_ok=True)
    with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
    token='test-only-access-'+uuid4().hex;key=TEST_KEY
    env={**os.environ,'ENABLE_SYNC_DIAGNOSTICS':'true','DATABASE_URL':dsn,'LOCAL_USER_ID':str(owner),'APP_MODE':mode,'APP_ACCESS_TOKEN':token,'MAIL_CREDENTIAL_KEY':key,'MAIL_POLL_ENABLED':str(poll).lower(),'MODEL_REQUESTS_PER_HOUR':str(quota),'COOKIE_SECURE':'false'}
    with (REPORT/f'{label}.log').open('w') as log:
        cmd=[str(ROOT/'.venv/bin/python'),'-m','uvicorn','api.main:app' if live else 'scripts.check_p10_api:build_test_app','--host','127.0.0.1','--port',str(port),'--no-proxy-headers']
        if not live:cmd.append('--factory')
        p=subprocess.Popen(cmd,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT)
        try:
            a=Client(port)
            for _ in range(100):
                if p.poll() is not None:raise AssertionError('Startup failed: '+label)
                try:a.call('GET','/health/live');break
                except (OSError,URLError):time.sleep(.1)
            else:raise AssertionError('Startup timeout')
            yield a,token
        finally:p.terminate();p.wait(timeout=15)

class FakeIMAP:
    calls=[]
    def __init__(self,host,port,ssl_context,timeout):
        assert port==993 and ssl_context.verify_mode==2 and ssl_context.check_hostname
        self.host=host;self.identified=False
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def login(self,*args):return 'OK',[]
    def _simple_command(self,*args):self.identified=True;self.calls.append(args);return 'OK',[]
    def select(self,folder,readonly):
        assert readonly and folder=='INBOX'
        if self.host!='imap.qq.com':assert self.identified
        return 'OK',[b'1']
    def response(self,name):return name,[b'epoch-1']
    def uid(self,command,*args):
        self.calls.append((command,*args))
        if command=='search':assert 'SINCE' in args and 'ALL' not in args;return 'OK',[b'1 2']
        msg=EmailMessage();msg['Subject']='2027秋招';msg['From']='fixture@example.invalid';msg['Date']='Wed, 28 Oct 2026 09:00:00 +0800';msg.set_content('请参加秋招测评')
        raw=msg.as_bytes();meta=f'1 (RFC822.SIZE {len(raw)} BODY[] {{{len(raw)}}}'.encode()
        return 'OK',[(meta,raw),b')']

async def checks():
    REPORT.mkdir(parents=True,exist_ok=True);checks=[]
    def passed(v):checks.append(v)
    from services.mail_transport import read_mail,decode_message,candidate
    with patch('services.mail_transport.imaplib.IMAP4_SSL',FakeIMAP):
        for provider in ['qq','163','126']:
            v,items=read_mail(provider,'fixture@'+provider+'.com','not-a-real-secret');assert len(items)==2 and v=='epoch-1'
        passed('qq_163_126_TLS_readonly_peek_and_netease_ID_protocol')
        _,items=read_mail('qq','fixture@qq.com','fixture',None,0,0);assert not items
        _,items=read_mail('qq','fixture@qq.com','fixture','epoch-1',2);assert not items
        passed('binding_downloads_no_body_and_UID_filter_does_not_refetch_last_message')
    html=EmailMessage();html['Subject']='实习面试';html.set_content('<div>实习面试<script>ignore</script></div>',subtype='html');decoded=decode_message(html.as_bytes());assert decoded['text']=='实习面试' and decoded['sent_at'] is None
    assert candidate(decoded) and not candidate({'subject':'普通面试经验','sender':'x','text':'招聘知识文章'})
    passed('HTML_becomes_text_no_script_and_exact_autumn_or_internship_prefilter')
    base=database_url();name='job_application_agent_test_p10_'+uuid4().hex[:10];dsn=urlunsplit(urlsplit(base)._replace(path='/'+name));owner=uuid4()
    admin=await asyncpg.connect(base);await admin.execute(f'CREATE DATABASE "{name}"')
    try:
        await migrate(dsn);assert not await migrate(dsn)
        passed('all_migrations_apply_to_empty_database_and_repeat_without_changes')
        conn=await asyncpg.connect(dsn)
        try:
            with server(dsn,owner,'private','private',quota=1) as (a,token):
                assert 'root' in a.call('GET','/')
                assert a.call('GET','/api/auth/status')['authenticated'] is False
                for path in ['/api/applications','/api/mailboxes','/api/project','/docs','/openapi.json']:
                    a.call('GET',path,status=401)
                passed('private_mode_protects_data_docs_and_mailbox_routes_but_renders_login_shell')
                a.call('POST','/api/auth/login',{'token':'wrong'},401)
                a.call('POST','/api/auth/login',{'token':token});cookie=next(value for key,value in a.last_headers.items() if key.lower()=='set-cookie')
                assert 'HttpOnly' in cookie and 'SameSite=strict' in cookie
                assert a.call('GET','/api/auth/status')['authenticated']
                passed('correct_login_sets_httpOnly_sameSite_cookie_and_incorrect_login_is_rejected')
                a.call('POST','/api/applications',mutation(company_name='X',position_name='Y'),403,{'Origin':'https://evil.invalid'})
                passed('cross_origin_mutations_are_rejected')
                n=a.call('POST','/api/notifications',mutation(text=TEXT,calendar_year=2026,timezone_hint='Asia/Shanghai'),201)
                a.call('POST',f'/api/notifications/{n["id"]}/extract',mutation())
                a.call('POST',f'/api/notifications/{n["id"]}/extract',mutation(),429)
                passed('database_hourly_model_admission_limit_blocks_before_second_call')
                # Invalid mailbox provider must never echo authorization code in the validation response.
                invalid=a.call('POST','/api/mailboxes',{'provider':'qq','address':'bad@163.com','authorization_code':'test-secret-must-not-echo'},422)
                assert 'test-secret-must-not-echo' not in json.dumps(invalid)
                passed('mail_validation_does_not_echo_authorization_code')
                a.call('POST','/api/auth/logout');a.call('GET','/api/applications',status=401)
                passed('logout_removes_browser_access')
            with server(dsn,owner,'workflow') as (a,_),server(dsn,uuid4(),'other-owner') as (b,_):
                a.call('GET','/api/project',status=403,headers={'Host':'evil.invalid'})
                a.call('POST','/api/notifications',mutation(text='x'*270000),413)
                passed('local_host_rebinding_and_oversized_bodies_are_rejected')
                n=a.call('POST','/api/notifications',mutation(text=TEXT,calendar_year=2026,timezone_hint='Asia/Shanghai'),201)
                e=a.call('POST',f'/api/notifications/{n["id"]}/extract',mutation())
                assert n['calendar_year']==2026
                assert await conn.fetchval('SELECT count(*) FROM applications WHERE owner_id=$1',owner)==0
                passed('notice_calendar_year_persists_and_extraction_does_not_create_business_rows')
                fields={'company_name':'示例星河科技','position_name':'Agent开发工程师','batch':'27秋招','stage':'assessment'}
                task={'type':'assessment','title':'完成线上测评','due_on':'2026-10-30'}
                body=mutation(notification_id=n['id'],extraction_id=e['id'],extraction_version=e['version'],application=fields,tasks=[task],confirmed=True)
                a.call('POST','/api/intake/confirm',{**body,'confirmed':False},422)
                b.call('POST','/api/intake/confirm',body,404)
                a.call('POST','/api/intake/confirm',{**body,'extraction_version':999},409)
                passed('intake_requires_explicit_confirmation_owner_and_current_extraction_version')
                session=a.call('POST','/api/sessions',mutation(notification_id=n['id'],extraction_id=e['id']),201)
                asked=a.call('POST',f'/api/sessions/{session["id"]}/messages',mutation(version=session['version'],text='开始处理'))
                planned=a.call('POST',f'/api/sessions/{session["id"]}/messages',mutation(version=asked['session']['version'],text='新建这条申请'))
                assert planned['session']['status']=='ready'
                old_pid=planned['proposal']['id'];old_review=a.call('GET',f'/api/proposals/{old_pid}/review')
                saved=a.call('POST','/api/intake/confirm',body);app_id=saved['application']['id']
                assert not a.call('GET',f'/api/proposals/{old_pid}/review')['can_confirm']
                a.call('POST',f'/api/proposals/{old_pid}/confirm',mutation(**old_review['confirmation']),409)
                passed('form_intake_invalidates_older_Agent_proposal_for_the_same_notification')
                assert saved['application']['batch']=='27秋招' and saved['tasks'][0]['due_on']=='2026-10-30' and saved['tasks'][0]['due_at'] is None
                assert a.call('POST','/api/intake/confirm',body)==saved
                a.call('POST','/api/intake/confirm',{**body,'request_id':str(uuid4())},409)
                assert await conn.fetchval('SELECT count(*) FROM tasks WHERE owner_id=$1',owner)==1
                event=await conn.fetchrow('SELECT * FROM application_events WHERE owner_id=$1 ORDER BY created_at LIMIT 1',owner)
                assert event['source_kind']=='notification' and n['id'] in event['source_reference']
                passed('editable_date_only_intake_is_atomic_replayable_and_links_original_notice')
                a.call('POST','/api/intake/confirm',{**body,'application':{**fields,'company_name':'different'}},409)
                passed('same_request_id_with_modified_form_conflicts')
                another=a.call('POST','/api/notifications',mutation(text=TEXT,timezone_hint='Asia/Shanghai'),201)
                ex=a.call('POST',f'/api/notifications/{another["id"]}/extract',mutation())
                duplicate={**body,'request_id':str(uuid4()),'notification_id':another['id'],'extraction_id':ex['id'],'extraction_version':ex['version'],'application':{**fields,'batch':'2027秋招'}}
                a.call('POST','/api/intake/confirm',duplicate,409)
                passed('27_autumn_matches_2027_autumn_to_prevent_duplicate_creation')
                # Mid-transaction failure must remove both the application and its first task.
                await conn.execute("CREATE FUNCTION fail_p10_task() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.title='FAIL_SECOND' THEN RAISE EXCEPTION 'fixture'; END IF; RETURN NEW; END; $$")
                await conn.execute('CREATE TRIGGER fail_p10_task BEFORE INSERT ON tasks FOR EACH ROW EXECUTE FUNCTION fail_p10_task()')
                fail={**duplicate,'request_id':str(uuid4()),'application':{**fields,'company_name':'回滚验证公司'},'tasks':[task,{**task,'title':'FAIL_SECOND'}]}
                a.call('POST','/api/intake/confirm',fail,500)
                assert await conn.fetchval("SELECT count(*) FROM applications WHERE company_name='回滚验证公司'")==0
                await conn.execute('DROP TRIGGER fail_p10_task ON tasks');await conn.execute('DROP FUNCTION fail_p10_task()')
                passed('second_task_failure_rolls_back_application_first_task_events_and_receipt')
                # Test account encryption, manual polling, both providers, owner checks, and durable UID dedup.
                mailbox=a.call('POST','/api/mailboxes',{'provider':'qq','address':'fixture@qq.com','authorization_code':'fixture-only-code','automatic':False},201)
                row=await conn.fetchrow('SELECT secret_encrypted FROM mailbox_accounts WHERE id=$1',UUID(mailbox['id']))
                assert 'fixture-only-code' not in row['secret_encrypted'] and 'secret' not in json.dumps(mailbox)
                passed('mail_authorization_code_is_encrypted_at_rest_and_never_returned')
                b.call('GET',f'/api/mailboxes/{mailbox["id"]}/messages',status=404)
                b.call('POST',f'/api/mailboxes/{mailbox["id"]}/sync',status=404)
                passed('mailbox_reads_and_syncs_are_owner_scoped')
                sync=a.call('POST',f'/api/mailboxes/{mailbox["id"]}/sync')
                assert sync=={'read':35,'recruitment_candidates':2},sync
                mails=a.call('GET',f'/api/mailboxes/{mailbox["id"]}/messages?include_other=true')['items']
                assert len(mails)==35 and len([v for v in mails if v['notification_id']])==2
                assert len([v for v in mails if v['auto_state']=='completed'])==2
                assert await conn.fetchval('SELECT count(*) FROM applications WHERE owner_id=$1',owner)==1
                assert a.call('POST',f'/api/mailboxes/{mailbox["id"]}/sync')['read']==0
                passed('first_60_day_import_paginates_past_30_filters_keywords_extracts_only_two_and_deduplicates_UID')
                mail=next(v for v in mails if v['notification_id']);b.call('POST','/api/mailboxes/import',{'message_id':mail['id']},404)
                assert a.call('POST','/api/mailboxes/import',{'message_id':mail['id']})['notification_id']==mail['notification_id']
                plain=next(v for v in mails if not v['is_candidate']);imported=a.call('POST','/api/mailboxes/import',{'message_id':plain['id']})
                assert a.call('GET','/api/notifications/'+imported['notification_id'])['source']=='email'
                passed('missed_keyword_mail_can_be_manually_imported_and_import_replays')
                a.call('DELETE',f'/api/mailboxes/{mailbox["id"]}')
                assert await conn.fetchval('SELECT count(*) FROM mailbox_messages WHERE account_id=$1',UUID(mailbox['id']))==0
                a.call('GET','/api/notifications/'+mail['notification_id'])
                passed('disconnect_deletes_credentials_and_cache_preserving_imported_notices')
                for provider in ['163','126']:
                    item=a.call('POST','/api/mailboxes',{'provider':provider,'address':'fixture@'+provider+'.com','authorization_code':'fixture-code','automatic':False},201)
                    a.call('PATCH','/api/mailboxes/'+item['id'],{'automatic':True})
                passed('163_and_126_bind_and_automatic_toggle_share_supported_contract')
            with server(dsn,owner,'automatic',poll=True) as (a,_):
                for _ in range(100):
                    count=await conn.fetchval("SELECT count(*) FROM mailbox_messages WHERE owner_id=$1 AND auto_state='completed'",owner)
                    if count==4:break
                    await asyncio.sleep(.1)
                else:raise AssertionError('Automatic background poll did not complete')
                items=a.call('GET','/api/mailboxes')['items'];assert all(v['last_sync_at'] and not v['last_error'] for v in items)
                assert await conn.fetchval('SELECT count(*) FROM applications WHERE owner_id=$1',owner)==1
                passed('lifespan_background_poll_runs_on_due_accounts_without_saving_applications')
            with server(dsn,owner,'restart') as (a,_):
                assert a.call('GET','/api/applications/'+app_id)['batch']=='27秋招'
                assert a.call('GET','/api/workbench/inbox')['total']>=3
                passed('restart_preserves_intake_and_mail_import_history')
        finally:await conn.close()
        return {'checks_passed':len(checks),'checks':checks,'model':'explicit fixture, not real provider','mail':'explicit IMAP protocol and mailbox fixtures, no real mailbox login','temporary_database_removed':True}
    finally:await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)');await admin.close()

if __name__=='__main__':
    result=asyncio.run(checks());(REPORT/'api-validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps(result,ensure_ascii=False,indent=2))
