"""Live P4 extraction + P5 tool loop on four synthetic development scenarios."""
import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit,urlunsplit
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import asyncpg
from database.config import database_url
from database.migrate import migrate
from scripts.check_p5_api import mutation,server


async def checks(report_path):
    base=database_url(); name='job_application_agent_test_p5_live_'+uuid4().hex[:10]
    dsn=urlunsplit(urlsplit(base)._replace(path='/'+name))
    admin=await asyncpg.connect(base)
    await admin.execute(f'CREATE DATABASE "{name}"')
    report={'material':'seen synthetic development scenarios','real_model_called':True,'cases':[],'http_steps':[],'temporary_database_removed':False}
    def save():
        report_path.parent.mkdir(parents=True,exist_ok=True)
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    def passed(entry):
        report['cases'].append(entry); save()
        print(json.dumps({'case':entry['id'],'passed':entry['passed'],'elapsed_seconds':entry['elapsed_seconds']},ensure_ascii=False),flush=True)
    owner=uuid4()
    try:
        await migrate(dsn)
        conn=await asyncpg.connect(dsn)
        try:
            async def counts():
                return {table:await conn.fetchval(f'SELECT count(*) FROM {table}') for table in ['applications','tasks','application_events']}
            with server(dsn,owner,'live',fixture=False) as client:
                def recorded(method,path,body=None,status=200):
                    code,value=client.call(method,path,body,None)
                    report['http_steps'].append({'method':method,'path':path,'request':body,'status':code,'response':value})
                    save()
                    assert code==status,(method,path,code,value)
                    return value
                app=client.call('POST','/api/applications',mutation(company_name='示例星河科技',company_aliases=['星河'],position_name='Agent开发工程师',batch='2027秋招'),201)
                client.call('POST','/api/applications',mutation(company_name='示例星河科技',position_name='后端开发工程师',batch='2027秋招'),201)

                def begin(text,sent_at='2026-10-27T09:00:00+08:00'):
                    notice=recorded('POST','/api/notifications',mutation(text=text,source='manual',sent_at=sent_at,timezone_hint='Asia/Shanghai'),201)
                    extraction=recorded('POST',f'/api/notifications/{notice["id"]}/extract',mutation())
                    session=recorded('POST','/api/sessions',mutation(notification_id=notice['id'],extraction_id=extraction['id']),201)
                    return notice,extraction,session
                def turn(session,text):
                    questions=session['state']['questions']
                    reply={}
                    if len(questions)>1:
                        question=next((item for item in questions if item['field']=='sent_at'),questions[0])
                        reply={'reply_to_question_id':question['id']}
                    return recorded('POST',f'/api/sessions/{session["id"]}/messages',mutation(version=session['version'],text=text,**reply))

                started=time.monotonic()
                original=await counts()
                n,e,s=begin('示例星河科技2027秋招：请于2026年10月30日18:00前完成线上测评。')
                first=turn(s,'请处理这条通知。')
                assert first['session']['status']=='awaiting_user',first
                assert first['session']['state']['candidates']['total']==2,first
                # Read back state before a short answer, as a refreshed browser would.
                resumed=client.call('GET',f'/api/sessions/{s["id"]}')
                second=turn(resumed,'Agent开发岗')
                ok=second['session']['status']=='ready' and second['proposal']['payload']['application_id']==app['id']
                passed({'id':'ambiguous_position_followup','passed':ok,'notice':n,'extraction':e,'first_turn':first,'second_turn':second,'elapsed_seconds':round(time.monotonic()-started,2)})
                assert await counts()==original

                started=time.monotonic()
                n,e,s=begin('示例远帆科技2027秋招Agent开发工程师：请于2026年10月30日18:00前完成线上测评。')
                first=turn(s,'请处理这条通知。')
                assert first['session']['status']=='awaiting_user',first
                second=turn(first['session'],'请新建这条申请。')
                ok=second['session']['status']=='ready' and second['proposal']['payload']['new_application']['company_name']=='示例远帆科技'
                passed({'id':'no_match_explicit_new','passed':ok,'notice':n,'extraction':e,'first_turn':first,'second_turn':second,'elapsed_seconds':round(time.monotonic()-started,2)})
                assert await counts()==original

                started=time.monotonic()
                n,e,s=begin('示例星河科技2027秋招Agent开发工程师：请在本周五18:00前完成线上测评。',sent_at=None)
                first=turn(s,'请处理这条通知。')
                assert first['session']['status']=='awaiting_user',first
                second=turn(first['session'],'原通知发送于2026年10月19日09:00，时区Asia/Shanghai。')
                result=second['session']['state']['result']
                due=result['tasks'][0]['due_at']
                ok=second['session']['status']=='ready' and due is not None and datetime.fromisoformat(due)==datetime.fromisoformat('2026-10-23T18:00:00+08:00')
                passed({'id':'source_time_followup','passed':ok,'notice':n,'extraction':e,'first_turn':first,'second_turn':second,'elapsed_seconds':round(time.monotonic()-started,2)})
                assert await counts()==original

                started=time.monotonic()
                old=client.call('POST','/api/tasks',mutation(application_id=app['id'],type='interview',title='第一轮面试',round=1,starts_at='2026-10-29T14:00:00+08:00',source_timezone='Asia/Shanghai'),201)
                original=await counts()
                n,e,s=begin('示例星河科技2027秋招Agent开发工程师：原定2026年10月29日14:00的第一轮面试现改至2026年10月31日15:00，其他安排不变。')
                first=turn(s,'请处理这条改期通知。')
                action=first['proposal']['payload']['task_actions'][0] if first.get('proposal') else {}
                ok=first['session']['status']=='ready' and action.get('action')=='update' and action.get('task_id')==old['id']
                passed({'id':'reschedule_existing_task','passed':ok,'notice':n,'extraction':e,'old_task':old,'first_turn':first,'elapsed_seconds':round(time.monotonic()-started,2)})
                assert await counts()==original
                report['business_tables_untouched_by_agent']=True
        finally: await conn.close()
    finally:
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)'); await admin.close()
        report['temporary_database_removed']=True
        report['cases_checked']=len(report['cases'])
        report['cases_passed']=sum(item['passed'] for item in report['cases'])
        save()
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--report',type=Path,default=ROOT/'.work/p5/live-validation.json')
    options=parser.parse_args()
    result=asyncio.run(checks(options.report))
    print(json.dumps({key:value for key,value in result.items() if key not in {'cases','http_steps'}},ensure_ascii=False,indent=2))
    if result['cases_passed']!=4: sys.exit(1)
