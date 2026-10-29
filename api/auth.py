import hmac
import os
import time
from fastapi import APIRouter,Request,Response,HTTPException
from pydantic import SecretStr
from schemas.recruitment import InputModel
from core.access import COOKIE,authenticated,cookie_value
router=APIRouter(prefix='/api/auth',tags=['个人空间登录'])
class Login(InputModel):
    token: SecretStr
class AuthStatus(InputModel):
    mode: str
    authenticated: bool
@router.get('/status',response_model=AuthStatus)
async def status(request:Request):return {'mode':request.app.state.access_mode,'authenticated':request.app.state.access_mode=='local' or authenticated(request)}
@router.post('/login')
async def login(body:Login,request:Request,response:Response):
    app=request.app
    attempts=app.state.login_attempts[request.client.host];now=time.monotonic()
    while attempts and attempts[0]<now-60:attempts.popleft()
    if len(attempts)>=10:raise HTTPException(429,'登录尝试过多，请一分钟后再试')
    attempts.append(now)
    if not app.state.access_token or not hmac.compare_digest(body.token.get_secret_value(),app.state.access_token):raise HTTPException(401,'访问口令不正确')
    response.set_cookie(COOKIE,cookie_value(app),max_age=8*3600,httponly=True,samesite='strict',secure=os.environ.get('COOKIE_SECURE','false').lower()=='true')
    return {'authenticated':True}
@router.post('/logout')
async def logout(response:Response):response.delete_cookie(COOKIE);return {'authenticated':False}
