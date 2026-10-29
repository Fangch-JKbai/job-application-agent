"""Read-only configuration check; only field names/status are printed."""
import argparse,json,sys
from pathlib import Path
from urllib.parse import urlsplit
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from dotenv import dotenv_values
from core.runtime_settings import validate

def check(path):
    if not path.is_file():return {'valid':False,'issues':['配置文件不存在']}
    values=dotenv_values(path)
    try:
        validate(values)
        try:url=urlsplit(values.get('DATABASE_URL') or '')
        except ValueError:raise ValueError('DATABASE_URL格式无效') from None
        if url.scheme not in {'postgres','postgresql'} or not url.path.startswith('/job_application_agent_'):raise ValueError('DATABASE_URL需指向本项目PostgreSQL数据库')
    except ValueError as error:return {'valid':False,'issues':[str(error)]}
    return {'valid':True,'mode':values.get('APP_MODE','local'),'cookie_secure':values.get('COOKIE_SECURE','false').lower()=='true','mail_key_present':bool(values.get('MAIL_CREDENTIAL_KEY')),'session_secret_source':'APP_SESSION_SECRET' if values.get('APP_SESSION_SECRET') else 'APP_ACCESS_TOKEN' if values.get('APP_ACCESS_TOKEN') else 'process_random','database_configured':True,'note':'未连接数据库、邮箱或模型；无效密钥不会输出原值'}
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--env-file',type=Path,default=ROOT/'.env');args=p.parse_args();result=check(args.env_file);print(json.dumps(result,ensure_ascii=False));raise SystemExit(0 if result['valid'] else 1)
