"""Authenticated, content-free operational projection for the personal workspace."""
import json
from uuid import UUID
from core.job_settings import positive
from services.jobs import JobService

def decoded(value):return json.loads(value) if isinstance(value,str) else value

class Operations:
    def __init__(self,pool,owner):self.pool,self.owner=pool,owner
    async def snapshot(self):
        queue=dict(await self.pool.fetchrow("""SELECT count(*) FILTER(WHERE status='queued') AS queued,
            count(*) FILTER(WHERE status='running') AS running,
            count(*) FILTER(WHERE status='running' AND lease_until<=clock_timestamp()) AS expired,
            count(*) FILTER(WHERE status='failed' AND finished_at>clock_timestamp()-interval '1 day') AS failed_last_day,
            extract(epoch FROM clock_timestamp()-min(created_at) FILTER(WHERE status='queued')) AS oldest_queued_seconds,
            clock_timestamp() AS sampled_at FROM jobs WHERE owner_id=$1""",self.owner))
        instances=[]
        for row in await self.pool.fetch("SELECT id,role,status,started_at,last_seen_at,pool,http,last_seen_at>clock_timestamp()-$1*interval '1 second' AS fresh FROM runtime_instances ORDER BY started_at DESC",positive('OBSERVATION_STALE_SECONDS',20)):
            value=dict(row);value['pool']=decoded(value['pool']);value['http']=decoded(value['http']);instances.append(value)
        models=dict(await self.pool.fetchrow("""SELECT count(*) AS extraction_attempts,
            count(*) FILTER(WHERE status='failed') AS failed_attempts,count(*) FILTER(WHERE status='processing') AS processing_attempts,
            count(usage->>'total_tokens') AS attempts_with_known_total_tokens,
            sum((usage->>'total_tokens')::bigint) AS known_total_tokens,
            sum((usage->>'prompt_tokens')::bigint) AS known_prompt_tokens,
            sum((usage->>'completion_tokens')::bigint) AS known_completion_tokens FROM notification_extractions WHERE owner_id=$1""",self.owner))
        mail=[dict(r) for r in await self.pool.fetch("""SELECT id,provider,enabled,last_sync_at,next_sync_at,(last_error IS NOT NULL) AS has_error,
            (enabled AND ((last_sync_at IS NULL AND created_at<clock_timestamp()-interval '5 minutes') OR last_sync_at<clock_timestamp()-$2*interval '1 second')) AS stale
            FROM mailbox_accounts WHERE owner_id=$1 ORDER BY created_at""",self.owner,positive('MAIL_POLL_SECONDS',43200)*2)]
        pending=await self.pool.fetchval("SELECT count(*) FROM mailbox_messages WHERE owner_id=$1 AND auto_state IN ('pending','processing')",self.owner)
        fresh={role:sum(r['role']==role and r['status']=='running' and r['fresh'] for r in instances) for role in ['api','worker','scheduler']}
        conditions=[]
        def condition(code,active,value,threshold):
            conditions.append({'code':code,'active':bool(active),'details':{'value':value,'threshold':threshold}})
        condition('worker_not_seen',queue['queued']+queue['running']>0 and not fresh['worker'],fresh['worker'],1)
        condition('expired_job_lease',queue['expired']>0,queue['expired'],0)
        age=float(queue['oldest_queued_seconds'] or 0)
        condition('queue_wait_high',age>positive('OBSERVATION_QUEUE_WAIT_SECONDS',30),age,positive('OBSERVATION_QUEUE_WAIT_SECONDS',30))
        condition('recent_job_failure',queue['failed_last_day']>0,queue['failed_last_day'],0)
        enabled=sum(bool(m['enabled']) for m in mail)
        condition('scheduler_not_seen',enabled>0 and not fresh['scheduler'],fresh['scheduler'],1)
        condition('mail_sync_unhealthy',any(m['enabled'] and (m['stale'] or m['has_error']) for m in mail),sum(bool(m['enabled'] and (m['stale'] or m['has_error'])) for m in mail),0)
        return {'scope':'personal_workspace_database','queue':queue,'models':models,'mailboxes':mail,'pending_mail_messages':pending,'instances':instances,'fresh_instances':fresh,'conditions':conditions,
            'limits':{'global_execution_slots':positive('MODEL_CONCURRENCY_LIMIT',2),'global_queue':positive('JOB_QUEUE_LIMIT',200),'owner_queue':positive('JOB_OWNER_QUEUE_LIMIT',100)}}

    async def check(self):
        snapshot=await self.snapshot()
        async with self.pool.acquire() as conn,conn.transaction():
            await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))','alerts:'+str(self.owner))
            for condition in snapshot['conditions']:
                if condition['active']:
                    await conn.execute("""INSERT INTO operational_alerts(owner_id,code,status,details) VALUES($1,$2,'active',$3::jsonb)
                      ON CONFLICT(owner_id,code) DO UPDATE SET status='active',last_seen_at=clock_timestamp(),resolved_at=NULL,
                      first_seen_at=CASE WHEN operational_alerts.status='resolved' THEN clock_timestamp() ELSE operational_alerts.first_seen_at END,
                      episodes=operational_alerts.episodes+CASE WHEN operational_alerts.status='resolved' THEN 1 ELSE 0 END,details=excluded.details""",self.owner,condition['code'],json.dumps(condition['details']))
                else:await conn.execute("UPDATE operational_alerts SET status='resolved',resolved_at=clock_timestamp(),last_seen_at=clock_timestamp() WHERE owner_id=$1 AND code=$2 AND status='active'",self.owner,condition['code'])
        return {'checked_at':snapshot['queue']['sampled_at'],'alerts':await self.alerts()}

    async def alerts(self):
        return [{**dict(r),'details':decoded(r['details'])} for r in await self.pool.fetch('SELECT code,status,first_seen_at,last_seen_at,resolved_at,episodes,details FROM operational_alerts WHERE owner_id=$1 ORDER BY code',self.owner)]

    async def trace(self,jid):
        job=await JobService(self.pool,self.owner).get(jid)
        attempts=[dict(r) for r in await self.pool.fetch('SELECT id,number,worker_id,status,error_kind,started_at,finished_at FROM job_attempts WHERE job_id=$1 ORDER BY number',jid)]
        requests=[dict(r) for r in await self.pool.fetch('SELECT request_id AS command_id,http_request_id,created_at FROM job_requests WHERE owner_id=$1 AND job_id=$2 ORDER BY created_at,request_id',self.owner,jid)]
        extractions=[]
        for r in await self.pool.fetch('SELECT id,job_token AS job_attempt_id,status,phase,error_kind,error_status,elapsed_ms,provider,model,usage,created_at,finished_at FROM notification_extractions WHERE owner_id=$1 AND job_id=$2 ORDER BY created_at,id',self.owner,jid):
            value=dict(r);value['usage']=decoded(value['usage']);extractions.append(value)
        phase=job['status']
        if phase=='queued' and job['attempt_count']:phase='scheduled_retry'
        elif phase=='running':
            expired=await self.pool.fetchval("SELECT lease_until<=clock_timestamp() FROM jobs WHERE id=$1",jid)
            phase='waiting_for_takeover' if expired else 'waiting_model' if extractions and extractions[-1]['status']=='processing' else 'applying_result'
        elif phase=='succeeded' and job['result']:phase=job['result']['status']
        # No original notice, model output, answer text, email address or credential.
        return {'job_id':jid,'notification_id':job['notification_id'],'phase':phase,'status':job['status'],'attempt_count':job['attempt_count'],'error_kind':job['error_kind'],'application_id':job['result'].get('application_id') if job['result'] else None,'requests':requests,'attempts':attempts,'extractions':extractions}
