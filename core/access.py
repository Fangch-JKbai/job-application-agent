"""One personal workspace, optional private-mode login; no public registration."""
from collections import defaultdict,deque
from datetime import datetime,timezone
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import time
from urllib.parse import urlsplit
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from services.errors import ServiceError

COOKIE='job_agent_session'
class CallLimit(ServiceError):
    status_code=429
    @property
    def retry_after(self):return 3600-int(time.time())%3600

def configure_access(app):
    from core.runtime_settings import validate
    validate()
    mode=os.environ.get('APP_MODE','local')
    if mode not in {'local','private'}:raise ValueError('APP_MODE只支持local或private')
    token=os.environ.get('APP_ACCESS_TOKEN','')
    if mode=='private' and len(token)<32:raise ValueError('private模式需要至少32字符的APP_ACCESS_TOKEN')
    limit=int(os.environ.get('MODEL_REQUESTS_PER_HOUR','30'))
    interval=int(os.environ.get('MAIL_POLL_SECONDS','43200'))
    if not 1<=limit<=10000 or interval<60:raise ValueError('模型每小时上限需为1～10000，邮箱轮询间隔至少60秒')
    app.state.access_mode=mode;app.state.access_token=token
    secret=os.environ.get('APP_SESSION_SECRET') or token
    if secret and len(secret)<32:raise ValueError('APP_SESSION_SECRET至少32字符')
    app.state.cookie_key=hmac.new(secret.encode(),b'job-agent-session-v1',hashlib.sha256).digest() if secret else secrets.token_bytes(32)
    app.state.login_attempts=defaultdict(deque)

def cookie_value(app):
    expiry=str(int(time.time())+8*3600)
    return expiry+'.'+hmac.new(app.state.cookie_key,expiry.encode(),hashlib.sha256).hexdigest()
def valid_cookie(app,value):
    try:
        expiry,signature=value.split('.')
        return int(expiry)>time.time() and hmac.compare_digest(signature,hmac.new(app.state.cookie_key,expiry.encode(),hashlib.sha256).hexdigest())
    except (ValueError,AttributeError):return False

def authenticated(request):
    app=request.app
    bearer=request.headers.get('authorization','')
    return (bool(app.state.access_token) and hmac.compare_digest(bearer,'Bearer '+app.state.access_token)) or valid_cookie(app,request.cookies.get(COOKIE))

async def admit_model(pool,owner):
    limit=int(os.environ.get('MODEL_REQUESTS_PER_HOUR','30'))
    hour=datetime.now(timezone.utc).replace(minute=0,second=0,microsecond=0)
    value=await pool.fetchval("""INSERT INTO model_admissions(owner_id,hour,count) VALUES($1,$2,1)
        ON CONFLICT(owner_id,hour) DO UPDATE SET count=model_admissions.count+1 WHERE model_admissions.count<$3 RETURNING count""",owner,hour,limit)
    if value is None:raise CallLimit('本小时模型处理次数已达到配置上限，请下个整点后再试')

async def access_guard(request):
    app=request.app;path=request.url.path;method=request.method
    if not hasattr(app.state,'access_mode'):return None  # OpenAPI export has no running lifespan.
    if app.state.access_mode=='local':
        peer=request.client.host if request.client else ''
        try:local=ipaddress.ip_address(peer).is_loopback
        except ValueError:local=peer=='testclient'
        # Also reject DNS rebinding; forwarded headers are disabled in supplied launch commands.
        if not local or request.url.hostname not in {'localhost','127.0.0.1','::1','testserver'}:
            return JSONResponse(status_code=403,content={'detail':'本地模式只允许本机访问；远程使用请配置private登录模式'})
    if method not in {'GET','HEAD','OPTIONS'}:
        origin=request.headers.get('origin')
        if origin and urlsplit(origin).netloc!=request.url.netloc:
            return JSONResponse(status_code=403,content={'detail':'不接受跨站修改请求'})
    public=path in {'/','/health/live','/health/ready','/api/auth/status','/api/auth/login'} or path.startswith('/workbench-assets/') or path.startswith('/review/assets/')
    if app.state.access_mode=='private' and not public and not authenticated(request):
        return JSONResponse(status_code=401,content={'detail':'请先登录个人工作台'})
    if getattr(app.state,'recovery_paused',False) and method not in {'GET','HEAD','OPTIONS'} and path not in {'/api/auth/login','/api/auth/logout'}:
        return JSONResponse(status_code=409,content={'detail':'恢复副本处于只读核验状态；请完成核验并显式启用处理后重启服务'})
    if method not in {'GET','HEAD','OPTIONS'}:
        body=bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body)>256000:return JSONResponse(status_code=413,content={'detail':'提交内容过长，请缩短到256KB以内'})
        request._body=bytes(body)
    model_route=bool(re.fullmatch(r'/api/notifications/[^/]+/extract|/api/sessions/[^/]+/messages|/api/processing/[^/]+/[^/]+/retry',path))
    sync_model_route=model_route or bool(re.fullmatch(r'/api/notifications/[^/]+/process',path))
    if method=='POST' and sync_model_route and os.environ.get('ENABLE_SYNC_DIAGNOSTICS','false').lower()!='true':
        return JSONResponse(status_code=409,content={'detail':'同步模型诊断入口默认关闭，请通过后台任务处理通知'})
    if method=='POST' and model_route:
        try:await admit_model(app.state.pool,app.state.user_id)
        except CallLimit as error:return JSONResponse(status_code=429,content={'detail':str(error)})
    return None
