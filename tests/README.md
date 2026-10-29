# 检查入口

本地发布入口为`scripts/check_release.py`。它不读取用户现有.env，使用合成数据、随机测试配置及独立临时容器/卷。

```bash
python3 scripts/check_release.py --suite quick --output .work/check-quick
python3 scripts/check_release.py --suite full --output .work/check-full
```

需要Docker，输出目录必须是新目录。quick先生成并校验源码归档，解压后执行锁定依赖构建，再验证20项四服务发布流程。full额外运行46项可靠性、11项升级、31项观测和25项自动保存回归。结束后删除临时容器、网络、卷和测试.env，保留合成报告及镜像。

`check_p118_release.py`在测试覆盖文件中显式启用延迟模型替身，正式Compose入口始终是正常worker。不要把替身结果当成模型准确率。

专项脚本包括`check_p117_recovery.py`、`check_p11_reliability.py`、`check_p11_upgrade.py`、`check_p11_observation.py`和`check_automatic.py`。多数底层业务脚本需要Python依赖环境和独立测试PG连接，统一发布入口已负责准备环境，优先使用它。

旧P2～P10脚本保留供查阅；P6的人工确认属于兼容协议，当前产品默认自动保存。带`live`的脚本会调用模型，须自行明确配置凭据，常规发布检查不会运行它们。性能驱动`benchmark_p11*.py`也不在quick/full中自动执行。

已有结果和限制见[验证说明](../docs/validation.md)，数据恢复操作见[恢复说明](../docs/backup-restore.md)。
