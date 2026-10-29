"""Small, explicit live-model smoke check using synthetic notices only."""
import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit,urlunsplit
from urllib.request import Request,urlopen
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import asyncpg
from database.config import database_url
from database.migrate import migrate
import scripts.check_p3_api as harness

PREFIX='示例星河科技2027秋招Agent开发工程师：'
CASES=[
 {'id':'absolute','text':PREFIX+'请于2026年10月30日18:00前完成线上测评。','sent_at':'2026-10-27T09:00:00+08:00'},
 {'id':'relative_known','text':PREFIX+'请在本周五18:00前完成线上测评。','sent_at':'2026-10-19T09:00:00+08:00'},
 {'id':'relative_unknown','text':PREFIX+'请在本周五18:00前完成线上测评。','sent_at':None},
 {'id':'date_only','text':PREFIX+'面试日期为2026年10月30日，具体时刻另行通知。','sent_at':'2026-10-27T09:00:00+08:00'},
 {'id':'forwarded_and_multiple_times','text':'【本次通知】'+PREFIX+'线上测评于2026年10月28日09:00开放，截止2026年10月30日18:00。\n【旧邮件引用，不是本次安排】示例远山科技2027秋招后端开发工程师：笔试安排在2026年10月29日10:00。','sent_at':'2026-10-27T09:00:00+08:00'},
 {'id':'conflicting_dates','text':PREFIX+'笔试为2026年10月30日09:00。注意事项又写笔试为2026年10月31日09:00，原文没有说明哪个时间有效。','sent_at':'2026-10-27T09:00:00+08:00'},
]


def call(client,method,path,body=None):
    request=Request(client.base+path,method=method,data=json.dumps(body,ensure_ascii=False).encode() if body else None,headers={'Content-Type':'application/json'})
    try:
        with urlopen(request,timeout=75) as response: code,raw=response.status,response.read()
    except HTTPError as error:
        code,raw=error.code,error.read()
    return code,json.loads(raw) if raw.startswith((b'{',b'[')) else raw.decode()


def expectation(case,result):
    if result.get('company_name')!='示例星河科技' or result.get('position_name')!='Agent开发工程师': return False
    tasks=result['tasks']
    def same(value,expected):
        return value is not None and datetime.fromisoformat(value)==datetime.fromisoformat(expected)
    if case=='absolute':
        return len(tasks)==1 and tasks[0]['type']=='assessment' and same(tasks[0]['due_at'],'2026-10-30T18:00:00+08:00')
    if case=='relative_known':
        return len(tasks)==1 and same(tasks[0]['due_at'],'2026-10-23T18:00:00+08:00') and tasks[0]['time_basis'].get('due_at')=='relative'
    if case=='relative_unknown':
        return all(t['due_at'] is None and t['due_on'] is None for t in tasks) and any(f['field']=='sent_at' for f in result['missing_fields'])
    if case=='date_only':
        return len(tasks)==1 and tasks[0]['starts_on']=='2026-10-30' and tasks[0]['starts_at'] is None
    if case=='forwarded_and_multiple_times':
        return len(tasks)==1 and tasks[0]['type']=='assessment' and same(tasks[0]['opens_at'],'2026-10-28T09:00:00+08:00') and same(tasks[0]['due_at'],'2026-10-30T18:00:00+08:00')
    if case=='conflicting_dates':
        return len(tasks)<=1 and all(t['starts_at'] is None and t['starts_on'] is None for t in tasks) and bool(result['missing_fields']) and bool(result['issues'])
    raise ValueError(case)


async def run(selected,checkpoint):
    work=ROOT/'.work/p4'
    harness.REPORT=work/'live-http-logs'
    harness.REPORT.mkdir(parents=True,exist_ok=True)
    base=database_url()
    name='job_application_agent_test_p4live_'+uuid4().hex[:8]
    dsn=urlunsplit(urlsplit(base)._replace(path='/'+name))
    admin=await asyncpg.connect(base)
    await admin.execute(f'CREATE DATABASE "{name}"')
    observations=[]
    try:
        await migrate(dsn)
        with harness.running_server(dsn,uuid4(),'real-model') as client:
            for case in CASES:
                if selected and case['id']!=selected: continue
                code,notice=call(client,'POST','/api/notifications',{'request_id':str(uuid4()),'text':case['text'],'source':'manual','sent_at':case['sent_at'],'timezone_hint':'Asia/Shanghai'})
                assert code==201
                started=time.perf_counter()
                code,attempt=call(client,'POST',f'/api/notifications/{notice["id"]}/extract',{'request_id':str(uuid4())})
                elapsed=round(time.perf_counter()-started,3)
                passed=code==200 and attempt['status']=='completed' and expectation(case['id'],attempt['result'])
                observations.append({'case_id':case['id'],'material':'synthetic','input':case,'http_status':code,'passed':passed,'latency_seconds':elapsed,'attempt':attempt})
                checkpoint.parent.mkdir(parents=True,exist_ok=True)
                checkpoint.write_text(json.dumps({'state':'in_progress','observations':observations},ensure_ascii=False,indent=2)+'\n')
                error=attempt.get('error_message') if isinstance(attempt,dict) else attempt
                print(json.dumps({'case':case['id'],'passed':passed,'http_status':code,'latency_seconds':elapsed,'error':error},ensure_ascii=False),flush=True)
        conn=await asyncpg.connect(dsn)
        try:
            counts=[await conn.fetchval(f'SELECT count(*) FROM {table}') for table in ['applications','tasks','application_events']]
            untouched=all(value==0 for value in counts)
            assert untouched
        finally:
            await conn.close()
        return {'checked_on':'2026-10-27','mode':'real provider via HTTP + PostgreSQL','cases_checked':len(observations),'cases_passed':sum(item['passed'] for item in observations),'observations':observations,'business_tables_untouched':True,'temporary_database_removed':True,'scope':'Small synthetic smoke check, not a held-out benchmark or production acceptance.'}
    finally:
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case',choices=[c['id'] for c in CASES])
    parser.add_argument('--report',type=Path,default=ROOT/'.work/p4/live-validation.json')
    args=parser.parse_args()
    result=asyncio.run(run(args.case,args.report))
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({key:value for key,value in result.items() if key!='observations'},ensure_ascii=False,indent=2))
    raise SystemExit(0 if result['cases_checked']==result['cases_passed'] else 1)
