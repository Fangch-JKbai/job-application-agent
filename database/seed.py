"""Import one P1 case's synthetic initial context; never execute expected actions."""
import argparse
import asyncio
from datetime import date, datetime
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import asyncpg

from core.identity import current_user_id
from database.config import database_url

DATA = Path(__file__).resolve().parents[1] / 'data/examples/p1'


async def seed(case_id: str, dsn: str | None = None) -> dict:
    owner = current_user_id()
    dataset = json.loads((DATA / 'cases.json').read_text())
    fixtures = json.loads((DATA / 'fixtures.json').read_text())
    case = next(item for item in dataset['cases'] if item['id'] == case_id)
    applications = [a for a in fixtures['applications'] if a['id'] in case['context']['application_ids']]
    tasks = [t for t in fixtures['tasks'] if t['id'] in case['context']['task_ids']]

    def ident(kind, key):
        return uuid5(NAMESPACE_URL, f'job-application-agent:{owner}:{case_id}:{kind}:{key}')

    counts = {'applications': 0, 'tasks': 0, 'events': 0}
    conn = await asyncpg.connect(database_url(dsn))
    try:
        async with conn.transaction():
            await conn.execute('INSERT INTO app_users(id,display_name) VALUES($1,$2) ON CONFLICT(id) DO NOTHING', owner, '本地开发用户')
            other_case = await conn.fetchval("SELECT sample_case_id FROM applications WHERE owner_id=$1 AND data_origin='synthetic' AND sample_case_id<>$2 LIMIT 1", owner, case_id)
            if other_case:
                raise ValueError('P1 案例彼此独立；请在单独的空数据库中导入另一案例')
            for app in applications:
                app_id = ident('application', app['id'])
                inserted = await conn.fetchval('''INSERT INTO applications(
                    id,owner_id,company_name,company_aliases,position_name,batch,stage,outcome,data_origin,sample_case_id,sample_key)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8,'synthetic',$9,$10)
                    ON CONFLICT(id) DO NOTHING RETURNING id''',
                    app_id, owner, app['company_name'], app['company_aliases'], app['position_name'], app['batch'], app['stage'], app['outcome'], case_id, app['id'])
                if inserted:
                    counts['applications'] += 1
                    await conn.execute('''INSERT INTO application_events(
                        id,owner_id,application_id,event_type,actor_type,source_kind,source_reference,after_data,operation_id)
                        VALUES($1,$2,$3,'application_created','system','synthetic_seed',$4,$5::jsonb,$6)''',
                        ident('event', app['id']), owner, app_id, f'{case_id}:fixtures.json#applications/{app["id"]}', json.dumps(app,ensure_ascii=False), ident('operation', app['id']))
                    counts['events'] += 1
            for task in tasks:
                app_id, task_id = ident('application', task['application_id']), ident('task', task['id'])
                timing = [datetime.fromisoformat(task[key]) if task.get(key) else None for key in ['opens_at','starts_at','ends_at','due_at']]
                days = [date.fromisoformat(task[key]) if task.get(key) else None for key in ['starts_on','due_on']]
                inserted = await conn.fetchval('''INSERT INTO tasks(
                    id,owner_id,application_id,type,title,status,round,opens_at,starts_at,ends_at,due_at,starts_on,due_on,
                    source_timezone,data_origin,sample_case_id,sample_key)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,'synthetic',$15,$16)
                    ON CONFLICT(id) DO NOTHING RETURNING id''',
                    task_id, owner, app_id, task['type'], f'示例事项：{task["type"]}', task['status'], task.get('round'),
                    *timing, *days, case['input']['timezone_hint'] if any(timing) else None, case_id, task['id'])
                if inserted:
                    counts['tasks'] += 1
                    await conn.execute('''INSERT INTO application_events(
                        id,owner_id,application_id,task_id,event_type,actor_type,source_kind,source_reference,after_data,operation_id)
                        VALUES($1,$2,$3,$4,'task_created','system','synthetic_seed',$5,$6::jsonb,$7)''',
                        ident('event', task['id']), owner, app_id, task_id, f'{case_id}:fixtures.json#tasks/{task["id"]}', json.dumps(task,ensure_ascii=False), ident('operation', task['id']))
                    counts['events'] += 1
        return {'case_id':case_id,'material':'synthetic','inserted':counts,'expected_actions_executed':False}
    finally:
        await conn.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(seed(args.case)), ensure_ascii=False))
