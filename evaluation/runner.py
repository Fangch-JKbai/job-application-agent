"""Drive the actual HTTP application with scripted synthetic users and frozen oracles."""
import argparse,asyncio,hashlib,json,os,shutil,socket,subprocess,sys,time
from contextlib import contextmanager
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from urllib.error import HTTPError,URLError
from urllib.parse import urlsplit,urlunsplit
from urllib.request import Request,urlopen
from uuid import UUID,uuid4,uuid5,NAMESPACE_URL
import asyncpg
from database.config import database_url
from database.migrate import migrate
from evaluation.grading import APP_FIELDS,TASK_FIELDS,business_view,compare,expected_business,grade,summary,token_cost,verify_dataset
ROOT=Path(__file__).resolve().parents[1];DATA=ROOT/'evals/p9';WORK=ROOT/'.work/p9'


def save(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str)+'\n')
def request_id():return str(uuid4())
def source_manifest():
    paths=[p for folder in ['agent','api','services','schemas','core','evaluation'] for p in (ROOT/folder).glob('*.py')]
    paths+=list((ROOT/'database/migrations').glob('*.sql'))+[ROOT/'pyproject.toml',ROOT/'uv.lock',ROOT/'scripts/evaluate_p9.py']
    return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}
def freeze():
    verify_dataset(DATA);WORK.mkdir(parents=True,exist_ok=True)
    path=WORK/'final-engine.json'
    if path.exists():raise ValueError('Final engine already frozen; preserve it and use a new dataset version for further tuning')
    save(path,{'frozen_at':datetime.now().astimezone().isoformat(),'dataset_lock':json.loads((DATA/'dataset-lock.json').read_text()),'source_hashes':source_manifest()})
    print('Final engine frozen before holdout execution')

class HTTP:
    def __init__(self,base,output):self.base,self.output=base,output;self.steps=[]
    def call(self,method,path,body=None):
        start=time.perf_counter();request=Request(self.base+path,method=method,data=json.dumps(body,ensure_ascii=False).encode() if body is not None else None,headers={'Content-Type':'application/json'})
        try:
            with urlopen(request,timeout=240) as response:status,raw,cid=response.status,response.read(),response.headers.get('X-Request-ID')
        except HTTPError as error:status,raw,cid=error.code,error.read(),error.headers.get('X-Request-ID')
        with (self.output/'raw-http.jsonl').open('a') as f:
            f.write(json.dumps({'method':method,'path':path,'status':status,'body':raw.decode(),'request_correlation_id':cid},ensure_ascii=False)+'\n')
        data=json.loads(raw)
        row={'method':method,'path':path,'request':body,'status':status,'response':data,'request_correlation_id':cid,'elapsed_ms':round((time.perf_counter()-start)*1000)}
        self.steps.append(row)
        with (self.output/'http.jsonl').open('a') as f:f.write(json.dumps(row,ensure_ascii=False,default=str)+'\n')
        return status,data
    def ok(self,method,path,body=None):
        code,data=self.call(method,path,body)
        if code not in (200,201):raise ValueError(f'HTTP {code} at {method} {path}')
        return data

