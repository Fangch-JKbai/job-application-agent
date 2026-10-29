"""Start the real delivery image against a disposable Compose database, no model/mail calls."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from dotenv import dotenv_values
from scripts.check_p10_api import Client,mutation
REPORT=ROOT/'.work/p10';PROJECT='job-agent-p10-smoke-'+uuid4().hex[:8]

def main():
    with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
    env={**os.environ,'APP_PORT':str(port)}
    args=['docker','compose','--env-file','.env','-f','compose.delivery.yaml','-p',PROJECT]
    checks=[];image=None
    with (REPORT/'container-smoke.log').open('w') as log:
        def run(*cmd):subprocess.run([*args,*cmd],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,check=True)
        try:
            # The delivery build is verified separately. Tag that exact image for this disposable Compose project.
            image=json.loads(subprocess.check_output(['docker','image','inspect','job-agent-delivery-app:latest'],text=True))[0]['Id']
            subprocess.run(['docker','tag',image,PROJECT+'-app:latest'],check=True)
            run('up','-d','--no-build','--wait')
            a=Client(port)
            for _ in range(100):
                try:a.call('GET','/health/live');break
                except OSError:time.sleep(.2)
            else:raise AssertionError('Container app did not start')
            assert 'root' in a.call('GET','/')
            checks.append('image_migrates_empty_own_volume_and_serves_built_UI')
            a.call('GET','/api/applications',status=401)
            token=dotenv_values(ROOT/'.env')['APP_ACCESS_TOKEN'];a.call('POST','/api/auth/login',{'token':token})
            assert a.call('GET','/api/applications')['total']==0
            checks.append('container_forces_private_login_before_business_data')
            app=a.call('POST','/api/applications',mutation(company_name='示例部署验收公司',position_name='Agent开发工程师',batch='27秋招'),201)
            a.call('POST','/api/tasks',mutation(application_id=app['id'],type='assessment',title='部署待办',due_on='2026-11-03'),201)
            checks.append('container_real_HTTP_postgres_create_and_readback')
            run('restart','app')
            for _ in range(100):
                try:a.call('GET','/health/live');break
                except OSError:time.sleep(.2)
            a.call('GET','/api/applications',status=401)
            a.call('POST','/api/auth/login',{'token':token})
            assert a.call('GET','/api/applications/'+app['id'])['batch']=='27秋招'
            assert a.call('GET','/api/tasks')['items'][0]['due_on']=='2026-11-03'
            checks.append('restart_logs_out_browser_and_preserves_application_and_date_only_task')
        finally:
            run('down','--volumes','--remove-orphans')
    report={'checks_passed':len(checks),'checks':checks,'image_id':image,'temporary_compose_project':PROJECT,'temporary_containers_and_volume_removed':True,'real_model_called':False,'real_mailbox_used':False}
    (REPORT/'container-validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print(json.dumps(report,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
