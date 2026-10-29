"""P11.5 reproducible load experiment; all inputs and model responses are synthetic."""
import argparse,asyncio,hashlib,json,math,os,platform,secrets,socket,subprocess,sys,tempfile,time
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime,timezone
from pathlib import Path
from uuid import UUID,uuid4
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import asyncpg,httpx
from database.migrate import migrate,MIGRATIONS
from scripts.check_automatic import extractor
from scripts.check_p4_api import TEXT,normal_result
from agent.notification_extractor import ExtractionFailure
OWNER=UUID('00000000-0000-0000-0000-000000001155')

async def model_fixture(notice):
    mode=notice.get('source_reference') or ''
    delay=float(os.environ.get('BENCH_MODEL_DELAY','0.25'))
    if mode.startswith('bench-fault:'):
        c=await asyncpg.connect(os.environ['DATABASE_URL'])
        try:first=await c.fetchval('INSERT INTO bench_fault_calls(notification_id) VALUES($1) ON CONFLICT DO NOTHING RETURNING notification_id',notice['id'])
        finally:await c.close()
        if first:
            if mode.endswith('429'):raise ExtractionFailure('benchmark fixture 429',429,retryable=True,retry_after_seconds=1)
            if mode.endswith('timeout'):await asyncio.sleep(2)
    await asyncio.sleep(delay)
    result=await extractor({**notice,'source_reference':'submission'})
    return result

def build_benchmark_app():
    from api.main import app
    original=app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(app):
        async with original(app):
            app.state.notification_extractor=model_fixture
            yield
    app.router.lifespan_context=lifespan
    @app.get('/_bench/pool',include_in_schema=False)
    async def pool_metrics():return app.state.pool.metrics()
    return app

async def seed(c,small=False):
    await c.execute('INSERT INTO app_users(id,display_name) VALUES($1,$2)',OWNER,'合成压测用户')
    apps=100 if small else 5000
    await c.execute("""INSERT INTO applications(id,owner_id,company_name,position_name,batch,created_at,updated_at)
      SELECT md5('bench-app-'||i)::uuid,$1,'示例基线公司'||i,'Agent开发工程师','27秋招',
      '2026-01-01'::timestamptz+i*interval '1 second','2026-01-01'::timestamptz+i*interval '1 second' FROM generate_series(1,$2::int) i""",OWNER,apps)
    if small:return
    await c.execute("""INSERT INTO tasks(id,owner_id,application_id,type,title,due_on,created_at,updated_at)
      SELECT md5('bench-task-'||i)::uuid,$1,md5('bench-app-'||((i-1)%5000+1))::uuid,'assessment','合成测评',
      '2026-10-29'::date+i%14,'2026-01-01'::timestamptz+i*interval '1 second','2026-01-01'::timestamptz+i*interval '1 second' FROM generate_series(1,10000) i""",OWNER)
    await c.execute("""INSERT INTO application_events(id,owner_id,application_id,event_type,actor_type,source_kind,after_data,operation_id,created_at)
      SELECT md5('bench-event-'||i)::uuid,$1,md5('bench-app-'||((i-1)%5000+1))::uuid,'application_created','system','synthetic_seed','{}',md5('bench-operation-'||i)::uuid,
      '2026-01-01'::timestamptz+i*interval '1 second' FROM generate_series(1,30000) i""",OWNER)
    await c.execute("""INSERT INTO notifications(id,owner_id,request_id,request_fingerprint,text,source,hints,imported_at,calendar_year,timezone_hint)
      SELECT md5('bench-notice-'||i)::uuid,$1,md5('bench-import-'||i)::uuid,'synthetic-seed', $2,'manual','{}',
      '2026-01-01'::timestamptz+i*interval '1 second',2026,'Asia/Shanghai' FROM generate_series(1,50000) i""",OWNER,TEXT)
    await c.execute("""INSERT INTO notification_extractions(id,owner_id,notification_id,request_id,status,prompt_version,phase,model_result,finished_at)
      SELECT md5('bench-extract-'||i)::uuid,$1,md5('bench-notice-'||i)::uuid,md5('bench-extract-request-'||i)::uuid,'completed','synthetic-seeded-history','completed',$2::jsonb,now()
      FROM generate_series(1,10000) i""",OWNER,json.dumps(normal_result(),ensure_ascii=False))
    await c.execute("""INSERT INTO jobs(id,owner_id,notification_id,operation,input_version,input_key,status,attempt_count,created_at,finished_at)
      SELECT md5('bench-job-'||i)::uuid,$1,md5('bench-notice-'||i)::uuid,'process_notification',0,'0','succeeded',1,
      '2026-01-01'::timestamptz+i*interval '1 second','2026-01-01'::timestamptz+i*interval '1 second' FROM generate_series(1,50000) i""",OWNER)
    await c.execute('CREATE TABLE bench_fault_calls(notification_id UUID PRIMARY KEY)')
    await c.execute('ANALYZE')

