"""Reduce P11.5 raw artifacts without selecting favorable runs."""
import argparse,csv,json,statistics
from pathlib import Path
from xml.etree import ElementTree

def load(p):return json.loads(p.read_text())
def dist(values):return {'median':statistics.median(values),'min':min(values),'max':max(values)}
def resource(rows):
    cpus={};memory={};connections=[];queue=[];running=[];errors=[]
    import re
    for row in rows:
        if 'sampling_error_type' in row:errors.append(row);continue
        connections.append(row['postgres']['connections']);queue.append(row['queue']['queued']);running.append(row['queue']['running'])
        for c in row['containers']:
            role=c['Name'].split('-')[-2]
            if c['CPUPerc'].endswith('%'):cpus.setdefault(role,[]).append(float(c['CPUPerc'][:-1]))
            text=c['MemUsage'].split('/')[0].strip();match=re.fullmatch(r'([\d.]+)(B|KiB|MiB|GiB)',text)
            if match:
                size=float(match[1])*{'B':1/1048576,'KiB':1/1024,'MiB':1,'GiB':1024}[match[2]];memory.setdefault(role,[]).append(size)
    return {'peak_cpu_percent_of_one_core':{k:max(v) for k,v in cpus.items()},'peak_memory_mib':{k:max(v) for k,v in memory.items()},'peak_database_connections':max(connections) if connections else None,'peak_sampled_queued':max(queue) if queue else None,'peak_sampled_running':max(running) if running else None,'sampling_errors':errors}

def chart(out,values):
    # Standalone SVG: fixed linear axis, every trial is shown, no library/network dependency.
    colors={'sync':'#52687c','queue':'#16816e'};width=880;height=380;left=70;top=45;bottom=290;scale=(bottom-top)/1700
    pieces=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">','<rect width="100%" height="100%" fill="white"/>','<g font-family="Arial,sans-serif" fill="#243746">','<text x="70" y="25" font-size="17">Query P95 during five 1.5-second fixture model waits</text>']
    for tick in [0,500,1000,1500]:
        y=bottom-tick*scale;pieces.append(f'<path d="M70 {y} H845" stroke="#e0e5ea"/><text x="20" y="{y+4}" font-size="12">{tick}</text>')
    for group,mode in enumerate(['sync','queue']):
        for i,value in enumerate(values[mode]):
            x=130+group*380+i*90;y=bottom-value*scale
            pieces.append(f'<rect x="{x}" y="{y:.2f}" width="50" height="{bottom-y:.2f}" fill="{colors[mode]}"/><text x="{x+25}" y="{y-7:.2f}" text-anchor="middle" font-size="12">{value:.1f}</text><text x="{x+25}" y="310" text-anchor="middle" font-size="12">Trial {i+1}</text>')
        pieces.append(f'<text x="{245+group*380}" y="335" text-anchor="middle" font-size="14">'+('Synchronous shared API pool' if mode=='sync' else 'Independent worker queue')+'</text>')
    pieces.append('<text x="70" y="365" font-size="11">Milliseconds; 100 applications; 60 reads / trial; 10 closed-loop clients; identical 4-CPU service budget.</text></g></svg>')
    svg=''.join(pieces);ElementTree.fromstring(svg);(out/'isolation.svg').write_text(svg)

