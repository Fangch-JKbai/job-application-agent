"""Browser acceptance starts from a real SIGKILL interruption in a fixture DB."""
import asyncio,json,sys
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit
from uuid import UUID,uuid4
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import asyncpg
from database.config import database_url
from database.migrate import migrate
from scripts.check_p8_api import server,notice,mutation,wait_for,ignore_closed

async def main():
    base=database_url();name='job_application_agent_test_p8_browser_'+uuid4().hex[:10];dsn=urlunsplit(urlsplit(base)._replace(path='/'+name));owner=uuid4()
    admin=await asyncpg.connect(base);await admin.execute(f'CREATE DATABASE "{name}"')
    try:
        await migrate(dsn);conn=await asyncpg.connect(dsn)
        try:
            with server(dsn,owner,'browser-setup') as (a,_):
                target=a.call('POST','/api/applications',mutation(company_name='示例星河科技',position_name='Agent开发工程师',batch='2027秋招'),201)
                a.call('POST','/api/applications',mutation(company_name='示例星河科技',position_name='后端开发工程师',batch='2027秋招'),201)
                n=notice(a);e=a.call('POST',f'/api/notifications/{n["id"]}/extract',mutation());s=a.call('POST','/api/sessions',mutation(notification_id=n['id'],extraction_id=e['id']),201)
                first=a.call('POST',f'/api/sessions/{s["id"]}/messages',mutation(version=s['version'],text='请处理这条通知。'))
            with server(dsn,owner,'browser-killed','answer') as (a,p):
                request=asyncio.create_task(asyncio.to_thread(a.call,'POST',f'/api/sessions/{s["id"]}/messages',mutation(version=first['session']['version'],text='Agent开发岗')))
                async def checkpoint():
                    row=await conn.fetchrow("SELECT * FROM agent_turns WHERE session_id=$1 AND status='processing'",UUID(s['id']))
                    return row if row and row['phase']=='model' and any(t.get('name')=='record_answer' for t in json.loads(row['trace'])) else None
                turn=await wait_for(checkpoint);p.kill();p.wait();await ignore_closed(request)
            with server(dsn,owner,'browser') as (a,_):
                info={'url':a.base+'/#/notifications/'+n['id'],'base':a.base,'notification_id':n['id'],'session_id':s['id'],'interrupted_turn_id':str(turn['id']),'target_id':target['id'],'database_name':name,'material':'synthetic fixture database; backend actually SIGKILLed after record_answer checkpoint'}
                (ROOT/'.work/p8/browser-preview.json').write_text(json.dumps(info,ensure_ascii=False,indent=2)+'\n');print(info['url'],flush=True)
                while True:await asyncio.sleep(1)
        finally:await conn.close()
    finally:await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)');await admin.close()

if __name__=='__main__':
    try:asyncio.run(main())
    except KeyboardInterrupt:print('P8 preview stopped; temporary database removed')
