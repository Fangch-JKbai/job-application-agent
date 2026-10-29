"""Render observed denominators, failures and trace pointers without rewriting run evidence."""
from decimal import Decimal
import json
from pathlib import Path
from evaluation.grading import summary
ROOT=Path(__file__).resolve().parents[1]

def cost_totals(results):
    totals={k:0 for k in ['prompt_tokens','completion_tokens','total_tokens']};calls=known=priced=0;low=Decimal(0);high=Decimal(0)
    for row in results:
        c=row['cost'];calls+=c['calls'];known+=c['calls_with_usage'];priced+=c['calls_priced']
        for key in totals:totals[key]+=(c['tokens'] or {}).get(key,0)
        if c['estimated_observed_cost_usd_range']:
            lo,hi=c['estimated_observed_cost_usd_range'];low+=Decimal(lo);high+=Decimal(hi)
    tool_events=[item for row in results for turn in row.get('processing_rows',{}).get('agent_turns',[]) for item in (json.loads(turn['trace']) if isinstance(turn['trace'],str) else turn['trace']) if item['kind']=='tool']
    return {'tool_calls':len(tool_events),'tool_errors':sum(isinstance(t.get('output'),dict) and 'error' in t['output'] for t in tool_events),'model_calls':calls,'calls_with_usage':known,'calls_priced':priced,'tokens':totals if known else None,'observed_cost_usd_range':[str(low),str(high)] if priced else None,'complete':calls==known==priced and bool(calls),'not_account_bill':True}

def compact(report):
    results=report['results']
    return {'run_id':report['run_id'],'split':report['split'],'started_at':report['started_at'],'finished_at':report.get('finished_at'),'state':report['state'],'material_type':report['material_type'],'real_model_called':report['real_model_called'],
        'requested_provider':report['requested_provider'],'requested_model':report['requested_model'],'dataset_lock':report['dataset_lock'],'source_hashes':report['source_hashes'],'package_versions':report['package_versions'],
        'summary':summary(results),'cost':cost_totals(results),'temporary_database_removed':report['temporary_database_removed'],
        'cases':[{'case_id':r['case_id'],'grade':r['grade'],'protocol_error':r['protocol_error'],'elapsed_ms':r['elapsed_ms'],'scripted_human_review':r['scripted_human_review'],'cost':{k:v for k,v in r['cost'].items() if k not in {'per_call','basis'}},'evidence':f'.work/p9/{report["run_id"]}/{r["case_id"]}/result.json'} for r in results]}

LABELS={'extraction_fields':'提取字段（含空值与精度）','first_turn_and_questions':'首轮状态与必要追问','application_match':'最终目标申请','final_session':'最终会话状态','no_preconfirmation_business_writes':'确认前业务数据不变','database_final_state':'数据库最终业务字段','unrelated_records_preserved':'无关记录保留','request_replay_no_extra_work':'已执行请求回放不增工作','confirmed_and_replayed_once':'明确确认且回放一次执行','manual_review_preserves_original':'人工核对保留模型原结果','semantic_duplicate_no_extra_business_writes':'再次导入原通知不增业务写入','protocol_completed':'预定交互流程完成'}

