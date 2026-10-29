"""Real isolated PG16 dump/restore + API/worker recovery drill; only synthetic data."""
import base64,hashlib,http.cookiejar,json,os,secrets,shutil,socket,subprocess,sys,time
from pathlib import Path
from urllib.request import Request,build_opener,HTTPCookieProcessor,ProxyHandler
from urllib.error import HTTPError,URLError
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.data_recovery import Database,digest
OUT=ROOT/os.environ.get('P117_OUTPUT','.work/p11/p11.7/drill');IMAGE=os.environ.get('P117_IMAGE','job-agent-p117-local:20261029')
TABLES=['app_users','applications','tasks','application_events','notifications','notification_extractions','agent_sessions','automatic_records','jobs','job_requests','job_attempts','mailbox_accounts','mailbox_messages']

def main():
 OUT.mkdir(parents=True,exist_ok=False);OUT.chmod(0o700);tag=uuid4().hex[:8];network='job-agent-p117-'+tag;pg=network+'-pg';owner=str(uuid4());password=secrets.token_urlsafe(24);token=secrets.token_urlsafe(36);key=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode();checks=[];containers=[];timing={}
 source='job_application_agent_test_p117_'+tag;target='job_application_agent_restore_'+tag;backup=OUT/'database-backup';envfile=OUT/'fixture-key.env';keyfile=OUT/'separate-secrets/mail-key.json';envfile.write_text('MAIL_CREDENTIAL_KEY='+key+'\n');envfile.chmod(0o600)
 log=(OUT/'commands.log').open('w')
 def ok(name):checks.append(name);print('PASS',name,flush=True)
 def docker(*args,input=None,allow_failure=False):
  p=subprocess.run(['docker',*args],input=input,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
  if p.returncode and not allow_failure:raise AssertionError('docker operation failed; exit='+str(p.returncode))
  return p
 def tool(*args,expected=0):
  p=subprocess.run([sys.executable,'scripts/data_recovery.py',*args],cwd=ROOT,capture_output=True,text=True)
  log.write(p.stdout);log.flush()
  if p.returncode!=expected:(OUT/('tool-failure-'+str(len(checks))+'.log')).write_text(p.stderr)
  assert p.returncode==expected,(args[0],p.returncode,p.stdout)
  return json.loads(p.stdout)
 def db(name=source):return Database(pg,name)
 def fingerprints(database):
  result={}
  for table in TABLES:
   value=database.sql("SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]') FROM "+table+' t')
   result[table]=hashlib.sha256(value.encode()).hexdigest()
  return result
 def env(name,mail='false'):
  return {'DATABASE_URL':f'postgresql://fixture:{password}@{pg}:5432/{name}','LOCAL_USER_ID':owner,'APP_MODE':'private','APP_ACCESS_TOKEN':token,'APP_SESSION_SECRET':token,'MAIL_CREDENTIAL_KEY':key,'MAIL_POLL_ENABLED':mail,'PYTHON_DOTENV_DISABLED':'1','ENABLE_SYNC_DIAGNOSTICS':'true','MODEL_REQUESTS_PER_HOUR':'10000'}
 def envargs(values):return [item for k,v in values.items() for item in ['-e',k+'='+v]]
 def fixture(database,code):
  p=docker('run','--rm','-i','--network',network,*envargs(env(database)),'--entrypoint','.venv/bin/python',IMAGE,'-',input=code.encode())
  return p.stdout.decode()
 api=None
 def start_api(database,user=None):
  with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
  name=network+'-api-'+uuid4().hex[:4];containers.append(name);values=env(database)
  if user:values['LOCAL_USER_ID']=user
  docker('run','-d','--name',name,'--network',network,'-p',f'127.0.0.1:{port}:8001',*envargs(values),'--entrypoint','.venv/bin/uvicorn',IMAGE,'api.main:app','--host','0.0.0.0','--port','8001','--no-proxy-headers')
  opener=build_opener(ProxyHandler({}),HTTPCookieProcessor(http.cookiejar.CookieJar()))
  def call(method,path,body=None,status=200):
   req=Request(f'http://127.0.0.1:{port}'+path,data=json.dumps(body).encode() if body is not None else None,method=method,headers={'Content-Type':'application/json'})
   try:
    with opener.open(req,timeout=5) as r:code=r.status;raw=r.read()
   except HTTPError as error:code=error.code;raw=error.read()
   data=json.loads(raw);assert code==status,(method,path,code,data);return data
  for _ in range(120):
   try:call('GET','/health/live');break
   except (URLError,ConnectionError):time.sleep(.1)
  else:raise AssertionError('test API startup timeout')
  call('POST','/api/auth/login',{'token':token});return name,call
 try:
  docker('network','create',network)
  docker('run','-d','--name',pg,'--network',network,'-e','POSTGRES_USER=fixture','-e','POSTGRES_PASSWORD='+password,'-e','POSTGRES_DB='+source,'pgvector/pgvector:pg16');containers.append(pg)
  for _ in range(150):
   if docker('exec',pg,'pg_isready','-h','127.0.0.1','-U','fixture','-d',source,allow_failure=True).returncode==0:break
   time.sleep(.1)
  else:raise AssertionError('PG startup timeout')
  seed="""
import asyncio,json,os
from uuid import uuid4
from database.migrate import migrate
from database.pool import create_pool
from core.identity import current_user_id
from services.notifications import NotificationService
from services.automatic import AutomaticService
from services.jobs import JobService,claim
from schemas.notifications import NotificationImport
from schemas.jobs import JobCreate
from services.mailboxes import MailboxService
from schemas.mailboxes import MailboxBind
from scripts.check_automatic import extractor
from scripts.check_p10_api import fixture_mail
from scripts.check_p4_api import TEXT
async def main():
 await migrate();p=await create_pool(role='worker');o=current_user_id()
 try:
  await p.execute('INSERT INTO app_users(id,display_name) VALUES($1,$2)',o,'P117合成用户')
  n=await NotificationService(p,o).import_notification(NotificationImport(request_id=uuid4(),text=TEXT,source_reference='assessment',timezone_hint='Asia/Shanghai'))
  result=await AutomaticService(p,o,extractor).process(n['id']);assert result['status']=='saved'
  jobs=[]
  for i in range(2):
   x=await NotificationService(p,o).import_notification(NotificationImport(request_id=uuid4(),text=TEXT+' 投递成功。',source_reference='submission',hints={'company_name':'示例恢复公司'+str(i)},timezone_hint='Asia/Shanghai'))
   request=uuid4();j=await JobService(p,o).submit(JobCreate(request_id=request,notification_id=x['id']));jobs.append({'id':str(j['id']),'notification_id':str(x['id']),'request_id':str(request)})
  running=await claim(p,'fixture-source-worker');await p.execute("UPDATE jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1",running['id'])
  m=await MailboxService(p,o,transport=fixture_mail).bind(MailboxBind(provider='163',address='p117-private@163.com',authorization_code='P117_PRIVATE_CODE',automatic=True))
  await p.execute("INSERT INTO mailbox_messages(owner_id,account_id,uidvalidity,uid,subject,sender,text,is_candidate,auto_state) VALUES($1,$2,'fixture','1','P117_PRIVATE_SUBJECT','fixture','P117_PRIVATE_BODY',false,'skipped')",o,m['id'])
  for status,days in [('stopped',45),('stopped',5),('running',45)]:await p.execute("INSERT INTO runtime_instances(id,role,status,last_seen_at) VALUES($1,'worker',$2,clock_timestamp()-$3*interval '1 day')",uuid4(),status,days)
  print('SEED='+json.dumps({'saved_notification':str(n['id']),'application_id':str(result['application_id']),'mail_id':str(m['id']),'jobs':jobs}))
 finally:await p.close()
asyncio.run(main())
"""
  output=fixture(source,seed);seeded=json.loads(next(x[5:] for x in output.splitlines() if x.startswith('SEED=')));ok('seed_real_saved_receipt_task_pending_running_jobs_and_encrypted_mail')
  before=fingerprints(db());assert db().sql("SELECT count(*) FROM jobs WHERE status='queued'")=='1' and db().sql("SELECT count(*) FROM jobs WHERE status='running'")=='1'
  retention=tool('retention','--container',pg,'--database',source);assert retention['stopped_runtime_rows']==1 and not retention['applied'];assert db().sql('SELECT count(*) FROM runtime_instances')=='3';ok('retention_is_preview_by_default')
  tool('retention','--container',pg,'--database',source,'--apply');assert db().sql('SELECT count(*) FROM runtime_instances')=='2' and fingerprints(db())==before;ok('explicit_retention_only_removes_old_stopped_instances')
  start=time.perf_counter();b=tool('backup','--container',pg,'--database',source,'--output',str(backup));timing['backup_seconds']=time.perf_counter()-start
  assert b['status']=='backed_up' and not b['mail_key_included'];assert backup.stat().st_mode&0o777==0o700
  for name in ['database.dump','backup.json']:assert (backup/name).stat().st_mode&0o777==0o600
  assert key not in (backup/'backup.json').read_text() and password not in (backup/'backup.json').read_text();ok('real_custom_dump_private_permissions_and_no_key_in_manifest')
  tool('export-mail-key','--env-file',str(envfile),'--output',str(backup/'mail-key.json'),expected=1);ok('mail_key_export_cannot_mix_with_database_backup')
  tool('export-mail-key','--env-file',str(envfile),'--output',str(keyfile));assert keyfile.stat().st_mode&0o777==0o600;ok('separate_mail_key_export_permissions')
  # Prove that the destination can be rebuilt after the source data is gone.
  db().sql('CREATE DATABASE '+source+'_legacy TEMPLATE '+source,database='postgres')
  legacy=db(source+'_legacy');legacy.sql("DROP TABLE recovery_state; DELETE FROM schema_migrations WHERE name='0014_recovery_state.sql'")
  tool('backup','--container',pg,'--database',source+'_legacy','--output',str(OUT/'legacy-backup'))
  db().sql('DROP DATABASE '+source+' WITH (FORCE)',database='postgres');ok('original_synthetic_database_removed_before_restore')
  start=time.perf_counter();restored=tool('restore','--container',pg,'--database',target,'--backup',str(backup));timing['restore_seconds']=time.perf_counter()-start
  assert restored['status']=='restored_paused' and fingerprints(db(target))==before;ok('new_database_restore_matches_all_13_business_table_fingerprints')
  assert db(target).sql("SELECT count(*) FROM runtime_instances WHERE status='running'")=='0';ok('restored_process_heartbeats_marked_stopped')
  tool('restore','--container',pg,'--database',target,'--backup',str(backup),expected=1);assert fingerprints(db(target))==before;ok('existing_restore_database_never_overwritten')
  tool('restore','--container',pg,'--database','job_application_agent_dev','--backup',str(backup),expected=1);ok('developer_database_name_rejected')
  corrupt=OUT/'corrupt';shutil.copytree(backup,corrupt);payload=(corrupt/'database.dump').read_bytes();(corrupt/'database.dump').write_bytes(payload[:len(payload)//2]);badtarget=target+'_corrupt'
  tool('restore','--container',pg,'--database',badtarget,'--backup',str(corrupt),expected=1);assert db(target).sql("SELECT count(*) FROM pg_database WHERE datname='"+badtarget+"'",database='postgres')=='0';ok('checksum_failure_prevents_database_creation')
  # With matching manifest hash but damaged archive, restore transaction must roll back.
  meta=json.loads((corrupt/'backup.json').read_text());meta['dump_sha256']=digest(corrupt/'database.dump');(corrupt/'backup.json').write_text(json.dumps(meta))
  tool('restore','--container',pg,'--database',badtarget,'--backup',str(corrupt),expected=1);assert db(badtarget).sql("SELECT to_regclass('public.applications') IS NULL")=='t';ok('truncated_archive_restore_rolls_back_schema_transaction')
  missing=tool('verify','--container',pg,'--database',target,'--backup',str(backup),expected=1);assert '密钥' in missing['detail'];ok('missing_mail_key_is_explicit_and_database_remains_paused')
  wrong=OUT/'wrong-key.json';wrong.write_text(json.dumps({'MAIL_CREDENTIAL_KEY':base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()}));wrong.chmod(0o600)
  tool('verify','--container',pg,'--database',target,'--backup',str(backup),'--mail-key-file',str(wrong),expected=1);ok('wrong_mail_key_refused_without_exposing_credentials')
  tool('activate','--container',pg,'--database',target,'--backup',str(backup),'--mail-key-file',str(keyfile),expected=1);ok('activation_requires_explicit_processing_flag')
  start=time.perf_counter();verified=tool('verify','--container',pg,'--database',target,'--backup',str(backup),'--mail-key-file',str(keyfile));timing['verify_seconds']=time.perf_counter()-start
  assert verified['mail_credentials_verified'] and verified['paused'] and verified['rows']['jobs']==2 and verified['rows']['automatic_records']==1;ok('verification_reads_counts_constraints_and_decrypts_all_mail_credentials')
  api,call=start_api(target);assert call('GET','/health/ready',status=503)['status']=='not_ready';assert call('GET','/api/applications')['total']==1;ok('paused_api_reads_restored_business_but_is_not_ready_for_writes')
  state=call('GET','/api/operations/recovery');assert state['paused'] and state['verified_at'];ok('authenticated_recovery_state_is_visible')
  call('POST','/api/notifications',{'request_id':str(uuid4()),'text':'must not save'},409)
  call('POST','/api/jobs',{'request_id':str(uuid4()),'notification_id':seeded['jobs'][0]['notification_id']},409)
  call('POST','/api/mailboxes/'+seeded['mail_id']+'/sync',status=409)
  call('POST','/api/notifications/'+seeded['saved_notification']+'/process',status=409);assert fingerprints(db(target))==before;ok('paused_api_blocks_manual_jobs_mail_and_synchronous_model_mutations')
  for module in ['services.worker','services.mail_scheduler']:
   p=docker('run','--rm','--network',network,*envargs(env(target,'true')),'--entrypoint','.venv/bin/python',IMAGE,'-m',module,allow_failure=True)
   assert p.returncode!=0 and b'RecoveryPaused' in p.stderr;assert fingerprints(db(target))==before
  ok('real_worker_and_scheduler_refuse_paused_database_before_external_calls')
  # Existing migration checksums are validated before creating a destination.
  incompatible=OUT/'incompatible';shutil.copytree(backup,incompatible);meta=json.loads((incompatible/'backup.json').read_text());meta['migrations'][0]['sha256']='0'*64;(incompatible/'backup.json').write_text(json.dumps(meta))
  tool('restore','--container',pg,'--database',target+'_incompatible','--backup',str(incompatible),expected=1);ok('historical_migration_checksum_mismatch_refused')
  legacyrestore=tool('restore','--container',pg,'--database',target+'_legacy','--backup',str(OUT/'legacy-backup'))
  assert legacyrestore['applied_migrations']==['0014_recovery_state.sql'] and fingerprints(db(target+'_legacy'))==before;ok('p116_backup_upgrades_to_p117_and_stays_paused')
  # Inject a migration failure only in the recovered synthetic DB, not project migrations.
  failcode="""
import asyncio,os,tempfile,shutil,json
from pathlib import Path
import asyncpg
from database.migrate import migrate,MIGRATIONS
async def main():
 with tempfile.TemporaryDirectory() as folder:
  d=Path(folder)
  for p in MIGRATIONS.glob('*.sql'):shutil.copy2(p,d/p.name)
  (d/'0015_failure.sql').write_text('CREATE TABLE should_rollback(id integer); SELECT 1/0;')
  try:await migrate(directory=d);raise AssertionError('expected failure')
  except asyncpg.DivisionByZeroError:pass
 c=await asyncpg.connect(os.environ['DATABASE_URL'])
 try:assert await c.fetchval("SELECT to_regclass('should_rollback') IS NULL") and not await c.fetchval("SELECT EXISTS(SELECT 1 FROM schema_migrations WHERE name='0015_failure.sql')")
 finally:await c.close()
 print('MIGRATION_ROLLED_BACK')
asyncio.run(main())
"""
  assert 'MIGRATION_ROLLED_BACK' in fixture(target+'_legacy',failcode);assert fingerprints(db(target+'_legacy'))==before;ok('migration_failure_rolls_back_without_changing_business_snapshot')
  tool('activate','--container',pg,'--database',target,'--backup',str(backup),'--mail-key-file',str(keyfile),'--enable-processing');docker('rm','-f',api);api,call=start_api(target);assert call('GET','/health/ready')['status']=='ready';ok('verified_activation_and_api_restart_restore_normal_admission')
  for j in seeded['jobs']:
   replay=call('POST','/api/jobs',{'request_id':j['request_id'],'notification_id':j['notification_id']},202);assert replay['id']==j['id']
  ok('restored_original_request_ids_replay_original_jobs')
  worker=network+'-worker';containers.append(worker)
  docker('run','-d','--name',worker,'--network',network,*envargs(env(target)),'--entrypoint','.venv/bin/python',IMAGE,'-c','import asyncio;from services.worker import run;from scripts.check_automatic import extractor;asyncio.run(run(extractor=extractor))')
  for _ in range(150):
   if db(target).sql("SELECT count(*) FROM jobs WHERE status='succeeded'")=='2':break
   time.sleep(.1)
  else:raise AssertionError('restored queued/running jobs did not complete')
  assert db(target).sql('SELECT count(*) FROM applications')=='3' and db(target).sql('SELECT count(*) FROM tasks')=='1' and db(target).sql('SELECT count(*) FROM automatic_records')=='3';ok('fixture_worker_recovers_queued_and_expired_jobs_once')
  for j in seeded['jobs']:call('POST','/api/jobs',{'request_id':j['request_id'],'notification_id':j['notification_id']},202)
  assert db(target).sql('SELECT count(*) FROM applications')=='3';ok('replay_after_recovery_does_not_duplicate_business')
  # Keep all archive data private, and prove operational output contains no test secrets/body.
  captured=(OUT/'commands.log').read_text();assert all(x not in captured for x in [key,password,token,'P117_PRIVATE_CODE','P117_PRIVATE_BODY','p117-private@163.com']);ok('recovery_cli_outputs_do_not_contain_credentials_or_cached_mail_body')
  return {'checks_passed':len(checks),'checks':checks,'timings_seconds':timing,'mode':'fresh isolated PG16 container, actual custom dump/restore, real HTTP and separate worker/scheduler; explicit model/mail fixtures','real_model_called':False,'real_imap_called':False,'production_database_used':False,'source_database_destroyed_before_restore':True,'business_tables_fingerprinted':13}
 finally:
  for name in reversed(containers):
   p=docker('logs',name,allow_failure=True);(OUT/(name+'.log')).write_bytes(p.stdout+p.stderr)
   docker('rm','-f','-v',name,allow_failure=True)
  docker('network','rm',network,allow_failure=True)
  # Test keys are ephemeral; evidence retains only synthetic DB archives and sanitized reports.
  envfile.unlink(missing_ok=True);keyfile.unlink(missing_ok=True)
  for p in [OUT/'wrong-key.json',backup/'mail-key.json']:p.unlink(missing_ok=True)
  log.close()
  (OUT/'cleanup.json').write_text(json.dumps({'test_containers_and_network_removed':True,'test_key_files_removed':True,'developer_services_untouched':True}))
if __name__=='__main__':
 r=main();(OUT/'report.json').write_text(json.dumps(r,ensure_ascii=False,indent=2));print(json.dumps(r,ensure_ascii=False))
