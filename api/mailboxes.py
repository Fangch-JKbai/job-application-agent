from uuid import UUID
from fastapi import APIRouter,Request,Response
from schemas.mailboxes import MailboxBind,MailboxSetting,MailboxImport,MailboxList,MailboxOut,MailMessageList,ImportedMessage
from services.mailboxes import MailboxService
router=APIRouter(prefix='/api/mailboxes',tags=['邮箱'])
def service(request):return MailboxService(request.app.state.pool,request.app.state.user_id,request.app.state.notification_extractor,request.app.state.mail_transport)
@router.get('',response_model=MailboxList)
async def accounts(request:Request):return {'items':await service(request).list(),'configured':bool(__import__('os').environ.get('MAIL_CREDENTIAL_KEY'))}
@router.post('',status_code=201,response_model=MailboxOut)
async def bind(body:MailboxBind,request:Request):
    result=await service(request).bind(body)
    return result
@router.patch('/{account_id}')
async def setting(account_id:UUID,body:MailboxSetting,request:Request):
    result=await service(request).setting(account_id,body.automatic)
    return result
@router.delete('/{account_id}')
async def disconnect(account_id:UUID,request:Request):return await service(request).disconnect(account_id)
@router.post('/{account_id}/sync',status_code=202)
async def sync(account_id:UUID,request:Request):
    from services.mail_scheduler import request_sync
    await service(request).account(account_id)
    return await request_sync(request.app.state.pool,request.app.state.user_id,account_id)
@router.get('/{account_id}/messages',response_model=MailMessageList)
async def messages(account_id:UUID,request:Request,include_other:bool=False):return {'items':await service(request).messages(account_id,include_other)}
@router.post('/import',response_model=ImportedMessage)
async def import_message(body:MailboxImport,request:Request):
    from schemas.jobs import JobCreate
    from services.jobs import JobService
    from uuid import uuid5
    notice=await service(request).import_message(body.message_id)
    await JobService(request.app.state.pool,request.app.state.user_id).submit(JobCreate(request_id=uuid5(body.message_id,'manual-job'),notification_id=notice['notification_id']))
    return notice
