import asyncio
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
import hashlib
import json
import logging
import os
from uuid import UUID,uuid5
from cryptography.fernet import Fernet
from schemas.notifications import NotificationImport,ExtractionRequest
from services.errors import NotFound,Conflict,InvalidInput
from services.mail_transport import read_mail,candidate
from services.notifications import NotificationService
from services.recruitment import snapshot

logger=logging.getLogger(__name__)
def cipher():
    key=os.environ.get('MAIL_CREDENTIAL_KEY')
    if not key:raise InvalidInput('请先运行scripts/configure_delivery.py配置邮箱凭据加密密钥')
    return Fernet(key.encode())
PUBLIC='id,address,provider,enabled,created_at,last_sync_at,last_error'

class MailboxService:
    def __init__(self,pool,owner,extractor=None,transport=read_mail):
        self.pool,self.owner,self.extractor,self.transport=pool,owner,extractor,transport
    async def list(self):
        return [snapshot(row) for row in await self.pool.fetch(f'SELECT {PUBLIC} FROM mailbox_accounts WHERE owner_id=$1 ORDER BY created_at',self.owner)]
    async def account(self,account_id):
        row=await self.pool.fetchrow('SELECT * FROM mailbox_accounts WHERE owner_id=$1 AND id=$2',self.owner,account_id)
        if row is None:raise NotFound('邮箱账户不存在')
        return row
    async def bind(self,body):
        code=body.authorization_code.get_secret_value();encrypted=cipher().encrypt(code.encode()).decode()
        # Binding tests read-only login and INBOX access, without downloading messages.
        await asyncio.to_thread(self.transport,body.provider,body.address,code,None,0,0)
        row=await self.pool.fetchrow(f'''INSERT INTO mailbox_accounts(owner_id,address,provider,secret_encrypted,enabled)
            VALUES($1,$2,$3,$4,$5) ON CONFLICT(owner_id,address) DO UPDATE SET secret_encrypted=excluded.secret_encrypted,enabled=excluded.enabled,last_error=NULL,next_sync_at=now() RETURNING {PUBLIC}''',self.owner,body.address,body.provider,encrypted,body.automatic)
        return snapshot(row)
    async def setting(self,account_id,automatic):
        await self.account(account_id)
        await self.pool.execute('UPDATE mailbox_accounts SET enabled=$3,next_sync_at=CASE WHEN $3 THEN now() ELSE next_sync_at END WHERE owner_id=$1 AND id=$2',self.owner,account_id,automatic)
        return {'automatic':automatic}
    async def disconnect(self,account_id):
        # A sync owns this same advisory lock until model classification finishes.
        async with self.pool.acquire() as conn:
            if not await conn.fetchval('SELECT pg_try_advisory_lock($1)',self.lock_key(account_id)):raise Conflict('邮箱正在同步，请结束后解绑')
            try:
                row=await conn.fetchrow('DELETE FROM mailbox_accounts WHERE owner_id=$1 AND id=$2 RETURNING id',self.owner,account_id)
                if not row:raise NotFound('邮箱账户不存在')
            finally:await conn.execute('SELECT pg_advisory_unlock($1)',self.lock_key(account_id))
        return {'disconnected':True}
    def lock_key(self,account_id):return int.from_bytes(hashlib.sha256(f'mail:{self.owner}:{account_id}'.encode()).digest()[:8],signed=True)
    async def messages(self,account_id,include_other=False):
        await self.account(account_id)
        condition='' if include_other else ' AND is_candidate=true'
        rows=await self.pool.fetch('SELECT id,subject,sender,sent_at,is_candidate,notification_id,processing_error,auto_state,imported_at FROM mailbox_messages WHERE owner_id=$1 AND account_id=$2'+condition+' ORDER BY imported_at DESC,id DESC LIMIT 50',self.owner,account_id)
        return [snapshot(row) for row in rows]
    async def import_message(self,message_id):
        async with self.pool.acquire() as conn,conn.transaction():
            return await self._import_message(conn,message_id)
    async def _import_message(self,conn,message_id):
        row=await conn.fetchrow('SELECT * FROM mailbox_messages WHERE owner_id=$1 AND id=$2 FOR UPDATE',self.owner,message_id)
        if not row:raise NotFound('邮件记录不存在')
        if row['notification_id']:return {'notification_id':str(row['notification_id'])}
        text=f'主题：{row["subject"]}\n发件人：{row["sender"]}\n\n{row["text"]}'
        year=datetime.now(ZoneInfo('Asia/Shanghai')).year
        notice=await NotificationService(conn,self.owner).import_notification(NotificationImport(request_id=uuid5(message_id,'notice'),text=text,source='email',source_reference=f'mail-message:{message_id}',sent_at=row['sent_at'],timezone_hint='Asia/Shanghai',calendar_year=year))
        await conn.execute('UPDATE mailbox_messages SET notification_id=$3 WHERE owner_id=$1 AND id=$2',self.owner,message_id,notice['id'])
        return {'notification_id':str(notice['id'])}

    async def enqueue_cached(self,account_id):
        from schemas.jobs import JobCreate
        from services.jobs import JobService,QueueFull
        from core.access import CallLimit
        count=0
        while True:
            try:
                async with self.pool.acquire() as conn,conn.transaction():
                    row=await conn.fetchrow("""SELECT id FROM mailbox_messages WHERE owner_id=$1 AND account_id=$2 AND is_candidate
                      AND (auto_state='pending' OR (auto_state='completed' AND processing_job_id IS NULL AND notification_id IS NOT NULL
                       AND NOT EXISTS(SELECT 1 FROM automatic_records a WHERE a.owner_id=mailbox_messages.owner_id AND a.notification_id=mailbox_messages.notification_id)))
                      ORDER BY imported_at,id FOR UPDATE SKIP LOCKED LIMIT 1""",self.owner,account_id)
                    if not row:return count
                    notice=await self._import_message(conn,row['id'])
                    job=await JobService(self.pool,self.owner,connection=conn).submit(JobCreate(request_id=uuid5(row['id'],'automatic-job'),notification_id=notice['notification_id']))
                    await conn.execute("UPDATE mailbox_messages SET processing_job_id=$2,auto_state='processing' WHERE id=$1",row['id'],job['id'])
                    count+=1
            except (QueueFull,CallLimit):return count

    async def reclassify_cached(self,conn,account_id):
        # Revisit only untouched skipped mail; never reset failures, completed work or imports.
        rows=await conn.fetch("SELECT id,subject,sender,text FROM mailbox_messages WHERE owner_id=$1 AND account_id=$2 AND auto_state='skipped' AND notification_id IS NULL AND processing_error IS NULL",self.owner,account_id)
        ids=[row['id'] for row in rows if candidate(row)]
        if ids:
            await conn.execute("UPDATE mailbox_messages SET is_candidate=true,auto_state='pending' WHERE owner_id=$1 AND account_id=$2 AND id=ANY($3::uuid[]) AND auto_state='skipped' AND notification_id IS NULL AND processing_error IS NULL",self.owner,account_id,ids)
        return ids

    async def sync(self,account_id,heartbeat=None):
        await self.account(account_id)  # Ownership before lock probing.
        async with self.pool.acquire() as conn:
            locked=await conn.fetchval('SELECT pg_try_advisory_lock($1)',self.lock_key(account_id))
            if not locked:raise Conflict('邮箱正在同步，请稍后刷新')
            try:
                row=await conn.fetchrow('SELECT * FROM mailbox_accounts WHERE owner_id=$1 AND id=$2',self.owner,account_id)
                if not row:raise NotFound('邮箱账户不存在')
                code=cipher().decrypt(row['secret_encrypted'].encode()).decode()
                imported=0;read_count=0
                known_validity,last_uid=row['uidvalidity'],row['last_uid']
                since=row['created_at'].astimezone(ZoneInfo('Asia/Shanghai')).date()-timedelta(days=60)
                while True:
                    if heartbeat:await heartbeat(conn)
                    validity,messages=await asyncio.to_thread(self.transport,row['provider'],row['address'],code,known_validity,last_uid,30,since)
                    if heartbeat:await heartbeat(conn)
                    read_count+=len(messages)
                    for message in messages:
                        is_candidate=candidate(message)
                        async with conn.transaction():
                            item=await conn.fetchrow('''INSERT INTO mailbox_messages(owner_id,account_id,uidvalidity,uid,subject,sender,sent_at,text,is_candidate,processing_error,auto_state)
                                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) ON CONFLICT(account_id,uidvalidity,uid) DO NOTHING RETURNING id''',self.owner,account_id,validity,message['uid'],message['subject'],message['sender'],message['sent_at'],message['text'],is_candidate,message.get('processing_error'),'pending' if is_candidate and not message.get('processing_error') else 'skipped')
                            await conn.execute('UPDATE mailbox_accounts SET uidvalidity=$3,last_uid=$4 WHERE owner_id=$1 AND id=$2',self.owner,account_id,validity,int(message['uid']))
                    if len(messages)<30:break
                    known_validity,last_uid=validity,int(messages[-1]['uid'])
                await self.reclassify_cached(conn,account_id)
                # Raw messages and UID are already durable. A separate cache drain enqueues
                # candidates transactionally; a full queue never discards cached candidates.
                await conn.execute('UPDATE mailbox_accounts SET last_sync_at=now(),last_error=NULL WHERE owner_id=$1 AND id=$2',self.owner,account_id)
                return {'read':read_count,'recruitment_candidates':await self.enqueue_cached(account_id)}
            except Exception as error:
                await conn.execute('UPDATE mailbox_accounts SET last_sync_at=now(),last_error=$3 WHERE owner_id=$1 AND id=$2',self.owner,account_id,'同步未完成：'+type(error).__name__+'，请检查连接或服务配置')
                raise
            finally:await conn.execute('SELECT pg_advisory_unlock($1)',self.lock_key(account_id))

