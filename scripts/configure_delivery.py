"""Add missing personal deployment secrets to this project's ignored .env only."""
from pathlib import Path
import os
import argparse
from uuid import uuid4
import secrets
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from cryptography.fernet import Fernet
from dotenv import dotenv_values

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--container',action='store_true');args=parser.parse_args()
    path=ROOT/'.env'
    if not path.exists():
        if not args.container:raise SystemExit('请先运行scripts/setup_dev_db.py；纯容器安装可使用--container')
        password=secrets.token_urlsafe(24)
        template=(ROOT/'.env.example').read_text().replace('CHANGE_ME',password).replace('00000000-0000-0000-0000-000000000001',str(uuid4())).replace('APP_MODE=local','APP_MODE=private')
        path.write_text(template);path.chmod(0o600)
    values=dotenv_values(path);add=[]
    if not values.get('MAIL_CREDENTIAL_KEY'):add.append('MAIL_CREDENTIAL_KEY='+Fernet.generate_key().decode())
    if not values.get('APP_ACCESS_TOKEN'):add.append('APP_ACCESS_TOKEN='+secrets.token_urlsafe(36))
    if not values.get('APP_SESSION_SECRET'):add.append('APP_SESSION_SECRET='+secrets.token_urlsafe(36))
    if 'APP_MODE' not in values:add.append('APP_MODE=local')
    if add:
        with path.open('a') as out:out.write('\n# P10 personal delivery settings\n'+'\n'.join(add)+'\n')
    path.chmod(0o600)
    print('Personal delivery settings ready in ignored .env; values were not printed.')
if __name__=='__main__':main()