PROFILES={
 'applications_page':('SELECT * FROM applications WHERE owner_id=$1 ORDER BY created_at DESC,id DESC LIMIT 20',(OWNER,)),
 'tasks_page':('SELECT * FROM tasks WHERE owner_id=$1 ORDER BY created_at DESC,id DESC LIMIT 20',(OWNER,)),
 'capacity':("SELECT count(*),count(*) FILTER(WHERE owner_id=$1) FROM jobs WHERE status IN ('queued','running')",(OWNER,)),
 'notifications_page':('SELECT id FROM notifications WHERE owner_id=$1 ORDER BY imported_at DESC,id DESC LIMIT 20',(OWNER,)),
 'inbox':(None,(OWNER,20,0)),
}
async def explain(c,out):
    import re
    sql=re.search(r"rows=await pool.fetch\('''(SELECT n.id.*?)'''",(ROOT/'api/workbench.py').read_text(),re.S).group(1)
    results={}
    for name,(query,args) in PROFILES.items():
        plans=[]
        for _ in range(3):plans.append(json.loads(await c.fetchval('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) '+(query or sql),*args))[0])
        results[name]=plans
    (out/'explain.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))

class Environment:
    def __init__(self,out,config,phase,small=False,delay=.25,limit=2,slots=1,timeout=90):
        self.out,self.config,self.phase,self.small=out,config,phase,small
        self.delay,self.limit,self.slots,self.timeout=delay,limit,slots,timeout
        self.project='job-agent-p115-'+uuid4().hex[:8];self.started=False;self.pool=None;self.metrics=[];self.stop=asyncio.Event()
    def command(self,*args,capture=False):
        cmd=['docker','compose','-f',str(self.compose),'-p',self.project,*args]
        return subprocess.check_output(cmd,text=True) if capture else subprocess.run(cmd,stdout=self.log,stderr=subprocess.STDOUT,check=True)
    async def __aenter__(self):
        self.out.mkdir(parents=True,exist_ok=False);self.log=(self.out/'compose.log').open('w')
        def port():
            with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]
        pgport,apiport=port(),port();password=secrets.token_urlsafe(24);self.token=secrets.token_urlsafe(36)
        self.dsn=f'postgresql://bench:{password}@127.0.0.1:{pgport}/job_application_agent_bench'
        env={'DATABASE_URL':f'postgresql://bench:{password}@postgres:5432/job_application_agent_bench','LOCAL_USER_ID':str(OWNER),'APP_MODE':'private','APP_ACCESS_TOKEN':self.token,'APP_SESSION_SECRET':secrets.token_urlsafe(36),'MAIL_POLL_ENABLED':'false','MODEL_REQUESTS_PER_HOUR':'10000','API_DB_POOL_MAX':'5','WORKER_DB_POOL_MAX':'8','WORKER_CONCURRENCY':str(self.slots),'MODEL_CONCURRENCY_LIMIT':str(self.limit),'JOB_QUEUE_LIMIT':'200','JOB_OWNER_QUEUE_LIMIT':'100','JOB_LEASE_SECONDS':'3','JOB_EXECUTION_TIMEOUT':str(self.timeout),'ENABLE_SYNC_DIAGNOSTICS':'true','BENCH_MODEL_DELAY':str(self.delay)}
        self.envfile=self.out/'runtime.env';self.envfile.write_text('\n'.join(k+'='+v for k,v in env.items())+'\n');self.envfile.chmod(0o600)
        services={'postgres':{'image':self.config['postgres_image'],'environment':{'POSTGRES_USER':'bench','POSTGRES_PASSWORD':password,'POSTGRES_DB':'job_application_agent_bench'},'ports':[f'127.0.0.1:{pgport}:5432'],'volumes':['benchdata:/var/lib/postgresql/data'],'healthcheck':{'test':['CMD-SHELL','pg_isready -U bench -d job_application_agent_bench'],'interval':'1s','timeout':'3s','retries':30}}}
        for role in ['api','worker1','worker2']:
            cmd=['.venv/bin/python','-m','uvicorn','scripts.benchmark_p11:build_benchmark_app','--factory','--host','0.0.0.0','--port','8001','--no-proxy-headers'] if role=='api' else ['.venv/bin/python','scripts/benchmark_p11.py','--worker']
            services[role]={'image':self.config['image'],'env_file':str(self.envfile),'command':cmd,'volumes':[str(ROOT/'scripts/benchmark_p11.py')+':/app/scripts/benchmark_p11.py:ro'],'depends_on':{'postgres':{'condition':'service_healthy'}},'stop_grace_period':'5s'}
        services['api']['ports']=[f'127.0.0.1:{apiport}:8001']
        for role,service in services.items():
            resource=self.config['resources'][role];service.update(cpus=resource['cpus'],mem_limit=str(resource['memory_mb'])+'m')
        self.compose=self.out/'compose.json';self.compose.write_text(json.dumps({'services':services,'volumes':{'benchdata':{}}}));self.compose.chmod(0o600)
        try:
            self.started=True;await asyncio.to_thread(self.command,'up','-d','--pull','never','--wait','postgres')
            if self.phase=='before':
                with tempfile.TemporaryDirectory() as d:
                    import shutil
                    for p in MIGRATIONS.glob('*.sql'):
                        if p.name<'0012':shutil.copy2(p,Path(d)/p.name)
                    await migrate(self.dsn,Path(d))
            else:await migrate(self.dsn)
            self.pool=await asyncpg.create_pool(self.dsn,min_size=1,max_size=2,command_timeout=120)
            async with self.pool.acquire() as c:await seed(c,self.small)
            await asyncio.to_thread(self.command,'up','-d','--pull','never','api','worker1','worker2')
            self.client=httpx.AsyncClient(base_url=f'http://127.0.0.1:{apiport}',headers={'Authorization':'Bearer '+self.token},trust_env=False,timeout=10,limits=httpx.Limits(max_connections=110,max_keepalive_connections=110))
            for _ in range(200):
                try:
                    if (await self.client.get('/health/live')).status_code==200:break
                except httpx.TransportError:pass
                await asyncio.sleep(.1)
            else:raise RuntimeError('benchmark API startup failed')
            self.ids=(await asyncio.to_thread(self.command,'ps','-q',capture=True)).split()
            inspected=subprocess.check_output(['docker','inspect','--format','{{json .HostConfig.NanoCpus}} {{json .HostConfig.Memory}}',*self.ids],text=True)
            (self.out/'limits.txt').write_text(inspected)
            self.monitor=asyncio.create_task(self.sample())
            return self
        except BaseException:
            await self.close();raise
    async def sample(self):
        while not self.stop.is_set():
            stamp=time.time()
            try:
                stats=await asyncio.to_thread(subprocess.check_output,['docker','stats','--no-stream','--format','{{json .}}',*self.ids],text=True)
                row=await self.pool.fetchrow("SELECT count(*) FILTER(WHERE status='queued') AS queued,count(*) FILTER(WHERE status='running') AS running,extract(epoch FROM clock_timestamp()-min(created_at) FILTER(WHERE status='queued')) AS oldest_queued_seconds FROM jobs")
                pg=await self.pool.fetchrow("SELECT count(*) AS connections,count(*) FILTER(WHERE wait_event_type='Lock') AS waiting_locks FROM pg_stat_activity WHERE datname=current_database()")
                self.metrics.append({'time':stamp,'containers':[json.loads(x) for x in stats.splitlines()],'queue':dict(row),'postgres':dict(pg)})
            except Exception as exc:self.metrics.append({'time':stamp,'sampling_error_type':type(exc).__name__})
            try:await asyncio.wait_for(self.stop.wait(),timeout=1)
            except TimeoutError:pass
    async def close(self):
        self.stop.set()
        if hasattr(self,'monitor'):await self.monitor
        if hasattr(self,'client'):await self.client.aclose()
        if self.pool:await self.pool.close()
        removed=False
        try:
            if self.started:
                await asyncio.to_thread(self.command,'logs','--no-color')
                await asyncio.to_thread(self.command,'down','--volumes','--remove-orphans')
            removed=True
        finally:
            for p in [self.envfile,self.compose]:p.unlink(missing_ok=True)
            (self.out/'resources.json').write_text(json.dumps(self.metrics,default=str,indent=2))
            (self.out/'cleanup.json').write_text(json.dumps({'containers_and_volume_removed':removed,'runtime_secret_files_removed':True}))
            self.log.close()
    async def __aexit__(self,*args):await self.close()
    async def request(self,method,path,body=None,due=None):
        sent=time.perf_counter();sent_unix=time.time();due=sent if due is None else due
        try:
            r=await self.client.request(method,path,json=body);status=r.status_code
            value=r.json() if r.headers.get('content-type','').startswith('application/json') else None
        except httpx.TransportError as exc:status='transport_error';value=None
        end=time.perf_counter()
        return {'offered_at':sent_unix-(sent-due),'responded_at':sent_unix+(end-sent),'method':method,'path':path.split('?')[0],'status':status,'ms':(end-due)*1000,'service_ms':(end-sent)*1000,'client_lag_ms':(sent-due)*1000},value
    async def notice(self,index,mode='submission'):
        company='示例负载公司'+str(index)
        body={'request_id':str(uuid4()),'text':TEXT+' 投递成功，简历进入简历库。','source_reference':mode,'timezone_hint':'Asia/Shanghai','hints':{'company_name':company}}
        r,value=await self.request('POST','/api/notifications',body);assert r['status']==201,(r,value)
        return value['id']

