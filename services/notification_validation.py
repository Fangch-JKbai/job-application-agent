"""Check result shape/evidence against this notification, not global user history."""
from schemas.notifications import ExtractionResult
from agent.notification_extractor import ExtractionFailure

TIMES = ['opens_at','starts_at','ends_at','due_at','starts_on','due_on']


def validate_evidence(result:ExtractionResult,notification,review_text=None):
    sources = {'text':notification['text']}
    if notification['timezone_hint']:
        sources['timezone_hint'] = notification['timezone_hint']
    for field in ['company_name','position_name','batch']:
        value = notification['hints'].get(field)
        if value:
            sources[field.removesuffix('_name')+'_hint' if field != 'batch' else 'batch_hint'] = value
    if review_text is not None:
        sources['review'] = review_text

    def check(evidence,label):
        if evidence is None:
            raise ExtractionFailure(f'{label}缺少原文依据')
        if evidence.source not in sources or evidence.quote not in sources[evidence.source]:
            raise ExtractionFailure(f'{label}的证据不在对应来源中')

    for label,evidence in result.evidence.items():
        check(evidence,label)
    for field in ['company_name','position_name','batch']:
        value = getattr(result,field)
        if value is not None:
            evidence = result.evidence.get(field)
            if evidence is None:
                raise ExtractionFailure(f'{field}缺少原文依据')
            if value not in evidence.quote:
                raise ExtractionFailure(f'{field}不是所引用的原始名称')
    if result.notification_type != 'other':
        check(result.evidence.get('notification_type'),'通知类型')
    for index,task in enumerate(result.tasks):
        label = f'事项{index+1}'
        for field,evidence in task.evidence.items():
            check(evidence,label+'.'+field)
        for field in ['type','title','round',*TIMES]:
            value = getattr(task,field)
            evidence = task.evidence.get(field)
            if value is not None and evidence is None:
                raise ExtractionFailure(f'{label}.{field}缺少原文依据')
            if field in TIMES and value is not None:
                basis = task.time_basis.get(field)
                if basis not in {'explicit','relative'}:
                    raise ExtractionFailure(f'{label}.{field}缺少明确的时间依据类型')
                if basis == 'relative' and notification['sent_at'] is None:
                    raise ExtractionFailure('原通知发送时间未知，不能推算相对日期')
        if task.source_timezone and task.source_timezone != notification['timezone_hint']:
            check(task.evidence.get('source_timezone'),label+'.source_timezone')
    return result
