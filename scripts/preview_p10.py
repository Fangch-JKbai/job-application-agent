"""Disposable browser demo. Model/mail fixtures, never real mailbox credentials."""
import asyncio,json,sys
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import asyncpg
from database.config import database_url
from database.migrate import migrate
from scripts.check_p10_api import server,mutation
async def main():
    base=database_url();name='job_application_agent_test_p10_browser_'+uuid4().hex[:10];dsn=urlunsplit(urlsplit(base)._replace(path='/'+name));owner=uuid4()
    admin=await asyncpg.connect(base);await admin.execute(f'CREATE DATABASE "{name}"')
    try:
        await migrate(dsn)
        with server(dsn,owner,'browser-preview',poll=True) as (a,_):
            for position in ['Agent开发工程师','后端开发工程师']:
                a.call('POST','/api/applications',mutation(company_name='示例星河科技',position_name=position,batch='2027秋招'),201)
            info={'url':a.base+'/#/applications?new=1','base':a.base,'database':name,'owner_id':str(owner),'material':'synthetic model/mail fixtures; real browser, HTTP and PostgreSQL'}
            (ROOT/'.work/p10/browser-preview.json').write_text(json.dumps(info,ensure_ascii=False,indent=2)+'\n');print(info['url'],flush=True)
            while True:await asyncio.sleep(1)
    finally:await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)');await admin.close()
if __name__=='__main__':
    try:asyncio.run(main())
    except KeyboardInterrupt:print('Preview stopped and disposable database removed')