def read_path(i):
    import hashlib
    aid=str(UUID(hashlib.md5(b'bench-app-1').hexdigest()))
    return ['/api/applications?limit=20','/api/tasks?limit=20','/api/workbench/inbox?limit=20','/api/applications/'+aid,'/api/applications/'+aid+'/events?limit=20','/api/tasks/upcoming?days=7&limit=20','/api/jobs?limit=20'][i%7]

def distribution(values):
    if not values:return None
    values=sorted(values)
    return {name:round(values[max(0,math.ceil(len(values)*q)-1)],3) for name,q in [('p50',.5),('p95',.95),('p99',.99),('max',1)]}
def summarize(rows,duration):
    observed=[r for r in rows if r['status']!='client_dropped'];ok=[r for r in rows if isinstance(r['status'],int) and 200<=r['status']<300]
    return {'offered':len(rows),'observed':len(observed),'statuses':dict(Counter(str(r['status']) for r in rows)),'offered_rps':len(rows)/duration,'success_per_offered_window_rps':len(ok)/duration,'latency_ms':distribution([r['ms'] for r in observed]),'send_to_response_ms':distribution([r['service_ms'] for r in observed]),'client_lag_ms':distribution([r['client_lag_ms'] for r in observed]),'by_path':{path:{'statuses':dict(Counter(str(r['status']) for r in observed if r['path']==path)),'latency_ms':distribution([r['ms'] for r in observed if r['path']==path])} for path in sorted({r['path'] for r in observed})}}

