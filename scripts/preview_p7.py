"""Temporary workbench for browser acceptance; Ctrl+C removes only its test DB."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit,urlunsplit
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import asyncpg
from database.config import database_url
from database.migrate import migrate
from scripts.check_p7_api import server,mutation


async def main(live=False):
    base=database_url();name='job_application_agent_test_p7_browser_'+uuid4().hex[:10];dsn=urlunsplit(urlsplit(base)._replace(path='/'+name));owner=uuid4()
    admin=await asyncpg.connect(base);await admin.execute(f'CREATE DATABASE "{name}"')
    try:
        await migrate(dsn)
        with server(dsn,owner,'browser-live' if live else 'browser',fixture=not live) as client:
            target=client.call('POST','/api/applications',mutation(company_name='示例星河科技',position_name='Agent开发工程师',batch='2027秋招'),201)
            other=client.call('POST','/api/applications',mutation(company_name='示例星河科技',position_name='后端开发工程师',batch='2027秋招'),201)
            info={'base':client.base,'url':client.base+'/','target':target,'other':other,'material':'synthetic test database','database_name':name,'real_model':live}
            (ROOT/('.work/p7/live-browser-preview.json' if live else '.work/p7/browser-preview.json')).write_text(json.dumps(info,ensure_ascii=False,indent=2)+'\n')
            print(info['url'],flush=True)
            while True:await asyncio.sleep(1)
    finally:
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)');await admin.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--live',action='store_true');options=parser.parse_args()
    try:asyncio.run(main(options.live))
    except KeyboardInterrupt:print('P7 browser preview stopped; temporary database removed')
