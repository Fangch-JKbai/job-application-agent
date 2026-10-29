"""Validate supplied settings without logging any secret values."""
import os
from cryptography.fernet import Fernet

def validate(values=None):
    values=os.environ if values is None else values
    issues=[]
    if values.get('APP_MODE','local') not in {'local','private'}:issues.append('APP_MODE只支持local/private')
    if values.get('APP_MODE')=='private' and len(values.get('APP_ACCESS_TOKEN',''))<32:issues.append('private模式APP_ACCESS_TOKEN至少32字符')
    secret=values.get('APP_SESSION_SECRET')
    if secret and len(secret)<32:issues.append('APP_SESSION_SECRET至少32字符')
    if values.get('COOKIE_SECURE','false').lower() not in {'true','false'}:issues.append('COOKIE_SECURE只支持true/false')
    key=values.get('MAIL_CREDENTIAL_KEY')
    if key:
        try:Fernet(key.encode())
        except (ValueError,TypeError):issues.append('MAIL_CREDENTIAL_KEY不是有效的Fernet密钥')
    for name in ['MODEL_REQUESTS_PER_HOUR','MAIL_POLL_SECONDS','API_DB_POOL_MAX','WORKER_DB_POOL_MAX','SCHEDULER_DB_POOL_MAX','DB_ACQUIRE_TIMEOUT','DB_QUERY_TIMEOUT','WORKER_CONCURRENCY','MODEL_CONCURRENCY_LIMIT','JOB_QUEUE_LIMIT','JOB_OWNER_QUEUE_LIMIT','JOB_LEASE_SECONDS','JOB_MAX_ATTEMPTS','JOB_EXECUTION_TIMEOUT','MAIL_SCHEDULER_TICK_SECONDS','MAIL_SYNC_LEASE_SECONDS','OBSERVATION_HEARTBEAT_SECONDS','OBSERVATION_STALE_SECONDS','OBSERVATION_QUEUE_WAIT_SECONDS']:
        if name in values:
            try:
                n=int(values[name])
                if n<1:raise ValueError()
                if name=='MODEL_REQUESTS_PER_HOUR' and n>10000:raise ValueError()
                if name=='MAIL_POLL_SECONDS' and n<60:raise ValueError()
            except (ValueError,TypeError):issues.append(name+'超出允许范围或不是整数')
    if issues:raise ValueError('；'.join(issues))
