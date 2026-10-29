"""Apply ordered SQL migrations transactionally, recording their checksums."""
import asyncio
import hashlib
import json
from pathlib import Path

import asyncpg

from database.config import database_url

MIGRATIONS = Path(__file__).with_name('migrations')


async def migrate(dsn: str | None = None, directory: Path = MIGRATIONS) -> list[str]:
    files = sorted(directory.glob('*.sql'))
    conn = await asyncpg.connect(database_url(dsn))
    try:
        # A second CLI invocation waits for the first; closing releases this lock.
        await conn.execute('SELECT pg_advisory_lock($1)', 470218320)
        await conn.execute('''CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY, sha256 TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now())''')
        applied = {row['name']: row['sha256'] for row in await conn.fetch('SELECT name, sha256 FROM schema_migrations')}
        present = {path.name for path in files}
        if applied.keys() - present:
            raise ValueError('已执行的迁移文件缺失，请保留历史迁移')
        for path in files:
            checksum = hashlib.sha256(path.read_bytes()).hexdigest()
            if path.name in applied and applied[path.name] != checksum:
                raise ValueError(f'已执行迁移被修改：{path.name}；请新建后续迁移')
        completed = []
        for path in files:
            if path.name in applied:
                continue
            async with conn.transaction():
                await conn.execute(path.read_text())
                await conn.execute('INSERT INTO schema_migrations(name, sha256) VALUES($1,$2)', path.name, hashlib.sha256(path.read_bytes()).hexdigest())
            completed.append(path.name)
        return completed
    finally:
        await conn.close()


if __name__ == '__main__':
    print(json.dumps({'applied': asyncio.run(migrate())}, ensure_ascii=False))
