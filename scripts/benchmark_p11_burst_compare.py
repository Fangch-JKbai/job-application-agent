"""Supplementary preregistered burst comparison after the 200ms goal was missed."""
import argparse,asyncio,hashlib,json,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts import benchmark_p11 as bench
from scripts import benchmark_p11_pipeline as observer
async def main(output):
    OUT=ROOT/output
    OUT.mkdir(parents=True,exist_ok=False)
    config=json.loads((ROOT/'evals/p11/load-contract.json').read_text())
    variants={'before_merge':'job-agent-p11-local:20261028','after_merge':'job-agent-p115-local:20261029'}
    conditions={'purpose':'same burst; preserve original 200ms goal and failed initial observation','parent_contract_sha256':hashlib.sha256((ROOT/'evals/p11/load-contract.json').read_bytes()).hexdigest(),'variants':{k:{'image':image,'id':subprocess.check_output(['docker','image','inspect','--format','{{.Id}}',image],text=True).strip()} for k,image in variants.items()},'repeats_per_variant':3,'phase':'both include 0012 indexes','submissions':240,'concurrency':30,'owner_queue_limit':100,'resources':config['resources'],'expected_accepted':100,'expected_429':140,'submit_p95_goal_ms':200,'driver_sha256':hashlib.sha256((ROOT/'scripts/benchmark_p11.py').read_bytes()).hexdigest(),'variant_order':'three before, then three after; not randomized','code_change':'reuse one latest-extraction snapshot; capacity check before quota precheck'}
    (OUT/'conditions.json').write_text(json.dumps(conditions,indent=2))
    bench.wait_jobs=observer.detailed_wait;bench.Environment.request=observer.observed_request
    results=[]
    for variant,image in variants.items():
        for repeat in range(1,4):
            current={**config,'image':image}
            result=await bench.pipeline_trial(OUT/f'{variant}-{repeat}',current,'burst',.25)
            item={'variant':variant,'repeat':repeat,'submit_p95_ms':result['submission']['latency_ms']['p95'],'submit_p99_ms':result['submission']['latency_ms']['p99'],'statuses':result['submission']['statuses'],'query_p95_ms':result['query']['latency_ms']['p95'],'accepted_jobs':result['accepted_jobs'],'all_reconciled':result['all_accepted_jobs_reconciled'],'goal_met':result['submission']['latency_ms']['p95']<200}
            results.append(item);print(json.dumps(item),flush=True)
    (OUT/'summary.json').write_text(json.dumps(results,indent=2))
if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',required=True);args=parser.parse_args();asyncio.run(main(args.output))
