"""Inspect processing attempts and explicitly recover/retry a stopped attempt."""
from typing import Annotated,Literal
from uuid import UUID
from fastapi import APIRouter,Depends,Request,Response
from api.sessions import get_session_service
from api.routes import Limit,Offset
from api.notifications import get_notification_service
from schemas.processing import AttemptDetail,AttemptView,Kind,RetryRequest,RetryResponse
from schemas.recruitment import Page
from services.processing import ProcessingService
router=APIRouter(prefix='/api/processing',tags=['处理记录与恢复'])

def get_processing_service(request:Request):return ProcessingService(request.app.state.pool,request.app.state.user_id)
Service=Annotated[ProcessingService,Depends(get_processing_service)]

@router.get('',response_model=Page[AttemptView],summary='查看阶段、耗时、用量和中断状态')
async def history(service:Service,notification_id:UUID|None=None,status:Literal['processing','completed','failed']|None=None,limit:Limit=20,offset:Offset=0):
    return await service.list(notification_id,limit,offset,status)

@router.get('/{kind}/{attempt_id}',response_model=AttemptDetail,summary='查看单次处理与已持久化工具轨迹')
async def detail(kind:Kind,attempt_id:UUID,service:Service):return await service.detail(kind,attempt_id)

@router.post('/{kind}/{attempt_id}/recover',response_model=AttemptDetail,summary='将已无执行进程的处理中记录标为中断；不调用模型')
async def recover(kind:Kind,attempt_id:UUID,service:Service):return await service.recover(kind,attempt_id)

@router.post('/{kind}/{attempt_id}/retry',response_model=RetryResponse,summary='明确重试最新失败处理，保留来源关联')
async def retry(kind:Kind,attempt_id:UUID,body:RetryRequest,response:Response,service:Service,session_service=Depends(get_session_service),notice_service=Depends(get_notification_service)):
    result=await service.retry(kind,attempt_id,body,notice_service.extractor,session_service.model)
    failed=result['extraction'] if kind=='extraction' else result['message']['turn']
    if failed['status']=='failed':response.status_code=failed['error_status']
    return result
