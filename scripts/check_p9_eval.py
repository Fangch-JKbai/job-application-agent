"""Offline oracle tests use golden data and deliberate corruptions, never a model."""
import json,sys
from copy import deepcopy
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from evaluation.grading import compare,expected_business,grade,summary,token_cost,verify_dataset
from schemas.notifications import NotificationType
from typing import get_args
ROOT=Path(__file__).resolve().parents[1];DATA=ROOT/'evals/p9'
def pick(value):
    if isinstance(value,dict):
        if set(value)=={'one_of'}:return pick(value['one_of'][0])
        return {k:pick(v) for k,v in value.items()}
    if isinstance(value,list):return [pick(v) for v in value]
    return value

def golden(case):
    ids={row['key']:'id:'+row['key'] for row in case['applications']}
    e=case['expected'];ready=e['final_status']=='ready';pid=ids.get(e['target'])
    proposal={'payload':{'application_id':pid,'new_application':{'company_name':'new'} if e['target']=='new:0' else None}}
    return {'app_ids':ids,'extraction':{'status':'completed','result':pick(e['extraction'])},'first':{'session':{'status':e['first_status'],'state':{'questions':[{'field':e['question_fields'][0]}] if e['question_fields'] else []}}},
        'last':{'session':{'status':e['final_status']},'proposal':proposal if ready else None},'preconfirmation_unchanged':True,'final_business':pick(expected_business(case)),
        'unrelated_records_preserved':True,'replays_ok':True,'confirmation_ok':ready,'review_preserved':True,'duplicate_ok':True,'elapsed_ms':10}

def checks():
    passed=[];verify_dataset(DATA)
    cases=[c for split in ['development','holdout'] for c in json.loads((DATA/(split+'.json')).read_text())['cases']]
    assert len(cases)==18 and len({c['id'] for c in cases})==18
    for c in cases:
        assert c['material_type']=='synthetic' and c['expected']['extraction']['notification_type'] in get_args(NotificationType)
        assert grade(c,golden(c))['all_passed'],c['id']
        for answer in c['answers']:assert set(answer['answer_fields']) & set(c['expected']['question_fields'])
    passed.append('frozen_8_development_10_holdout_contracts_and_golden_oracles_are_valid')
    base=cases[0]
    for name,mutate,metric in [
      ('wrong_clock',lambda r:r['extraction']['result']['tasks'][0].update(due_at='2026-10-31T10:00:00Z'),'extraction_fields'),
      ('wrong_target',lambda r:r['last']['proposal']['payload'].update(application_id='foreign'),'application_match'),
      ('early_write',lambda r:r.update(preconfirmation_unchanged=False),'no_preconfirmation_business_writes'),
      ('wrong_db_result',lambda r:r['final_business']['applications']['a'].update(outcome='accepted'),'database_final_state'),
      ('unrelated_edit',lambda r:r.update(unrelated_records_preserved=False),'unrelated_records_preserved'),
      ('request_duplicate',lambda r:r.update(replays_ok=False),'request_replay_no_extra_work'),
      ('no_confirmation',lambda r:r.update(confirmation_ok=False),'confirmed_and_replayed_once'),
      ('protocol_failure',lambda r:r.update(protocol_error={'message':'stopped'}),'protocol_completed')]:
        r=golden(base);mutate(r);assert grade(base,r)['metrics'][metric] is False;passed.append('oracle_rejects_'+name)
    r=golden(cases[1]);r['first']['session']['state']['questions']=[];assert not grade(cases[1],r)['metrics']['first_turn_and_questions'];passed.append('question_grade_does_not_pass_on_status_alone')
    assert not compare('2026-10-30T18:00:00+08:00','2026-10-30T10:00:00Z','due_at');passed.append('equivalent_offsets_compare_as_same_instant')
    assert compare(None,'2026-11-06T00:00:00+08:00','starts_at');passed.append('unknown_time_is_not_equivalent_to_midnight')
    assert compare({'due_at':None},{},'task');passed.append('missing_key_is_not_silently_treated_as_explicit_null')
    case=next(c for c in cases if c.get('duplicate_notification'));r=golden(case);r['duplicate_ok']=False;assert not grade(case,r)['metrics']['semantic_duplicate_no_extra_business_writes'];passed.append('duplicate_notification_mutation_is_scored_as_failure')
    case=next(c for c in cases if c.get('review'));r=golden(case);r['review_preserved']=False;assert not grade(case,r)['metrics']['manual_review_preserves_original'];passed.append('manual_review_cannot_hide_original_output_rewrite')
    price=json.loads((DATA/'pricing.json').read_text());call={'provider':'deepseek','model':'deepseek-flash','usage':{'prompt_tokens':100,'completion_tokens':10,'total_tokens':110,'prompt_cache_hit_tokens':80,'prompt_cache_miss_tokens':20}}
    cost=token_cost([call],price);assert cost['estimated_observed_cost_usd_range']==['0.00000924','0.00001848'] and cost['cost_complete'];passed.append('cached_uncached_and_output_prices_produce_exact_decimal_range')
    call['usage']['prompt_tokens']=99;assert token_cost([call],price)['estimated_observed_cost_usd_range'] is None;passed.append('inconsistent_cache_split_is_not_invented_for_cost_estimation')
    cost=token_cost([{'usage':None}],price);assert cost['tokens'] is None and not cost['usage_complete'] and cost['estimated_observed_cost_usd_range'] is None;passed.append('missing_usage_remains_unknown_instead_of_zero')
    r=golden(base);r.update(case_id='x');r['grade']=grade(base,r);s=summary([r]);assert s['passed']==1 and s['metrics']['extraction_fields']['evaluated']==1;passed.append('summary_denominators_are_observed_evaluations')
    return {'mode':'offline deterministic grader with explicit golden fixtures; no HTTP or model','checks_passed':len(passed),'checks':passed,'real_model_called':False}
if __name__=='__main__':
    report=checks();path=ROOT/'.work/p9';path.mkdir(parents=True,exist_ok=True);(path/'grader-validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print(json.dumps(report,ensure_ascii=False,indent=2))
