import asyncio,json,os,sys
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit
from uuid import uuid4
r=Path(__file__).resolve().parents[1];sys.path.insert(0,str(r))
import asyncpg
from database.config import database_url
from database.migrate import migrate
from database.pool import create_pool
from services.mailboxes import MailboxService
from services.mail_transport import candidate
from schemas.mailboxes import MailboxBind
from scripts.check_p10_api import fixture_mail,fixture_extractor,TEXT
async def main():
 examples=[('上海人工智能实验室-线上笔试邀请',True),('中国移动2027校园招聘在线统一笔试通知',True),('【测评邀请】示例公司测评邀请函',True),('【顺丰】-2027届校招-专业笔试邀请函',True),('感谢投递AI应用开发岗位',True),('Interview invitation',True),('2027秋招',True),('实习生邀请',True),('招聘系统邮箱认证，不要回复邮件',False),('招聘网站：账户已绑定邮箱',False),('警惕招聘诈骗安全提醒',False),('面试经验分享',False),('Subscription Billing Problem',False),('验证码',False),('天气预报',False)]
 for subject,expected in examples:assert candidate({'subject':subject,'sender':'sender','text':'普通正文'})==expected,subject
 base=database_url();name='job_application_agent_test_filter_'+uuid4().hex[:8];dsn=urlunsplit(urlsplit(base)._replace(path='/'+name));admin=await asyncpg.connect(base);await admin.execute(f'CREATE DATABASE "{name}"')
 try:
  await migrate(dsn);pool=await create_pool(dsn=dsn);owner=uuid4()
  try:
   await pool.execute("INSERT INTO app_users(id,display_name) VALUES($1,'fixture')",owner)
   service=MailboxService(pool,owner,fixture_extractor,fixture_mail)
   account=await service.bind(MailboxBind(provider='qq',address='fixture@qq.com',authorization_code='fixture-code',automatic=False));aid=__import__('uuid').UUID(account['id'])
   await service.sync(aid)
   rows=await pool.fetch("SELECT id FROM mailbox_messages WHERE owner_id=$1 AND auto_state='skipped' ORDER BY uid LIMIT 2",owner)
   await pool.execute("UPDATE mailbox_messages SET subject='线上笔试邀请',text=$2 WHERE id=$1",rows[0]['id'],TEXT)
   await pool.execute("UPDATE mailbox_messages SET subject='线上笔试邀请',processing_error='邮件过大' WHERE id=$1",rows[1]['id'])
   pointer=await pool.fetchval('SELECT last_uid FROM mailbox_accounts WHERE id=$1',aid)
   result=await service.sync(aid);assert result=={'read':0,'recruitment_candidates':1},result
   count=await pool.fetchval('SELECT count(*) FROM notifications WHERE owner_id=$1',owner);assert count==3
   assert await pool.fetchval('SELECT last_uid FROM mailbox_accounts WHERE id=$1',aid)==pointer
   assert await pool.fetchval('SELECT auto_state FROM mailbox_messages WHERE id=$1',rows[1]['id'])=='skipped'
   assert (await service.sync(aid))=={'read':0,'recruitment_candidates':0}
   assert await pool.fetchval('SELECT count(*) FROM notifications WHERE owner_id=$1',owner)==count
   assert await pool.fetchval('SELECT count(*) FROM applications WHERE owner_id=$1',owner)==0
  finally:await pool.close()
 finally:await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)');await admin.close()
 print(json.dumps({'keyword_cases_passed':len(examples),'cached_mail_rescan_passed':True,'deduplication_and_original_cursor_preserved':True,'errored_mail_not_reset':True,'business_unchanged':True,'real_model_called':False,'temporary_database_removed':True},ensure_ascii=False))
asyncio.run(main())
