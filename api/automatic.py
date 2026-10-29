from uuid import UUID
from fastapi import APIRouter,Request
from schemas.automatic import AutomaticOut
from services.automatic import AutomaticService
router=APIRouter(tags=['自动记录'])
def service(request):return AutomaticService(request.app.state.pool,request.app.state.user_id,request.app.state.notification_extractor)
@router.get('/api/notifications/{notification_id}/automatic',response_model=AutomaticOut)
async def get(notification_id:UUID,request:Request):return await service(request).get(notification_id)
@router.post('/api/notifications/{notification_id}/process',response_model=AutomaticOut)
async def process(notification_id:UUID,request:Request):return await service(request).process(notification_id)
@router.post('/api/notifications/{notification_id}/undo',response_model=AutomaticOut)
async def undo(notification_id:UUID,request:Request):return await service(request).undo(notification_id)
