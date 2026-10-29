"""Migrate P11.1 data in a disposable database and check default HTTP boundaries."""
import asyncio,json,os,sys,tempfile,shutil
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import asyncpg
from database.config import database_url
from database.migrate import migrate,MIGRATIONS
from services.notifications import NotificationService
from schemas.notifications import NotificationImport,ExtractionRequest
from scripts.check_automatic import extractor,server
from scripts.check_p4_api import TEXT,normal_result
from scripts.check_p11_reliability import settings
from services.jobs import JobService
from schemas.jobs import JobCreate
OUT=ROOT/os.environ.get('P11_UPGRADE_OUTPUT','.work/p11/p11.2-4/upgrade')
async def main():
 OUT.mkdir(parents=True,exist_ok=False);base=database_url();name='job_application_agent_test_upgrade_'+uuid4().hex[:8];dsn=urlunsplit(urlsplit(base)._replace(path='/'+name));admin=await asyncpg.connect(base);await admin.execute(f'CREATE DATABASE "{name}"');checks=[]
 try:
  with tempfile.TemporaryDirectory(prefix='job-agent-old-migrations-') as folder:
   for p in MIGRATIONS.glob('*.sql'):
    if p.name<'0010':shutil.copy2(p,Path(folder)/p.name)
   await migrate(dsn,Path(folder))
  conn=await asyncpg.connect(dsn)
  try:
   owner=uuid4();await conn.execute('INSERT INTO app_users(id,display_name) VALUES($1,$2)',owner,'合成升级用户')
   service=NotificationService(conn,owner,extractor)
   n=await service.import_notification(NotificationImport(request_id=uuid4(),text=TEXT,source_reference='assessment'))
   old=uuid4();await conn.execute("INSERT INTO jobs(id,owner_id,notification_id,operation,input_version) VALUES($1,$2,$3,'process_notification',0)",old,owner,n['id'])
   # Seed a historical completed extraction; new runtime requires migrations before use.
   await conn.execute("INSERT INTO notification_extractions(owner_id,notification_id,request_id,status,prompt_version,phase,model_result) VALUES($1,$2,$3,'completed','historical-fixture','completed',$4::jsonb)",owner,n['id'],uuid4(),json.dumps(normal_result(),ensure_ascii=False))
   newer=uuid4();await conn.execute("INSERT INTO jobs(id,owner_id,notification_id,operation,input_version,status) VALUES($1,$2,$3,'process_notification',1,'running')",newer,owner,n['id'])
   completed=await migrate(dsn);assert completed==['0010_job_leases.sql','0011_mail_scheduling.sql','0012_performance_indexes.sql','0013_observation.sql','0014_recovery_state.sql'];checks.append('ordered_upgrade_from_0009')
   assert await conn.fetchval('SELECT count(DISTINCT input_key) FROM jobs WHERE notification_id=$1',n['id'])==2;checks.append('multiple_legacy_versions_keep_distinct_keys')
   assert await conn.fetchval("SELECT status FROM jobs WHERE id=$1",newer)=='failed';checks.append('unknown_legacy_running_outcome_is_not_blindly_replayed')
   assert await migrate(dsn)==[];checks.append('migrations_idempotent')
   from scripts import check_automatic as c
   c.REPORT=OUT/'http'
   with settings(JOB_QUEUE_LIMIT=1,JOB_OWNER_QUEUE_LIMIT=1),server(dsn,owner,'bounded-default',diagnostics=False) as a:
    # The legacy queued job occupies the only slot; replay remains allowed.
    result=a.call('POST','/api/jobs',{'request_id':str(uuid4()),'notification_id':str(n['id'])},202)
    assert result['id']==str(newer);checks.append('semantic_replay_allowed_when_queue_full')
    nn=a.call('POST','/api/notifications',{'request_id':str(uuid4()),'text':TEXT},201)
    a.call('POST','/api/jobs',{'request_id':str(uuid4()),'notification_id':nn['id']},429)
    assert a.last_headers.get('Retry-After',a.last_headers.get('retry-after'))=='10';checks.append('queue_full_http_429_and_retry_after')
    assert a.call('GET','/api/notifications/'+nn['id'])['text']==TEXT;checks.append('overloaded_submission_preserves_original_notice')
    a.call('POST','/api/jobs/'+str(old)+'/cancel');assert a.call('GET','/api/jobs/'+str(old))['status']=='cancelled';checks.append('cancel_endpoint')
    a.call('POST','/api/jobs/'+str(old)+'/retry',{'request_id':str(uuid4())},409);checks.append('retry_old_snapshot_requires_new_input')
    assert a.call('GET','/api/jobs/'+str(old)+'/attempts')==[];checks.append('attempt_history_endpoint')
    a.call('POST',f'/api/notifications/{nn["id"]}/process',status=409);checks.append('default_synchronous_processing_blocked')
  finally:await conn.close()
  return {'checks_passed':len(checks),'checks':checks,'mode':'real disposable PostgreSQL upgrade and HTTP; model fixture only','temporary_database_removed':True}
 finally:await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)');await admin.close()
if __name__=='__main__':
 result=asyncio.run(main());(OUT/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False))