def main(root):
    before=root/'before-v2';after=root/'after-v2';isolation=root/'isolation-v2';pipeline=root/'pipeline-v2'
    conditions=[load(p/'conditions.json') for p in [before,after,isolation,pipeline]]
    assert len({c['config_sha256'] for c in conditions})==1
    assert len({c['driver_script_sha256'] for c in conditions})==1
    assert len({c['image_id'] for c in conditions})==1
    report={'conditions_identical':True,'read':{},'sql':{},'isolation':{},'pipeline':{},'resources':{}}
    csvrows=[]
    for phase,p in [('before',before),('after',after)]:
        trials=load(p/'summary.json');report['read'][phase]={}
        for rate in [10,30,100]:
            subset=[r for r in trials if r['offered_rps']==rate]
            report['read'][phase][rate]={'offered':sum(r['offered'] for r in subset),'p95_ms':dist([r['latency_ms']['p95'] for r in subset]),'p99_ms':dist([r['latency_ms']['p99'] for r in subset]),'http_p95_ms':dist([r['send_to_response_ms']['p95'] for r in subset]),'all_criteria_met':all(r['criteria_met'] for r in subset),'statuses':{s:sum(r['statuses'].get(s,0) for r in subset) for s in {s for r in subset for s in r['statuses']}}}
        for i,r in enumerate(trials):csvrows.append({'phase':phase,'rate_rps':r['offered_rps'],'trial':i%3+1,'requests':r['offered'],'p50_ms':r['latency_ms']['p50'],'p95_ms':r['latency_ms']['p95'],'p99_ms':r['latency_ms']['p99'],'http_p95_ms':r['send_to_response_ms']['p95'],'client_lag_p95_ms':r['client_lag_ms']['p95'],'criteria_met':r['criteria_met']})
        plans=load(p/f'{phase}-r10-n1/explain.json')
        for name,rows in plans.items():report['sql'].setdefault(name,{})[phase]={'execution_ms':dist([r['Execution Time'] for r in rows]),'last_plan':rows[-1]['Plan']}
    with (root/'read-trials.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(csvrows[0]));writer.writeheader();writer.writerows(csvrows)
    values={}
    for mode in ['sync','queue']:
        rows=[load(isolation/f'{mode}-{i}/report.json') for i in [1,2,3]];values[mode]=[r['latency_ms']['p95'] for r in rows];report['isolation'][mode]={'p95_ms':dist(values[mode]),'trials':values[mode],'statuses':[r['statuses'] for r in rows]}
    chart(root,values)
    for p in sorted(pipeline.iterdir()):
        if not p.is_dir():continue
        r=load(p/'report.json');jobs=r['jobs'];errors=[e['error_status'] for j in jobs for e in j['extractions'] if e['status']=='failed']
        report['pipeline'][p.name]={'accepted_jobs':r['accepted_jobs'],'all_reconciled':r['all_accepted_jobs_reconciled'],'unexpected_business_duplicates':r['unexpected_business_duplicates'],'query_p95_ms':r['query']['latency_ms']['p95'],'query_statuses':r['query']['statuses'],'submit_p95_ms':r['submission']['latency_ms']['p95'] if r['submission'] else None,'submit_statuses':r['submission']['statuses'] if r['submission'] else None,'queue_seconds':r['queue_seconds'],'end_to_end_seconds':r['end_to_end_seconds'],'attempt_counts':dict(__import__('collections').Counter(j['attempt_count'] for j in jobs)),'failed_extraction_statuses':dict(__import__('collections').Counter(errors))}
    report['burst_comparison']={}
    burst=root/'burst-optimization'
    rows=load(burst/'summary.json')
    for variant in ['before_merge','after_merge']:
        selected=[r for r in rows if r['variant']==variant];server=[];waits=[]
        for row in selected:
            folder=burst/f"{variant}-{row['repeat']}";values=[]
            for line in (folder/'compose.log').read_text().splitlines():
                start=line.find('{')
                if start<0:continue
                try:entry=json.loads(line[start:])
                except json.JSONDecodeError:continue
                fields=entry.get('fields',{})
                if entry.get('msg')=='http_request' and fields.get('route')=='/api/jobs' and fields.get('method')=='POST':values.append(fields['elapsed_ms'])
            assert len(values)==240
            values.sort();p95=values[__import__('math').ceil(len(values)*.95)-1]
            server.append(p95);waits.append(load(folder/'pool-metrics.json')['max_wait_seconds']*1000)
        report['burst_comparison'][variant]={'client_p95_ms':dist([r['submit_p95_ms'] for r in selected]),'client_trials':[r['submit_p95_ms'] for r in selected],'server_p95_ms':dist(server),'server_trials':server,'pool_max_wait_ms_trials':waits,'every_client_trial_below_200ms':all(r['goal_met'] for r in selected),'every_server_trial_below_200ms':all(v<200 for v in server),'trials':selected}
    cleanups=list(root.rglob('cleanup.json'))
    assert all(all(load(p).values()) for p in cleanups)
    report['cleanups_verified']=len(cleanups)
    for p in root.rglob('resources.json'):report['resources'][str(p.parent.relative_to(root))]=resource(load(p))
    (root/'results.json').write_text(json.dumps(report,indent=2));print(json.dumps({k:v for k,v in report.items() if k not in ['sql','resources']},indent=2))
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('root',type=Path);args=p.parse_args();main(args.root)
