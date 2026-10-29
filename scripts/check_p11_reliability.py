"""P11.2-4 real PG/HTTP/process faults; model and IMAP are explicit fixtures."""
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit,urlunsplit
from uuid import uuid4,UUID
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import asyncpg
from database.config import database_url
from database.migrate import migrate
from database.pool import create_pool
from schemas.notifications import NotificationImport
from schemas.jobs import JobCreate
from services.notifications import NotificationService
from services.jobs import JobService,claim,QueueFull
from services.worker import execute,run
from services.mail_scheduler import tick,request_sync
from services.mailboxes import MailboxService
from services.errors import Conflict
from scripts.check_automatic import extractor,server
from scripts.check_p4_api import TEXT
from scripts.check_p10_api import fixture_mail,TEST_KEY
from schemas.mailboxes import MailboxBind
from agent.notification_extractor import ExtractionFailure

OUT=ROOT/os.environ.get('P11_LONG_OUTPUT','.work/p11/p11.2-4/run-1')
@contextmanager
def settings(**values):
 old={key:os.environ.get(key) for key in values}
 os.environ.update({key:str(v) for key,v in values.items()})
 try:yield
 finally:
  for key,value in old.items():
   if value is None:os.environ.pop(key,None)
   else:os.environ[key]=value

async def delayed(n):
 await asyncio.sleep(float(os.environ.get('P11_FIXTURE_DELAY','2')))
 return await extractor(n)

