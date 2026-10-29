"""Four-service local container smoke; fresh secrets/volume, no model or mailbox calls."""
import json,os,secrets,socket,subprocess,sys,time
from pathlib import Path
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.check_p10_api import Client,mutation
from scripts.check_p4_api import TEXT,normal_result
OUT=ROOT/os.environ.get('P11_CONTAINER_OUTPUT','.work/p11/p11.2-4/container');IMAGE=os.environ.get('P11_CONTAINER_IMAGE','job-agent-p118-local:20261029')
def main():
 OUT.mkdir(parents=True,exist_ok=False);project='job-agent-p11-smoke-'+uuid4().hex[:8]
 with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
 token=secrets.token_urlsafe(36);owner=uuid4();password=secrets.token_urlsafe(24)
 envfile=OUT/'smoke.env';envfile.write_text('\n'.join(['APP_IMAGE='+IMAGE,'POSTGRES_PASSWORD='+password,'APP_ACCESS_TOKEN='+token,'APP_SESSION_SECRET='+secrets.token_urlsafe(36),'LOCAL_USER_ID='+str(owner),'APP_PORT='+str(port),'POSTGRES_IMAGE=pgvector/pgvector:pg16','MAIL_POLL_ENABLED=true','ENABLE_SYNC_DIAGNOSTICS=false'])+'\n');envfile.chmod(0o600)
 # Copy the actual delivery compose, replacing only build tags and its env_file.
 # This prevents loading the user's real .env while testing the delivery entrypoints.
 text=(ROOT/'compose.delivery.yaml').read_text().replace('  build: .','  build: '+str(ROOT)).replace('    build: .','    image: '+IMAGE).replace('env_file: .env','env_file: '+str(envfile))
 compose=OUT/'compose.yaml';compose.write_text(text);compose.chmod(0o600)
 args=['docker','compose','--env-file',str(envfile),'-f',str(compose),'-p',project];checks=[];report={}
 with (OUT/'compose.log').open('w') as log:
  def run(*cmd,input=None):return subprocess.run([*args,*cmd],cwd=ROOT,input=input,text=True,stdout=log,stderr=subprocess.STDOUT,check=True)
  try:
   run('up','-d','--pull','never','--wait');a=Client(port)
   for _ in range(150):
    try:a.call('GET','/health/live');break
    except OSError:time.sleep(.1)
   else:raise AssertionError('container API startup failed')
   a.call('GET','/api/jobs',status=401);a.call('POST','/api/auth/login',{'token':token});checks.append('private_login_enforced')
   notice=a.call('POST','/api/notifications',mutation(text=TEXT,timezone_hint='Asia/Shanghai'),201)
   sql="INSERT INTO notification_extractions(owner_id,notification_id,request_id,status,prompt_version,phase,model_result) VALUES('%s','%s','%s','completed','container-seeded-fixture','completed','%s'::jsonb);"%(owner,notice['id'],uuid4(),json.dumps(normal_result(),ensure_ascii=False).replace("'","''"))
   run('exec','-T','postgres','psql','-U','job_agent','-d','job_application_agent_dev','-v','ON_ERROR_STOP=1',input=sql)
   job=a.call('POST','/api/jobs',mutation(notification_id=notice['id']),202)
   for _ in range(100):
    done=a.call('GET','/api/jobs/'+job['id'])
    if done['status'] in {'succeeded','failed'}:break
    time.sleep(.1)
   assert done['status']=='succeeded' and done['result']['status']=='saved';checks.append('real_container_worker_saves_seeded_extraction_without_model_call')
   assert a.call('GET','/api/applications')['total']==1;assert a.call('GET','/api/tasks')['total']==1;checks.append('container_business_readback')
   for service in ['postgres','app','worker','scheduler']:
    cid=subprocess.check_output([*args,'ps','-q',service],text=True).strip();assert cid
    assert subprocess.check_output(['docker','inspect','--format','{{.State.Running}}',cid],text=True).strip()=='true'
   checks.append('api_worker_scheduler_database_all_running')
   run('restart','app')
   for _ in range(100):
    try:a.call('GET','/health/live');break
    except OSError:time.sleep(.1)
   assert a.call('GET','/api/applications')['total']==1;checks.append('container_restart_preserves_cookie_and_saved_business')
   a.call('POST','/api/notifications/'+notice['id']+'/process',status=409);checks.append('container_synchronous_model_bypass_disabled')
   if os.environ.get('P11_CONTAINER_OBSERVATION')=='true':
    assert a.call('GET','/health/ready')['status']=='ready';checks.append('container_readiness_check')
    for _ in range(60):
     metrics=a.call('GET','/api/operations/metrics')
     if all(metrics['fresh_instances'][r]>=1 for r in ['api','worker','scheduler']):break
     time.sleep(.1)
    else:raise AssertionError('three roles did not register fresh heartbeats')
    checks.append('container_api_worker_scheduler_heartbeats_visible')
    trace=a.call('GET','/api/operations/jobs/'+job['id']);assert trace['phase']=='saved' and trace['requests'][0]['http_request_id'] and trace['attempts'][0]['status']=='succeeded'
    (OUT/'task-trace.json').write_text(json.dumps(trace,indent=2));checks.append('container_request_job_attempt_business_trace')
   report={'checks_passed':len(checks),'checks':checks,'image_id':subprocess.check_output(['docker','image','inspect','--format','{{.Id}}',IMAGE],text=True).strip(),'temporary_project':project,'mode':'actual delivery Compose with isolated env/image/volume; synthetic seeded extraction','real_model_called':False,'real_mailbox_used':False}
  finally:
   run('down','--volumes','--remove-orphans')
   envfile.unlink();compose.unlink()
   report['temporary_containers_volume_and_test_secret_files_removed']=True
 (OUT/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False))
if __name__=='__main__':main()
