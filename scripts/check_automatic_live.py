"""Real model, three invented development notices; disposable database, no personal mail."""
import asyncio,json,sys
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import asyncpg
from database.config import database_url
from database.migrate import migrate
from scripts import check_p10_api as p10
async def main():
 out=ROOT/'.work/automatic/live';out.mkdir(parents=True,exist_ok=True);p10.REPORT=out
 base=database_url();name='job_application_agent_test_auto_live_'+uuid4().hex[:8];dsn=urlunsplit(urlsplit(base)._replace(path='/'+name));owner=uuid4();admin=await asyncpg.connect(base);await admin.execute(f'CREATE DATABASE "{name}"');cases=[]
 try:
  await migrate(dsn)
  with p10.server(dsn,owner,'real-model',live=True) as (a,_):
   def call(method,path,body=None,status=200):
    code,data=a.call(method,path,body,status=None)
    with (out/'http.jsonl').open('a') as f:f.write(json.dumps({'method':method,'path':path,'status':code,'data':data},ensure_ascii=False)+'\n')
    assert code==status,(path,code,data)
    return data
   for kind,text in [('receipt','示例澄河科技：感谢您的投递，您的简历已进入我们的简历库，我们会尽快查阅。'),('assessment','示例澄河科技实习Agent开发工程师：请于11月8日前完成在线测评。'),('rejection','示例澄河科技：很遗憾，您应聘的实习Agent开发工程师未能通过筛选。')]:
    n=call('POST','/api/notifications',p10.mutation(text=text,source='manual',calendar_year=2026,timezone_hint='Asia/Shanghai'),201)
    r=call('POST',f'/api/notifications/{n["id"]}/process');ex=call('GET',f'/api/notifications/{n["id"]}/extractions')
    assert r['status']=='saved',r
    if kind=='receipt':assert r['application']['position_name'] is None and r['application']['stage']=='submitted';aid=r['application_id']
    else:assert r['application_id']==aid
    if kind=='assessment':assert r['application']['position_name']=='Agent开发工程师';tasks=call('GET','/api/tasks?application_id='+aid)['items'];assert len(tasks)==1 and tasks[0]['due_on']=='2026-11-08'
    if kind=='rejection':assert r['application']['outcome']=='rejected' and call('GET','/api/tasks?application_id='+aid)['items'][0]['status']=='pending'
    assert call('POST',f'/api/notifications/{n["id"]}/process')==r
    cases.append({'kind':kind,'text':text,'saved_without_confirmation':True,'notification_id':n['id']})
  report={'checks_passed':len(cases),'cases':cases,'material':'synthetic seen development cases, real model, automatic writes without confirmation','real_mailbox_used':False,'temporary_database_removed':True}
  (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False))
 finally:
  await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)');await admin.close();(out/'progress.json').write_text(json.dumps({'completed_cases':cases,'temporary_database_removed':True},ensure_ascii=False,indent=2))
if __name__=='__main__':asyncio.run(main())
