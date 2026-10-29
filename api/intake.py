from fastapi import APIRouter,Request
from schemas.intake import IntakeConfirmation,IntakeSaved,IntakeReceipt
from services.intake import IntakeService
router=APIRouter(tags=['通知录入申请'])
@router.post('/api/intake/confirm',response_model=IntakeSaved,summary='确认可编辑表单并原子创建申请及待办')
async def confirm(body:IntakeConfirmation,request:Request):
    return await IntakeService(request.app.state.pool,request.app.state.user_id).confirm(body)

from uuid import UUID
@router.get('/api/intake/{notification_id}',response_model=IntakeReceipt)
async def receipt(notification_id:UUID,request:Request):
    from services.notifications import NotificationService
    await NotificationService(request.app.state.pool,request.app.state.user_id).get_notification(notification_id)
    value=await request.app.state.pool.fetchval("SELECT result->'application'->>'id' FROM intake_receipts WHERE owner_id=$1 AND notification_id=$2",request.app.state.user_id,notification_id)
    return {'application_id':value}