async def arrivals(env,rate,duration,label,operation=None):
    pending=set();rows=[];started=time.perf_counter();count=round(rate*duration)
    async def one(i,due):
        row,_=await env.request('GET',read_path(i),due=due) if operation is None else await operation(i,due)
        rows.append(row)
    for i in range(count):
        due=started+i/rate
        await asyncio.sleep(max(0,due-time.perf_counter()))
        if len(pending)>=100:rows.append({'status':'client_dropped','path':'client-cap','ms':None});continue
        task=asyncio.create_task(one(i,due));pending.add(task);task.add_done_callback(pending.discard)
    if pending:await asyncio.gather(*pending)
    report=summarize(rows,duration);report.update(label=label,duration_seconds=duration,wall_seconds=time.perf_counter()-started)
    (env.out/(label+'-requests.json')).write_text(json.dumps(rows,indent=2))
    (env.out/(label+'-summary.json')).write_text(json.dumps(report,indent=2));return report

async def wait_jobs(env,ids,timeout=180):
    started=time.perf_counter();rows=[]
    while time.perf_counter()-started<timeout:
        rows=await env.pool.fetch('SELECT id,status,error_kind,attempt_count,created_at,finished_at FROM jobs WHERE id=ANY($1::uuid[])',ids)
        if all(r['status'] in {'succeeded','failed','cancelled'} for r in rows) and len(rows)==len(ids):break
        await asyncio.sleep(.2)
    else:raise AssertionError('accepted tasks did not drain before timeout')
    jobs=[]
    for row in rows:
        a=await env.pool.fetch('SELECT started_at,finished_at,status FROM job_attempts WHERE job_id=$1 ORDER BY number',row['id'])
        jobs.append({**dict(row),'queue_seconds':(a[0]['started_at']-row['created_at']).total_seconds() if a else None,'attempt_execution_seconds':[(x['finished_at']-x['started_at']).total_seconds() if x['finished_at'] else None for x in a],'end_to_end_seconds':(row['finished_at']-row['created_at']).total_seconds() if row['finished_at'] else None})
    return jobs

