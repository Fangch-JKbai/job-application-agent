# P1 人工验收材料

`contract.json` 定义业务词表与规则编号；`fixtures.json` 是模拟的初始申请、事项和已保存通知；`cases.json` 是 25 条独立场景的输入与人工预期。

全部材料标为 synthetic，不是维护者的真实求职经历。示例 ID 仅方便引用，不规定未来数据库主键。公司别名是夹具已给定的信息，不是模型推断。

字段 expected.fields 附 expected.evidence，source 为 notification 或 followup:序号。动作只是一份待核对方案；确认前业务变更为空。上下文只加载案例指定的申请、待办和历史通知，不把所有夹具都当作可用信息。

在项目根目录运行 `.venv/bin/python scripts/check_p1_cases.py --self-check` 检查材料一致性。业务含义和边界见 `docs/P1-业务规则.md` 与 `docs/P1-验收案例.md`。
