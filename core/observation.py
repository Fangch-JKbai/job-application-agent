"""Bounded process-local HTTP metrics and task-local database wait measurements."""
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
import math

BUCKETS=(.005,.01,.025,.05,.1,.2,.5,1.,2.,5.,10.,math.inf)
_wait=ContextVar('db_wait',default=None)

@contextmanager
def measure_waits():
    values={'acquires':0,'wait_ms':0.,'max_wait_ms':0.,'failed_acquires':0}
    token=_wait.set(values)
    try:yield values
    finally:_wait.reset(token)

def record_wait(seconds,success):
    row=_wait.get()
    if row is not None:
        row['acquires']+=1;row['wait_ms']+=seconds*1000
        row['max_wait_ms']=max(row['max_wait_ms'],seconds*1000)
        row['failed_acquires']+=int(not success)

def method_label(value):return value if value in {'GET','POST','PATCH','DELETE','PUT','HEAD','OPTIONS'} else 'OTHER'

class HttpMetrics:
    def __init__(self):self.rows={}
    def observe(self,method,route,status,seconds):
        key=(method_label(method),route,str(status//100)+'xx')
        row=self.rows.setdefault(key,{'count':0,'seconds_total':0.,'max_seconds':0.,'buckets':[0]*len(BUCKETS)})
        row['count']+=1;row['seconds_total']+=seconds;row['max_seconds']=max(row['max_seconds'],seconds)
        for i,bound in enumerate(BUCKETS):row['buckets'][i]+=int(seconds<=bound)
    def snapshot(self):
        return [{'method':key[0],'route':key[1],'status_class':key[2],'count':r['count'],
          'seconds_total':round(r['seconds_total'],6),'max_seconds':round(r['max_seconds'],6),
          'buckets':[{'le_seconds':'+Inf' if math.isinf(b) else b,'count':c} for b,c in zip(BUCKETS,r['buckets'])]}
          for key,r in sorted(self.rows.items())]
