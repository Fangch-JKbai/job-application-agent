"""P11.1 real HTTP/PG, separate worker, explicit delayed model fixture."""
import asyncio,json,os,subprocess,sys,time
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit
from uuid import uuid4,UUID
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import asyncpg
import httpx
from database.config import database_url
from database.migrate import migrate
from scripts.check_automatic import extractor,server,notice,mutation
from scripts.check_p4_api import TEXT
from services.worker import run
from agent.notification_extractor import ModelReply
OUT=Path(os.environ.get('P11_TEST_OUTPUT',str(ROOT/'.work/p11/p11.1')))
async def delayed(n):
 await asyncio.sleep(1)
 if n['source_reference']=='exception':raise RuntimeError('fixture-only private detail')
 return await extractor(n)

async def checks():
 from scripts import check_automatic
 check_automatic.REPORT=OUT
 OUT.mkdir(parents=True,exist_ok=True)
 base=database_url();name='job_application_agent_test_jobs_'+uuid4().hex[:8];dsn=urlunsplit(urlsplit(base)._replace(path='/'+name));owner=uuid4();admin=await asyncpg.connect(base);worker=None;checks=[]
 await admin.execute(f'CREATE DATABASE "{name}"')
 try:
  await migrate(dsn)
  conn=await asyncpg.connect(dsn)
  try:
   with server(dsn,owner,'before-restart') as a:
    n=notice(a,'submission','示例异步公司');payload=mutation(notification_id=n['id'])
    started=time.monotonic();j=a.call('POST','/api/jobs',payload,202);elapsed=time.monotonic()-started
    assert j['status']=='queued' and elapsed<1;checks.append('accepted_202_before_model_runs')
    assert a.last_headers.get('location',a.last_headers.get('Location'))=='/api/jobs/'+j['id'];checks.append('location_header')
    assert a.call('POST','/api/jobs',payload,202)['id']==j['id'];checks.append('request_replay')
    a.call('POST','/api/jobs',{**payload,'notification_id':str(uuid4())},409);checks.append('request_content_conflict')
    async with httpx.AsyncClient(trust_env=False) as client:
     rs=await asyncio.gather(*(client.post(a.base+'/api/jobs',json=mutation(notification_id=n['id'])) for _ in range(8)))
     assert all(r.status_code==202 and r.json()['id']==j['id'] for r in rs)
    checks.append('concurrent_semantic_dedup')
    a.call('POST','/api/jobs',mutation(notification_id=str(uuid4())),404);checks.append('unknown_notice_rejected')
    a.call('POST','/api/jobs',mutation(notification_id=n['id'],input_version=99),409);checks.append('stale_explicit_version_rejected')
   with server(dsn,owner,'after-restart') as a,server(dsn,uuid4(),'other-owner') as other:
    assert a.call('GET','/api/jobs/'+j['id'])['status']=='queued';checks.append('queue_survives_api_restart')
    other.call('GET','/api/jobs/'+j['id'],status=404)
    assert other.call('GET','/api/jobs')['total']==0
    other.call('POST','/api/jobs',mutation(notification_id=n['id']),404);checks.append('owner_isolation')
    with (OUT/'worker.log').open('w') as log:
     worker=subprocess.Popen([sys.executable,'scripts/check_p11_jobs.py','--worker'],cwd=ROOT,env={**os.environ,'DATABASE_URL':dsn,'MAIL_POLL_ENABLED':'false','MODEL_REQUESTS_PER_HOUR':'10000'},stdout=log,stderr=subprocess.STDOUT)
     async def wait(jid,statuses):
      for _ in range(150):
       value=a.call('GET','/api/jobs/'+jid)
       if value['status'] in statuses:return value
       if worker.poll() is not None:raise AssertionError('worker stopped')
       await asyncio.sleep(.05)
      raise AssertionError('job timeout')
     await wait(j['id'],{'running'})
     started=time.monotonic();a.call('GET','/api/applications');query_ms=(time.monotonic()-started)*1000
     assert query_ms<200;checks.append('api_query_available_during_worker_model_wait')
     done=await wait(j['id'],{'succeeded','failed'});assert done['status']=='succeeded' and done['result']['status']=='saved';checks.append('worker_saves_and_job_exposes_business_result')
     assert a.call('POST','/api/jobs',payload,202)['id']==j['id'];checks.append('original_request_replays_after_extraction_version_changes')
     for mode,expected in [('no_company','unresolved'),('other','ignored'),('bad','failed'),('exception','failed')]:
      no=notice(a,mode,'示例'+mode) if mode!='other' else a.call('POST','/api/notifications',mutation(text=TEXT,source_reference='other',hints={'company_name':'示例宣传公司'}),201);jj=a.call('POST','/api/jobs',mutation(notification_id=no['id']),202);out=await wait(jj['id'],{'succeeded','failed'})
      if expected=='failed':assert out['status']=='failed' and out['error'] and 'private detail' not in out['error']
      else:assert out['status']=='succeeded' and out['result']['status']==expected,(mode,out)
      checks.append('business_'+mode)
     # A review changes version while queued: fail explicitly rather than silently use new input.
     no=notice(a,'submission','示例版本公司');ex=a.call('POST',f'/api/notifications/{no["id"]}/extract',mutation())
     worker.terminate();await asyncio.to_thread(worker.wait,15);worker=None
     jj=a.call('POST','/api/jobs',mutation(notification_id=no['id']),202)
     await conn.execute('UPDATE notification_extractions SET version=version+1 WHERE id=$1',UUID(ex['id']))
     worker=subprocess.Popen([sys.executable,'scripts/check_p11_jobs.py','--worker'],cwd=ROOT,env={**os.environ,'DATABASE_URL':dsn,'MAIL_POLL_ENABLED':'false','MODEL_REQUESTS_PER_HOUR':'10000'},stdout=log,stderr=subprocess.STDOUT)
     out=await wait(jj['id'],{'failed'});assert '变化' in out['error'];checks.append('queued_input_change_rejected_after_worker_restart')
     assert a.call('GET','/api/jobs?notification_id='+no['id'])['total']==1;checks.append('filtered_pagination')
     assert await conn.fetchval('SELECT count(*) FROM applications WHERE owner_id=$1',owner)==1;checks.append('no_extra_business_rows_on_failed_or_ignored_jobs')
     return {'checks_passed':len(checks),'checks':checks,'mode':'real HTTP/PostgreSQL, separate worker with explicit model fixture','submission_seconds':elapsed,'query_during_model_ms':query_ms,'temporary_environment_cleaned':True}
  finally:await conn.close()
 finally:
  if worker is not None and worker.poll() is None:worker.terminate();await asyncio.to_thread(worker.wait,15)
  await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)');await admin.close()
if __name__=='__main__':
 if '--worker' in sys.argv:asyncio.run(run(extractor=delayed))
 else:
  OUT.mkdir(parents=True,exist_ok=True)
  result=asyncio.run(checks());(OUT/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False))
