# 备份与恢复

数据库包含通知原文、邮件缓存、申请、待办、事件、提取、会话、业务回执、任务及尝试历史。邮箱授权码是密文，但通知和缓存正文仍是敏感数据。数据库归档压缩不等于加密，应存放于受保护位置。

邮箱绑定需要原`MAIL_CREDENTIAL_KEY`。数据库归档不包含该密钥或.env；重新生成密钥不能解密旧授权码。密钥和数据库分开保管，独立离线副本优于同盘两个目录。当前没有定时异地备份、WAL归档或PITR。

## 备份

工具调用已有PG16容器中的pg_dump/pg_restore，宿主无需另装客户端。先找出delivery数据库容器：

```bash
db_container=$(docker compose --env-file .env -f compose.delivery.yaml ps -q postgres)
backup_dir="$HOME/job-agent-backups/first"
key_file="$HOME/job-agent-keys/mail-key.json"
python3 scripts/data_recovery.py backup --container "$db_container" --output "$backup_dir"
```

密钥导出需要现有Python依赖环境：

```bash
.venv/bin/python scripts/data_recovery.py export-mail-key --output "$key_file"
```

纯Docker环境可用已构建镜像运行导出，将配置文件只读挂载、密钥目录单独挂载，不挂载数据库备份目录：

```bash
mkdir -p -m 700 "$HOME/job-agent-keys"
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$PWD/.env:/config/runtime.env:ro" \
  -v "$HOME/job-agent-keys:/keys" --entrypoint /app/.venv/bin/python \
  job-application-agent:local /app/scripts/data_recovery.py export-mail-key \
  --env-file /config/runtime.env --output /keys/mail-key.json
```

每次选新目录/文件，归档和密钥不覆盖已有路径。备份目录0700，归档/清单/导出密钥0600。SHA256检测损坏，不证明来源可信；只恢复可信归档。配置脚本和这些命令不会自动备份你的既有数据。

## 恢复到新库

```bash
restore_db=job_application_agent_restore_first
python3 scripts/data_recovery.py restore --container "$db_container" \
  --database "$restore_db" --backup "$backup_dir"
.venv/bin/python scripts/data_recovery.py verify --container "$db_container" \
  --database "$restore_db" --backup "$backup_dir" --mail-key-file "$key_file"
```

存在邮箱绑定时verify需cryptography依赖及原密钥，实际验证所有授权码密文。普通管理启动允许无邮箱密钥，恢复有绑定账户时则必须验证。输出仅包含数量、用户ID和状态；没有绑定时verify不需要密钥。

工具只恢复至新建的`job_application_agent_restore_*`库，不覆盖开发库。校验归档、PG主版本和迁移前缀，使用单事务恢复；迁移失败保留目标供排查，不自动破坏性降级或删除。

恢复后默认paused：API可登录回读，业务写入409，/health/ready为503，worker和启用的scheduler拒绝执行。API心跳仍会写观测数据。准备新的.env，把连接指向恢复库，LOCAL_USER_ID用verify返回值，邮箱密钥用原值，MAIL_POLL_ENABLED=false，先只启动API回读；不要直接执行默认delivery.sh up，因为它仍连接固定开发库并启动后台服务。

核验后显式启用：

```bash
.venv/bin/python scripts/data_recovery.py activate --container "$db_container" \
  --database "$restore_db" --backup "$backup_dir" \
  --mail-key-file "$key_file" --enable-processing
```

重启API刷新恢复状态，再按需要启动worker与邮箱。原任务编号、尝试、租约、期限及幂等关系保留；过期任务仍按既有规则处理。数据库业务去重不等于外部模型恰好调用一次。

默认保留业务和邮件历史。retention默认预览，只允许显式删除超过保留期且stopped的运行实例，不清理业务记录。解绑邮箱不等于擦除已导入通知或历史归档。
