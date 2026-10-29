"""Temporary synthetic review page for browser checks; Ctrl+C cleans up its DB."""
import asyncio
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
from scripts.check_p6_api import draft,server


async def main():
    base=database_url();name='job_application_agent_test_p6_preview_'+uuid4().hex[:10]
    dsn=urlunsplit(urlsplit(base)._replace(path='/'+name));owner=uuid4()
    admin=await asyncpg.connect(base);await admin.execute(f'CREATE DATABASE "{name}"')
    try:
        await migrate(dsn)
        with server(dsn,owner,'preview') as client:
            context=draft(client,'multi',company='示例星河科技')
            (ROOT/'.work/p6/preview.json').write_text(json.dumps({'base':client.base,'url':client.base+context['proposal']['review_url'],'context':context},ensure_ascii=False,indent=2)+'\n')
            print(client.base+context['proposal']['review_url'],flush=True)
            while True:await asyncio.sleep(1)
    finally:
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)');await admin.close()


if __name__=='__main__':
    try:asyncio.run(main())
    except KeyboardInterrupt:print('Preview stopped; temporary database removed')
