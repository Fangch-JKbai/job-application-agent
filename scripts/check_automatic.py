import asyncio,json,os,socket,subprocess,sys,time
from contextlib import contextmanager,asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit
from uuid import uuid4,UUID
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import asyncpg
from database.config import database_url
from database.migrate import migrate
from scripts.check_p10_api import Client,mutation,fixture_mail
from scripts.check_p4_api import normal_result,TEXT
from agent.notification_extractor import ModelReply
REPORT=ROOT/'.work/automatic'
async def extractor(n):
 r=normal_result();mode=n['source_reference'] or ('missing_position' if '投递成功' in n['text'] and '测评' not in n['text'] else 'assessment');company=n['hints'].get('company_name') or '示例星河科技';r['company_name']=company;r['evidence']['company_name']={'source':'company_hint' if n['hints'].get('company_name') else 'text','quote':company}
 if mode in {'submission','missing_position','old_other'}:
  r['tasks']=[];r['notification_type']='other' if mode=='old_other' else 'application_received';r['evidence']['notification_type']={'source':'text','quote':'投递成功' if '投递成功' in n['text'] else '感谢您应聘'}
 if mode=='missing_position':r['position_name']=None;r['evidence'].pop('position_name',None);r['missing_fields']=[{'field':'position_name','question':'岗位？'}]
 if mode=='no_company':r['company_name']=None;r['evidence'].pop('company_name')
 if mode in {'schedule','cancel'}:
  r['notification_type']='schedule_change' if mode=='schedule' else 'cancellation';r['tasks'][0]['due_at']=None;r['tasks'][0]['due_on']='2026-11-01';r['tasks'][0]['time_basis']={'due_on':'explicit'};r['tasks'][0]['evidence'].pop('due_at');r['tasks'][0]['evidence']['due_on']={'source':'text','quote':'2026年11月1日'}
 if mode=='rejection':r['notification_type']='rejection';r['tasks']=[]
 if mode=='other':r['notification_type']='other';r['tasks']=[]
 if mode=='bad':return ModelReply('{bad','fixture','fixture')
 return ModelReply(json.dumps(r,ensure_ascii=False),'fixture','fixture')
def build_test_app():
 from api.main import app
 orig=app.router.lifespan_context
 @asynccontextmanager
 async def lifespan(app):
  async with orig(app):
   app.state.notification_extractor=extractor;app.state.mail_transport=fixture_mail
   yield
 app.router.lifespan_context=lifespan
 return app
@contextmanager
def server(dsn,owner,label='api',*,diagnostics=True):
 REPORT.mkdir(parents=True,exist_ok=True)
 with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
 with (REPORT/(label+'.log')).open('w') as log:
  p=subprocess.Popen([str(ROOT/'.venv/bin/python'),'-m','uvicorn','scripts.check_automatic:build_test_app','--factory','--host','127.0.0.1','--port',str(port),'--no-proxy-headers'],cwd=ROOT,env={**os.environ,'DATABASE_URL':dsn,'LOCAL_USER_ID':str(owner),'APP_MODE':'local','ENABLE_SYNC_DIAGNOSTICS':str(diagnostics).lower(),'MODEL_REQUESTS_PER_HOUR':'10000','MAIL_POLL_ENABLED':'false'},stdout=log,stderr=subprocess.STDOUT)
  try:
   a=Client(port)
   for _ in range(100):
    if p.poll() is not None:raise AssertionError('Startup failed')
    try:a.call('GET','/health/live');break
    except OSError:time.sleep(.1)
   else:raise AssertionError('Startup timeout: '+label)
   yield a
  finally:p.terminate();p.wait(timeout=15)
def notice(a,mode='assessment',company='示例星河科技'):
 return a.call('POST','/api/notifications',mutation(text=TEXT+' 投递成功，简历进入简历库。新安排2026年11月1日',source_reference=mode,timezone_hint='Asia/Shanghai',hints={'company_name':company}),201)
