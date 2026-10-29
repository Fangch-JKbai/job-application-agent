"""P11.5 pipeline-only evidence collector; preserves the frozen load driver."""
import argparse,asyncio,hashlib,json,sys
from pathlib import Path
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts import benchmark_p11 as bench
original_wait=bench.wait_jobs
original_request=bench.Environment.request
async def observed_request(env,*args,**kwargs):
    row,value=await original_request(env,*args,**kwargs)
    if not hasattr(env,'pipeline_http_rows'):env.pipeline_http_rows=[]
    if args[:2]==('POST','/api/jobs') and isinstance(value,dict):row['job_id']=value.get('id')
    env.pipeline_http_rows.append(row)
    return row,value
async def detailed_wait(env,ids,timeout=180):
    jobs=await original_wait(env,ids,timeout)
    for job in jobs:
        job['attempts']=[dict(r) for r in await env.pool.fetch('SELECT id,number,status,error_kind,started_at,finished_at FROM job_attempts WHERE job_id=$1 ORDER BY number',job['id'])]
        job['extractions']=[dict(r) for r in await env.pool.fetch('SELECT id,status,error_kind,error_status,retryable,retry_after_seconds,created_at,finished_at FROM notification_extractions WHERE job_id=$1 ORDER BY created_at,id',job['id'])]
    _,metrics=await env.request('GET','/_bench/pool')
    (env.out/'pool-metrics.json').write_text(json.dumps(metrics,indent=2))
    (env.out/'http-requests.json').write_text(json.dumps(env.pipeline_http_rows,indent=2))
    return jobs
async def main(output):
    bench.wait_jobs=detailed_wait
    bench.Environment.request=observed_request
    await bench.main(SimpleNamespace(output=output,suite='pipeline',phase='after'))
    out=ROOT/output
    (out/'observer.json').write_text(json.dumps({'observer_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'load_driver_sha256':hashlib.sha256((ROOT/'scripts/benchmark_p11.py').read_bytes()).hexdigest(),'extra_evidence':'attempt and extraction rows after accepted jobs reach terminal states'},indent=2))
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',required=True);args=p.parse_args();asyncio.run(main(args.output))
