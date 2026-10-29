"""Small, explicit resource budget; shared by all API/worker processes."""
import os

def positive(name,default):
    value=int(os.environ.get(name,str(default)))
    if value<1:raise ValueError(name+'必须大于0')
    return value

def lease_seconds():return positive('JOB_LEASE_SECONDS',30)
def max_attempts():return positive('JOB_MAX_ATTEMPTS',3)
def worker_slots():return positive('WORKER_CONCURRENCY',2)
