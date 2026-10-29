"""Private PostgreSQL16 backup/restore through the existing DB container, no shell or network uploads."""
import argparse,hashlib,json,os,re,subprocess,sys
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
MIGRATIONS=ROOT/'database/migrations'
TABLES=['app_users','applications','tasks','application_events','notifications','notification_extractions','agent_sessions','automatic_records','jobs','job_requests','job_attempts','mailbox_accounts','mailbox_messages']
class RecoveryError(Exception):pass

def timestamp():return datetime.now(timezone.utc).isoformat()
def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        while chunk:=f.read(1024*1024):h.update(chunk)
    return h.hexdigest()
def private_json(path,value):
    with open(path,'x',encoding='utf-8',opener=lambda p,flags:os.open(p,flags,0o600)) as f:json.dump(value,f,ensure_ascii=False,indent=2)
def private_output(path):
    path=path.expanduser().resolve()
    if path.is_relative_to(ROOT) and not any(path.is_relative_to(ROOT/p) for p in ['.work','data/private']):raise RecoveryError('项目内备份只允许放入已忽略的.work或data/private；也可选择项目外独立目录')
    return path
def identifier(value):return '"'+value.replace('"','""')+'"'
def literal(value):return "'"+value.replace("'","''")+"'"

class Database:
    def __init__(self,container,database=None):
        self.container=container
        result=subprocess.run(['docker','inspect',container],capture_output=True)
        if result.returncode:raise RecoveryError('无法访问指定数据库容器')
        env=dict(x.split('=',1) for x in json.loads(result.stdout)[0]['Config']['Env'] if '=' in x)
        self.user=env.get('POSTGRES_USER','postgres');self.database=database or env.get('POSTGRES_DB',self.user)
        if not re.fullmatch(r'job_application_agent_[a-z0-9_]+',self.database):raise RecoveryError('只支持本项目命名的独立数据库')
    def command(self,*args,stdin=None,stdout=None):
        result=subprocess.run(['docker','exec','-i',self.container,*args],stdin=stdin,stdout=stdout or subprocess.PIPE,stderr=subprocess.PIPE)
        if result.returncode:raise RecoveryError(f'{args[0]}失败（退出码{result.returncode}）；错误原文未写入日志，目标库保留供排查')
        return result.stdout
    def sql(self,sql,database=None):
        result=subprocess.run(['docker','exec','-i',self.container,'psql','-X','-qAt','-v','ON_ERROR_STOP=1','-U',self.user,'-d',database or self.database],input=sql.encode(),capture_output=True)
        if result.returncode:raise RecoveryError('数据库操作失败；检查容器、连接权限、目标库是否已存在；原始错误未输出')
        return result.stdout.decode().strip()
    def migrations(self):return json.loads(self.sql("SELECT coalesce(json_agg(x),'[]') FROM (SELECT name,sha256 FROM schema_migrations ORDER BY name) x"))
    def version(self):return int(self.sql("SELECT current_setting('server_version_num')",database="postgres"))

def compatible(rows):
    files=sorted(MIGRATIONS.glob('*.sql'))
    if not rows or len(rows)>len(files):raise RecoveryError('备份版本未知或高于当前代码；请使用匹配版本的恢复工具')
    for row,path in zip(rows,files):
        if row!={'name':path.name,'sha256':digest(path)}:raise RecoveryError('迁移版本/校验和不兼容；历史迁移不得改写')
    return files[len(rows):]
def checked_archive(folder):
    folder=folder.expanduser().resolve();meta=json.loads((folder/'backup.json').read_text());dump=folder/'database.dump'
    if meta.get('format')!='job-agent-backup-v1' or meta.get('recovery_state_data_excluded') is not True or digest(dump)!=meta['dump_sha256']:raise RecoveryError('备份格式或SHA256不匹配，未执行恢复')
    compatible(meta['migrations']);return folder,meta

