"""P9 reproducible model evaluation alongside the local job-search workbench."""
from contextlib import asynccontextmanager,suppress
import asyncio
import os
from time import monotonic
from uuid import uuid4
from core.observation import HttpMetrics,measure_waits,method_label
from services.runtime_observation import runtime
import logging

from fastapi import FastAPI,Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

import core.logging  # noqa: F401
from api.routes import router
from api.notifications import router as notification_router
from api.sessions import router as session_router
from api.confirmations import router as confirmation_router
from api.workbench import router as workbench_router
from api.processing import router as processing_router
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from agent.notification_extractor import extract_notification
from core.identity import current_user_id
from database.pool import create_pool
from services.errors import ServiceError
from core.access import configure_access,access_guard
from api.auth import router as auth_router
from api.mailboxes import router as mailbox_router
from api.intake import router as intake_router
from services.mail_transport import read_mail

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app:FastAPI):
    from database.config import load_settings
    load_settings()
    configure_access(app)
    app.state.user_id = current_user_id()
    pool = await create_pool()
    try:
        app.state.pool = pool
        from core.recovery_guard import state
        app.state.recovery_paused=(await state(pool))['paused']
        if app.state.recovery_paused:
            if not await pool.fetchval('SELECT EXISTS(SELECT 1 FROM app_users WHERE id=$1)',app.state.user_id):raise ValueError('LOCAL_USER_ID不属于恢复副本，请使用verify报告中的用户编号')
        else:
            await pool.execute('INSERT INTO app_users(id,display_name) VALUES($1,$2) ON CONFLICT(id) DO NOTHING',app.state.user_id,'本地用户')
        app.state.notification_extractor = extract_notification
        app.state.mail_transport = read_mail
        app.state.http_metrics=HttpMetrics()
        async with runtime(pool,'api',http=app.state.http_metrics) as instance:
            app.state.instance_id=instance
            logger.info('Management API started',extra={'fields':{'instance_id':str(instance)}})
            yield
    finally:
        await pool.close()
        logger.info('Manual management API stopped')


app = FastAPI(
    title='招聘通知与申请进度管理 Agent',
    description='P10：个人工作台、通知录入、QQ/网易邮箱自动读取与可复现部署。',
    version='0.1.0',lifespan=lifespan,
)
app.include_router(router)
app.include_router(notification_router)
app.include_router(session_router)
app.include_router(confirmation_router)
app.include_router(workbench_router)
app.include_router(processing_router)
app.include_router(auth_router)
app.include_router(mailbox_router)
app.include_router(intake_router)
from api.automatic import router as automatic_router
app.include_router(automatic_router)
app.mount('/workbench-assets',StaticFiles(directory=Path(__file__).resolve().parents[1]/'web/dist/workbench-assets',check_dir=False),name='workbench-assets')


@app.middleware('http')
async def request_observation(request:Request,call_next):
    cid=uuid4().hex
    with core.logging.log_context(correlation_id=cid,request_id=cid),measure_waits() as waits:
        started=monotonic();status=500
        try:
            response=await access_guard(request)
            if response is None:response=await call_next(request)
            status=response.status_code
        except asyncio.CancelledError:
            status=499
            raise
        except Exception as error:
            logger.error('http_unexpected_error',extra={'fields':core.logging.error_fields(error)})
            response=JSONResponse(status_code=500,content={'detail':'服务内部错误，请凭请求编号查看日志'})
        finally:
            route=request.scope.get('route');name=getattr(route,'path','unmatched');elapsed=monotonic()-started
            if hasattr(request.app.state,'http_metrics'):request.app.state.http_metrics.observe(request.method,name,status,elapsed)
            logger.info('http_request',extra={'fields':{'method':method_label(request.method),'route':name,'status':status,'elapsed_ms':round(elapsed*1000,3),'db_wait':waits}})
        response.headers['X-Request-ID']=cid
        response.headers['X-App-Elapsed-Ms']=str(round(elapsed*1000,3))
        return response


@app.exception_handler(ServiceError)
async def business_error(request:Request,error:ServiceError):
    return JSONResponse(status_code=error.status_code,content={'detail':str(error)},headers={'Retry-After':str(getattr(error,'retry_after',10))} if error.status_code==429 else None)


@app.get('/health/live')
async def live() -> dict[str,str]:
    return {'status':'alive','service':'job-application-agent'}


@app.get('/api/project')
async def project_status() -> dict:
    return {
        'name':'招聘通知与申请进度管理 Agent','stage':'personal_delivery',
        'completed_milestones':['P0','P1','P2','P3','P4','P5','P6','P7','P8','P9','P10'],
        'implemented':['application_management','task_management','event_history','versioned_edits','manual_request_replay','notification_import','notification_extraction','extraction_review','agent_clarification','application_matching','draft_proposals','human_review','confirmed_updates','execution_readback','frontend_workbench','typed_api_contract','processing_history','manual_recovery','attempt_retry','step_checkpoints','usage_observation','reproducible_model_evaluation','editable_notice_intake','date_only_intake','automatic_imap_import','private_workspace_login','model_admission_limit','deployment_bundle'],
        'planned':['public_multiuser_service'],
    }


@app.exception_handler(RequestValidationError)
async def invalid_request(request:Request,error:RequestValidationError):
    return JSONResponse(status_code=422,content={'detail':[{key:item[key] for key in ['loc','msg','type']} for item in error.errors()]})

from api.jobs import router as jobs_router
app.include_router(jobs_router)

@app.exception_handler(TimeoutError)
async def database_busy(request:Request,error:TimeoutError):
    return JSONResponse(status_code=503,content={'detail':'数据库资源暂时繁忙，请稍后重试'},headers={'Retry-After':'1'})

from api.operations import router as operations_router
app.include_router(operations_router)

@app.get('/health/ready',tags=['运行观测'])
async def ready(request:Request):
    try:
        async with asyncio.timeout(2):
            available=await request.app.state.pool.fetchval("""SELECT current_setting('transaction_read_only')='off' AND NOT pg_is_in_recovery()
                AND (SELECT count(*)=2 FROM schema_migrations WHERE name IN ('0013_observation.sql','0014_recovery_state.sql'))
                AND EXISTS(SELECT 1 FROM recovery_state WHERE singleton AND NOT paused)
                AND has_table_privilege(current_user,'jobs','INSERT') AND has_table_privilege(current_user,'job_requests','INSERT')""")
        if available:return {'status':'ready'}
    except Exception as error:
        logger.warning('readiness_failed',extra={'fields':core.logging.error_fields(error)})
    return JSONResponse(status_code=503,content={'status':'not_ready'},headers={'Retry-After':'1'})
