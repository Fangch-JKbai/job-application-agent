"""Restored databases stay readable until the recovery CLI explicitly activates them."""
from services.errors import ServiceError
class RecoveryPaused(ServiceError):
    status_code=409
    def __init__(self):super().__init__('恢复副本处于只读核验状态；请完成恢复核验并显式启用处理后重启服务')
async def state(pool):
    row=await pool.fetchrow('SELECT paused,backup_sha256,restored_at,verified_at,activated_at FROM recovery_state WHERE singleton')
    if row is None:raise RecoveryPaused()
    return dict(row)
async def require_active(pool):
    if (await state(pool))['paused']:raise RecoveryPaused()
