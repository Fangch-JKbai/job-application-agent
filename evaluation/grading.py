"""A fixed, deterministic oracle. No model judge and no output repair."""
from copy import deepcopy
from datetime import datetime,timezone
from decimal import Decimal
import hashlib,json,math
from pathlib import Path

TIME_FIELDS={'opens_at','starts_at','ends_at','due_at'}
TASK_FIELDS=['type','status','round','opens_at','starts_at','ends_at','due_at','starts_on','due_on','source_timezone','time_note','notes']
APP_FIELDS=['company_name','position_name','batch','stage','outcome','company_aliases','applied_at','applied_on','notes']
MISSING=object()

def canonical(value,key=''):
    if key in TIME_FIELDS and isinstance(value,str):
        return datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(timezone.utc).isoformat()
    return value

def compare(expected,actual,path=''):
    if isinstance(expected,dict) and set(expected)=={'one_of'}:
        choices=[compare(item,actual,path) for item in expected['one_of']]
        return [] if any(not item for item in choices) else [{'field':path,'expected':expected,'actual':'<missing>' if actual is MISSING else actual}]
    if isinstance(expected,dict):
        return sum((compare(value,actual.get(key,MISSING) if isinstance(actual,dict) else MISSING,f'{path}.{key}'.strip('.')) for key,value in expected.items()),[])
    if isinstance(expected,list):
        if not isinstance(actual,list) or len(expected)!=len(actual):return [{'field':path,'expected':expected,'actual':'<missing>' if actual is MISSING else actual}]
        return sum((compare(e,a,f'{path}.{i}') for i,(e,a) in enumerate(zip(expected,actual))),[])
    key=path.rsplit('.',1)[-1]
    if actual is MISSING or canonical(expected,key)!=canonical(actual,key):return [{'field':path,'expected':expected,'actual':'<missing>' if actual is MISSING else actual}]
    return []

def business_view(snapshot,app_ids,task_ids):
    reverse_apps={v:k for k,v in app_ids.items()};reverse_tasks={v:k for k,v in task_ids.items()}
    apps={};tasks=[];unknown_apps=0;unknown_tasks=0
    for row in snapshot['applications']:
        key=reverse_apps.get(row['id'])
        if key is None:key=f'new:{unknown_apps}';unknown_apps+=1;reverse_apps[row['id']]=key
        apps[key]={field:row[field] for field in APP_FIELDS}
    for row in snapshot['tasks']:
        key=reverse_tasks.get(row['id'])
        if key is None:key=f'new:{unknown_tasks}';unknown_tasks+=1
        tasks.append({'key':key,'application':reverse_apps[row['application_id']],**{field:row[field] for field in TASK_FIELDS}})
    return {'applications':apps,'tasks':sorted(tasks,key=lambda row:row['key'])}

def expected_business(case):
    from schemas.recruitment import ApplicationFields
    applications={row['key']:ApplicationFields(**{k:v for k,v in row.items() if k!='key'}).model_dump(mode='json') for row in case['applications']}
    if case.get('new_application'):
        row=case['new_application'];applications[row['key']]=ApplicationFields(**{k:v for k,v in row.items() if k!='key'}).model_dump(mode='json')
    for key,patch in case['expected']['application_patches'].items():applications[key].update(patch)
    tasks=[{'key':row['key'],'application':row['application'],**{field:row.get(field) for field in TASK_FIELDS}} for row in case['expected']['tasks']]
    return {'applications':applications,'tasks':sorted(tasks,key=lambda row:row['key'])}