async def read_trial(out,config,phase,rate,repeat):
    async with Environment(out,config,phase) as env:
        if rate==10 and repeat==0:
            async with env.pool.acquire() as c:await explain(c,env.out)
        await arrivals(env,10,config['read']['warmup_seconds'],'warmup')
        result=await arrivals(env,rate,config['read']['duration_seconds'],'read')
        result['api_pool']=(await env.request('GET','/_bench/pool'))[1]
        result['criteria_met']=bool(result['latency_ms'] and result['latency_ms']['p95']<200 and not result['statuses'].get('client_dropped') and sum(v for k,v in result['statuses'].items() if k!='200')/result['offered']<=.01)
        (out/'report.json').write_text(json.dumps(result,indent=2));return result

async def isolation_trial(out,config,mode):
    async with Environment(out,config,'before',small=True,delay=1.5,limit=5,slots=3) as env:
        ids=[await env.notice(i) for i in range(5)]
        pending=[];jobs=[]
        if mode=='sync':pending=[asyncio.create_task(env.request('POST','/api/notifications/'+n+'/process')) for n in ids]
        else:
            for n in ids:
                row,job=await env.request('POST','/api/jobs',{'request_id':str(uuid4()),'notification_id':n});assert row['status']==202;jobs.append(UUID(job['id']))
        for _ in range(200):
            if await env.pool.fetchval("SELECT count(*) FROM notification_extractions WHERE notification_id=ANY($1::uuid[]) AND status='processing'",[UUID(n) for n in ids])==5:break
            await asyncio.sleep(.01)
        else:raise AssertionError('five overlapping model waits not established')
        sem=asyncio.Semaphore(10)
        async def one(i):
            async with sem:return (await env.request('GET',['/api/applications?limit=50','/api/tasks?limit=50'][i%2]))[0]
        started=time.perf_counter();rows=await asyncio.gather(*(one(i) for i in range(60)));result=summarize(rows,time.perf_counter()-started)
        if pending:assert all(r[0]['status']==200 for r in await asyncio.gather(*pending))
        if jobs:assert all(j['status']=='succeeded' for j in await wait_jobs(env,jobs))
        assert await env.pool.fetchval('SELECT count(*) FROM applications')==105
        (out/'requests.json').write_text(json.dumps(rows,indent=2));(out/'report.json').write_text(json.dumps(result,indent=2));return result

