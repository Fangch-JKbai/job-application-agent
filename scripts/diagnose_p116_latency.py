"""Supplementary request-paired diagnostics; frozen P11.5 load driver is unmodified."""
import asyncio,hashlib,json,platform,subprocess,sys,time
from collections import Counter
from pathlib import Path
from uuid import UUID,uuid4
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import httpx
from scripts.benchmark_p11 import Environment,distribution,wait_jobs,arrivals
OUT=ROOT/'.work/p11/p11.6/latency'
IMAGE='job-agent-p116-local:20261029'
CONTRACT={'version':'p116-diagnostic-v1','variants':['cold','warm'],'repeats_each':3,'submissions':240,'concurrency':30,'warmup_requests':30,'warmup_concurrency':30,'read_during_drain_rps':30,'read_during_drain_seconds':15,'phase':'after','model_delay_seconds':.25,'same_resource_and_data_contract':'evals/p11/load-contract.json','scope':'New P11.6 image; supplementary diagnosis, not a replacement rerun of P11.5 before/after. Cold replaces HTTP client immediately before timed burst. Warm adds 30 concurrent health requests with the replacement client. Includes all 202 and 429 latencies. Traces omit event info and all headers except generated request ID and elapsed time.'}

class TracedClient(httpx.AsyncClient):
    def __init__(self,*args,**kwargs):super().__init__(*args,**kwargs);self.rows=[]
    async def request(self,method,url,**kwargs):
        start=time.perf_counter();events=[]
        async def trace(name,info):events.append({'event':name,'ms':round((time.perf_counter()-start)*1000,3)})
        kwargs['extensions']={**kwargs.get('extensions',{}),'trace':trace}
        response=await super().request(method,url,**kwargs)
        end=time.perf_counter()
        self.rows.append({'method':method,'path':str(url).split('?')[0],'status':response.status_code,'client_ms':(end-start)*1000,'request_id':response.headers.get('x-request-id'),'app_ms':float(response.headers['x-app-elapsed-ms']) if 'x-app-elapsed-ms' in response.headers else None,'events':events})
        return response

def paired(out,rows):
    logs={}
    for line in (out/'compose.log').read_text().splitlines():
        pos=line.find('{')
        if pos<0:continue
        try:value=json.loads(line[pos:])
        except json.JSONDecodeError:continue
        if value.get('msg')=='http_request' and value.get('request_id'):logs[value['request_id']]=value
    for row in rows:
        log=logs.get(row['request_id']);assert log and log['fields']['status']==row['status'],row['request_id']
        row['server']=log['fields'];assert abs(row['app_ms']-row['server']['elapsed_ms'])<.001
        row['outside_app_ms']=row['client_ms']-row['app_ms']
        def span(name):
            starts=[e['ms'] for e in row['events'] if e['event']==name+'.started']
            ends=[e['ms'] for e in row['events'] if e['event']==name+'.complete']
            return ends[-1]-starts[0] if starts and ends else None
        row['spans_ms']={k:span('connection.connect_tcp' if k=='connect_tcp' else 'http11.'+k) for k in ['connect_tcp','send_request_headers','send_request_body','receive_response_headers','receive_response_body']}
    return {'paired_requests':len(rows),'statuses':dict(Counter(str(r['status']) for r in rows)),
      'client_ms':distribution([r['client_ms'] for r in rows]),'app_ms':distribution([r['app_ms'] for r in rows]),'outside_app_ms':distribution([r['outside_app_ms'] for r in rows]),
      'connections_opened_in_burst':sum(r['spans_ms']['connect_tcp'] is not None for r in rows),
      'db_wait_ms':distribution([r['server']['db_wait']['wait_ms'] for r in rows]),
      'spans_ms':{key:distribution([r['spans_ms'][key] for r in rows if r['spans_ms'][key] is not None]) for key in rows[0]['spans_ms']},
      'slowest_request_id':max(rows,key=lambda r:r['client_ms'])['request_id']}

async def trial(config,variant,repeat):
    out=OUT/f'{variant}-{repeat}';trace_rows=[]
    async with Environment(out,config,'after',delay=.25) as env:
        notices=[await env.notice(i) for i in range(240)]
        await asyncio.to_thread(env.command,'stop','worker1','worker2')
        base=str(env.client.base_url);await env.client.aclose()
        env.client=TracedClient(base_url=base,headers={'Authorization':'Bearer '+env.token},trust_env=False,timeout=10,limits=httpx.Limits(max_connections=110,max_keepalive_connections=110))
        if variant=='warm':await asyncio.gather(*(env.client.request('GET','/health/live') for _ in range(30)))
        env.client.rows.clear();sem=asyncio.Semaphore(30);ids=[]
        async def one(i):
            async with sem:
                row,value=await env.request('POST','/api/jobs',{'request_id':str(uuid4()),'notification_id':notices[i]})
                if row['status']==202:ids.append(UUID(value['id']))
                return row
        started=time.perf_counter();raw=await asyncio.gather(*(one(i) for i in range(240)));wall=time.perf_counter()-started
        trace_rows=list(env.client.rows);assert len(trace_rows)==240 and len(ids)==100
        assert Counter(str(r['status']) for r in raw)=={'202':100,'429':140}
        (out/'requests.json').write_text(json.dumps(raw,indent=2))
        await asyncio.to_thread(env.command,'start','worker1','worker2')
        query=await arrivals(env,30,15,'drain-reads');jobs=await wait_jobs(env,ids)
        assert all(j['status']=='succeeded' for j in jobs)
        assert await env.pool.fetchval('SELECT count(*) FROM automatic_records WHERE notification_id=ANY($1::uuid[])',[UUID(n) for n in notices])==100
        assert await env.pool.fetchval('SELECT count(*) FROM applications')==5100
        (out/'jobs.json').write_text(json.dumps(jobs,default=str,indent=2))
    result=paired(out,trace_rows);result.update(variant=variant,repeat=repeat,wall_seconds=wall,accepted_jobs=100,all_accepted_jobs_reconciled=True,unexpected_business_duplicates=0,query=query)
    (out/'traces.json').write_text(json.dumps(trace_rows,indent=2));(out/'report.json').write_text(json.dumps(result,indent=2));return result

async def main():
    global OUT
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--output',default='.work/p11/p11.6/latency');parser.add_argument('--variant',choices=['cold','warm']);parser.add_argument('--repeat',type=int,choices=[1,2,3]);args=parser.parse_args()
    OUT=ROOT/args.output
    OUT.mkdir(parents=True,exist_ok=False);config=json.loads((ROOT/'evals/p11/load-contract.json').read_text());config['image']=IMAGE
    meta={'contract':CONTRACT,'effective_environment':config,'driver_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'frozen_driver_sha256':hashlib.sha256((ROOT/'scripts/benchmark_p11.py').read_bytes()).hexdigest(),'image_id':subprocess.check_output(['docker','image','inspect','--format','{{.Id}}',IMAGE],text=True).strip(),'platform':platform.platform(),'python':platform.python_version(),'httpx':httpx.__version__}
    (OUT/'contract.json').write_text(json.dumps(meta,indent=2));results=[]
    for variant in ([args.variant] if args.variant else CONTRACT['variants']):
        for repeat in ([args.repeat] if args.repeat else range(1,4)):
            r=await trial(config,variant,repeat);results.append(r)
            print(json.dumps({k:r[k] for k in ['variant','repeat','client_ms','app_ms','outside_app_ms','connections_opened_in_burst']},ensure_ascii=False),flush=True)
            (OUT/'report.json').write_text(json.dumps(results,indent=2))
if __name__=='__main__':asyncio.run(main())