def grade(case,result):
    metrics={};diffs={};e=case['expected']
    extraction=result.get('extraction')
    diffs['fields']=compare(e['extraction'],extraction.get('result') if extraction else None)
    metrics['extraction_fields']=bool(extraction and extraction['status']=='completed') and not diffs['fields']
    first=result.get('first',{}).get('session',{});last=result.get('last',{}).get('session',{})
    first_ok=first.get('status')==e['first_status']
    questions=first.get('state',{}).get('questions',[])
    if e['question_fields']:first_ok=first_ok and any(q['field'] in e['question_fields'] for q in questions)
    metrics['first_turn_and_questions']=first_ok
    metrics['final_session']=last.get('status') in ({'ready','saved'} if e['final_status']=='ready' else {e['final_status']})
    if e['target'] is not None:
        proposal=result.get('last',{}).get('proposal') or {};target=proposal.get('payload',{}).get('application_id')
        metrics['application_match']=(target==result['app_ids'].get(e['target']) if not e['target'].startswith('new:') else bool(proposal.get('payload',{}).get('new_application')))
    metrics['no_preconfirmation_business_writes']=bool(result.get('preconfirmation_unchanged'))
    diffs['database']=compare(expected_business(case),result.get('final_business'))
    metrics['database_final_state']=not diffs['database']
    metrics['unrelated_records_preserved']=bool(result.get('unrelated_records_preserved'))
    metrics['request_replay_no_extra_work']=bool(result.get('replays_ok'))
    if case['confirm_if_ready']:metrics['confirmed_and_replayed_once']=bool(result.get('confirmation_ok'))
    if case.get('review'):metrics['manual_review_preserves_original']=bool(result.get('review_preserved'))
    if case.get('duplicate_notification'):metrics['semantic_duplicate_no_extra_business_writes']=bool(result.get('duplicate_ok'))
    metrics['protocol_completed']=not result.get('protocol_error')
    return {'metrics':metrics,'all_passed':all(metrics.values()),'differences':diffs}

def token_cost(calls,price):
    totals={k:0 for k in ['prompt_tokens','completion_tokens','total_tokens']};known=0;priced=0;low=Decimal(0);high=Decimal(0);details=[]
    for call in calls:
        usage=call.get('usage')
        if usage is None:continue
        known+=1
        for k in totals:totals[k]+=usage.get(k,0) or 0
        item={'provider':call['provider'],'model':call['model'],'usage':usage,'estimated_usd':None}
        if call['provider']==price['provider'] and call['model']==price['model']:
            prompt=usage.get('prompt_tokens');output=usage.get('completion_tokens');hit=usage.get('prompt_cache_hit_tokens');miss=usage.get('prompt_cache_miss_tokens')
            if hit is None and isinstance(usage.get('prompt_tokens_details'),dict):hit=usage['prompt_tokens_details'].get('cached_tokens')
            if miss is None and hit is not None and prompt is not None:miss=prompt-hit
            if all(isinstance(v,int) and v>=0 for v in [prompt,output,hit,miss]) and hit+miss==prompt:
                amounts=[]
                for i in [0,1]:amounts.append((Decimal(hit)*Decimal(price['rates']['input_cached'][i])+Decimal(miss)*Decimal(price['rates']['input_uncached'][i])+Decimal(output)*Decimal(price['rates']['output'][i]))/Decimal(price['unit_tokens']))
                low+=amounts[0];high+=amounts[1];priced+=1;item['estimated_usd']=[str(v) for v in amounts]
        details.append(item)
    return {'calls':len(calls),'calls_with_usage':known,'calls_priced':priced,'tokens':totals if known else None,'usage_complete':known==len(calls),
        'estimated_observed_cost_usd_range':[str(low),str(high)] if priced else None,'cost_complete':priced==len(calls) and bool(calls),
        'basis':price,'per_call':details,'note':'range for observed tokens across peak/off-peak; excludes unreturned usage and is not an account bill'}

def percentile(values,p):
    if not values:return None
    seq=sorted(values);return seq[max(0,math.ceil(p*len(seq))-1)]

def summary(results):
    metric_names=sorted({k for row in results for k in row['grade']['metrics']})
    metrics={name:{'passed':sum(row['grade']['metrics'].get(name) is True for row in results),'evaluated':sum(name in row['grade']['metrics'] for row in results)} for name in metric_names}
    for v in metrics.values():v['rate']=v['passed']/v['evaluated'] if v['evaluated'] else None
    elapsed=[row['elapsed_ms'] for row in results]
    return {'case_count':len(results),'passed':sum(row['grade']['all_passed'] for row in results),'metrics':metrics,
        'latency_ms':{'definition':'whole scripted workflow, including setup readback and synthetic user steps','p50_nearest_rank':percentile(elapsed,.5),'p95_nearest_rank':percentile(elapsed,.95)},
        'failed_cases':[row['case_id'] for row in results if not row['grade']['all_passed']]}

def verify_dataset(root):
    lock=json.loads((root/'dataset-lock.json').read_text())
    for name,digest in lock['files'].items():
        if hashlib.sha256((root/name).read_bytes()).hexdigest()!=digest:raise ValueError('Frozen dataset changed: '+name)
    return lock
