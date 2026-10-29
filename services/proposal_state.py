"""Small facts shared by draft preparation and confirmation."""
import hashlib
import json


def proposal_digest(payload):
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


async def task_scope(conn,owner_id,application_id,lock=False):
    rows = await conn.fetch('SELECT id,version FROM tasks WHERE owner_id=$1 AND application_id=$2 ORDER BY id'+(' FOR UPDATE' if lock else ''),owner_id,application_id)
    return proposal_digest([[str(row['id']),row['version']] for row in rows])