def backup(db,output):
    output=private_output(output);output.mkdir(mode=0o700,parents=True,exist_ok=False);output.chmod(0o700)
    rows=db.migrations();compatible(rows);version=db.version()
    if version//10000!=16:raise RecoveryError('此恢复工具当前只验证PostgreSQL16')
    partial=output/'database.dump.partial'
    with open(partial,'xb',opener=lambda p,flags:os.open(p,flags,0o600)) as f:db.command('pg_dump','-U',db.user,'-d',db.database,'--format=custom','--no-owner','--no-acl','--exclude-table-data=public.recovery_state',stdout=f)
    if db.migrations()!=rows:raise RecoveryError('备份期间发生迁移；保留partial但未发布有效备份，请迁移结束后重试')
    dump=output/'database.dump';partial.rename(dump)
    meta={'format':'job-agent-backup-v1','created_at':timestamp(),'source_database':db.database,'postgres_version_num':version,'dump_sha256':digest(dump),'dump_bytes':dump.stat().st_size,'migrations':rows,'mail_key_included':False,'runtime_env_included':False,'recovery_state_data_excluded':True}
    private_json(output/'backup.json',meta)
    return {'status':'backed_up','dump_sha256':meta['dump_sha256'],'bytes':meta['dump_bytes'],'mail_key_included':False}

def restore_name(db):
    if not re.fullmatch(r'job_application_agent_restore_[a-z0-9_]+',db.database):raise RecoveryError('恢复只允许新建job_application_agent_restore_前缀数据库，不支持覆盖现有库')
def restore(db,folder):
    restore_name(db);folder,meta=checked_archive(folder)
    if db.version()//10000!=meta['postgres_version_num']//10000:raise RecoveryError('源/目标PostgreSQL主版本不匹配')
    # CREATE is deliberate: an existing database is never dropped or overwritten.
    db.sql('CREATE DATABASE '+identifier(db.database),database='postgres')
    with (folder/'database.dump').open('rb') as f:db.command('pg_restore','-U',db.user,'-d',db.database,'--no-owner','--no-acl','--single-transaction','--exit-on-error',stdin=f)
    rows=db.migrations()
    if rows!=meta['migrations']:raise RecoveryError('恢复后的迁移目录与备份清单不一致；停止启用')
    added=compatible(rows)
    sql=['BEGIN;','SELECT pg_advisory_xact_lock(470218320);']
    for path in added:
        sql += [path.read_text(),"INSERT INTO schema_migrations(name,sha256) VALUES("+literal(path.name)+','+literal(digest(path))+');']
    sql += ["INSERT INTO recovery_state(singleton,paused,backup_sha256,restored_at) VALUES(true,true,"+literal(meta['dump_sha256'])+",clock_timestamp()) ON CONFLICT(singleton) DO UPDATE SET paused=true,backup_sha256=excluded.backup_sha256,restored_at=excluded.restored_at,verified_at=NULL,activated_at=NULL;","UPDATE runtime_instances SET status='stopped';",'COMMIT;']
    db.sql('\n'.join(sql))
    return {'status':'restored_paused','database':db.database,'applied_migrations':[p.name for p in added],'mail_poll_enabled':False,'next':'verify，再显式activate；不要先启动worker或邮箱调度器'}

def verify(db,folder,key_file=None):
    restore_name(db);_,meta=checked_archive(folder);rows=db.migrations();pending=compatible(rows)
    if pending:raise RecoveryError('目标库尚未升级到当前代码版本')
    state=json.loads(db.sql('SELECT row_to_json(x) FROM recovery_state x WHERE singleton'))
    if state['backup_sha256']!=meta['dump_sha256']:raise RecoveryError('目标库不属于此备份恢复链路')
    counts={table:int(db.sql('SELECT count(*) FROM '+identifier(table))) for table in TABLES}
    if db.sql("SELECT count(*) FROM pg_constraint WHERE connamespace='public'::regnamespace AND NOT convalidated")!='0':raise RecoveryError('目标库存在未验证约束')
    users=json.loads(db.sql("SELECT coalesce(json_agg(id ORDER BY id),'[]') FROM app_users"))
    key_ok=not counts['mailbox_accounts']
    if counts['mailbox_accounts']:
        if key_file is None:raise RecoveryError('业务数据已恢复，但缺少独立邮箱密钥；副本保持只读，请提供--mail-key-file重新verify')
        from cryptography.fernet import Fernet,InvalidToken
        try:
            values=json.loads(key_file.read_text());fernet=Fernet(values['MAIL_CREDENTIAL_KEY'].encode())
            for row in json.loads(db.sql("SELECT json_agg(secret_encrypted) FROM mailbox_accounts")):fernet.decrypt(row.encode())
            key_ok=True
        except (ValueError,KeyError,TypeError,InvalidToken):raise RecoveryError('邮箱密钥无效或不匹配；副本保持只读，未输出密钥或授权码') from None
    db.sql('UPDATE recovery_state SET verified_at=clock_timestamp() WHERE singleton')
    return {'status':'verified','database':db.database,'rows':counts,'user_ids':users,'mail_credentials_verified':key_ok,'paused':state['paused'],'dump_sha256':meta['dump_sha256']}

