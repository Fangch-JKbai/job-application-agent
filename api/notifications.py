"""P4 routes store/extract/review notifications; never execute application changes."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter,Depends,Request,Response

from api.routes import Limit,Offset
from schemas.notifications import ExtractionOut,ExtractionRequest,ExtractionReview,NotificationImport,NotificationOut
from schemas.recruitment import Page
from services.notifications import NotificationService

router = APIRouter(prefix='/api/notifications',tags=['通知'])


def get_notification_service(request:Request):
    return NotificationService(request.app.state.pool,request.app.state.user_id,request.app.state.notification_extractor)


Service = Annotated[NotificationService,Depends(get_notification_service)]


@router.post('',response_model=NotificationOut,status_code=201,summary='保存原始通知')
async def import_notification(body:NotificationImport,service:Service):
    return await service.import_notification(body)


@router.get('',response_model=Page[NotificationOut],summary='查看通知列表')
async def list_notifications(service:Service,limit:Limit=50,offset:Offset=0):
    return await service.list_notifications(limit,offset)


@router.get('/{notification_id}',response_model=NotificationOut,summary='查看原文与导入信息')
async def get_notification(notification_id:UUID,service:Service):
    return await service.get_notification(notification_id)


@router.post('/{notification_id}/extract',response_model=ExtractionOut,summary='提取字段，失败保留记录')
async def extract(notification_id:UUID,body:ExtractionRequest,response:Response,service:Service):
    result = await service.extract(notification_id,body)
    if result['status'] == 'failed':
        response.status_code = result['error_status']
    return result


@router.get('/{notification_id}/extractions',response_model=Page[ExtractionOut],summary='查看该通知的提取历史')
async def list_extractions(notification_id:UUID,service:Service,limit:Limit=50,offset:Offset=0):
    return await service.list_extractions(notification_id,limit,offset)


@router.get('/{notification_id}/extractions/{extraction_id}',response_model=ExtractionOut,summary='查看提取状态与结果')
async def get_extraction(notification_id:UUID,extraction_id:UUID,service:Service):
    return await service.get_extraction(notification_id,extraction_id)


@router.patch('/{notification_id}/extractions/{extraction_id}',response_model=ExtractionOut,summary='保存人工核对结果')
async def review(notification_id:UUID,extraction_id:UUID,body:ExtractionReview,service:Service):
    return await service.review(notification_id,extraction_id,body)
