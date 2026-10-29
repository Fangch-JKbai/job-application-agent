"""P11.6 real HTTP/PostgreSQL checks. All model and mail inputs are synthetic."""
import asyncio,io,json,logging,os,socket,subprocess,sys,time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit
from uuid import UUID,uuid4
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import asyncpg
from database.config import database_url
from database.migrate import migrate
from database.pool import create_pool
from services.jobs import JobService,claim
from services.worker import execute
from services.operations import Operations
from services.runtime_observation import runtime
from core.logging import _JSONFormatter
from core.observation import measure_waits
from scripts.check_automatic import extractor,notice
from scripts.check_p10_api import Client,mutation,TEST_KEY
from scripts.check_p11_reliability import settings
OUT=ROOT/os.environ.get('P116_OUTPUT','.work/p11/p11.6/observation')
SECRET='P116_PRIVATE_SENTINEL'

def build_test_app():
    from scripts.check_automatic import build_test_app as build
    app=build()
    @app.post('/_fixture/close-pool')
    async def close_pool():await app.state.pool.close();return {'closed':True}
    @app.get('/_fixture/error')
    async def failure():raise RuntimeError(SECRET)
    return app

@contextmanager
def server(dsn,owner):
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    token='test-only-'+uuid4().hex
    env={**os.environ,'DATABASE_URL':dsn,'LOCAL_USER_ID':str(owner),'APP_MODE':'private','APP_ACCESS_TOKEN':token,'APP_SESSION_SECRET':token,'MAIL_CREDENTIAL_KEY':TEST_KEY,'MAIL_POLL_ENABLED':'false','MODEL_REQUESTS_PER_HOUR':'10000','ENABLE_SYNC_DIAGNOSTICS':'false','OBSERVATION_HEARTBEAT_SECONDS':'1'}
    with (OUT/'api.log').open('w') as log:
        p=subprocess.Popen([sys.executable,'-m','uvicorn','scripts.check_p11_observation:build_test_app','--factory','--host','127.0.0.1','--port',str(port),'--no-proxy-headers'],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT)
        try:
            a=Client(port)
            for _ in range(100):
                if p.poll() is not None:raise AssertionError('startup failed; see api.log')
                try:a.call('GET','/health/live');break
                except OSError:time.sleep(.1)
            else:raise AssertionError('startup timeout')
            yield a,token
        finally:p.terminate();p.wait(timeout=15)

