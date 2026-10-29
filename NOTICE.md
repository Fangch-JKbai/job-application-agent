# 来源与改造说明

本项目选择性复用 [jawwad-ali/ai-customer-support-agent](https://github.com/jawwad-ali/ai-customer-support-agent) 的基础代码和设计方式。上游为 MIT 许可，Copyright (c) 2026 Jawwad Ali；许可全文及版权声明保留在 [LICENSE](LICENSE)。

复用起点是一个本地学习版本，其文件包含学习期间的修改，因此不能把该版本的 Git HEAD 等同于上游发布版本。逐文件来源哈希保存在 [reference/manifest.json](reference/manifest.json)。`reference/upstream/` 是仅在学习工作区保留的只读对照快照，不随本公开仓库发布。

直接衍生与演进部分主要包括 `core/logging.py` 的结构化日志基础、`database/pool.py` 的连接池方式；HTTP、模型客户端、上下文及前端基础也曾作为对照材料。招聘通知、申请与事项模型、自动匹配保存、撤销、持久任务、租约隔离、独立邮箱调度、评测和发布验证由本项目逐阶段实现，不把原客服系统的全部功能或测试成绩归为本项目成果。

项目由维护者与 AI 协作实现。真实邮箱、密钥、个人求职资料和本地开发日志不属于公开内容。公开测试数据使用合成样例；已完成能力与仍待验证的部分见 [docs/validation.md](docs/validation.md)。