def process(a,n):return a.call('POST',f'/api/notifications/{n["id"]}/process')
async def checks():
 REPORT.mkdir(parents=True,exist_ok=True);base=database_url();name='job_application_agent_test_auto_'+uuid4().hex[:8];dsn=urlunsplit(urlsplit(base)._replace(path='/'+name));owner=uuid4();admin=await asyncpg.connect(base);await admin.execute(f'CREATE DATABASE "{name}"');checks=[]
 def ok(v):checks.append(v)
 try:
  await migrate(dsn);conn=await asyncpg.connect(dsn)
  try:
   with server(dsn,owner) as a,server(dsn,uuid4(),'other') as b:
    n=notice(a,'submission');saved=process(a,n);assert saved['status']=='saved' and saved['application']['stage']=='submitted' and a.call('GET','/api/tasks')['total']==0;ok('submission_receipt_saves_without_questions_or_tasks')
    assert process(a,n)==saved and a.call('GET','/api/applications')['total']==1;ok('same_notice_replay')
    b.call('POST',f'/api/notifications/{n["id"]}/process',status=404);b.call('POST',f'/api/notifications/{n["id"]}/undo',status=404);ok('owner_isolation')
    e=notice(a,'missing_position','示例岗位未知公司');m=process(a,e);assert m['status']=='saved' and m['application']['position_name'] is None;ok('missing_role_and_questions_do_not_block')
    assert process(a,notice(a,'old_other','示例旧回执公司'))['status']=='saved';ok('legacy_other_submission_supported')
    receipt=a.call('POST','/api/notifications',mutation(text='示例回执公司2027秋招Agent开发工程师：感谢您应聘本公司，您的简历已经发送至我们的简历数据库。',source_reference='old_other',hints={'company_name':'示例回执公司'}),201);assert process(a,receipt)['status']=='saved';ok('legacy_thanks_for_applying_database_receipt_supported')
    for pos in ['Agent开发工程师','后端开发工程师']:a.call('POST','/api/applications',mutation(company_name='示例多岗位公司',position_name=pos,batch='27秋招'),201)
    assert process(a,notice(a,'missing_position','示例多岗位公司'))['status']=='unresolved';ok('ambiguous_matches_stay_in_inbox')
    assert process(a,notice(a,'no_company'))['status']=='unresolved';ok('missing_company_not_fabricated')
    z=a.call('POST','/api/notifications',mutation(text=TEXT,source_reference='other',hints={'company_name':'示例宣传公司'}),201);assert process(a,z)['status']=='ignored';ok('non_progress_notice_archived')
    bad=notice(a,'bad');assert process(a,bad)['status']=='failed' and process(a,bad)['status']=='failed';ok('failed_extraction_not_silently_retried')
    p=process(a,notice(a,company='示例测评公司'));aid=p['application_id'];assert p['application']['stage']=='assessment' and a.call('GET','/api/tasks?application_id='+aid)['total']==1;ok('assessment_saves_application_and_task')
    assert process(a,notice(a,company='示例测评公司'))['application_id']==aid and a.call('GET','/api/tasks?application_id='+aid)['total']==1;ok('semantic_duplicate_no_extra_task')
    shifted=process(a,notice(a,'schedule','示例测评公司'));assert shifted['status']=='saved' and a.call('GET','/api/tasks?application_id='+aid)['items'][0]['due_on']=='2026-11-01';ok('schedule_updates_original_task')
    undo=a.call('POST',f'/api/notifications/{shifted["notification_id"]}/undo');assert undo['status']=='undone' and a.call('GET','/api/tasks?application_id='+aid)['items'][0]['due_at'] is not None;assert a.call('POST',f'/api/notifications/{shifted["notification_id"]}/undo')==undo;ok('undo_restores_time_precision_and_replays')
    reject=process(a,notice(a,'rejection','示例测评公司'));assert reject['application']['outcome']=='rejected' and a.call('GET','/api/tasks?application_id='+aid)['items'][0]['status']=='pending';ok('rejection_preserves_pending_tasks')
    a.call('POST',f'/api/notifications/{reject["notification_id"]}/undo');assert a.call('GET','/api/applications/'+aid)['outcome']=='active';ok('undo_restores_existing_outcome')
    cancelled=process(a,notice(a,'cancel','示例测评公司'));assert cancelled['status']=='saved' and a.call('GET','/api/tasks?application_id='+aid)['items'][0]['status']=='cancelled';a.call('POST',f'/api/notifications/{cancelled["notification_id"]}/undo');assert a.call('GET','/api/tasks?application_id='+aid)['items'][0]['status']=='pending';ok('cancellation_and_undo_target_only_original_task')
    manual=a.call('POST','/api/applications',mutation(company_name='示例保留阶段公司',position_name='Agent开发工程师',batch='27秋招'),201);a.call('PATCH','/api/applications/'+manual['id'],mutation(version=manual['version'],stage='screening'));guarded=process(a,notice(a,company='示例保留阶段公司'));assert guarded['application']['stage']=='screening' and guarded['warnings'];ok('automatic_progress_preserves_manual_stage_edit')
    created=process(a,notice(a,company='示例撤销公司'));a.call('POST',f'/api/notifications/{created["notification_id"]}/undo');assert a.call('GET','/api/applications/'+created['application_id'])['outcome']=='withdrawn';ok('new_application_undo_is_soft_withdrawal')
    edited=process(a,notice(a,company='示例手动公司'));app=a.call('GET','/api/applications/'+edited['application_id']);a.call('PATCH','/api/applications/'+app['id'],mutation(version=app['version'],notes='手动内容'));a.call('POST',f'/api/notifications/{edited["notification_id"]}/undo',status=409);ok('undo_preserves_later_manual_edit')
    task=a.call('GET','/api/tasks?application_id='+app['id'])['items'][0];a.call('PATCH','/api/tasks/'+task['id'],mutation(version=task['version'],title='手动安排'));assert process(a,notice(a,'schedule','示例手动公司'))['status']=='unresolved';ok('reschedule_preserves_manual_task_edit')
    # Simulate already persisted tool facts from the long historical conversation.
    q=notice(a,'missing_position','示例旧公司3');from services.notifications import NotificationService
    from schemas.notifications import ExtractionRequest
    ex=a.call('POST',f'/api/notifications/{q["id"]}/extract',mutation())
    session=a.call('POST','/api/sessions',mutation(notification_id=q['id'],extraction_id=str(ex['id'])),201);state=session['state'];state['result'].update(company_name='示例修正公司',position_name='agent开发岗');state['confirmed_fields']={'company_name':{'value':'示例修正公司'},'position_name':{'value':'agent开发岗'}};await conn.execute('UPDATE agent_sessions SET state=$2::jsonb WHERE id=$1',UUID(session['id']),json.dumps(state,ensure_ascii=False));fixed=process(a,q);assert fixed['application']['company_name']=='示例修正公司' and fixed['application']['position_name']=='agent开发岗';ok('old_dialogue_facts_reused_without_reconfirmation')
    await conn.execute("CREATE FUNCTION fail_auto() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'fixture rollback'; END; $$");await conn.execute('CREATE TRIGGER fail_auto BEFORE INSERT ON tasks FOR EACH ROW EXECUTE FUNCTION fail_auto()');fail=notice(a,company='示例回滚公司');a.call('POST',f'/api/notifications/{fail["id"]}/process',status=500);assert await conn.fetchval("SELECT count(*) FROM applications WHERE company_name='示例回滚公司'")==0;assert await conn.fetchval('SELECT count(*) FROM automatic_records WHERE notification_id=$1',UUID(fail['id']))==0;await conn.execute('DROP TRIGGER fail_auto ON tasks');await conn.execute('DROP FUNCTION fail_auto()');ok('transaction_rolls_back_on_task_failure')
    mail=a.call('POST','/api/mailboxes',{'provider':'qq','address':'fixture@qq.com','authorization_code':'fixture-code','automatic':False},201);a.call('POST','/api/mailboxes/'+mail['id']+'/sync',status=202)
    from services.mail_scheduler import tick
    from services.jobs import claim
    from services.worker import execute
    from database.pool import create_pool
    mail_pool=await create_pool(dsn=dsn,role='worker')
    try:
     await tick(mail_pool,fixture_mail)
     for _ in range(2):
      queued=await claim(mail_pool,'automatic-regression-mail');assert queued is not None;await execute(mail_pool,queued,extractor)
    finally:await mail_pool.close()
    assert await conn.fetchval('SELECT count(*) FROM automatic_records WHERE owner_id=$1 AND notification_id IN (SELECT notification_id FROM mailbox_messages WHERE account_id=$2)',owner,UUID(mail['id']))==2;ok('mail_sync_records_without_human_confirmation')
    assert await conn.fetchval("SELECT count(*) FROM application_events WHERE owner_id=$1 AND actor_type='system'",owner)>0;ok('automated_events_have_system_actor')
  finally:await conn.close()
  return {'checks_passed':len(checks),'checks':checks,'mode':'real HTTP/PostgreSQL with explicit model/mail fixtures','temporary_database_removed':True}
 finally:await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)');await admin.close()
if __name__=='__main__':
 r=asyncio.run(checks());(REPORT/'report.json').write_text(json.dumps(r,ensure_ascii=False,indent=2));print(json.dumps(r,ensure_ascii=False))
