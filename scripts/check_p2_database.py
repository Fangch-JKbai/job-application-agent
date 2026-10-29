"""Real PostgreSQL migration and constraint checks in two disposable databases."""
import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import asyncpg
from core.identity import current_user_id
from database.config import database_url
from database.migrate import MIGRATIONS, migrate
from database.pool import create_pool
from database.seed import seed


async def rejected(conn, sql, *values, code):
    try:
        async with conn.transaction():
            await conn.execute(sql, *values)
    except asyncpg.PostgresError as error:
        assert error.sqlstate == code, (error.sqlstate, code)
    else:
        raise AssertionError(f'Expected PostgreSQL error {code}')


async def checks():
    base = database_url()
    owner = current_user_id()
    suffix = uuid4().hex[:10]
    names = [f'job_application_agent_test_{suffix}_{letter}' for letter in ['a','b']]
    urls = [urlunsplit(urlsplit(base)._replace(path='/' + name)) for name in names]
    admin = await asyncpg.connect(base)
    results = []
    created = []

    def passed(name):
        results.append(name)

    try:
        version = await admin.fetchval('SHOW server_version')
        for name in names:
            await admin.execute(f'CREATE DATABASE "{name}"')
            created.append(name)
        migration_files = sorted(MIGRATIONS.glob('*.sql'))
        assert await migrate(urls[0]) == [path.name for path in migration_files]
        assert await migrate(urls[0]) == []
        passed('empty_database_migration_and_repeat_noop')
        concurrent = await asyncio.gather(migrate(urls[1]), migrate(urls[1]))
        assert sorted(map(len, concurrent)) == [0,len(migration_files)]
        passed('concurrent_migrations_serialized')
        conn = await asyncpg.connect(urls[0])
        second = await asyncpg.connect(urls[1])
        try:
            signature = "SELECT table_name,column_name,data_type,is_nullable,column_default FROM information_schema.columns WHERE table_schema='public' ORDER BY table_name,ordinal_position"
            assert await conn.fetch(signature) == await second.fetch(signature)
            passed('two_empty_databases_have_same_schema')
            with tempfile.TemporaryDirectory(prefix='job-agent-migration-') as temporary:
                directory = Path(temporary)
                for source in migration_files:
                    (directory/source.name).write_bytes(source.read_bytes())
                original = migration_files[0]
                (directory / original.name).write_text(original.read_text() + '\n-- changed after apply\n')
                try:
                    await migrate(urls[0], directory)
                except ValueError as error:
                    assert '迁移被修改' in str(error)
                else:
                    raise AssertionError('Migration checksum change was accepted')
                passed('changed_applied_migration_rejected')
                (directory / original.name).write_bytes(original.read_bytes())
                (directory / '9999_fail.sql').write_text('CREATE TABLE rollback_probe(id INT); SELECT 1/0;')
                try:
                    await migrate(urls[0], directory)
                except asyncpg.DivisionByZeroError:
                    pass
                else:
                    raise AssertionError('Broken migration unexpectedly succeeded')
                assert await conn.fetchval("SELECT to_regclass('public.rollback_probe')") is None
                assert await conn.fetchval('SELECT count(*) FROM schema_migrations') == len(migration_files)
                passed('failed_migration_rolls_back_schema_and_history')

            first = await seed('P1-012', urls[0])
            repeat = await seed('P1-012', urls[0])
            assert first['inserted'] == {'applications':1,'tasks':1,'events':2}
            assert repeat['inserted'] == {'applications':0,'tasks':0,'events':0}
            passed('case_initial_context_import_and_idempotence')
            app = await conn.fetchrow('SELECT * FROM applications WHERE owner_id=$1', owner)
            task = await conn.fetchrow('SELECT * FROM tasks WHERE owner_id=$1', owner)
            assert app['stage'] == 'interview' and task['type'] == 'interview'
            assert await conn.fetchval("SELECT count(*) FROM tasks WHERE type='assessment'") == 0
            passed('expected_case_actions_not_executed_by_seed')
            try:
                await seed('P1-009', urls[0])
            except ValueError:
                pass
            else:
                raise AssertionError('Independent cases were silently mixed')
            passed('different_case_not_silently_merged')

            contract = json.loads((ROOT / 'data/examples/p1/contract.json').read_text())
            async with conn.transaction():
                for stage in contract['application_stages']:
                    for outcome in contract['application_outcomes']:
                        await conn.execute('UPDATE applications SET stage=$1,outcome=$2 WHERE id=$3', stage,outcome,app['id'])
                for status in contract['task_statuses']:
                    for kind in contract['task_types']:
                        await conn.execute('UPDATE tasks SET status=$1,type=$2 WHERE id=$3',status,kind,task['id'])
            # Restore the initial fields; these are direct database capability checks.
            await conn.execute("UPDATE applications SET stage='interview',outcome='active' WHERE id=$1", app['id'])
            await conn.execute("UPDATE tasks SET status='pending',type='interview' WHERE id=$1", task['id'])
            passed('all_p1_status_and_type_values_supported')
            await rejected(conn, "UPDATE applications SET stage='unknown' WHERE id=$1",app['id'],code='23514')
            await rejected(conn, "UPDATE applications SET company_name=' ' WHERE id=$1",app['id'],code='23514')
            await rejected(conn, "UPDATE tasks SET status='overdue' WHERE id=$1",task['id'],code='23514')
            passed('invalid_status_empty_name_and_persisted_overdue_rejected')

            other_owner, other_app = uuid4(), uuid4()
            await conn.execute("INSERT INTO app_users(id,display_name) VALUES($1,'另一个测试用户')", other_owner)
            await conn.execute("INSERT INTO applications(id,owner_id,company_name,position_name) VALUES($1,$2,'测试公司','测试岗位')", other_app,other_owner)
            local_ids = await conn.fetch('SELECT id FROM applications WHERE owner_id=$1',current_user_id())
            assert other_app not in [row['id'] for row in local_ids]
            passed('server_identity_used_for_owner_scoped_read')
            await rejected(conn, "INSERT INTO tasks(owner_id,application_id,type,title) VALUES($1,$2,'assessment','跨归属事项')",owner,other_app,code='23503')
            passed('composite_foreign_key_rejects_cross_owner_task')

            tx = conn.transaction()
            await tx.start()
            try:
                for position,batch in [('另一个岗位','2027秋招'),(app['position_name'],'2027实习'),(app['position_name'],app['batch'])]:
                    await conn.execute('INSERT INTO applications(owner_id,company_name,position_name,batch) VALUES($1,$2,$3,$4)',owner,app['company_name'],position,batch)
                await conn.execute("INSERT INTO tasks(owner_id,application_id,type,title,status,due_at,source_timezone) VALUES($1,$2,'assessment','补充测评','pending',$3,'Asia/Shanghai')",owner,app['id'],datetime.fromisoformat('2026-10-30T18:00:00+08:00'))
                assert await conn.fetchval('SELECT stage FROM applications WHERE id=$1',app['id']) == 'interview'
                assert await conn.fetchval('SELECT count(*) FROM tasks WHERE application_id=$1',app['id']) == 2
            finally:
                await tx.rollback()
            passed('same_company_multiple_applications_and_parallel_tasks_supported')

            tx = conn.transaction()
            await tx.start()
            try:
                date_only = await conn.fetchrow("INSERT INTO tasks(owner_id,application_id,type,title,starts_on) VALUES($1,$2,'interview','时刻待通知',DATE '2026-10-30') RETURNING starts_on,starts_at",owner,app['id'])
                assert date_only['starts_at'] is None and date_only['starts_on'].isoformat() == '2026-10-30'
                await rejected(conn,"UPDATE tasks SET starts_on=DATE '2026-10-29' WHERE id=$1",task['id'],code='23514')
                await rejected(conn,"INSERT INTO tasks(owner_id,application_id,type,title,opens_at,due_at,source_timezone) VALUES($1,$2,'assessment','反向时间','2026-10-30T19:00:00+08:00','2026-10-30T18:00:00+08:00','Asia/Shanghai')",owner,app['id'],code='23514')
                await rejected(conn,"INSERT INTO tasks(owner_id,application_id,type,title,due_at) VALUES($1,$2,'assessment','缺少来源时区','2026-10-30T18:00:00+08:00')",owner,app['id'],code='23514')
            finally:
                await tx.rollback()
            passed('date_precision_order_and_timezone_fields_checked')

            event = await conn.fetchrow('SELECT * FROM application_events WHERE owner_id=$1 ORDER BY created_at LIMIT 1',owner)
            await rejected(conn,"UPDATE application_events SET reason='重写' WHERE id=$1",event['id'],code='55000')
            await rejected(conn,'DELETE FROM application_events WHERE id=$1',event['id'],code='55000')
            passed('event_history_is_append_only')
            event_sql = "INSERT INTO application_events(owner_id,application_id,event_type,actor_type,source_kind,after_data,operation_id) VALUES($1,$2,'application_updated','local_user','manual',$3::jsonb,$4)"
            await rejected(conn,event_sql,owner,app['id'],'[]',uuid4(),code='23514')
            await rejected(conn,event_sql,owner,app['id'],'{}',event['operation_id'],code='23505')
            await rejected(conn,"INSERT INTO application_events(owner_id,application_id,task_id,event_type,actor_type,source_kind,after_data,operation_id) VALUES($1,$2,$3,'task_updated','local_user','manual','{}',$4)",owner,app['id'],uuid4(),uuid4(),code='23503')
            passed('event_payload_reference_and_operation_sequence_constraints')

            before = await conn.fetchrow('SELECT version,notes FROM applications WHERE id=$1',app['id'])
            try:
                async with conn.transaction():
                    await conn.execute("UPDATE applications SET notes='应回滚',version=version+1 WHERE id=$1",app['id'])
                    await conn.execute(event_sql,owner,app['id'],'[]',uuid4())
            except asyncpg.CheckViolationError:
                pass
            assert await conn.fetchrow('SELECT version,notes FROM applications WHERE id=$1',app['id']) == before
            passed('business_update_and_event_failure_rollback_together')
        finally:
            await conn.close()
            await second.close()

        pool = await create_pool(dsn=urls[0], max_size=2)
        try:
            async def change(note):
                async with pool.acquire() as connection:
                    return await connection.fetchval('UPDATE applications SET notes=$1,version=version+1,updated_at=now() WHERE id=$2 AND owner_id=$3 AND version=$4 RETURNING version',note,app['id'],owner,before['version'])
            versions = await asyncio.gather(change('编辑A'),change('编辑B'))
            assert versions.count(before['version']+1) == 1 and versions.count(None) == 1
            passed('optimistic_version_allows_only_one_concurrent_edit')
        finally:
            await pool.close()
        final = await asyncpg.connect(urls[0])
        try:
            saved = await final.fetchrow('SELECT notes,version FROM applications WHERE id=$1',app['id'])
            assert saved['notes'] in ['编辑A','编辑B'] and saved['version'] == before['version']+1
            assert (await seed('P1-012',urls[0]))['inserted'] == {'applications':0,'tasks':0,'events':0}
            assert await final.fetchrow('SELECT notes,version FROM applications WHERE id=$1',app['id']) == saved
            passed('reconnect_persistence_and_seed_does_not_overwrite_edits')
        finally:
            await final.close()
        return {'checked_on':'2026-10-27','engine':'PostgreSQL','server_version':version,'checks_passed':len(results),'checks':results,'disposable_databases':2,'scope':'Real database DDL, sample import and SQL constraints; no HTTP business service or Agent execution.'}
    finally:
        for name in created:
            await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,default=ROOT/'.work/p2/validation.json')
    args = parser.parse_args()
    result = asyncio.run(checks())
    result['temporary_databases_removed'] = True
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))
