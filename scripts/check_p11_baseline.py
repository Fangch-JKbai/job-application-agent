"""P11.0: disposable real HTTP/PostgreSQL baseline; never calls a model or IMAP."""
import argparse
import asyncio
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import asyncpg
import httpx
from database.config import database_url
from database.migrate import migrate
from scripts.check_automatic import extractor
from agent.notification_extractor import ModelReply


def build_baseline_app():
    from api.main import app
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        async with original(app):
            app.state.baseline_active = 0
            app.state.baseline_peak = 0

            async def delayed(notice):
                app.state.baseline_active += 1
                app.state.baseline_peak = max(app.state.baseline_peak, app.state.baseline_active)
                try:
                    await asyncio.sleep(float(os.environ['BASELINE_MODEL_DELAY']))
                    if notice['source_reference'] == 'invalid_fixture':
                        return ModelReply('{invalid', 'fixture', 'fixture')
                    return await extractor(notice)
                finally:
                    app.state.baseline_active -= 1

            app.state.notification_extractor = delayed
            yield

    app.router.lifespan_context = lifespan

    @app.get('/_baseline/metrics', include_in_schema=False)
    async def metrics():
        pool = app.state.pool
        return {'active_model_fixtures': app.state.baseline_active,
                'peak_model_fixtures': app.state.baseline_peak,
                'pool_size': pool.get_size(), 'pool_idle': pool.get_idle_size(),
                'pool_max': pool.get_max_size()}

    return app


def summary(samples, seconds):
    values = sorted(s['ms'] for s in samples)
    def percentile(p):
        return round(values[max(0, math.ceil(len(values) * p) - 1)], 2)
    return {'requests': len(samples), 'elapsed_seconds': round(seconds, 3),
            'throughput_rps': round(len(samples) / seconds, 2),
            'latency_ms': {'p50': percentile(.5), 'p95': percentile(.95), 'max': round(values[-1], 2)},
            'status_counts': dict(Counter(str(s['status']) for s in samples)),
            'transport_errors': sum(s['status'] == 'transport_error' for s in samples)}


