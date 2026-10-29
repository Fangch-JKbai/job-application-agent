"""Single local release gate: package, clean-directory build, Compose acceptance, optional regressions."""
import argparse,base64,json,os,secrets,shutil,subprocess,sys,tarfile
from pathlib import Path
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.package_delivery import package,verify

def check(out,image,suite):
    out.mkdir(parents=True,exist_ok=False);log=(out/'gate.log').open('w');checks=[]
    def run(args,**kwargs):
        result=subprocess.run(args,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,**kwargs)
        log.flush()
        if result.returncode:raise RuntimeError('发布检查失败，详见 '+str(out/'gate.log'))
    archive=package(out/'source','p11.8');source=Path(archive['archive']);verified=verify(source);checks.append('archive_hashes_verified')
    twin=package(out/'determinism','p11.8');assert twin['archive_sha256']==archive['archive_sha256'];checks.append('source_archive_deterministic')
    clean=out/'unpacked';clean.mkdir()
    with tarfile.open(source) as tar:tar.extractall(clean)
    source_root=clean/'job-application-agent'
    assert not (source_root/'.env').exists() and not (source_root/'.venv').exists();checks.append('unpacked_without_private_env_or_dependencies')
    print('Building frozen unpacked source',flush=True)
    run(['docker','build','--pull=false','-t',image,str(source_root)])
    checks.append('locked_backend_frontend_and_types_build_from_unpacked_source')
    run([sys.executable,str(source_root/'scripts/check_p118_release.py'),'--image',image,'--output',str(out/'acceptance')])
    checks.append('actual_delivery_acceptance')
    if suite=='full':
        regression(out/'regressions',image,log);checks.append('core_reliability_upgrade_observation_automatic_regressions')
    image_id=subprocess.check_output(['docker','image','inspect','--format','{{.Id}}',image],text=True).strip()
    report={'suite':suite,'gate_checks_passed':len(checks),'checks':checks,'source_archive_sha256':archive['archive_sha256'],'source_files':verified['source_files'],'image':image,'image_id':image_id,'acceptance':'acceptance/report.json','regressions':'regressions' if suite=='full' else None,'mode':'local clean unpacked archive build; synthetic fixtures; no remote deployment','real_mail_or_model_called':False}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));log.close();print(json.dumps(report,ensure_ascii=False))

def regression(out,image,log):
    out.mkdir();tag=uuid4().hex[:8];network='job-agent-p118-reg-'+tag;pg=network+'-pg';runner=network+'-runner';envfile=out/'runtime.env';created=[]
    values={'DATABASE_URL':'postgresql://fixture:'+secrets.token_urlsafe(24)+'@'+pg+':5432/job_application_agent_test_base','MAIL_CREDENTIAL_KEY':base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),'APP_SESSION_SECRET':secrets.token_urlsafe(36),'PYTHON_DOTENV_DISABLED':'1','MAIL_POLL_ENABLED':'false','MODEL_REQUESTS_PER_HOUR':'10000','ENABLE_SYNC_DIAGNOSTICS':'true'}
    password=values['DATABASE_URL'].split('fixture:',1)[1].split('@')[0]
    envfile.write_text('\n'.join(k+'='+v for k,v in values.items())+'\n');envfile.chmod(0o600)
    def docker(*args,check=True):
        r=subprocess.run(['docker',*args],stdout=log,stderr=subprocess.STDOUT);log.flush()
        if check and r.returncode:raise RuntimeError('回归失败，详见gate.log')
        return r.returncode
    try:
        docker('network','create',network);created.append('network')
        docker('run','-d','--name',pg,'--network',network,'--cpus','2','--memory','1g','-e','POSTGRES_USER=fixture','-e','POSTGRES_PASSWORD='+password,'-e','POSTGRES_DB=job_application_agent_test_base','pgvector/pgvector:pg16');created.append(pg)
        import time
        for _ in range(120):
            if docker('exec',pg,'pg_isready','-U','fixture','-d','job_application_agent_test_base',check=False)==0:break
            time.sleep(.25)
        else:raise RuntimeError('临时回归数据库未就绪')
        program="""import os,subprocess,sys
from pathlib import Path
runs=[('P11_LONG_OUTPUT','reliability','scripts/check_p11_reliability.py'),('P11_UPGRADE_OUTPUT','upgrade','scripts/check_p11_upgrade.py'),('P116_OUTPUT','observation','scripts/check_p11_observation.py'),('AUTOMATIC_OUTPUT','automatic','scripts/check_automatic.py')]
for key,name,path in runs:
 os.environ[key]='.work/release-regressions/'+name
 if name=='automatic':
  import scripts.check_automatic as h
  h.REPORT=Path('/app/.work/release-regressions/automatic')
  import asyncio,json
  h.REPORT.mkdir(parents=True,exist_ok=False)
  result=asyncio.run(h.checks())
  (h.REPORT/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
 else:subprocess.run([sys.executable,path],check=True)
"""
        # Keep container until outputs have been copied, including failure outputs.
        created.append(runner)
        code=docker('run','--name',runner,'--network',network,'--env-file',str(envfile),'--entrypoint','.venv/bin/python',image,'-u','-c',program,check=False)
        docker('cp',runner+':/app/.work/release-regressions/.',str(out),check=False)
        if code:raise RuntimeError('业务回归失败，保留本轮输出；详见gate.log')
    finally:
        cleanup=[]
        for name in reversed(created):
            if name!='network':cleanup.append(docker('rm','-f',name,check=False)==0)
        if 'network' in created:cleanup.append(docker('network','rm',network,check=False)==0)
        envfile.unlink(missing_ok=True)
        cleaned=all(cleanup)
        (out/'cleanup.json').write_text(json.dumps({'temporary_containers_network_and_env_removed':cleaned}))
        if not cleaned:raise RuntimeError('临时回归资源清理失败，请查看gate.log')
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--image',default='job-agent-p118-local:20261029');p.add_argument('--suite',choices=['quick','full'],default='quick');a=p.parse_args()
    check(a.output.resolve(),a.image,a.suite)
