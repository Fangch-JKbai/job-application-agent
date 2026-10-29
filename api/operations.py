from uuid import UUID
from fastapi import APIRouter,Request
from services.operations import Operations
router=APIRouter(prefix='/api/operations',tags=['运行观测'])
def service(request):return Operations(request.app.state.pool,request.app.state.user_id)
@router.get('/metrics')
async def metrics(request:Request):
    result=await service(request).snapshot()
    result['current_api']={'instance_id':request.app.state.instance_id,'scope':'this_process_since_start','http':request.app.state.http_metrics.snapshot(),'pool':request.app.state.pool.metrics()}
    return result
@router.post('/check')
async def check(request:Request):return await service(request).check()
@router.get('/alerts')
async def alerts(request:Request):return {'items':await service(request).alerts()}
@router.get('/jobs/{job_id}')
async def trace(job_id:UUID,request:Request):return await service(request).trace(job_id)

@router.get('/recovery')
async def recovery(request:Request):
    from core.recovery_guard import state
    return await state(request.app.state.pool)