async def run(args, output):
    base = database_url()
    name = 'job_application_agent_test_p11_' + uuid4().hex[:10]
    dsn = urlunsplit(urlsplit(base)._replace(path='/' + name))
    owner = uuid4()
    admin = await asyncpg.connect(base)
    process = None
    created = False
    report = {'stage': 'P11.0', 'mode': 'real HTTP/PostgreSQL, delayed model fixture, no IMAP',
              'started_at': datetime.now(timezone.utc).isoformat(),
              'conditions': {'python': platform.python_version(), 'platform': platform.platform(),
                             'client_cpu_count': os.cpu_count(), 'web_processes': 1, 'pool_max': 5,
                             'fixture_delay_seconds': args.delay, 'seed_applications': args.seed,
                             'requests_per_read_round': args.requests, 'concurrency': args.concurrency,
                             'percentile': 'nearest rank; failed requests included',
                             'workload': 'closed-loop finite requests; not sustained capacity or real model throughput'},
              'rounds': [], 'checks': [], 'cleanup': {}}
    report['source_sha256'] = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in
                              ['api/main.py', 'database/pool.py', 'services/automatic.py',
                               'services/notifications.py', 'services/processing_state.py',
                               'scripts/check_p11_baseline.py']}
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        created = True
        await migrate(dsn)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        env = {**os.environ, 'ENABLE_SYNC_DIAGNOSTICS': 'true', 'DATABASE_URL': dsn, 'LOCAL_USER_ID': str(owner),
               'APP_MODE': 'local', 'MAIL_POLL_ENABLED': 'false',
               'MODEL_REQUESTS_PER_HOUR': '10000', 'BASELINE_MODEL_DELAY': str(args.delay)}
        with (output / 'server.log').open('w') as log:
            process = subprocess.Popen([sys.executable, '-m', 'uvicorn',
                'scripts.check_p11_baseline:build_baseline_app', '--factory', '--host', '127.0.0.1',
                '--port', str(port), '--no-proxy-headers'], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', trust_env=False,
                    timeout=30, limits=httpx.Limits(max_connections=150, max_keepalive_connections=150)) as client:
                for _ in range(100):
                    if process.poll() is not None:
                        raise RuntimeError('Baseline service failed; see server.log')
                    try:
                        response = await client.get('/health/live')
                        if response.status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    await asyncio.sleep(.1)
                else:
                    raise RuntimeError('Baseline service startup timed out')
                async def checked(method, path, body=None):
                    r = await client.request(method, path, json=body)
                    r.raise_for_status()
                    return r.json()
                for i in range(args.seed):
                    await checked('POST', '/api/applications', {'request_id': str(uuid4()),
                        'company_name': f'示例基线公司{i:04d}', 'position_name': 'Agent开发工程师', 'batch': '27秋招'})
                await checked('GET', '/api/applications?limit=50')

                async def timed(method, path):
                    started = time.perf_counter()
                    try:
                        r = await client.request(method, path)
                        status = r.status_code
                    except httpx.TransportError as error:
                        return {'status': 'transport_error', 'ms': (time.perf_counter()-started)*1000,
                                'error_type': type(error).__name__}
                    return {'status': status, 'ms': (time.perf_counter()-started)*1000}

                async def read_round(label, concurrency):
                    sem = asyncio.Semaphore(concurrency)
                    async def one(i):
                        async with sem:
                            path = ['/api/applications?limit=50', '/api/tasks?limit=50'][i % 2]
                            return await timed('GET', path)
                    started = time.perf_counter()
                    samples = await asyncio.gather(*(one(i) for i in range(args.requests)))
                    result = summary(samples, time.perf_counter()-started)
                    result.update(label=label, concurrency=concurrency, samples=samples)
                    report['rounds'].append(result)
                    assert all(s['status'] == 200 for s in samples), label

                for c in args.concurrency:
                    await read_round(f'read_c{c}', c)
                notices = []
                for i in range(5):
                    n = await checked('POST', '/api/notifications', {'request_id': str(uuid4()),
                        'text': f'示例后台公司{i}2027秋招投递成功，简历进入简历库。',
                        'source_reference': 'missing_position', 'hints': {'company_name': f'示例后台公司{i}'}})
                    notices.append(n['id'])
                started = time.perf_counter()
                pending = [asyncio.create_task(timed('POST', f'/api/notifications/{n}/process')) for n in notices]
                for _ in range(200):
                    metrics = await checked('GET', '/_baseline/metrics')
                    if metrics['active_model_fixtures'] == 5:
                        break
                    if all(p.done() for p in pending):
                        raise AssertionError('Fixture overlap not achieved')
                    await asyncio.sleep(.01)
                else:
                    raise AssertionError('Could not establish five overlapping model waits')
                report['mixed_overlap_metrics'] = metrics
                await read_round('read_during_5_model_waits', 10)
                samples = await asyncio.gather(*pending)
                report['rounds'].append({'label': 'process_5_delayed', 'samples': samples,
                                         **summary(samples, time.perf_counter()-started)})
                assert all(s['status'] == 200 for s in samples)
                for n in notices:
                    saved = await checked('GET', f'/api/notifications/{n}/automatic')
                    assert saved['status'] == 'saved'
                await checked('POST', f'/api/notifications/{notices[0]}/process')
                applications = await checked('GET', '/api/applications')
                assert applications['total'] == args.seed + 5
                report['checks'].append('five_saves_and_replay_no_duplicate_application')
                bad = await checked('POST', '/api/notifications', {'request_id': str(uuid4()),
                    'text': '示例失败公司投递成功', 'source_reference': 'invalid_fixture'})
                result = await checked('POST', f'/api/notifications/{bad["id"]}/process')
                assert result['status'] == 'failed'
                report['checks'].append('invalid_model_output_reported_as_business_failure')
                report['final_metrics'] = await checked('GET', '/_baseline/metrics')
                report['conditions']['postgres_version'] = await admin.fetchval('SHOW server_version')
                report['status'] = 'passed'
    except Exception as error:
        report.update(status='failed', error_type=type(error).__name__, error=str(error))
        raise
    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, 15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait)
            report['cleanup']['temporary_service_stopped'] = process.poll() is not None
        if created:
            await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
            report['cleanup']['temporary_database_removed'] = True
        await admin.close()
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--delay', type=float, default=1.5)
    parser.add_argument('--seed', type=int, default=100)
    parser.add_argument('--requests', type=int, default=60)
    parser.add_argument('--concurrency', type=int, nargs='+', default=[1, 10, 30])
    args = parser.parse_args()
    if args.delay <= 0 or args.seed < 1 or args.requests < 1 or any(c < 1 or c > 100 for c in args.concurrency):
        parser.error('delay/seed/requests must be positive; concurrency must be 1..100')
    output = ROOT / '.work/p11/p11.0' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:6])
    output.mkdir(parents=True)
    asyncio.run(run(args, output))
    print(output / 'report.json')
