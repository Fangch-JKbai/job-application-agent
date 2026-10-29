"""One synthetic live-model P4→P5→human confirmation→database flow."""
import asyncio
from datetime import datetime
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit,urlunsplit
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import asyncpg
from database.config import database_url
from database.migrate import migrate
from scripts.check_p6_api import mutation,server


async def checks():
    report_path=ROOT/'.work/p6/live-validation.json'
    report={'material':'one synthetic development scenario','real_model_called':True,'steps':[],'passed':False,'temporary_database_removed':False}
    def save():report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    base=database_url();name='job_application_agent_test_p6_live_'+uuid4().hex[:10]
    dsn=urlunsplit(urlsplit(base)._replace(path='/'+name));owner=uuid4()
    admin=await asyncpg.connect(base);await admin.execute(f'CREATE DATABASE "{name}"')
    try:
        await migrate(dsn)
        with server(dsn,owner,'live',fixture=False) as client:
            def call(method,path,body=None,status=200):
                code,result=client.call(method,path,body,None)
                report['steps'].append({'method':method,'path':path,'request':body,'status':code,'response':result});save()
                assert code==status,(path,code,result)
                return result
            target=call('POST','/api/applications',mutation(company_name='示例星河科技',position_name='Agent开发工程师',batch='2027秋招'),201)
            call('POST','/api/applications',mutation(company_name='示例星河科技',position_name='后端开发工程师',batch='2027秋招'),201)
            notice=call('POST','/api/notifications',mutation(text='示例星河科技2027秋招：请于2026年10月30日18:00前完成线上测评。',source='manual',sent_at='2026-10-27T09:00:00+08:00',timezone_hint='Asia/Shanghai'),201)
            extraction=call('POST',f'/api/notifications/{notice["id"]}/extract',mutation())
            session=call('POST','/api/sessions',mutation(notification_id=notice['id'],extraction_id=extraction['id']),201)
            first=call('POST',f'/api/sessions/{session["id"]}/messages',mutation(version=1,text='请处理这条通知。'))
            assert first['session']['status']=='awaiting_user' and first['session']['state']['candidates']['total']==2
            questions=first['session']['state']['questions']
            reply={}
            if len(questions)>1:
                question=next((question for question in questions if question['field']=='position_name'),questions[0])
                reply={'reply_to_question_id':question['id']}
            second=call('POST',f'/api/sessions/{session["id"]}/messages',mutation(version=first['session']['version'],text='Agent开发岗',**reply))
            assert second['session']['status']=='ready',second
            pid=second['proposal']['id']
            review=call('GET',f'/api/proposals/{pid}/review')
            assert review['can_confirm'] and second['proposal']['payload']['application_id']==target['id']
            confirm_body=mutation(**review['confirmation'])
            saved=call('POST',f'/api/proposals/{pid}/confirm',confirm_body)
            assert saved['executed'] and saved['application']['id']==target['id'] and saved['application']['stage']=='assessment'
            assert len(saved['tasks'])==1 and datetime.fromisoformat(saved['tasks'][0]['due_at'])==datetime.fromisoformat('2026-10-30T18:00:00+08:00')
            assert call('POST',f'/api/proposals/{pid}/confirm',confirm_body)==saved
            actual=call('GET',f'/api/tasks/{saved["tasks"][0]["id"]}')
            assert actual['version']==1 and actual['application_id']==target['id']
            conn=await asyncpg.connect(dsn)
            try:
                counts={table:await conn.fetchval(f'SELECT count(*) FROM {table}') for table in ['applications','tasks','application_events','proposal_executions']}
                assert counts=={'applications':2,'tasks':1,'application_events':4,'proposal_executions':1}
                report['database_counts']=counts
            finally:await conn.close()
            report['passed']=True
    finally:
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)');await admin.close()
        report['temporary_database_removed']=True;save()
    return {key:value for key,value in report.items() if key!='steps'}


if __name__=='__main__':print(json.dumps(asyncio.run(checks()),ensure_ascii=False,indent=2))
