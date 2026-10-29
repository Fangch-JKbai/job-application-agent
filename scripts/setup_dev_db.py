"""Create local configuration and start this project's isolated PostgreSQL."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from database.config import load_settings
from database.migrate import migrate
from database.seed import seed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='postgres:16', help='Use an already cached PostgreSQL 16 image')
    parser.add_argument('--port', type=int, default=15432)
    parser.add_argument('--seed-case')
    args = parser.parse_args()
    docker = shutil.which('docker')
    if docker is None:raise SystemExit('Docker CLI not found; install Docker and add it to PATH')
    env_file = ROOT / '.env'
    if not env_file.exists():
        password = secrets.token_urlsafe(32)
        data = (f'POSTGRES_IMAGE={args.image}\nPOSTGRES_PORT={args.port}\nPOSTGRES_PASSWORD={password}\n'
                f'DATABASE_URL=postgresql://job_agent:{password}@127.0.0.1:{args.port}/job_application_agent_dev\n'
                f'LOCAL_USER_ID={uuid4()}\n')
        with env_file.open('x') as handle:
            os.chmod(env_file, 0o600)
            handle.write(data)
    load_settings()
    subprocess.run([docker,'compose','--env-file',str(env_file),'up','-d','--pull','never','--wait','--wait-timeout','45'],cwd=ROOT,check=True)
    applied = asyncio.run(migrate())
    sample = asyncio.run(seed(args.seed_case)) if args.seed_case else None
    print(json.dumps({'database':'job_application_agent_dev','container':'job-application-agent-db','applied':applied,'sample':sample},ensure_ascii=False))


if __name__ == '__main__':
    main()