@contextmanager
def server(dsn,owner,out):
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    with (out/'server.log').open('w') as log:
        process=subprocess.Popen([str(ROOT/'.venv/bin/python'),'-m','uvicorn','api.main:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env={**os.environ,'DATABASE_URL':dsn,'LOCAL_USER_ID':str(owner)},stdout=log,stderr=subprocess.STDOUT)
        try:
            for _ in range(100):
                if process.poll() is not None:raise RuntimeError('Evaluation server exited before readiness')
                try:
                    with urlopen(f'http://127.0.0.1:{port}/health/live',timeout=1):break
                except (OSError,URLError):time.sleep(.1)
            else:raise RuntimeError('Evaluation server did not start')
            yield HTTP(f'http://127.0.0.1:{port}',out)
        finally:
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait()

async def seed(conn,owner,case):
    from schemas.recruitment import ApplicationFields,TaskFields
    ids={row['key']:uuid5(NAMESPACE_URL,f'p9:{owner}:{row["key"]}') for row in case['applications']};tasks={}
    async with conn.transaction():
        await conn.execute('INSERT INTO app_users(id,display_name) VALUES($1,$2)',owner,'P9合成评测身份')
        for source in case['applications']:
            values=ApplicationFields(**{k:v for k,v in source.items() if k!='key'}).model_dump();fields=list(values)
            await conn.execute(f"INSERT INTO applications(id,owner_id,{','.join(fields)},data_origin,sample_case_id,sample_key) VALUES($1,$2,{','.join('$'+str(i+3) for i in range(len(fields)))},'synthetic',${len(fields)+3},${len(fields)+4})",ids[source['key']],owner,*values.values(),case['id'],source['key'])
        for source in case['tasks']:
            ident=uuid5(NAMESPACE_URL,f'p9:{owner}:task:{source["key"]}');tasks[source['key']]=ident
            values=TaskFields(**{k:v for k,v in source.items() if k not in {'key','application'}}).model_dump();fields=list(values)
            await conn.execute(f"INSERT INTO tasks(id,owner_id,application_id,{','.join(fields)},data_origin,sample_case_id,sample_key) VALUES($1,$2,$3,{','.join('$'+str(i+4) for i in range(len(fields)))},'synthetic',${len(fields)+4},${len(fields)+5})",ident,owner,ids[source['application']],*values.values(),case['id'],source['key'])
    return {k:str(v) for k,v in ids.items()},{k:str(v) for k,v in tasks.items()}

async def snapshot(conn,owner):
    return json.loads(json.dumps({name:[dict(row) for row in await conn.fetch(f'SELECT * FROM {name} WHERE owner_id=$1 ORDER BY created_at,id',owner)] for name in ['applications','tasks','application_events']},default=str))
async def counts(conn,owner):
    return {name:await conn.fetchval(f'SELECT count(*) FROM {name} WHERE owner_id=$1',owner) for name in ['notifications','notification_extractions','agent_turns','action_proposals','proposal_executions','confirmation_requests']}
async def replay(client,conn,owner,method,path,body,expected,expected_status=None):
    before=await counts(conn,owner);code,value=client.call(method,path,body);after=await counts(conn,owner)
    return (code==expected_status if expected_status is not None else code in (200,201)) and value==expected and before==after

def apply_review(extraction,review):
    from copy import deepcopy
    result=deepcopy(extraction['result'])
    for path,value in review['patch'].items():
        parts=path.split('.')
        if parts[0]=='tasks':
            task=result['tasks'][int(parts[1])];task[parts[2]]=value;task['evidence'][parts[2]]={'source':'review','quote':value}
        else:result[path]=value;result['evidence'][path]={'source':'review','quote':value}
    return {'version':extraction['version'],'review_text':review['text'],'result':result}

async def run_workflow(client,conn,owner,notice,extraction,case,result):
    body={'request_id':request_id(),'notification_id':notice['id'],'extraction_id':extraction['id']}
    session=client.ok('POST','/api/sessions',body)
    result['replays_ok'] &= await replay(client,conn,owner,'POST','/api/sessions',body,session)
    body={'request_id':request_id(),'version':session['version'],'text':'请处理这条通知。'}
    code,message=client.call('POST',f'/api/sessions/{session["id"]}/messages',body)
    result['first']=message;result['last']=message
    if code!=200:raise ValueError(f'Initial agent turn returned HTTP {code}')
    result['replays_ok'] &= await replay(client,conn,owner,'POST',f'/api/sessions/{session["id"]}/messages',body,message)
    for answer in case['answers']:
        if message['session']['status']!='awaiting_user':raise ValueError('Scripted answer was not requested; not injecting it into an already-ready turn')
        questions=message['session']['state']['questions'];q=next((q for q in questions if q['field'] in answer['answer_fields']),None)
        if q is None:raise ValueError('No pending question matches predeclared scripted answer fields')
        body={'request_id':request_id(),'version':message['session']['version'],'text':answer['text'],'reply_to_question_id':q['id']}
        code,message=client.call('POST',f'/api/sessions/{session["id"]}/messages',body);result['last']=message
        if code!=200:raise ValueError(f'Followup agent turn returned HTTP {code}')
        result['replays_ok'] &= await replay(client,conn,owner,'POST',f'/api/sessions/{session["id"]}/messages',body,message)
    return message

async def confirm(client,conn,owner,message,result):
    pid=message['proposal']['id'];review=client.ok('GET',f'/api/proposals/{pid}/review')
    result['review']=review
    if not review['can_confirm']:raise ValueError('Ready proposal cannot be confirmed: '+str(review['blockers']))
    body={'request_id':request_id(),**review['confirmation']};saved=client.ok('POST',f'/api/proposals/{pid}/confirm',body);result['execution']=saved
    once=await replay(client,conn,owner,'POST',f'/api/proposals/{pid}/confirm',body,saved)
    result['confirmation_ok']=saved['executed'] and once
    result['replays_ok'] &= once

async def collect_calls(conn,owner):
    calls=[]
    for row in await conn.fetch('SELECT * FROM notification_extractions WHERE owner_id=$1 ORDER BY created_at,id',owner):
        calls.append({'kind':'extraction','attempt_id':str(row['id']),'provider':row['provider'],'model':row['model'],'usage':json.loads(row['usage']) if row['usage'] else None,'elapsed_ms':row['elapsed_ms']})
    for row in await conn.fetch('SELECT * FROM agent_turns WHERE owner_id=$1 ORDER BY created_at,id',owner):
        for event in json.loads(row['trace']):
            if event['kind'] in {'model','model_error'}:calls.append({'kind':'agent','attempt_id':str(row['id']),**event})
    return calls

def untouched(case,initial,final,result):
    expected=expected_business(case);before=business_view(initial,result['app_ids'],result['task_ids']);allowed=set()
    for key,values in expected['applications'].items():
        if compare(values,before['applications'].get(key)):allowed.add(key)
    before_tasks={t['key']:t for t in before['tasks']}
    for task in expected['tasks']:
        if compare(task,before_tasks.get(task['key'])):allowed.add(task['application'])
    allowed_ids={result['app_ids'][k] for k in allowed if k in result['app_ids']}
    initial_events={row['id'] for row in initial['application_events']}
    if any(row['id'] not in initial_events and row['application_id'] not in allowed_ids and row['application_id'] in result['app_ids'].values() for row in final['application_events']):return False
    for table in ['applications','tasks']:
        current={row['id']:row for row in final[table]}
        for row in initial[table]:
            app_id=row['id'] if table=='applications' else row['application_id']
            if app_id not in allowed_ids and current.get(row['id'])!=row:return False
    return True

async def run_case(dsn,conn,case,out,price):
    out.mkdir();owner=uuid4();app_ids,task_ids=await seed(conn,owner,case);initial=await snapshot(conn,owner)
    start=time.perf_counter();result={'case_id':case['id'],'split':case['split'],'material_type':case['material_type'],'scripted_human_review':bool(case.get('review')),'app_ids':app_ids,'task_ids':task_ids,'replays_ok':True,'initial_snapshot':initial,'protocol_error':None}
    with server(dsn,owner,out) as client:
        try:
            body={'request_id':request_id(),**case['notification']};notice=client.ok('POST','/api/notifications',body);result['notice']=notice
            result['replays_ok'] &= await replay(client,conn,owner,'POST','/api/notifications',body,notice)
            body={'request_id':request_id()};code,extraction=client.call('POST',f'/api/notifications/{notice["id"]}/extract',body);result['extraction']=extraction
            result['replays_ok'] &= await replay(client,conn,owner,'POST',f'/api/notifications/{notice["id"]}/extract',body,extraction,expected_status=code)
            if code!=200:raise ValueError(f'Extraction returned HTTP {code}')
            if case.get('review'):
                revised=client.ok('PATCH',f'/api/notifications/{notice["id"]}/extractions/{extraction["id"]}',apply_review(extraction,case['review']))
                result['reviewed_extraction']=revised;result['review_preserved']=revised['raw_output']==extraction['raw_output'] and revised['model_result']==extraction['model_result'] and revised['version']==extraction['version']+1
                extraction=revised
            message=await run_workflow(client,conn,owner,notice,extraction,case,result)
            before_confirm=await snapshot(conn,owner);result['preconfirmation_snapshot']=before_confirm;result['preconfirmation_unchanged']=before_confirm==initial
            if case['confirm_if_ready'] and message['session']['status']=='ready':await confirm(client,conn,owner,message,result)
            primary=await snapshot(conn,owner);result['after_primary_snapshot']=primary
            if case.get('duplicate_notification') and result.get('confirmation_ok'):
                duplicate=client.ok('POST','/api/notifications',{'request_id':request_id(),**case['notification']})
                code,ex=client.call('POST',f'/api/notifications/{duplicate["id"]}/extract',{'request_id':request_id()})
                if code!=200:raise ValueError(f'Duplicate extraction returned HTTP {code}')
                duplicate_result={'replays_ok':True};m=await run_workflow(client,conn,owner,duplicate,ex,{**case,'answers':[]},duplicate_result)
                if m['session']['status']=='ready':await confirm(client,conn,owner,m,duplicate_result)
                result['duplicate_flow']=duplicate_result
                result['duplicate_ok']=m['session']['status'] in {'ready','awaiting_user'} and await snapshot(conn,owner)==primary
        except Exception as error:
            result['protocol_error']={'type':type(error).__name__,'message':str(error)}
        finally:
            final=await snapshot(conn,owner);result['final_snapshot']=final;
            if 'preconfirmation_unchanged' not in result and not result.get('execution'):result['preconfirmation_unchanged']=final==initial
            result['final_business']=business_view(final,app_ids,task_ids)
            result['unrelated_records_preserved']=untouched(case,initial,final,result)
            result['processing_rows']={table:[dict(r) for r in await conn.fetch(f'SELECT * FROM {table} WHERE owner_id=$1 ORDER BY created_at,id',owner)] for table in ['notification_extractions','agent_sessions','agent_turns','action_proposals','proposal_executions']}
            calls=await collect_calls(conn,owner);result['cost']=token_cost(calls,price)
            result['elapsed_ms']=round((time.perf_counter()-start)*1000);result['http_steps']=len(client.steps)
    result['grade']=grade(case,result);save(out/'result.json',result);return result

async def run(split,run_id,case_ids=None):
    from agent.notification_extractor import model_config
    provider,_,model,_=model_config();lock=verify_dataset(DATA);engine=source_manifest()
    out=WORK/run_id;out.mkdir(parents=True,exist_ok=False)
    if split=='holdout':
        frozen=json.loads((WORK/'final-engine.json').read_text())
        if engine!=frozen['source_hashes']:raise ValueError('Engine changed after final freeze')
        if (WORK/'holdout-access.json').exists():raise ValueError('Holdout has already been opened; preserve first results, do not rerun as unseen')
        save(WORK/'holdout-access.json',{'opened_at':datetime.now().astimezone().isoformat(),'run_id':run_id,'dataset':lock})
    cases=json.loads((DATA/(split+'.json')).read_text())['cases']
    if case_ids:
        if split=='holdout':raise ValueError('Final holdout is evaluated as one fixed full split')
        cases=[c for c in cases if c['id'] in case_ids]
        if {c['id'] for c in cases}!=set(case_ids):raise ValueError('Unknown development case ID')
    report={'run_id':run_id,'split':split,'started_at':datetime.now().astimezone().isoformat(),'dataset_lock':lock,'source_hashes':engine,'requested_provider':provider,'requested_model':model,
        'package_versions':{k:version(k) for k in ['openai','asyncpg','fastapi','pydantic']},'real_model_called':True,'material_type':'synthetic','scripted_user':'predeclared answers; script confirms ready proposals in disposable DB, not an autonomous write tool','results':[],'temporary_database_removed':False,'state':'running'}
    for name in engine:
        target=out/'source'/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,target)
    save(out/'report.json',report)
    price=json.loads((DATA/'pricing.json').read_text());base=database_url();name='job_application_agent_test_p9_'+uuid4().hex[:10];dsn=urlunsplit(urlsplit(base)._replace(path='/'+name));admin=await asyncpg.connect(base)
    await admin.execute(f'CREATE DATABASE "{name}"')
    try:
        await migrate(dsn);conn=await asyncpg.connect(dsn)
        try:
            for case in cases:
                if source_manifest()!=engine:raise RuntimeError('Source changed during run; preserve partial results')
                result=await run_case(dsn,conn,case,out/case['id'],price);report['results'].append(result);report['summary']=summary(report['results']);save(out/'report.json',report)
                print(json.dumps({'case':case['id'],'passed':result['grade']['all_passed'],'failed_metrics':[k for k,v in result['grade']['metrics'].items() if not v],'protocol_error':result['protocol_error'],'elapsed_ms':result['elapsed_ms']},ensure_ascii=False),flush=True)
            report['state']='completed'
        finally:await conn.close()
    finally:
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)');await admin.close();report['temporary_database_removed']=True;report['finished_at']=datetime.now().astimezone().isoformat();save(out/'report.json',report)
    print(json.dumps(report['summary'],ensure_ascii=False));return report

def main():
    parser=argparse.ArgumentParser(description=__doc__);commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('freeze-final')
    p=commands.add_parser('run');p.add_argument('--split',choices=['development','holdout'],required=True);p.add_argument('--run-id',required=True);p.add_argument('--cases',nargs='+');p.add_argument('--live',action='store_true',required=True)
    args=parser.parse_args()
    if args.command=='freeze-final':freeze();return
    if not args.run_id.replace('-','').replace('_','').isalnum():parser.error('run-id uses letters, digits, hyphens or underscores')
    report=asyncio.run(run(args.split,args.run_id,args.cases));raise SystemExit(0 if report['summary']['passed']==report['summary']['case_count'] else 2)