async def checks():
 from scripts import check_automatic as harness
 harness.REPORT=OUT/'http';harness.REPORT.mkdir(parents=True,exist_ok=True)
 base=database_url();name='job_application_agent_test_long_'+uuid4().hex[:8];dsn=urlunsplit(urlsplit(base)._replace(path='/'+name));admin=await asyncpg.connect(base)
 await admin.execute(f'CREATE DATABASE "{name}"');pool=None;children=[];checks=[];observations={}
 def ok(name):checks.append(name);print('PASS',name,flush=True)
 try:
  await migrate(dsn);pool=await create_pool(dsn=dsn,role='worker')
  async def owner():
   value=uuid4();await pool.execute('INSERT INTO app_users(id,display_name) VALUES($1,$2)',value,'合成测试用户');return value
  async def submit(o=None,mode='submission'):
   o=o or await owner();n=await NotificationService(pool,o).import_notification(NotificationImport(request_id=uuid4(),text=TEXT+' 投递成功，简历进入简历库。',source_reference=mode,hints={'company_name':'示例'+uuid4().hex[:8]}))
   body=JobCreate(request_id=uuid4(),notification_id=n['id']);j=await JobService(pool,o).submit(body);return o,n,j
  async def get(j):return await pool.fetchrow('SELECT * FROM jobs WHERE id=$1',j['id'])
  async def run_one(j,model=extractor):
   job=await claim(pool,'inline-test');assert job['id']==j['id'];await execute(pool,job,model);return await get(j)
  o,n,j=await submit();arr=await asyncio.gather(claim(pool,'a'),claim(pool,'b'))
  assert sum(x is not None for x in arr)==1;await execute(pool,next(x for x in arr if x),extractor);assert (await get(j))['status']=='succeeded';ok('two_consumers_claim_once')
  # Old executor waits in the model while another consumer replaces its expired lease.
  o,n,j=await submit();old=await claim(pool,'old');entered=asyncio.Event();release=asyncio.Event()
  async def blocked(n):entered.set();await release.wait();return await extractor(n)
  task=asyncio.create_task(execute(pool,old,blocked));await entered.wait()
  await pool.execute("UPDATE jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1",j['id'])
  newer=await claim(pool,'new');assert newer['id']==j['id'] and newer['run_token']!=old['run_token']
  await execute(pool,newer,extractor);release.set();await task
  assert (await get(j))['status']=='succeeded'
  rows=await pool.fetch('SELECT status,raw_output,job_token FROM notification_extractions WHERE job_id=$1 ORDER BY created_at',j['id'])
  assert rows[0]['status']=='failed' and rows[0]['raw_output'] is None and rows[1]['status']=='completed'
  assert await pool.fetchval('SELECT count(*) FROM applications WHERE owner_id=$1',o)==1;ok('expired_executor_cannot_save_late_extraction_or_business')
  assert [r['status'] for r in await pool.fetch('SELECT status FROM job_attempts WHERE job_id=$1 ORDER BY number',j['id'])]==['expired','succeeded'];ok('attempt_history_preserved')
  # Pause after extraction completes but before the business transaction begins.
  o,n,j=await submit();old=await claim(pool,'late-business');entered=asyncio.Event();release=asyncio.Event();original_fetchrow=pool.fetchrow;intercept=True
  async def intercept_read(*args,**kwargs):
   nonlocal intercept
   row=await original_fetchrow(*args,**kwargs)
   if intercept and 'SELECT state FROM agent_sessions' in args[0]:intercept=False;entered.set();await release.wait()
   return row
  pool.fetchrow=intercept_read
  try:
   task=asyncio.create_task(execute(pool,old,extractor));await entered.wait()
   await pool.execute("UPDATE jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1",j['id'])
   newer=await claim(pool,'new-business');await execute(pool,newer,extractor);release.set();await task
   assert (await get(j))['status']=='succeeded' and await pool.fetchval('SELECT count(*) FROM applications WHERE owner_id=$1',o)==1
   assert await pool.fetchval('SELECT count(*) FROM notification_extractions WHERE job_id=$1',j['id'])==1;ok('completed_extraction_reused_and_late_business_commit_fenced')
  finally:pool.fetchrow=original_fetchrow
  # Failing the success update must roll back application, events and receipt together.
  o,n,j=await submit()
  await pool.execute("CREATE FUNCTION fail_job_commit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.status='succeeded' THEN RAISE EXCEPTION 'fixture commit failure'; END IF; RETURN NEW; END; $$")
  await pool.execute('CREATE TRIGGER fail_job_commit BEFORE UPDATE ON jobs FOR EACH ROW EXECUTE FUNCTION fail_job_commit()')
  result=await run_one(j)
  assert result['status']=='failed' and await pool.fetchval('SELECT count(*) FROM applications WHERE owner_id=$1',o)==0 and await pool.fetchval('SELECT count(*) FROM automatic_records WHERE owner_id=$1',o)==0;ok('success_state_and_business_rollback_atomically')
  await pool.execute('DROP TRIGGER fail_job_commit ON jobs');await pool.execute('DROP FUNCTION fail_job_commit()')
  rid=uuid4();await JobService(pool,o).retry(j['id'],rid);await run_one(j)
  assert (await get(j))['status']=='succeeded';assert (await JobService(pool,o).retry(j['id'],rid))['id']==j['id'];ok('manual_retry_reuses_completed_extraction_and_replays_request')
  # Cancel before claim, and while an external model is already in flight.
  o,n,j=await submit();await JobService(pool,o).cancel(j['id']);assert (await get(j))['status']=='cancelled';ok('queued_cancel')
  await JobService(pool,o).retry(j['id'],uuid4());old=await claim(pool,'cancel');entered=asyncio.Event();release=asyncio.Event()
  task=asyncio.create_task(execute(pool,old,blocked));await entered.wait();await JobService(pool,o).cancel(j['id']);release.set();await task
  assert (await get(j))['status']=='cancelled' and await pool.fetchval('SELECT count(*) FROM applications WHERE owner_id=$1',o)==0
  assert await pool.fetchval("SELECT phase='interrupted' AND elapsed_ms IS NOT NULL FROM notification_extractions WHERE job_id=$1 ORDER BY created_at DESC LIMIT 1",j['id']);ok('running_cancel_blocks_business_commit')
  await JobService(pool,o).retry(j['id'],uuid4());await run_one(j);assert (await get(j))['status']=='succeeded';ok('explicit_retry_after_cancel')
  try:await JobService(pool,o).cancel(j['id']);raise AssertionError('should conflict')
  except Conflict:ok('finished_job_requires_business_undo')
  # A cancellation racing a commit sees the committed terminal status, never an undo.
  o,n,j=await submit()
  await pool.execute("CREATE FUNCTION pause_job_success() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.status='succeeded' THEN PERFORM pg_sleep(.5); END IF; RETURN NEW; END; $$")
  await pool.execute('CREATE TRIGGER pause_job_success BEFORE UPDATE ON jobs FOR EACH ROW EXECUTE FUNCTION pause_job_success()')
  job=await claim(pool,'commit-race');task=asyncio.create_task(execute(pool,job,extractor))
  for _ in range(100):
   if await pool.fetchval("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND wait_event='PgSleep')"):break
   await asyncio.sleep(.01)
  else:raise AssertionError('commit pause not reached')
  cancellation=asyncio.create_task(JobService(pool,o).cancel(j['id']));await task
  try:await cancellation;raise AssertionError('cancel should see success')
  except Conflict:pass
  await pool.execute('DROP TRIGGER pause_job_success ON jobs');await pool.execute('DROP FUNCTION pause_job_success()')
  assert (await get(j))['status']=='succeeded' and await pool.fetchval('SELECT count(*) FROM applications WHERE owner_id=$1',o)==1;ok('cancel_vs_commit_keeps_committed_business_and_requires_undo')
  # Retry only explicit transient errors, with a delay and finite budget.
  o,n,j=await submit();calls=0
  async def flaky(n):
   nonlocal calls
   calls+=1
   if calls==1:raise ExtractionFailure('fixture 429',429,retryable=True,retry_after_seconds=2)
   return await extractor(n)
  result=await run_one(j,flaky);assert result['status']=='queued' and result['retryable'];assert (result['available_at']-result['started_at']).total_seconds()>=2;ok('transient_429_is_delayed')
  assert await claim(pool,'too-early') is None;ok('retry_after_not_claimed_early')
  await pool.execute('UPDATE jobs SET available_at=clock_timestamp() WHERE id=$1',j['id']);await run_one(j,flaky);assert calls==2 and (await get(j))['status']=='succeeded';ok('transient_retry_saves_once')
  o,n,j=await submit()
  async def unavailable(n):raise ExtractionFailure('fixture unavailable',503,retryable=True)
  with settings(JOB_MAX_ATTEMPTS=2):
   await run_one(j,unavailable);await pool.execute('UPDATE jobs SET available_at=clock_timestamp() WHERE id=$1',j['id']);await run_one(j,unavailable)
  assert (await get(j))['status']=='failed' and (await get(j))['attempt_count']==2;ok('automatic_retry_budget_exhausted')
  o,n,j=await submit(mode='bad');await run_one(j);assert (await get(j))['status']=='failed' and not (await get(j))['retryable'];ok('malformed_output_not_automatically_retried')
  o,n,j=await submit()
  async def unauthorized(n):raise ExtractionFailure('fixture 401',401)
  await run_one(j,unauthorized);assert (await get(j))['status']=='failed' and not (await get(j))['retryable'];ok('authentication_error_not_retried')
  o,n,j=await submit()
  with settings(JOB_EXECUTION_TIMEOUT=1,JOB_MAX_ATTEMPTS=1):await run_one(j,delayed)
  assert (await get(j))['status']=='failed' and (await get(j))['error_kind']=='timeout';ok('execution_timeout_is_bounded')
  o,n,j=await submit();await pool.execute("UPDATE jobs SET deadline_at=clock_timestamp()-interval '1 second' WHERE id=$1",j['id']);assert await claim(pool,'deadline') is None;assert (await get(j))['status']=='failed';ok('queued_deadline_expires_without_model_call')
  # DB-wide slots and queue bounds apply across independent worker processes.
  with settings(MODEL_CONCURRENCY_LIMIT=1):
   o,n,j=await submit();o2,n2,j2=await submit();one=await claim(pool,'slot-a');assert await claim(pool,'slot-b') is None;await execute(pool,one,extractor);await run_one(j2);ok('database_global_execution_limit')
  with settings(JOB_QUEUE_LIMIT=1,JOB_OWNER_QUEUE_LIMIT=1):
   o,n,j=await submit();assert (await JobService(pool,o).submit(JobCreate(request_id=uuid4(),notification_id=n['id'])))['id']==j['id']
   try:await submit();raise AssertionError('queue should reject')
   except QueueFull:pass
   await JobService(pool,o).cancel(j['id']);ok('bounded_queue_rejects_new_work_but_accepts_replay')
  with settings(JOB_QUEUE_LIMIT=3,JOB_OWNER_QUEUE_LIMIT=1):
   o,n,j=await submit()
   try:await submit(o);raise AssertionError('owner limit should reject')
   except QueueFull:pass
   other,nn,jj=await submit();await JobService(pool,o).cancel(j['id']);await JobService(pool,other).cancel(jj['id']);ok('owner_queue_limit_does_not_block_other_owner_capacity')
  with settings(MODEL_REQUESTS_PER_HOUR=1):
   from core.access import CallLimit
   o=await owner();await pool.execute("INSERT INTO model_admissions(owner_id,hour,count) VALUES($1,date_trunc('hour',clock_timestamp()),1)",o)
   try:await submit(o);raise AssertionError('quota should reject before queueing')
   except CallLimit:pass
   assert await pool.fetchval('SELECT count(*) FROM jobs WHERE owner_id=$1',o)==0;ok('exhausted_local_model_quota_rejected_at_submission')
  with settings(DB_ACQUIRE_TIMEOUT=1):small=await create_pool(dsn=dsn,max_size=1)
  try:
   async with small.acquire():
    try:await small.fetchval('SELECT 1');raise AssertionError('acquire should time out')
    except TimeoutError:ok('connection_acquisition_timeout')
  finally:await small.close()
  assert pool.metrics()['role']=='worker' and pool.get_max_size()==8;ok('role_pool_and_acquire_metrics')
  # Mail scheduler uses durable due state, queues only, and handles two competing ticks.
  with settings(MAIL_CREDENTIAL_KEY=TEST_KEY,MAIL_POLL_SECONDS=43200):
   o=await owner();mail=MailboxService(pool,o,transport=fixture_mail)
   account=await mail.bind(MailboxBind(provider='qq',address='synthetic@qq.com',authorization_code='fixture',automatic=True))
   await asyncio.gather(tick(pool,fixture_mail),tick(pool,fixture_mail))
   assert await pool.fetchval('SELECT count(*) FROM mail_sync_jobs WHERE account_id=$1',account['id'])==1;ok('two_schedulers_one_durable_sync')
   assert await request_sync(pool,o,account['id'],only_if_due=True) is None;ok('stale_due_snapshot_does_not_schedule_second_sync')
   assert await pool.fetchval('SELECT count(*) FROM jobs WHERE owner_id=$1',o)==2
   assert await pool.fetchval('SELECT count(*) FROM notification_extractions WHERE owner_id=$1',o)==0;ok('imap_only_queues_models')
   assert await pool.fetchval('SELECT last_uid FROM mailbox_accounts WHERE id=$1',account['id'])==36;ok('first_import_uid_cursor')
   for _ in range(2):
    job=await claim(pool,'mail-worker');await execute(pool,job,extractor)
   await tick(pool,fixture_mail)
   assert await pool.fetchval("SELECT count(*) FROM mailbox_messages WHERE account_id=$1 AND auto_state='completed'",account['id'])==2;ok('mail_state_reconciles_worker_results')
   await request_sync(pool,o,account['id']);await tick(pool,fixture_mail);assert await pool.fetchval('SELECT count(*) FROM jobs WHERE owner_id=$1',o)==2;ok('mail_resync_no_duplicate_jobs')
   # A full queue keeps candidate raw mail durable and drains it later without IMAP reread.
   with settings(JOB_QUEUE_LIMIT=1):
    oo,nn,jj=await submit();await pool.execute("UPDATE mailbox_messages SET auto_state='pending',processing_job_id=NULL WHERE account_id=$1 AND is_candidate=true",account['id'])
    # Existing semantic jobs are replays and may drain even at capacity; use a fresh message.
    await pool.execute("INSERT INTO mailbox_messages(owner_id,account_id,uidvalidity,uid,subject,sender,text,is_candidate,auto_state) VALUES($1,$2,'epoch-1','99','实习测评','fixture', $3,true,'pending')",o,account['id'],TEXT)
    await mail.enqueue_cached(account['id']);assert await pool.fetchval("SELECT auto_state FROM mailbox_messages WHERE account_id=$1 AND uid='99'",account['id'])=='pending';assert await pool.fetchval("SELECT notification_id FROM mailbox_messages WHERE account_id=$1 AND uid='99'",account['id']) is None
    await JobService(pool,oo).cancel(jj['id'])
   await mail.enqueue_cached(account['id']);assert await pool.fetchval("SELECT auto_state FROM mailbox_messages WHERE account_id=$1 AND uid='99'",account['id'])=='processing';ok('queue_full_preserves_mail_candidate_for_later_drain')
   pending=await claim(pool,'mail-last');await execute(pool,pending,extractor)
   await mail.setting(account['id'],False);await pool.execute("UPDATE mailbox_accounts SET next_sync_at=clock_timestamp()-interval '1 hour' WHERE id=$1",account['id']);before=await pool.fetchval('SELECT count(*) FROM mail_sync_jobs WHERE account_id=$1',account['id']);await tick(pool,fixture_mail);assert await pool.fetchval('SELECT count(*) FROM mail_sync_jobs WHERE account_id=$1',account['id'])==before;ok('paused_account_not_scheduled')
   await mail.setting(account['id'],True);assert await pool.fetchval('SELECT next_sync_at<=clock_timestamp() FROM mailbox_accounts WHERE id=$1',account['id']);ok('enable_wakes_through_database')
   await mail.disconnect(account['id'])
  with settings(MAIL_CREDENTIAL_KEY=TEST_KEY):
   o=await owner();failed_once=False
   def interrupted_mail(provider,address,code,known_validity=None,last_uid=0,limit=30,since=None):
    nonlocal failed_once
    if last_uid>=30 and not failed_once:failed_once=True;raise OSError('fixture disconnect')
    return fixture_mail(provider,address,code,known_validity,last_uid,limit,since)
   mail=MailboxService(pool,o,transport=interrupted_mail);account=await mail.bind(MailboxBind(provider='163',address='synthetic@163.com',authorization_code='fixture',automatic=True))
   try:await mail.sync(account['id']);raise AssertionError('disconnect should surface')
   except OSError:pass
   assert await pool.fetchval('SELECT last_uid FROM mailbox_accounts WHERE id=$1',account['id'])==31;ok('imap_disconnect_keeps_committed_cursor_and_raw_messages')
   await mail.sync(account['id']);assert await pool.fetchval('SELECT last_uid FROM mailbox_accounts WHERE id=$1',account['id'])==36;assert await pool.fetchval('SELECT count(*) FROM mailbox_messages WHERE account_id=$1',account['id'])==35;ok('imap_resume_does_not_duplicate_cached_uid')
   for _ in range(2):
    job=await claim(pool,'mail-disconnect');await execute(pool,job,extractor)
   before=await pool.fetchval('SELECT count(*) FROM applications WHERE owner_id=$1',o)
   def new_epoch(provider,address,code,known_validity=None,last_uid=0,limit=30,since=None):
    _,items=fixture_mail(provider,address,code,'epoch-1' if known_validity=='epoch-2' else None,last_uid,limit,since)
    return 'epoch-2',items
   await MailboxService(pool,o,transport=new_epoch).sync(account['id'])
   assert await pool.fetchval('SELECT count(*) FROM mailbox_messages WHERE account_id=$1',account['id'])==70;ok('uidvalidity_change_preserves_both_epochs_without_uid_collision')
   for _ in range(2):
    job=await claim(pool,'mail-epoch');await execute(pool,job,extractor)
   assert await pool.fetchval('SELECT count(*) FROM applications WHERE owner_id=$1',o)==before;ok('reimported_epoch_uses_existing_business_deduplication')
   await mail.disconnect(account['id'])
  o,n,j=await submit()
  with server(dsn,o,'default-routes',diagnostics=False) as a:
   a.call('POST',f'/api/notifications/{n["id"]}/process',status=409)
   a.call('POST',f'/api/notifications/{n["id"]}/extract',{'request_id':str(uuid4())},409)
   a.call('GET','/api/jobs/'+str(j['id']));ok('default_web_closes_synchronous_model_bypasses')
  await JobService(pool,o).cancel(j['id'])
  with settings(MAIL_CREDENTIAL_KEY=TEST_KEY,MAIL_SYNC_LEASE_SECONDS=1):
   o=await owner()
   def slow_mail(*args,**kwargs):
    if len(args)>5 and args[5]:time.sleep(1)
    return fixture_mail(*args,**kwargs)
   mail=MailboxService(pool,o,transport=fixture_mail);account=await mail.bind(MailboxBind(provider='126',address='synthetic@126.com',authorization_code='fixture',automatic=True))
   first=asyncio.create_task(tick(pool,slow_mail));await asyncio.sleep(1.3)
   token=await pool.fetchval("SELECT run_token FROM mail_sync_jobs WHERE account_id=$1 AND status='running'",account['id']);assert token is not None
   await tick(pool,slow_mail)
   assert await pool.fetchval('SELECT run_token FROM mail_sync_jobs WHERE account_id=$1',account['id'])==token
   await first;assert await pool.fetchval("SELECT status FROM mail_sync_jobs WHERE account_id=$1",account['id'])=='succeeded';ok('slow_imap_keeps_lease_alive_during_thread_wait')
   for _ in range(2):
    job=await claim(pool,'slow-mail');await execute(pool,job,extractor)
   await mail.disconnect(account['id'])
  # Real process kill during model wait; new process takes over on lease expiry.
  o,n,j=await submit()
  env={**os.environ,'DATABASE_URL':dsn,'MODEL_REQUESTS_PER_HOUR':'10000','JOB_LEASE_SECONDS':'2','WORKER_CONCURRENCY':'1','MAIL_POLL_ENABLED':'false'}
  with (OUT/'worker-process.log').open('w') as log:
   def spawn():
    child=subprocess.Popen([sys.executable,'scripts/check_p11_reliability.py','--worker'],env=env,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT);children.append(child);return child
   killed=spawn()
   for _ in range(150):
    if await pool.fetchval("SELECT EXISTS(SELECT 1 FROM notification_extractions WHERE job_id=$1 AND status='processing')",j['id']):break
    await asyncio.sleep(.03)
   else:raise AssertionError('model wait was not reached')
   killed.kill();await asyncio.to_thread(killed.wait)
   recovered=spawn()
   for _ in range(250):
    if (await get(j))['status']=='succeeded':break
    await asyncio.sleep(.04)
   else:raise AssertionError('SIGKILL recovery did not finish')
   recovered.terminate();await asyncio.to_thread(recovered.wait,15)
   assert (await get(j))['attempt_count']==2;ok('sigkill_during_model_wait_recovered_by_another_process')
   assert await pool.fetchval('SELECT count(*) FROM applications WHERE owner_id=$1',o)==1;ok('process_recovery_business_saved_once')
  # Private login cookie valid in independent Web processes and after restart.
  with settings(APP_SESSION_SECRET='fixture-session-secret-'+'x'*32):
   from scripts import check_p10_api as private_harness
   private_harness.REPORT=OUT/'private-http'
   from scripts.check_p10_api import server as private_server
   with private_server(dsn,o,'private-a',mode='private') as (a,token):
    a.call('POST','/api/auth/login',{'token':token});cookie='; '.join(c.name+'='+c.value for c in a.jar)
    with settings(APP_ACCESS_TOKEN=token):
     # The harness generates tokens per process; session secret remains shared.
     with private_server(dsn,o,'private-b',mode='private') as (bb,_):bb.call('GET','/api/applications',headers={'Cookie':cookie});ok('cookie_shared_across_web_processes')
   with private_server(dsn,o,'private-restart',mode='private') as (a,_):a.call('GET','/api/applications',headers={'Cookie':cookie});ok('cookie_survives_api_restart_with_stable_secret')
  # Two actual worker processes with a DB-wide limit of two; API uses its own pool.
  o=await owner();jobs=[]
  for _ in range(6):jobs.append((await submit(o))[2])
  with (OUT/'two-workers.log').open('w') as log,server(dsn,o,'queries-during-workers') as a:
   env={**os.environ,'DATABASE_URL':dsn,'MODEL_REQUESTS_PER_HOUR':'10000','JOB_LEASE_SECONDS':'3','WORKER_CONCURRENCY':'2','MODEL_CONCURRENCY_LIMIT':'2'}
   procs=[subprocess.Popen([sys.executable,'scripts/check_p11_reliability.py','--worker'],env=env,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT) for _ in range(2)];children.extend(procs)
   peak=0;latencies=[];peak_connections=0
   for _ in range(400):
    active=await pool.fetchval("SELECT count(*) FROM jobs WHERE owner_id=$1 AND status='running'",o);peak=max(peak,active)
    peak_connections=max(peak_connections,await pool.fetchval("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()"))
    if active and len(latencies)<30:
     start=time.monotonic();a.call('GET','/api/applications');latencies.append((time.monotonic()-start)*1000)
    if await pool.fetchval("SELECT count(*) FROM jobs WHERE owner_id=$1 AND status='succeeded'",o)==6:break
    await asyncio.sleep(.03)
   else:raise AssertionError('two workers did not drain queue')
   for proc in procs:proc.terminate();await asyncio.to_thread(proc.wait,15)
   assert peak==2 and latencies and max(latencies)<200;ok('two_worker_processes_respect_global_limit_and_api_remains_responsive')
   assert peak_connections<=31;ok('measured_connections_within_configured_process_budget')
   observations['two_workers']={'peak_running':peak,'query_samples_ms':latencies,'peak_database_connections':peak_connections,'connection_budget_for_this_test':31,'note':'short fixture experiment, not P11.5 capacity benchmark'}
  observations['pool']=pool.metrics()
  return {'checks_passed':len(checks),'checks':checks,'observations':observations,'mode':'real PostgreSQL/HTTP/process faults; model and IMAP fixtures','cleanup':{'temporary_database_removed':True,'child_processes_stopped':True}}
 finally:
  for child in children:
   if child.poll() is None:child.terminate();await asyncio.to_thread(child.wait,15)
  if pool:await pool.close()
  await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)');await admin.close()

if __name__=='__main__':
 if '--worker' in sys.argv:asyncio.run(run(extractor=delayed))
 else:
  OUT.mkdir(parents=True,exist_ok=False)
  with settings(MODEL_REQUESTS_PER_HOUR=10000,MAIL_POLL_ENABLED='false'):
   try:result=asyncio.run(checks())
   except Exception as exc:
    (OUT/'failure.json').write_text(json.dumps({'error_type':type(exc).__name__,'error':str(exc)},ensure_ascii=False,indent=2));raise
   (OUT/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False))
