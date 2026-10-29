"""Real-model development smoke on invented notices. Not an unseen accuracy set."""
import asyncio
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit,urlunsplit
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import asyncpg
from database.config import database_url
from database.migrate import migrate
from scripts.check_p10_api import server,mutation
REPORT=ROOT/'.work/p10/live'

async def main():
    REPORT.mkdir(parents=True,exist_ok=True)
    base=database_url();name='job_application_agent_test_p10_live_'+uuid4().hex[:10];dsn=urlunsplit(urlsplit(base)._replace(path='/'+name));owner=uuid4()
    admin=await asyncpg.connect(base);await admin.execute(f'CREATE DATABASE "{name}"');results=[]
    try:
        await migrate(dsn)
        with server(dsn,owner,'live-service',live=True) as (a,_):
            def call(method,path,body=None,status=200):
                code,data=a.call(method,path,body,status=None)
                with (REPORT/'http.jsonl').open('a') as out:out.write(json.dumps({'method':method,'path':path,'body':body,'status':code,'data':data},ensure_ascii=False)+'\n')
                assert code==status,(path,code,data)
                return data
            for company,position,batch,text,task_type,day in [
                ('示例澄川科技','Agent开发工程师','27秋招','示例澄川科技27秋招Agent开发工程师：请在11月3日前完成线上测评。','assessment','2026-11-03'),
                ('示例青屿科技','AI应用开发工程师','实习','示例青屿科技实习AI应用开发工程师：请于11月5日提交作品集，具体时刻不限。','material_submission','2026-11-05')]:
                n=call('POST','/api/notifications',mutation(text=text,source='manual',calendar_year=2026,timezone_hint='Asia/Shanghai'),201)
                e=call('POST',f'/api/notifications/{n["id"]}/extract',mutation());r=e['result']
                assert r['company_name']==company and r['position_name']==position and r['batch']==batch,r
                assert r['tasks'][0]['due_on']==day and r['tasks'][0]['due_at'] is None,r
                assert r['tasks'][0]['type']==task_type and not r['missing_fields'],r
                task={k:v for k,v in r['tasks'][0].items() if k not in {'evidence','time_basis'}}
                saved=call('POST','/api/intake/confirm',mutation(notification_id=n['id'],extraction_id=e['id'],extraction_version=e['version'],confirmed=True,application={'company_name':company,'position_name':position,'batch':batch},tasks=[task]))
                assert saved['tasks'][0]['due_on']==day
                results.append({'case':text,'notice_id':n['id'],'extraction_id':e['id'],'application_id':saved['application']['id'],'passed':True})
            # Known rejection termination problem becomes a seen development check; old P9 scores remain untouched.
            text='示例澄川科技27秋招Agent开发工程师：很遗憾，本次应聘未通过。'
            n=call('POST','/api/notifications',mutation(text=text,source='manual',calendar_year=2026,timezone_hint='Asia/Shanghai'),201)
            e=call('POST',f'/api/notifications/{n["id"]}/extract',mutation())
            session=call('POST','/api/sessions',mutation(notification_id=n['id'],extraction_id=e['id']),201)
            response=call('POST',f'/api/sessions/{session["id"]}/messages',mutation(version=session['version'],text='请处理这条通知。'))
            assert response['session']['status']=='ready',response
            pid=response['proposal']['id'];review=call('GET',f'/api/proposals/{pid}/review')
            saved=call('POST',f'/api/proposals/{pid}/confirm',mutation(**review['confirmation']))
            assert saved['application']['outcome']=='rejected'
            tasks=call('GET','/api/tasks?application_id='+saved['application']['id'])['items'];assert tasks[0]['status']=='pending'
            results.append({'case':text,'notice_id':n['id'],'passed':True,'remaining_task_preserved':True})
        conn=await asyncpg.connect(dsn)
        try:
            usages=[json.loads(r['usage']) for r in await conn.fetch('SELECT usage FROM notification_extractions WHERE usage IS NOT NULL')]
            summary={'executed_at':datetime.now().astimezone().isoformat(),'cases':results,'checks_passed':len(results),'material':'invented development cases; real model; explicit scripted confirmations','real_mailbox_used':False,'temporary_database_removed':True,'extraction_usage':usages}
            (REPORT/'report.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n');print(json.dumps(summary,ensure_ascii=False,indent=2))
        finally:await conn.close()
    finally:
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)');await admin.close()
        (REPORT/'progress.json').write_text(json.dumps({'completed_cases':results,'temporary_database_removed':True},ensure_ascii=False,indent=2)+'\n')

if __name__=='__main__':asyncio.run(main())
