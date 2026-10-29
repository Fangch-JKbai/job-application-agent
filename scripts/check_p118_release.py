"""Fresh local delivery acceptance; synthetic data and explicit delayed worker fixture only."""
import argparse,base64,hashlib,http.cookiejar,json,os,secrets,socket,subprocess,sys,time
from pathlib import Path
from urllib.error import HTTPError,URLError
from urllib.request import Request,build_opener,HTTPCookieProcessor,ProxyHandler
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
class Client:
    def __init__(self,port):
        self.base=f'http://127.0.0.1:{port}';self.opener=build_opener(ProxyHandler({}),HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def call(self,method,path,body=None,status=200):
        req=Request(self.base+path,method=method,data=json.dumps(body,ensure_ascii=False).encode() if body is not None else None,headers={'Content-Type':'application/json'})
        try:
            with self.opener.open(req,timeout=10) as r:code,raw=r.status,r.read()
        except HTTPError as e:code,raw=e.code,e.read()
        data=json.loads(raw) if raw.startswith((b'{',b'[')) else raw.decode()
        assert code==status,(method,path,code,data)
        return data

def mutation(**kw):return {'request_id':str(uuid4()),**kw}
def wait_for(fn,timeout=30):
    start=time.monotonic()
    while time.monotonic()-start<timeout:
        result=fn()
        if result:return result
        time.sleep(.15)
    raise AssertionError('release condition timed out')

async def fixture_worker(delay):
    import asyncio
    from agent.notification_extractor import ModelReply
    from scripts.check_p4_api import normal_result
    from services.worker import run
    async def extractor(notice):
        await asyncio.sleep(delay)
        data=normal_result();company=notice['text'].split('2027秋招')[0]
        data['company_name']=company;data['evidence']['company_name']['quote']=company
        return ModelReply(json.dumps(data,ensure_ascii=False),'release-explicit-fixture','fixture')
    await run(extractor=extractor)

def acceptance(image,out):
    out.mkdir(parents=True,exist_ok=False);tag=uuid4().hex[:8];project='job-agent-p118-'+tag
    with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
    values={'POSTGRES_PASSWORD':secrets.token_urlsafe(24),'APP_ACCESS_TOKEN':secrets.token_urlsafe(36),'APP_SESSION_SECRET':secrets.token_urlsafe(36),'MAIL_CREDENTIAL_KEY':base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),'LOCAL_USER_ID':str(uuid4()),'APP_PORT':str(port),'POSTGRES_IMAGE':'pgvector/pgvector:pg16','APP_IMAGE':image,'MAIL_POLL_ENABLED':'true','WORKER_CONCURRENCY':'1','JOB_LEASE_SECONDS':'3','OBSERVATION_HEARTBEAT_SECONDS':'1','OBSERVATION_STALE_SECONDS':'10','MODEL_REQUESTS_PER_HOUR':'10000','PYTHON_DOTENV_DISABLED':'1','ENABLE_SYNC_DIAGNOSTICS':'false'}
    envfile=ROOT/'.env';assert not envfile.exists(),'release acceptance requires an unpacked clean source directory'
    envfile.write_text('\n'.join(k+'='+v for k,v in values.items())+'\n');envfile.chmod(0o600)
    env={**os.environ,**values,'COMPOSE_PROJECT_NAME':project}
    compose=['docker','compose','--env-file',str(envfile),'-f',str(ROOT/'compose.delivery.yaml'),'-p',project]
    override=out/'fixture-worker.json';checks=[];restore_db='job_application_agent_restore_p118_'+tag;report={};failure=None
    log=(out/'commands.log').open('w')
    def command(args,stdin=None,check=True):
        r=subprocess.run(args,cwd=ROOT,env=env,input=stdin,text=True,capture_output=True)
        if r.returncode and check:raise AssertionError('release command failed: '+args[0]+' (exit '+str(r.returncode)+')')
        return r
    def comp(*args,fixture=False):return command([*compose,*(['-f',str(override)] if fixture else []),*args])
    def docker(*args):return command(['docker',*args]).stdout.strip()
    def cid(role):return comp('ps','-aq',role).stdout.strip()
    def sql(text,db='job_application_agent_dev'):
        return command(['docker','exec','-i',cid('postgres'),'psql','-X','-qAt','-U','job_agent','-d',db,'-v','ON_ERROR_STOP=1'],text).stdout.strip()
    def ok(name):checks.append(name);print('PASS '+name,flush=True)
    def tool(*args):
        r=command([sys.executable,'scripts/data_recovery.py',*args]);log.write(r.stdout);log.flush();return json.loads(r.stdout)
    def notice(client,label):
        text=label+'2027秋招Agent开发工程师：请于2026年10月30日18:00前完成线上测评。'
        return client.call('POST','/api/notifications',mutation(text=text,timezone_hint='Asia/Shanghai'),201)
    def seeded(client,label):
        n=notice(client,label)
        code="from scripts.check_p4_api import normal_result;import json;d=normal_result();d['company_name']="+repr(label)+";d['evidence']['company_name']['quote']="+repr(label)+";print(json.dumps(d,ensure_ascii=False))"
        result=docker('exec',cid('app'),'.venv/bin/python','-c',code)
        sql("INSERT INTO notification_extractions(owner_id,notification_id,request_id,status,prompt_version,phase,model_result) VALUES('"+values['LOCAL_USER_ID']+"','"+n['id']+"','"+str(uuid4())+"','completed','release-seeded-fixture','completed','"+result.replace("'","''")+"'::jsonb)")
        request=mutation(notification_id=n['id']);j=client.call('POST','/api/jobs',request,202)
        return n,j,request
    def done(client,j):
        value=client.call('GET','/api/jobs/'+j['id'])
        if value['status']=='failed':raise AssertionError('fixture job failed: '+str(value.get('error_kind')))
        return value if value['status']=='succeeded' else None
    def fixture(delay):
        override.write_text(json.dumps({'services':{'worker':{'command':['.venv/bin/python','scripts/check_p118_release.py','--fixture-worker','--delay',str(delay)]}}}))
        comp('up','-d','--no-deps','--pull','never','worker',fixture=True)
    def fingerprints(db):
        from scripts.data_recovery import TABLES
        return {table:hashlib.sha256(sql("SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]') FROM "+table+' t',db).encode()).hexdigest() for table in TABLES}
    try:
        comp('up','-d','--pull','never','--wait');client=Client(port);ok('fresh_delivery_compose_four_services_healthy')
        for role,cpu,memory in [('postgres',2,1024),('app',1,512),('worker',1,512),('scheduler',.25,256)]:
            info=json.loads(docker('inspect',cid(role)))[0];host=info['HostConfig']
            assert host['NanoCpus']==int(cpu*1e9) and host['Memory']==memory*1024**2
            assert host['LogConfig']=={'Type':'json-file','Config':{'max-size':'10m','max-file':'3'}}
            assert info['State']['Health']['Status']=='healthy'
            if role!='postgres':assert info['Config']['User']=='jobagent' and host['Init'] is True
        assert json.loads(docker('inspect',cid('app')))[0]['HostConfig']['PortBindings']['8001/tcp'][0]['HostIp']=='127.0.0.1'
        assert not json.loads(docker('inspect',cid('postgres')))[0]['HostConfig']['PortBindings']
        ok('docker_inspect_enforces_cpu_memory_rotation_init_and_loopback')
        (out/'runtime-config.json').write_text(json.dumps({role:{'limits':{k:json.loads(docker('inspect',cid(role)))[0]['HostConfig'].get(k) for k in ['NanoCpus','Memory','LogConfig','Init']},'health':'healthy'} for role in ['postgres','app','worker','scheduler']},indent=2))
        health_program="""import json,os,tempfile,time
from pathlib import Path
from scripts.check_runtime_health import healthy
with tempfile.TemporaryDirectory() as d:
 p=Path(d)/'heartbeat.json';os.environ['RUNTIME_HEALTH_FILE']=str(p)
 assert not healthy('worker')
 v={'role':'worker','pid':os.getpid(),'seen_at':time.time()};p.write_text(json.dumps(v));assert healthy('worker')
 v['seen_at']=0;p.write_text(json.dumps(v));assert not healthy('worker')
 v['seen_at']=time.time();v['pid']=2147483647;p.write_text(json.dumps(v));assert not healthy('worker')
 v['pid']=os.getpid();v['role']='scheduler';p.write_text(json.dumps(v));assert not healthy('worker')
 os.environ['MAIL_POLL_ENABLED']='false';assert healthy('scheduler')
"""
        docker('exec',cid('worker'),'.venv/bin/python','-c',health_program);ok('process_health_rejects_missing_stale_dead_or_wrong_role_marker')
        assert sql('SELECT count(*) FROM schema_migrations')=='14';ok('empty_volume_migrated_to_0014')
        client.call('GET','/api/applications',status=401);client.call('POST','/api/auth/login',{'token':values['APP_ACCESS_TOKEN']});ok('private_login_required')
        assert client.call('GET','/health/ready')['status']=='ready'
        assert '<html' in client.call('GET','/').lower();ok('built_workbench_and_readiness_served')
        n,j,request=seeded(client,'示例发布公司');saved=wait_for(lambda:done(client,j));assert saved['result']['status']=='saved'
        assert client.call('POST','/api/jobs',request,202)['id']==j['id'];ok('real_worker_seeded_extraction_save_and_request_replay')
        assert client.call('GET','/api/applications')['total']==1 and client.call('GET','/api/tasks')['total']==1;ok('business_readback')
        comp('restart','app');wait_for(lambda:comp('ps','--format','json','app').returncode==0)
        def ready():
            try:return client.call('GET','/health/ready')['status']=='ready'
            except (OSError,URLError,AssertionError):return False
        wait_for(ready);assert client.call('GET','/api/applications')['total']==1;ok('restart_preserves_cookie_business_and_volume')
        appid=saved['result']['application_id'];app=client.call('GET','/api/applications/'+appid)
        client.call('PATCH','/api/applications/'+appid,mutation(version=app['version'],notes='合成手动修改'))
        client.call('POST','/api/notifications/'+n['id']+'/undo',status=409);ok('undo_does_not_overwrite_later_manual_edit')
        undo_notice,undo_job,_=seeded(client,'示例撤销公司');wait_for(lambda:done(client,undo_job))
        undone=client.call('POST','/api/notifications/'+undo_notice['id']+'/undo');assert undone['status']=='undone'
        assert client.call('POST','/api/notifications/'+undo_notice['id']+'/undo')==undone;ok('automatic_undo_is_replayable')
        client.call('POST','/api/notifications/'+n['id']+'/process',status=409);ok('sync_model_bypass_remains_disabled')
        comp('stop','worker');fixture(6)
        first=notice(client,'示例优雅退出');first_job=client.call('POST','/api/jobs',mutation(notification_id=first['id']),202)
        wait_for(lambda:client.call('GET','/api/jobs/'+first_job['id'])['status']=='running')
        queued=notice(client,'示例停机排队');queued_job=client.call('POST','/api/jobs',mutation(notification_id=queued['id']),202)
        # Exercise the actual stop helper: API/scheduler stop first, then worker drains.
        r=command(['bash','scripts/delivery.sh','stop']);log.write(r.stdout+r.stderr)
        assert sql("SELECT status FROM jobs WHERE id='"+first_job['id']+"'")=='succeeded'
        assert sql("SELECT status FROM jobs WHERE id='"+queued_job['id']+"'")=='queued'
        assert json.loads(docker('inspect',cid('worker')))[0]['State']['ExitCode']==0
        try:Client(port).call('GET','/health/live');raise AssertionError('stopped API still accepting')
        except (OSError,URLError):pass
        ok('stop_helper_closes_api_and_scheduler_drains_inflight_leaves_queue')
        comp('up','-d','--pull','never','--wait',fixture=True);wait_for(ready);wait_for(lambda:done(client,queued_job));ok('restart_recovers_pending_job_once')
        comp('stop','worker');fixture(8)
        interrupted=notice(client,'示例限时强停');killed_job=client.call('POST','/api/jobs',mutation(notification_id=interrupted['id']),202)
        wait_for(lambda:client.call('GET','/api/jobs/'+killed_job['id'])['status']=='running')
        docker('stop','--time','1',cid('worker'));assert json.loads(docker('inspect',cid('worker')))[0]['State']['ExitCode']==137
        fixture(.2);wait_for(lambda:done(client,killed_job))
        attempts=client.call('GET','/api/jobs/'+killed_job['id']+'/attempts')
        assert len(attempts)==2 and {r['status'] for r in attempts}=={'expired','succeeded'};ok('grace_timeout_sigkill_lease_takeover_without_duplicate_write')
        assert sql("SELECT count(*) FROM automatic_records WHERE notification_id='"+interrupted['id']+"'")=='1'
        assert sql("SELECT count(*) FROM automatic_records WHERE notification_id='"+first['id']+"'")=='1';ok('shutdown_paths_keep_one_business_receipt')
        from scripts.data_recovery import TABLES
        original=fingerprints('job_application_agent_dev');backup=out/'database-backup'
        assert tool('backup','--container',cid('postgres'),'--output',str(backup))['status']=='backed_up'
        assert tool('restore','--container',cid('postgres'),'--database',restore_db,'--backup',str(backup))['status']=='restored_paused'
        assert fingerprints(restore_db)==original;ok('fresh_release_backup_restore_13_table_fingerprints_match')
        verified=tool('verify','--container',cid('postgres'),'--database',restore_db,'--backup',str(backup))
        assert verified['paused'] and verified['user_ids']==[values['LOCAL_USER_ID']];ok('restored_copy_verified_and_stays_paused')
        sql('DROP DATABASE "'+restore_db+'" WITH (FORCE)',db='postgres')
        assert client.call('GET','/api/operations/recovery')['paused'] is False;ok('restore_does_not_change_source_processing_state')
        assert all(client.call('GET','/api/operations/metrics')['fresh_instances'][role]>=1 for role in ['api','worker','scheduler']);ok('three_role_observation_still_fresh')
        report={'checks_passed':len(checks),'checks':checks,'image_id':docker('image','inspect','--format','{{.Id}}',image),'mode':'unpacked source archive; actual delivery Compose; seeded extraction and explicit delayed model fixture; synthetic data only','real_model_called':False,'real_mail_called':False,'business_tables_fingerprinted':len(TABLES),'project':project}
    except BaseException as exc:
        failure=exc
        (out/'failure.json').write_text(json.dumps({'checks_passed':len(checks),'checks':checks,'error_type':type(exc).__name__,'error':str(exc)},ensure_ascii=False,indent=2))
        raise
    finally:
        r=command([*compose,'logs','--no-color'],check=False);log.write(r.stdout+r.stderr);log.close()
        cleanup=command([*compose,'down','--volumes','--remove-orphans'],check=False)
        envfile.unlink(missing_ok=True);override.unlink(missing_ok=True)
        cleaned=cleanup.returncode==0
        (out/'cleanup.json').write_text(json.dumps({'containers_network_volume_removed':cleaned,'test_env_removed':not envfile.exists()}))
        assert cleaned,'release cleanup failed'
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False))
    return report

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--fixture-worker',action='store_true');p.add_argument('--delay',type=float,default=1);p.add_argument('--image');p.add_argument('--output',type=Path);a=p.parse_args()
    if a.fixture_worker:
        import asyncio
        asyncio.run(fixture_worker(a.delay))
    else:
        assert a.image and a.output,'--image and --output required'
        acceptance(a.image,a.output.resolve())
