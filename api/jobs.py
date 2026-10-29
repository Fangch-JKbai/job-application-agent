import logging
from uuid import UUID
logger=logging.getLogger(__name__)
from fastapi import APIRouter,Request,Response
from api.routes import Limit,Offset
from schemas.jobs import JobCreate,JobOut,JobRetry,JobAttemptOut
from schemas.recruitment import Page
from services.jobs import JobService
router=APIRouter(prefix='/api/jobs',tags=['后台任务'])
def service(request):return JobService(request.app.state.pool,request.app.state.user_id)

@router.post('',response_model=JobOut,status_code=202)
async def submit(body:JobCreate,request:Request,response:Response):
    job=await service(request).submit(body)
    logger.info('job_submitted',extra={'fields':{'job_id':str(job['id']),'notification_id':str(body.notification_id),'command_id':str(body.request_id),'status':job['status']}})
    response.headers['Location']='/api/jobs/'+str(job['id'])
    return job

@router.get('',response_model=Page[JobOut])
async def listing(request:Request,notification_id:UUID|None=None,limit:Limit=50,offset:Offset=0):
    return await service(request).list(notification_id,limit,offset)

@router.get('/{job_id}',response_model=JobOut)
async def get(job_id:UUID,request:Request):return await service(request).get(job_id)

@router.post('/{job_id}/cancel',response_model=JobOut)
async def cancel(job_id:UUID,request:Request):return await service(request).cancel(job_id)

@router.post('/{job_id}/retry',response_model=JobOut,status_code=202)
async def retry(job_id:UUID,body:JobRetry,request:Request):return await service(request).retry(job_id,body.request_id)

@router.get('/{job_id}/attempts',response_model=list[JobAttemptOut])
async def attempts(job_id:UUID,request:Request):
    await service(request).get(job_id)
    return [dict(r) for r in await request.app.state.pool.fetch('SELECT id,number,status,started_at,finished_at,error_kind FROM job_attempts WHERE job_id=$1 ORDER BY number',job_id)]
