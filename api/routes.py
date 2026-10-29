"""Thin HTTP layer; business operations live in services/recruitment.py."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter,Depends,Query,Request

from schemas.recruitment import (
    ApplicationCreate,ApplicationOut,ApplicationPatch,EventOut,Outcome,Page,Stage,
    TaskCreate,TaskOut,TaskPatch,TaskStatus,TaskType,
)
from services.recruitment import RecruitmentService

router = APIRouter(prefix='/api')


def get_service(request:Request) -> RecruitmentService:
    return RecruitmentService(request.app.state.pool,request.app.state.user_id)


Service = Annotated[RecruitmentService,Depends(get_service)]
Limit = Annotated[int,Query(ge=1,le=100)]
Offset = Annotated[int,Query(ge=0)]


@router.post('/applications',response_model=ApplicationOut,status_code=201,tags=['申请'],summary='手动创建申请')
async def create_application(body:ApplicationCreate,service:Service):
    return await service.create_application(body)


@router.get('/applications',response_model=Page[ApplicationOut],tags=['申请'],summary='分页查询与筛选申请')
async def list_applications(service:Service,company:str|None=None,position:str|None=None,stage:Stage|None=None,outcome:Outcome|None=None,limit:Limit=50,offset:Offset=0):
    return await service.list_applications(company,position,stage,outcome,limit,offset)


@router.get('/applications/{application_id}',response_model=ApplicationOut,tags=['申请'],summary='读取申请详情')
async def get_application(application_id:UUID,service:Service):
    return await service.get_application(application_id)


@router.patch('/applications/{application_id}',response_model=ApplicationOut,tags=['申请'],summary='编辑或显式纠正申请')
async def update_application(application_id:UUID,body:ApplicationPatch,service:Service):
    return await service.update_application(application_id,body)


@router.get('/applications/{application_id}/events',response_model=Page[EventOut],tags=['申请'],summary='读取变更时间线')
async def list_events(application_id:UUID,service:Service,limit:Limit=50,offset:Offset=0):
    return await service.list_events(application_id,limit,offset)


@router.post('/tasks',response_model=TaskOut,status_code=201,tags=['待办'],summary='为申请创建待办')
async def create_task(body:TaskCreate,service:Service):
    return await service.create_task(body)


@router.get('/tasks',response_model=Page[TaskOut],tags=['待办'],summary='分页查询待办')
async def list_tasks(service:Service,application_id:UUID|None=None,status:TaskStatus|None=None,type:TaskType|None=None,limit:Limit=50,offset:Offset=0):
    return await service.list_tasks(application_id,status,type,limit,offset)


# Register this literal path before the UUID path below.
@router.get('/tasks/upcoming',response_model=Page[TaskOut],tags=['待办'],summary='查询未来若干日内的未完成事项')
async def upcoming_tasks(service:Service,days:Annotated[int,Query(ge=1,le=90)]=7,limit:Limit=50,offset:Offset=0):
    return await service.upcoming_tasks(days,limit,offset)


@router.get('/tasks/{task_id}',response_model=TaskOut,tags=['待办'],summary='读取待办详情')
async def get_task(task_id:UUID,service:Service):
    return await service.get_task(task_id)


@router.patch('/tasks/{task_id}',response_model=TaskOut,tags=['待办'],summary='修改、完成、取消或纠正待办')
async def update_task(task_id:UUID,body:TaskPatch,service:Service):
    return await service.update_task(task_id,body)