async def checks():
    OUT.mkdir(parents=True,exist_ok=False)
    base=database_url();name='job_application_agent_test_observe_'+uuid4().hex[:8]
    dsn=urlunsplit(urlsplit(base)._replace(path='/'+name));admin=await asyncpg.connect(base)
    await admin.execute(f'CREATE DATABASE "{name}"');pool=None;passed=[]
    def ok(name):passed.append(name);print('PASS',name,flush=True)
    memory=io.StringIO();handler=logging.StreamHandler(memory);handler.setFormatter(_JSONFormatter());logging.getLogger().addHandler(handler)
    try:
        await migrate(dsn);pool=await create_pool(dsn=dsn,role='worker');owner=uuid4()
        with server(dsn,owner) as (a,token):
            assert a.call('GET','/health/ready')['status']=='ready';ok('public_liveness_and_readiness')
            a.call('GET','/api/operations/metrics',status=401);a.call('POST','/api/auth/login',{'token':token});ok('operations_requires_private_authentication')
            marker=await pool.fetchrow("DELETE FROM schema_migrations WHERE name='0013_observation.sql' RETURNING *")
            try:
                assert a.call('GET','/health/ready',status=503)['status']=='not_ready'
                assert a.call('GET','/health/live')['status']=='alive';ok('missing_schema_not_ready_while_alive')
            finally:await pool.execute('INSERT INTO schema_migrations(name,sha256,applied_at) VALUES($1,$2,$3)',*marker.values())
            n=notice(a,'submission',SECRET);command=uuid4()
            j=a.call('POST','/api/jobs',{'request_id':str(command),'notification_id':n['id']},202)
            request_id=a.last_headers.get('X-Request-ID',a.last_headers.get('x-request-id'));assert request_id
            assert float(a.last_headers.get('X-App-Elapsed-Ms',a.last_headers.get('x-app-elapsed-ms')))>=0
            trace=a.call('GET','/api/operations/jobs/'+j['id']);assert trace['phase']=='queued' and trace['requests'][0]['http_request_id']==request_id;ok('http_request_id_persisted_in_queued_job_trace')
            unknown=uuid4();a.call('GET','/api/operations/jobs/'+str(unknown),status=404)
            foreign=uuid4();await pool.execute('INSERT INTO app_users(id,display_name) VALUES($1,$2)',foreign,'合成其他用户')
            from services.notifications import NotificationService
            from schemas.notifications import NotificationImport
            from schemas.jobs import JobCreate
            fn=await NotificationService(pool,foreign).import_notification(NotificationImport(request_id=uuid4(),text='合成隔离通知'))
            fj=await JobService(pool,foreign).submit(JobCreate(request_id=uuid4(),notification_id=fn['id']))
            a.call('GET','/api/operations/jobs/'+str(fj['id']),status=404);await JobService(pool,foreign).cancel(fj['id']);ok('unknown_and_foreign_job_not_exposed')
            check=a.call('POST','/api/operations/check');check=a.call('POST','/api/operations/check')
            alert=next(x for x in check['alerts'] if x['code']=='worker_not_seen');assert alert['status']=='active' and alert['episodes']==1;ok('repeated_alert_checks_deduplicate')
            with settings(OBSERVATION_HEARTBEAT_SECONDS=1):
                async with runtime(pool,'worker') as iid:
                    old=await pool.fetchval('SELECT last_seen_at FROM runtime_instances WHERE id=$1',iid)
                    await asyncio.sleep(1.2)
                    assert await pool.fetchval('SELECT last_seen_at FROM runtime_instances WHERE id=$1',iid)>old;ok('runtime_heartbeat_advances')
                    snap=a.call('GET','/api/operations/metrics');assert snap['fresh_instances']['worker']==1
                    check=a.call('POST','/api/operations/check');assert next(x for x in check['alerts'] if x['code']=='worker_not_seen')['status']=='resolved';ok('fresh_worker_resolves_alert')
                assert await pool.fetchval('SELECT status FROM runtime_instances WHERE id=$1',iid)=='stopped';ok('graceful_runtime_exit_marked_stopped')
            check=a.call('POST','/api/operations/check');assert next(x for x in check['alerts'] if x['code']=='worker_not_seen')['episodes']==2;ok('recurrent_alert_starts_new_episode')
            # Heartbeat freshness is independent of a remembered running status.
            await pool.execute("UPDATE runtime_instances SET status='running',last_seen_at=clock_timestamp()-interval '1 hour' WHERE id=$1",iid)
            assert a.call('GET','/api/operations/metrics')['fresh_instances']['worker']==0;ok('stale_heartbeat_not_counted_alive')
            await pool.execute("UPDATE jobs SET created_at=clock_timestamp()-interval '1 minute' WHERE id=$1",UUID(j['id']))
            assert next(x for x in a.call('POST','/api/operations/check')['alerts'] if x['code']=='queue_wait_high')['status']=='active';ok('oldest_queue_age_alert')
            job=await claim(pool,'observation-fixture')
            assert job['id']==UUID(j['id'])
            await pool.execute("UPDATE jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1",job['id'])
            assert next(x for x in a.call('POST','/api/operations/check')['alerts'] if x['code']=='expired_job_lease')['status']=='active'
            assert a.call('GET','/api/operations/jobs/'+j['id'])['phase']=='waiting_for_takeover';ok('expired_lease_visible_in_metrics_and_trace')
            job=await claim(pool,'observation-takeover');await execute(pool,job,extractor)
            trace=a.call('GET','/api/operations/jobs/'+j['id']);assert trace['phase']=='saved' and [x['status'] for x in trace['attempts']]==['expired','succeeded']
            assert trace['extractions'][0]['job_attempt_id']==trace['attempts'][1]['id'];ok('saved_trace_links_attempt_and_extraction')
            logs=[json.loads(x) for x in memory.getvalue().splitlines()]
            observed=next(x for x in logs if x['msg']=='job_attempt_observed')
            assert observed['origin_request_id']==request_id and observed['job_attempt_id']==trace['attempts'][1]['id'] and observed['fields']['status']=='succeeded';ok('worker_log_links_http_job_notification_and_attempt')
            # The fixture returns invalid JSON; this is a real failure path, not an alert row stub.
            bad=notice(a,'bad','示例失败公司');bj=a.call('POST','/api/jobs',mutation(notification_id=bad['id']),202)
            job=await claim(pool,'observation-failure');await execute(pool,job,extractor)
            failed=a.call('GET','/api/operations/jobs/'+bj['id']);assert failed['status']=='failed' and failed['extractions'][0]['error_kind'];ok('model_output_failure_has_attempt_and_error_kind')
            assert a.call('GET','/health/ready')['status']=='ready';ok('model_failure_does_not_fail_database_readiness')
            assert next(x for x in a.call('POST','/api/operations/check')['alerts'] if x['code']=='recent_job_failure')['status']=='active';ok('terminal_failure_alert')
            eid=UUID(trace['extractions'][0]['id']);await pool.execute('UPDATE notification_extractions SET usage=$2::jsonb WHERE id=$1',eid,json.dumps({'total_tokens':123,'prompt_tokens':100,'completion_tokens':23}))
            models=a.call('GET','/api/operations/metrics')['models'];assert models['attempts_with_known_total_tokens']==1 and models['known_total_tokens']==123 and models['failed_attempts']==1;ok('known_tokens_and_unknown_usage_not_conflated')
            # A real extraction waits in flight while another HTTP request inspects it.
            pending=notice(a,'submission','示例等待模型公司');pj=a.call('POST','/api/jobs',mutation(notification_id=pending['id']),202)
            job=await claim(pool,'observation-wait');entered=asyncio.Event();release=asyncio.Event()
            async def slow(n):entered.set();await release.wait();return await extractor(n)
            task=asyncio.create_task(execute(pool,job,slow));await entered.wait()
            assert a.call('GET','/api/operations/jobs/'+pj['id'])['phase']=='waiting_model'
            release.set();await task;ok('in_flight_model_visible_without_waiting_for_result')
            broken=notice(a,'submission','示例写库失败公司');wj=a.call('POST','/api/jobs',mutation(notification_id=broken['id']),202)
            await pool.execute("CREATE FUNCTION p116_fail() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'P116_PRIVATE_SENTINEL'; END; $$")
            await pool.execute('CREATE TRIGGER p116_fail BEFORE INSERT ON applications FOR EACH ROW EXECUTE FUNCTION p116_fail()')
            try:
                job=await claim(pool,'observation-write-failure');await execute(pool,job,extractor)
                wt=a.call('GET','/api/operations/jobs/'+wj['id']);assert wt['status']=='failed' and wt['error_kind']=='internal_error' and wt['extractions'][0]['status']=='completed'
                assert await pool.fetchval('SELECT count(*) FROM automatic_records WHERE notification_id=$1',UUID(broken['id']))==0;ok('write_failure_distinguished_from_completed_extraction_and_no_commit')
            finally:
                await pool.execute('DROP TRIGGER p116_fail ON applications');await pool.execute('DROP FUNCTION p116_fail()')
            unclear=notice(a,'no_company');uj=a.call('POST','/api/jobs',mutation(notification_id=unclear['id']),202)
            job=await claim(pool,'observation-unresolved');await execute(pool,job,extractor)
            assert a.call('GET','/api/operations/jobs/'+uj['id'])['phase']=='unresolved';ok('unresolved_business_result_distinguished_from_execution_failure')
            mail=a.call('POST','/api/mailboxes',{'provider':'qq','address':'p116-private@qq.com','authorization_code':SECRET,'automatic':True},201)
            conditions=a.call('GET','/api/operations/metrics')['conditions'];assert next(x for x in conditions if x['code']=='scheduler_not_seen')['active'];ok('enabled_mailbox_without_scheduler_alert')
            await pool.execute("UPDATE mailbox_accounts SET last_error=$2,enabled=false WHERE id=$1",UUID(mail['id']),SECRET)
            assert not next(x for x in a.call('GET','/api/operations/metrics')['conditions'] if x['code']=='mail_sync_unhealthy')['active'];ok('disabled_mailbox_does_not_trigger_stale_error')
            a.call('GET','/_fixture/error?secret='+SECRET,status=500);error_id=a.last_headers.get('X-Request-ID',a.last_headers.get('x-request-id'));assert error_id;ok('unexpected_error_has_safe_response_and_request_id')
            a.call('GET','/does-not-exist-'+SECRET,status=404)
            snap=a.call('GET','/api/operations/metrics');metrics=snap['current_api']['http']
            assert any(x['route']=='/_fixture/error' and x['status_class']=='5xx' for x in metrics)
            assert any(x['route']=='/api/operations/jobs/{job_id}' for x in metrics)
            assert any(x['route']=='unmatched' and x['status_class']=='4xx' for x in metrics)
            assert all(x['buckets'][-1]['count']==x['count'] for x in metrics);ok('bounded_route_labels_histogram_and_error_counts')
            private=json.dumps({'metrics':snap,'trace':trace,'failure':failed,'alerts':a.call('GET','/api/operations/alerts')})
            assert SECRET not in private and 'p116-private@qq.com' not in private and n['text'] not in private;ok('operations_omit_notice_text_model_output_and_mail_credentials')
            # Hold the only connection and make a measured acquisition really wait.
            small=await create_pool(dsn=dsn,role='api',max_size=1)
            try:
                async with small.acquire():
                    async def wait():
                        with measure_waits() as values:await small.fetchval('SELECT 1');return values
                    task=asyncio.create_task(wait());await asyncio.sleep(.06)
                values=await task;assert values['wait_ms']>=40 and values['acquires']==1;ok('request_scoped_pool_wait_measures_actual_contention')
            finally:await small.close()
            a.call('POST','/_fixture/close-pool')
            assert a.call('GET','/health/ready',status=503)['status']=='not_ready'
            assert a.call('GET','/health/live')['status']=='alive';ok('closed_database_pool_not_ready_but_liveness_survives')
        api_log=(OUT/'api.log').read_text()
        assert SECRET not in api_log and 'p116-private@qq.com' not in api_log and n['text'] not in api_log and token not in api_log
        assert SECRET not in memory.getvalue();ok('fault_logs_omit_exception_message_query_and_private_content')
        parsed=[]
        for line in api_log.splitlines():
            if line.startswith('{'):parsed.append(json.loads(line))
        assert any(x.get('request_id')==error_id and x['msg']=='http_unexpected_error' and x['fields']['error_type']=='RuntimeError' for x in parsed)
        assert any(x.get('request_id')==request_id and x['msg']=='http_request' and x['fields']['db_wait']['acquires']>0 for x in parsed);ok('http_log_pairing_and_pool_wait_fields')
        (OUT/'worker.log').write_text(memory.getvalue())
        return {'checks_passed':len(passed),'checks':passed,'mode':'real HTTP/PostgreSQL, explicit synthetic extractor; no IMAP/model service calls','temporary_database_removed':True}
    finally:
        logging.getLogger().removeHandler(handler)
        if pool:await pool.close()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)');await admin.close()

if __name__=='__main__':
    result=asyncio.run(checks());(OUT/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False))