def activate(db,folder,key_file,enabled):
    if not enabled:raise RecoveryError('启用后台任务需明确传入--enable-processing；启用前检查待执行任务和邮箱配置')
    result=verify(db,folder,key_file)
    db.sql('UPDATE recovery_state SET paused=false,activated_at=clock_timestamp() WHERE singleton')
    return {'status':'activated','database':db.database,'restart_required':True,'mail_polling':'由MAIL_POLL_ENABLED控制；恢复时建议先保持false'}

def export_key(env_file,output):
    from dotenv import dotenv_values
    from cryptography.fernet import Fernet
    values=dotenv_values(env_file);key=values.get('MAIL_CREDENTIAL_KEY')
    if not key:raise RecoveryError('配置中没有邮箱密钥；没有自动生成替代密钥')
    try:Fernet(key.encode())
    except ValueError:raise RecoveryError('配置中的邮箱密钥格式无效') from None
    output=private_output(output)
    if any((p/'backup.json').exists() for p in output.parents):raise RecoveryError('邮箱密钥必须与数据库备份目录分开放置')
    output.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
    private_json(output,{'MAIL_CREDENTIAL_KEY':key})
    return {'status':'key_exported','permissions':'0600','note':'请独立保管；数据库备份没有此密钥，重新生成不能解密旧授权码'}

def retention(db,days,apply):
    if days<1:raise RecoveryError('保留天数至少1')
    where="status='stopped' AND last_seen_at<clock_timestamp()-"+str(days)+"*interval '1 day'"
    count=int(db.sql('SELECT count(*) FROM runtime_instances WHERE '+where))
    if apply:db.sql('DELETE FROM runtime_instances WHERE '+where)
    return {'stopped_runtime_rows':count,'applied':apply,'retention_days':days,'business_history_deleted':False}

def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    for name in ['backup','restore','verify','activate','retention']:
        p=sub.add_parser(name);p.add_argument('--container',default='job-application-agent-db');p.add_argument('--database')
        if name=='backup':p.add_argument('--output',type=Path,required=True)
        if name in ['restore','verify','activate']:p.add_argument('--backup',type=Path,required=True)
        if name in ['verify','activate']:p.add_argument('--mail-key-file',type=Path)
        if name=='activate':p.add_argument('--enable-processing',action='store_true')
        if name=='retention':p.add_argument('--days',type=int,default=30);p.add_argument('--apply',action='store_true')
    p=sub.add_parser('export-mail-key');p.add_argument('--env-file',type=Path,default=ROOT/'.env');p.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    try:
        if args.command=='export-mail-key':result=export_key(args.env_file,args.output)
        else:
            db=Database(args.container,args.database)
            if args.command=='backup':result=backup(db,args.output)
            elif args.command=='restore':result=restore(db,args.backup)
            elif args.command=='verify':result=verify(db,args.backup,args.mail_key_file)
            elif args.command=='activate':result=activate(db,args.backup,args.mail_key_file,args.enable_processing)
            else:result=retention(db,args.days,args.apply)
        print(json.dumps(result,ensure_ascii=False))
    except (RecoveryError,OSError,ValueError) as error:
        # Never stringify arbitrary OS/JSON/subprocess errors that could include private payloads.
        print(json.dumps({'status':'failed','detail':str(error) if isinstance(error,RecoveryError) else '文件/配置读取失败，请检查路径、格式与访问权限'},ensure_ascii=False));return 1
    return 0
if __name__=='__main__':raise SystemExit(main())