def render(run_ids):
    out=ROOT/'evals/p9/results';out.mkdir(exist_ok=True)
    reports=[json.loads((ROOT/'.work/p9'/run_id/'report.json').read_text()) for run_id in run_ids]
    text=['# P9：真实模型业务评测结果','', '这些结果来自固定的合成案例、真实模型及真实HTTP/PostgreSQL。脚本模拟用户回答和明确确认；不是私人通知、外部盲测、人工操作速度或线上用户准确率。开发集与留出集分别报告，不混成一个成功率。','']
    for report in reports:
        c=compact(report);(out/(report['run_id']+'.summary.json')).write_text(json.dumps(c,ensure_ascii=False,indent=2)+'\n');s=c['summary']
        text += [f'## {report["run_id"]}（{report["split"]}）','',f'严格工作流通过 **{s["passed"]}/{s["case_count"]}**；模型请求为 `{report["requested_provider"]}/{report["requested_model"]}`。完成时间：{report.get("finished_at","未结束")}。','', '| 检查维度 | 通过 / 实际检查案例 |','| --- | --- |']
        for name,value in s['metrics'].items():text.append(f'| {LABELS[name]} | {value["passed"]}/{value["evaluated"]} |')
        text += ['', '| 案例 | 工作流 | 总耗时 | 失败维度 |','| --- | --- | --- | --- |']
        for row in c['cases']:
            fail='、'.join(LABELS[k] for k,v in row['grade']['metrics'].items() if not v) or '—'
            text.append(f'| {row["case_id"]} | {"通过" if row["grade"]["all_passed"] else "失败"} | {row["elapsed_ms"]/1000:.2f}秒 | {fail} |')
        cost=c['cost'];span=cost['observed_cost_usd_range']
        amount=f'USD {span[0]}～{span[1]}' if span else '未知'
        text += ['',f'模型调用{cost["model_calls"]}次，取得用量{cost["calls_with_usage"]}次，可按冻结价格计算{cost["calls_priced"]}次；总Token：{(cost["tokens"] or {}).get("total_tokens","未知")}。工具调用{cost['tool_calls']}次，其中{cost['tool_errors']}次返回显式错误并留痕，工作流通过不等于全部工具首试正确。已取得用量的峰/谷价估算范围：{amount}。',
                 f'整段脚本工作流耗时，nearest-rank P50={s["latency_ms"]["p50_nearest_rank"]}ms、P95={s["latency_ms"]["p95_nearest_rank"]}ms；包括服务启动、HTTP/数据库回读与即时脚本回答，初始数据播种不计入，不是纯模型延迟或压测。', '', '失败案例（如有）保留原输出、评分差异与协议错误，不通过自动修JSON或看期望后补回答改写本轮结果。','']
        for row in c['cases']:
            if row['grade']['all_passed']:continue
            text += [f'### {row["case_id"]} 的实际失败','',f'协议错误：`{json.dumps(row["protocol_error"],ensure_ascii=False)}`。','', '```json',json.dumps(row['grade']['differences'],ensure_ascii=False,indent=2),'```','']
    text += ['## 解释边界与证据','', '8条开发案例用于建立基线；10条留出案例的具体文本和期望在开发实测前冻结，最终运行前再冻结代码。案例由同一AI作者编写、非随机抽样，业务规则与开发材料有重叠，因此不能据此推断真实世界泛化或给出总体可靠率。真实脱敏材料当前0条。', '',
             '字段评分覆盖显式实体、类型、轮次、时间与精度，允许冻结时声明的合理表示；不逐字评分自由命名的事项标题、自然语言追问措辞和time_note。匹配按预期目标ID，新申请按预期实体及数据库最终状态核对。只在预定问题出现时发送已写好的补充，不把期望表交给模型。', '',
             '确认前快照独立检查业务表未变化；确认由评测脚本模拟用户执行。再次提交相同请求及相同正文再次导入分开检查。API失败会留在分母中，指标仅计算实际适用案例，各维度分母不同。', '',
             '价格依据为2026-10-28核对的[DeepSeek官方价格页](https://api-docs.deepseek.com/quick_start/pricing/)，快照在[pricing.json](../pricing.json)。估算使用实际缓存命中/未命中/输出Token的峰谷范围，不推断日历时段或账户折扣，非实际账单。未返回或不一致的用量不补0，费用覆盖不完整时保留未知部分。','',
             '数据与价格哈希见[dataset-lock.json](../dataset-lock.json)。每轮摘要包含代码哈希与依赖版本；完整源快照、逐请求HTTP、模型原输出、用户脚本、工具轨迹、执行记录及数据库快照保留在项目`.work/p9/<run-id>/`。每条摘要给出对应result.json位置。临时库删除状态由报告记录。','']
    (out/'评测结果.md').write_text('\n'.join(text));print('Evaluation summaries and Markdown report written')

if __name__=='__main__':
    import sys
    render(sys.argv[1:])
