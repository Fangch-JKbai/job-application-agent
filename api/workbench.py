"""Read projection for the inbox and the built workbench entry point."""
from pathlib import Path
from fastapi import APIRouter,Request,HTTPException
from fastapi.responses import FileResponse
from api.routes import Limit,Offset
from schemas.recruitment import Page
from schemas.workbench import InboxItem
from services.recruitment import snapshot

router=APIRouter()
DIST=Path(__file__).resolve().parents[1]/'web/dist'


@router.get('/',include_in_schema=False)
async def index():
    if not (DIST/'index.html').exists():
        raise HTTPException(503,'工作台尚未构建，请在web目录执行npm ci和npm run build')
    return FileResponse(DIST/'index.html',headers={'Cache-Control':'no-store'})


@router.get('/api/workbench/inbox',response_model=Page[InboxItem],tags=['工作台'],summary='分页读取通知及最新处理状态')
async def inbox(request:Request,limit:Limit=20,offset:Offset=0):
    owner=request.app.state.user_id
    pool=request.app.state.pool
    total=await pool.fetchval('SELECT count(*) FROM notifications WHERE owner_id=$1',owner)
    rows=await pool.fetch('''SELECT n.id,n.text,n.source,n.imported_at,
        COALESCE(s.state->'result'->>'company_name',e.reviewed_result->>'company_name',e.model_result->>'company_name',n.hints->>'company_name') company_name,
        COALESCE(s.state->'result'->>'position_name',e.reviewed_result->>'position_name',e.model_result->>'position_name',n.hints->>'position_name') position_name,
        s.id session_id,s.status session_status,e.id extraction_id,e.status extraction_status,ir.result->'application'->>'id' intake_application_id,ar.status automatic_status
        FROM notifications n
        LEFT JOIN LATERAL (SELECT * FROM notification_extractions WHERE owner_id=n.owner_id AND notification_id=n.id ORDER BY created_at DESC,id DESC LIMIT 1) e ON true
        LEFT JOIN LATERAL (SELECT * FROM agent_sessions WHERE owner_id=n.owner_id AND notification_id=n.id AND extraction_id=e.id AND extraction_version=e.version ORDER BY created_at DESC,id DESC LIMIT 1) s ON true
        LEFT JOIN intake_receipts ir ON ir.owner_id=n.owner_id AND ir.notification_id=n.id
        LEFT JOIN automatic_records ar ON ar.owner_id=n.owner_id AND ar.notification_id=n.id
        WHERE n.owner_id=$1 ORDER BY n.imported_at DESC,n.id DESC LIMIT $2 OFFSET $3''',owner,limit,offset)
    return {'items':[snapshot(row) for row in rows],'total':total,'limit':limit,'offset':offset}
