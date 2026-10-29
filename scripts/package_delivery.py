"""Build a deterministic, source-only local release; runtime data is never a source."""
import argparse,gzip,hashlib,io,json,re,tarfile
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
FOLDERS=['api','agent','core','database','services','schemas','web/src','web/review','docs/evidence','scripts','data/examples','evals','evaluation','tests']
FILES=['CONTRIBUTING.md','docs/architecture.md','docs/deployment.md','docs/backup-restore.md','docs/validation.md','docs/roadmap.md','docs/assets/p11.5/isolation.svg','.gitignore','.python-version','README.md','NOTICE.md','LICENSE','pyproject.toml','uv.lock','.env.example','.dockerignore','Dockerfile','compose.yaml','compose.delivery.yaml','web/package.json','web/package-lock.json','web/index.html','web/vite.config.ts','web/tsconfig.json','web/openapi.json','reference/manifest.json']
BLOCKED={'.git','.env','.venv','.work','node_modules','__pycache__','.pytest_cache','private','logs'}

def sha(data):return hashlib.sha256(data).hexdigest()
def sources():
    files=[ROOT/name for name in FILES if (ROOT/name).is_file()]
    for name in FOLDERS:
        files.extend(p for p in (ROOT/name).rglob('*') if p.is_file() and not BLOCKED.intersection(p.relative_to(ROOT).parts) and not p.name.startswith('.env') and p.suffix not in {'.pyc','.pyo'} and (name!='docs' or p.suffix in {'.md','.svg'}))
    files=sorted(set(files))
    if any(p.is_symlink() for p in files):raise ValueError('发布源文件不得使用符号链接')
    return files

def package(output,label):
    if not re.fullmatch(r'[a-zA-Z0-9._-]+',label):raise ValueError('发布标签只能含字母、数字、点、下划线和短横线')
    output.mkdir(parents=True,exist_ok=False)
    content={str(p.relative_to(ROOT)):p.read_bytes() for p in sources()}
    manifest={'format':'job-agent-source-v1','label':label,'source_only':True,'files':{p:sha(data) for p,data in content.items()}}
    content['RELEASE-MANIFEST.json']=(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n').encode()
    archive=output/('job-application-agent-'+label+'.tar.gz')
    with archive.open('xb') as raw,gzip.GzipFile(filename='',fileobj=raw,mode='wb',mtime=0) as zipped,tarfile.open(fileobj=zipped,mode='w') as tar:
        for name,data in sorted(content.items()):
            info=tarfile.TarInfo('job-application-agent/'+name);info.size=len(data);info.mode=0o644;info.mtime=0
            tar.addfile(info,io.BytesIO(data))
    report={**manifest,'archive':str(archive),'archive_sha256':sha(archive.read_bytes()),'source_files':len(manifest['files'])}
    (output/'manifest.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    return {k:v for k,v in report.items() if k!='files'}

def verify(archive):
    with tarfile.open(archive) as tar:
        members=tar.getmembers();names=[m.name for m in members]
        if len(set(names))!=len(names) or any(not m.isfile() or not m.name.startswith('job-application-agent/') or '..' in Path(m.name).parts for m in members):raise ValueError('归档路径或文件类型无效')
        manifest=json.load(tar.extractfile('job-application-agent/RELEASE-MANIFEST.json'))
        if manifest['format']!='job-agent-source-v1':raise ValueError('归档清单格式不匹配')
        expected={'job-application-agent/'+p for p in manifest['files']}|{'job-application-agent/RELEASE-MANIFEST.json'}
        if set(names)!=expected:raise ValueError('归档内容与清单不一致')
        for name,digest in manifest['files'].items():
            if sha(tar.extractfile('job-application-agent/'+name).read())!=digest:raise ValueError('归档文件校验和不匹配')
        return {'verified':True,'label':manifest['label'],'source_files':len(manifest['files']),'archive_sha256':sha(archive.read_bytes())}
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path);p.add_argument('--label',default='p11.8');p.add_argument('--verify',type=Path);a=p.parse_args()
    if a.verify:result=verify(a.verify)
    else:result=package(a.output or ROOT/'.work/releases'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'),a.label)
    print(json.dumps(result,ensure_ascii=False))