async def pipeline_trial(out,config,kind,delay):
    fault=kind=='fault'
    async with Environment(out,config,'after',delay=delay,timeout=1 if fault else 90) as env:
        count=240 if kind=='burst' else 12 if fault else 60
        notices=[await env.notice(i,'bench-fault:'+('429' if i%2==0 else 'timeout') if fault else 'submission') for i in range(count)]
        ids=[]
        async def submit(i,due):
            row,value=await env.request('POST','/api/jobs',{'request_id':str(uuid4()),'notification_id':notices[i]},due=due)
            if row['status']==202:ids.append(UUID(value['id']))
            return row,value
        if kind=='burst':
            await asyncio.to_thread(env.command,'stop','worker1','worker2')
            sem=asyncio.Semaphore(30)
            async def one(i):
                async with sem:return (await submit(i,time.perf_counter()))[0]
            started=time.perf_counter();rows=await asyncio.gather(*(one(i) for i in range(count)));sub=summarize(rows,time.perf_counter()-started)
            assert len(ids)==100 and sub['statuses'].get('429')==140,sub
            await asyncio.to_thread(env.command,'start','worker1','worker2')
            query=await arrivals(env,30,15,'drain-reads')
        elif fault:
            for i in range(count):await submit(i,time.perf_counter())
            # Verify worker1 actually owns an in-flight extraction before SIGKILL.
            cid=(await asyncio.to_thread(env.command,'ps','-q','worker1',capture=True)).strip()
            hostname=(await asyncio.to_thread(subprocess.check_output,['docker','inspect','--format','{{.Config.Hostname}}',cid],text=True)).strip()
            for _ in range(250):
                target=await env.pool.fetchval("SELECT j.id FROM jobs j JOIN notification_extractions e ON e.job_token=j.run_token WHERE j.status='running' AND e.status='processing' AND j.worker_id LIKE $1 LIMIT 1",hostname+':%')
                if target:break
                await asyncio.sleep(.02)
            else:raise AssertionError('no in-flight model owned by killed worker')
            (out/'fault-event.json').write_text(json.dumps({'killed_service':'worker1','in_flight_job':str(target),'time':time.time()}))
            await asyncio.to_thread(env.command,'kill','-s','SIGKILL','worker1')
            await asyncio.sleep(1)
            await asyncio.to_thread(env.command,'start','worker1')
            rows=[];sub=None;query=await arrivals(env,30,10,'fault-reads')
        else:
            query,sub=await asyncio.gather(arrivals(env,30,60,'mixed-reads'),arrivals(env,1,60,'submissions',submit))
            rows=[]
        result_jobs=await wait_jobs(env,ids)
        assert all(j['status']=='succeeded' for j in result_jobs),result_jobs
        assert await env.pool.fetchval('SELECT count(*) FROM automatic_records WHERE notification_id=ANY($1::uuid[])',[UUID(n) for n in notices])==len(ids)
        assert await env.pool.fetchval('SELECT count(*) FROM applications')==5000+len(ids)
        result={'kind':kind,'model_delay_seconds':delay,'query':query,'submission':sub,'accepted_jobs':len(ids),'jobs':result_jobs,'queue_seconds':distribution([j['queue_seconds'] for j in result_jobs]),'end_to_end_seconds':distribution([j['end_to_end_seconds'] for j in result_jobs]),'all_accepted_jobs_reconciled':True,'unexpected_business_duplicates':0}
        (out/'report.json').write_text(json.dumps(result,default=str,indent=2));return result

async def main(args):
    config=json.loads((ROOT/'evals/p11/load-contract.json').read_text())
    out=ROOT/args.output;out.mkdir(parents=True,exist_ok=False)
    meta={'config':config,'config_sha256':hashlib.sha256((ROOT/'evals/p11/load-contract.json').read_bytes()).hexdigest(),'host':{'platform':platform.platform(),'python':platform.python_version(),'cpus':os.cpu_count()},'image_id':subprocess.check_output(['docker','image','inspect','--format','{{.Id}}',config['image']],text=True).strip(),'driver_script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    (out/'conditions.json').write_text(json.dumps(meta,indent=2))
    reports=[]
    if args.suite=='reads':
        for rate in config['read']['rates_rps']:
            for repeat in range(config['read']['repeats']):
                result=await read_trial(out/f'{args.phase}-r{rate}-n{repeat+1}',config,args.phase,rate,repeat)
                reports.append(result);print(args.phase,rate,repeat+1,result['latency_ms'],result['statuses'],flush=True)
    elif args.suite=='isolation':
        for mode in ['sync','queue']:
            for repeat in range(3):
                result=await isolation_trial(out/f'{mode}-{repeat+1}',config,mode);reports.append(result);print(mode,repeat+1,result['latency_ms'],flush=True)
    else:
        for kind,delay in [('mixed',.25),('mixed',1.5),('burst',.25),('fault',.1)]:
            result=await pipeline_trial(out/(kind+'-'+str(delay)),config,kind,delay);reports.append(result);print(kind,delay,result['accepted_jobs'],flush=True)
    (out/'summary.json').write_text(json.dumps(reports,default=str,indent=2))

if __name__=='__main__':
    if '--worker' in sys.argv:
        from services.worker import run
        asyncio.run(run(extractor=model_fixture))
    else:
        parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--suite',choices=['reads','isolation','pipeline'],required=True);parser.add_argument('--phase',choices=['before','after'],default='before');parser.add_argument('--output',required=True)
        args=parser.parse_args();asyncio.run(main(args))
