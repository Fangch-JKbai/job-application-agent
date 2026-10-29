"""Check this container's successful database heartbeat, without another DB connection."""
import json,os,sys,time
from pathlib import Path

def healthy(role):
    if role=='scheduler' and os.environ.get('MAIL_POLL_ENABLED','true').lower()!='true':return True
    try:
        state=json.loads(Path(os.environ['RUNTIME_HEALTH_FILE']).read_text())
        os.kill(state['pid'],0)
        return state['role']==role and 0<=time.time()-state['seen_at']<int(os.environ.get('OBSERVATION_STALE_SECONDS','20'))
    except (KeyError,ValueError,OSError,TypeError):return False
if __name__=='__main__':
    role=sys.argv[1]
    raise SystemExit(0 if role in {'worker','scheduler'} and healthy(role) else 1)
