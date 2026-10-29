"""Read back this project's migrations and the fixed local user's row counts."""
import asyncio
import json

import asyncpg

from core.identity import current_user_id
from database.config import database_url


async def status() -> dict:
    owner = current_user_id()
    conn = await asyncpg.connect(database_url())
    try:
        counts = {}
        for table in ['applications','tasks','application_events']:
            counts[table] = await conn.fetchval(f'SELECT count(*) FROM {table} WHERE owner_id=$1',owner)
        notification_counts = {table:await conn.fetchval(f'SELECT count(*) FROM {table} WHERE owner_id=$1',owner) for table in ['notifications','notification_extractions']}
        return {
            'database':await conn.fetchval('SELECT current_database()'),
            'migrations':[row['name'] for row in await conn.fetch('SELECT name FROM schema_migrations ORDER BY name')],
            'current_user_rows':counts,
            'notification_rows':notification_counts,
            'agent_rows':{table:await conn.fetchval(f'SELECT count(*) FROM {table} WHERE owner_id=$1',owner) for table in ['agent_sessions','agent_turns','action_proposals']},
            'execution_rows':{table:await conn.fetchval(f'SELECT count(*) FROM {table} WHERE owner_id=$1',owner) for table in ['proposal_executions','confirmation_requests']},
            'sample_cases':[row['sample_case_id'] for row in await conn.fetch("SELECT DISTINCT sample_case_id FROM applications WHERE owner_id=$1 AND data_origin='synthetic' ORDER BY sample_case_id",owner)],
        }
    finally:
        await conn.close()


if __name__ == '__main__':
    print(json.dumps(asyncio.run(status()),ensure_ascii=False,indent=2))
