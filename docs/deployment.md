# 本地运行与交付

当前已验证本地Docker Compose交付；公网TLS、域名和远程托管尚未实施。

## 首次安装

从仓库根目录执行README中的构建和初始化命令。初始化生成`.env`及随机口令、稳定会话密钥和Fernet邮箱密钥。纯Docker初始化挂载新克隆目录，使用本机UID/GID写文件；已有uv环境也可以运行：

```bash
uv sync --locked
.venv/bin/python scripts/configure_delivery.py --container
bash scripts/delivery.sh up
```

`APP_PORT`默认8001，仅绑定127.0.0.1。`APP_MODE`在delivery Compose中固定为private，需要个人口令登录。模型凭据、邮箱授权码和`.env`不进入Git。只读和手动管理不要求模型凭据，AI任务需要自己的模型供应商配置。

主要配置见`.env.example`：`LLM_PROVIDER`及对应模型凭据、`APP_ACCESS_TOKEN`、`APP_SESSION_SECRET`、`MAIL_CREDENTIAL_KEY`、`MAIL_POLL_ENABLED`和`MAIL_POLL_SECONDS`。模型名称由实际账户决定。QQ/163/126在工作台使用IMAP授权码绑定；若暂时不自动导入，启动前设`MAIL_POLL_ENABLED=false`。

## 四服务

| 服务 | 启动顺序与健康 | CPU / 内存上限 | 停机等待 |
| --- | --- | --- | --- |
| postgres | pg_isready；数据库持久卷 | 2核 / 1GiB | 30秒 |
| app | 数据库健康后迁移并启动，/health/ready | 1核 / 512MiB | 45秒，HTTP在途最多30秒 |
| worker | app就绪后启动；本进程成功DB心跳 | 1核 / 512MiB | 150秒 |
| scheduler | app就绪后启动；本进程成功DB心跳 | 0.25核 / 256MiB | 180秒 |

合计为4.25核、2.25GiB的上限，不是资源预留或已证明的容量。连接池默认API5、worker8、scheduler3，扩进程前重新计算总预算。应用容器使用非root用户和init。后台健康标记只在数据库心跳成功后刷新，不增加探针数据库连接；scheduler被明确禁用时按禁用状态健康。

每个容器的json-file日志设置为10m×3轮转。发布验收通过docker inspect确认CPU、内存、日志和网络设置生效；未制造大日志填满轮转或磁盘耗尽。数据库卷为delivery_pgdata，不暴露宿主数据库端口。

## 日常操作

```bash
bash scripts/delivery.sh status
bash scripts/delivery.sh logs worker
bash scripts/delivery.sh stop
bash scripts/delivery.sh up
```

stop先停止API和scheduler，再让worker完成在途任务；排队记录保留。超过容器等待期限时Docker强停，重启后依靠租约接管。stop保留卷与.env，日常更新不执行`down --volumes`。

更新前按[恢复说明](backup-restore.md)备份数据库和独立密钥，再stop、更新源码、up。旧迁移不改，新迁移追加。旧镜像不能自动回退数据库结构；不兼容时从归档恢复到新库核验，再切换连接。

## 原生开发

需要Python≥3.12、Node≥22.12、uv和Docker：

```bash
bash scripts/start_local.sh
```

脚本安装依赖、启动独立开发数据库、迁移、构建前端，并运行API/worker/scheduler。退出时关闭这些子进程；不会关闭无关服务。也可分别使用`database.migrate`、uvicorn、`services.worker`和`services.mail_scheduler`模块调试。
