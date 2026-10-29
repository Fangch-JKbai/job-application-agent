"""P5 conversational matching and draft proposal HTTP routes."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter,Depends,Request,Response

from agent.session_agent import call_model
from api.routes import Limit,Offset
from schemas.sessions import SessionCreate,SessionMessage
from schemas.workbench import SessionView,TurnView,MessageView,ProposalView
from schemas.recruitment import Page
from services.sessions import SessionService

router = APIRouter(prefix='/api',tags=['会话与方案'])


def get_session_service(request:Request):
    return SessionService(request.app.state.pool,request.app.state.user_id,call_model)


Service = Annotated[SessionService,Depends(get_session_service)]


@router.post('/sessions',response_model=SessionView,status_code=201,summary='从已完成提取创建会话')
async def create(body:SessionCreate,service:Service):
    return await service.create(body)


@router.get('/sessions',response_model=Page[SessionView],summary='查看会话列表')
async def list_sessions(service:Service,limit:Limit=20,offset:Offset=0,notification_id:UUID|None=None):
    return await service.list(limit,offset,notification_id)


@router.get('/sessions/{session_id}',response_model=SessionView,summary='读取会话与待解决问题')
async def get(session_id:UUID,service:Service):
    return await service.get(session_id)


@router.post('/sessions/{session_id}/messages',response_model=MessageView,summary='发送消息，澄清或生成方案')
async def message(session_id:UUID,body:SessionMessage,response:Response,service:Service):
    result = await service.message(session_id,body)
    if result['turn']['status'] == 'failed':
        response.status_code = result['turn']['error_status']
    return result


@router.get('/sessions/{session_id}/messages',response_model=Page[TurnView],summary='查看消息和工具调用记录')
async def messages(session_id:UUID,service:Service,limit:Limit=20,offset:Offset=0):
    return await service.turns(session_id,limit,offset)


@router.get('/proposals/{proposal_id}',response_model=ProposalView,summary='查看方案与核对页入口')
async def proposal(proposal_id:UUID,service:Service):
    return await service.proposal(proposal_id)
