import asyncio,json,sys
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import asyncpg
from database.config import database_url
from database.migrate import migrate
from scripts.check_automatic import server
async def main():
 base=database_url();name='job_application_agent_test_auto_browser_'+uuid4().hex[:8];dsn=urlunsplit(urlsplit(base)._replace(path='/'+name));owner=uuid4();admin=await asyncpg.connect(base);await admin.execute(f'CREATE DATABASE "{name}"')
 try:
  await migrate(dsn)
  with server(dsn,owner,'browser') as a:
   data={'url':a.base+'/#/applications?new=1','database':name,'base':a.base,'owner':str(owner),'fixtures':True};(ROOT/'.work/automatic/browser.json').write_text(json.dumps(data));print(data['url'],flush=True)
   while True:await asyncio.sleep(1)
 finally:await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)');await admin.close()
if __name__=='__main__':
 try:asyncio.run(main())
 except KeyboardInterrupt:print('Temporary preview cleaned')
