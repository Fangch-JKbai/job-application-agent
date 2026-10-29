"""A heartbeat per process; telemetry failure never rewrites a business result."""
import asyncio,json,logging,os,time
from pathlib import Path
from contextlib import asynccontextmanager,suppress
from uuid import uuid4
from core.job_settings import positive
from core.logging import error_fields
logger=logging.getLogger(__name__)

@asynccontextmanager
async def runtime(pool,role,worker_key=None,http=None):
    instance=uuid4()
    await pool.execute('INSERT INTO runtime_instances(id,role,worker_key,pool) VALUES($1,$2,$3,$4::jsonb)',instance,role,worker_key,json.dumps(pool.metrics()))
    marker=Path(os.environ['RUNTIME_HEALTH_FILE']) if os.environ.get('RUNTIME_HEALTH_FILE') else None
    def mark():
        if marker:marker.write_text(json.dumps({'role':role,'pid':os.getpid(),'instance_id':str(instance),'seen_at':time.time()}))
    mark()
    async def pulse():
        while True:
            await asyncio.sleep(positive('OBSERVATION_HEARTBEAT_SECONDS',5))
            try:
                await pool.execute("UPDATE runtime_instances SET last_seen_at=clock_timestamp(),pool=$2::jsonb,http=$3::jsonb WHERE id=$1",instance,json.dumps(pool.metrics()),json.dumps(http.snapshot() if http else []))
                mark()
            except Exception as exc:logger.warning('runtime_heartbeat_failed',extra={'fields':{'instance_id':str(instance),'role':role,**error_fields(exc)}})
    task=asyncio.create_task(pulse())
    try:yield instance
    finally:
        if marker:marker.unlink(missing_ok=True)
        task.cancel()
        with suppress(asyncio.CancelledError):await task
        try:await pool.execute("UPDATE runtime_instances SET status='stopped',last_seen_at=clock_timestamp(),pool=$2::jsonb WHERE id=$1",instance,json.dumps(pool.metrics()))
        except Exception as exc:logger.warning('runtime_stop_unrecorded',extra={'fields':{'instance_id':str(instance),'role':role,**error_fields(exc)}})
