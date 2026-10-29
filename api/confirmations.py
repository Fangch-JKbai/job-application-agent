"""Human review and explicit confirmation; not exposed as Agent tools."""
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter,Depends,Request
from fastapi.responses import FileResponse
from schemas.confirmations import ProposalConfirmation
from services.confirmations import ConfirmationService

router=APIRouter(tags=['核对与确认'])
WEB=Path(__file__).resolve().parents[1]/'web/review'


def get_confirmation_service(request:Request):
    return ConfirmationService(request.app.state.pool,request.app.state.user_id)


Service=Annotated[ConfirmationService,Depends(get_confirmation_service)]


@router.get('/api/proposals/{proposal_id}/review',summary='核对目标、变更、时间、原通知与当前有效性')
async def review(proposal_id:UUID,service:Service):
    return await service.review(proposal_id)


@router.post('/api/proposals/{proposal_id}/confirm',summary='确认具体方案并事务保存；不调用模型')
async def confirm(proposal_id:UUID,body:ProposalConfirmation,service:Service):
    return await service.confirm(proposal_id,body)


@router.get('/api/proposals/{proposal_id}/execution',summary='读取确认保存时的实际数据库结果')
async def execution(proposal_id:UUID,service:Service):
    return await service.execution(proposal_id)


@router.get('/review/proposals/{proposal_id}',include_in_schema=False)
async def review_page(proposal_id:UUID,service:Service):
    # Enforce the same local-user ownership before returning the page.
    await service.review(proposal_id)
    return FileResponse(WEB/'index.html',headers={'Cache-Control':'no-store'})


@router.get('/review/assets/{filename}',include_in_schema=False)
async def review_asset(filename:str):
    from fastapi import HTTPException
    if filename not in {'review.css','review.js'}:raise HTTPException(404)
    return FileResponse(WEB/filename)
