"""Validate human-authored P1 acceptance annotations; does not run an Agent."""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo


def fingerprint(text, sent_at, application_id):
    payload = {'text': text.replace('\r\n', '\n').strip(), 'sent_at': sent_at, 'application_id': application_id}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def validate(contract, fixtures, dataset):
    errors = []

    def require(condition, message):
        if not condition:
            errors.append(message)

    def moment(value, where):
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError('missing offset')
            return parsed
        except (ValueError, TypeError):
            errors.append(f'{where}: invalid or timezone-free timestamp {value!r}')
            return None

    require(contract.get('version') == dataset.get('version'), 'Contract/dataset version mismatch')
    require(fixtures.get('material_type') in contract['material_types'], 'Unlabelled fixture material')
    require(dataset.get('material_type') in contract['material_types'], 'Unlabelled dataset material')
    applications = {a['id']: a for a in fixtures['applications']}
    tasks = {t['id']: t for t in fixtures['tasks']}
    notifications = {n['id']: n for n in fixtures['notifications']}
    require(len(applications) == len(fixtures['applications']), 'Duplicate application IDs')
    require(len(tasks) == len(fixtures['tasks']), 'Duplicate task IDs')
    require(len(notifications) == len(fixtures['notifications']), 'Duplicate notification IDs')
    for app in applications.values():
        require(app['stage'] in contract['application_stages'], f"{app['id']}: unknown stage")
        require(app['outcome'] in contract['application_outcomes'], f"{app['id']}: unknown outcome")
    for task in tasks.values():
        require(task['application_id'] in applications, f"{task['id']}: missing parent application")
        require(task['status'] in contract['task_statuses'], f"{task['id']}: unknown task status")
        require(task['type'] in contract['task_types'], f"{task['id']}: unknown task type")
        for key in contract['timestamp_fields']:
            if task.get(key) is not None:
                moment(task[key], f"{task['id']}.{key}")
    for notice in notifications.values():
        require(notice['application_id'] in applications, f"{notice['id']}: missing application")
        moment(notice['sent_at'], f"{notice['id']}.sent_at")
        require(notice['fingerprint'] == fingerprint(notice['text'], notice['sent_at'], notice['application_id']), f"{notice['id']}: fingerprint mismatch")

    seen = set()
    coverage = set()
    for case in dataset['cases']:
        cid = case['id']
        require(cid not in seen, f'{cid}: duplicate case ID')
        seen.add(cid)
        require(case.get('material_type') in contract['material_types'], f'{cid}: unlabelled material')
        context, inp, expected = case['context'], case['input'], case['expected']
        app_ids, task_ids, notice_ids = set(context['application_ids']), set(context['task_ids']), set(context['known_notification_ids'])
        require(app_ids <= applications.keys(), f'{cid}: unknown application in context')
        require(task_ids <= tasks.keys(), f'{cid}: unknown task in context')
        require(notice_ids <= notifications.keys(), f'{cid}: unknown notification in context')
        for tid in task_ids & tasks.keys():
            require(tasks[tid]['application_id'] in app_ids, f'{cid}: task outside application context')
        for nid in notice_ids & notifications.keys():
            require(notifications[nid]['application_id'] in app_ids, f'{cid}: notice outside application context')
        for rid in case['rule_ids']:
            require(rid in contract['rules'], f'{cid}: unknown rule {rid}')
            coverage.add(rid)
        if inp['sent_at'] is not None:
            moment(inp['sent_at'], f'{cid}.sent_at')
        moment(inp['imported_at'], f'{cid}.imported_at')
        try:
            ZoneInfo(inp['timezone_hint'])
        except (KeyError, TypeError):
            errors.append(f'{cid}: invalid source timezone')
        selected = inp.get('selected_application_id')
        require(selected is None or selected in app_ids, f'{cid}: selected application outside context')
        matched = expected['matched_application_id']
        require(matched is None or matched in app_ids, f'{cid}: matched application outside context')
        require(set(expected['candidate_application_ids']) <= app_ids, f'{cid}: candidate outside context')
        require(expected['disposition'] in contract['dispositions'], f'{cid}: unknown disposition')
        require(expected['preconfirmation_mutations'] == [], f'{cid}: business mutation before confirmation')
        proposing = expected['disposition'] == 'propose_actions'
        require(expected['confirmation_required'] is proposing, f'{cid}: wrong confirmation policy')
        require(bool(expected['actions']) is proposing, f'{cid}: disposition/action mismatch')
        if expected['disposition'] == 'clarify':
            require(bool(expected['questions']), f'{cid}: clarification without question')

        fields = expected['fields']
        require(set(fields) <= set(contract['field_names']), f'{cid}: unknown annotated field')
        require(set(expected['evidence']) == {name for name, value in fields.items() if value is not None}, f'{cid}: evidence/annotated-field mismatch')
        for name, value in fields.items():
            if value is None:
                require(name not in expected['evidence'], f'{cid}.{name}: unknown value with invented evidence')
                continue
            evidence = expected['evidence'].get(name)
            if not evidence:
                errors.append(f'{cid}.{name}: missing evidence')
                continue
            source = evidence['source']
            if source == 'notification':
                source_text = inp['text']
            elif source.startswith('followup:'):
                try:
                    index = int(source.split(':', 1)[1])
                    if index < 0:
                        raise IndexError
                    source_text = inp['user_followups'][index]['text']
                except (ValueError, IndexError):
                    errors.append(f'{cid}.{name}: missing followup evidence')
                    continue
            else:
                errors.append(f'{cid}.{name}: unknown evidence source')
                continue
            require(bool(evidence['quote']) and evidence['quote'] in source_text, f'{cid}.{name}: quote not found in source')
            if name in contract['timestamp_fields']:
                moment(value, f'{cid}.{name}')
            if name in contract['date_fields']:
                try:
                    date.fromisoformat(value)
                except (ValueError, TypeError):
                    errors.append(f'{cid}.{name}: invalid date')
        if fields.get('notification_type') is not None:
            require(fields['notification_type'] in contract['notification_types'], f'{cid}: unknown notification type')
        for precise, day_only in [('starts_at', 'starts_on'), ('due_at', 'due_on')]:
            require(not (fields.get(precise) and fields.get(day_only)), f'{cid}: contradictory precision annotation')

        check = case.get('calendar_check')
        if check:
            require(check['type'] == 'this_week_friday_18', f'{cid}: unknown calendar annotation')
            if inp['sent_at'] is None:
                require(fields.get(check['field']) is None and expected['disposition'] == 'clarify', f'{cid}: relative date guessed without original timestamp')
            else:
                anchor = moment(inp['sent_at'], f'{cid}.calendar_anchor')
                if anchor:
                    anchor = anchor.astimezone(ZoneInfo(inp['timezone_hint']))
                    correct = (anchor + timedelta(days=4-anchor.weekday())).replace(hour=18, minute=0, second=0, microsecond=0)
                    actual = moment(fields.get(check['field']), f'{cid}.relative_expected')
                    require(actual == correct, f'{cid}: relative date not based on original sent week')

        for operation in expected['actions']:
            kind, target, changes = operation['type'], operation['application_id'], operation['changes']
            require(kind in contract['action_types'], f'{cid}: unknown action')
            require(target in app_ids and target == matched, f'{cid}: action targets an unrelated application')
            if kind in {'update_task', 'cancel_task'}:
                tid = operation.get('task_id')
                require(tid in task_ids, f'{cid}: target task outside context')
                if tid in tasks:
                    require(tasks[tid]['application_id'] == target, f'{cid}: target task belongs to another application')
            if kind == 'create_task':
                require(changes.get('type') in contract['task_types'], f'{cid}: invalid new task type')
                require(bool(changes.get('title')), f'{cid}: missing new task title')
                require('task_id' not in operation, f'{cid}: new task must not overwrite known ID')
            if 'status' in changes:
                require(changes['status'] in contract['task_statuses'], f'{cid}: invalid task status')
            if kind == 'cancel_task':
                require(changes.get('status') == 'cancelled', f'{cid}: cancellation marked as completion')
            if 'stage' in changes:
                new_stage = changes['stage']
                require(new_stage in contract['application_stages'], f'{cid}: invalid application stage')
                if target in applications and new_stage in contract['stage_display_order']:
                    old_stage = applications[target]['stage']
                    allow_correction = expected.get('allows_stage_correction') is True and inp['kind'] == 'user_update' and 'R12' in case['rule_ids']
                    require(allow_correction or contract['stage_display_order'].index(new_stage) >= contract['stage_display_order'].index(old_stage), f'{cid}: unjustified stage regression')
            if 'outcome' in changes:
                require(changes['outcome'] in contract['application_outcomes'], f'{cid}: invalid application outcome')
                if changes['outcome'] == 'accepted':
                    require(inp['kind'] == 'user_update', f'{cid}: notification itself accepted an offer')
            timing = {}
            if operation.get('task_id') in tasks:
                timing.update(tasks[operation['task_id']])
            timing.update(changes)
            for key in contract['timestamp_fields']:
                if timing.get(key) is not None:
                    moment(timing[key], f'{cid}.action.{key}')
                if key in changes and fields.get(key) is not None:
                    require(changes[key] == fields[key], f'{cid}: proposed timestamp differs from annotation')
            for earlier, later in [('opens_at', 'due_at'), ('starts_at', 'ends_at')]:
                if timing.get(earlier) and timing.get(later):
                    left = moment(timing[earlier], f'{cid}.{earlier}')
                    right = moment(timing[later], f'{cid}.{later}')
                    if left and right:
                        require(left <= right, f'{cid}: {earlier} is after {later}')
        if expected['disposition'] == 'duplicate':
            nid = expected.get('existing_notification_id')
            require(nid in notice_ids, f'{cid}: missing known duplicate notification')
            if nid in notifications:
                require(notifications[nid]['fingerprint'] == fingerprint(inp['text'], inp['sent_at'], matched), f'{cid}: exact duplicate does not match original context')

    require(set(contract['rules']) <= coverage, 'Some business rules have no acceptance case')
    return errors


def self_check(contract, fixtures, dataset):
    mutations = []
    bad = deepcopy(dataset)
    bad['cases'][0]['expected']['actions'][0]['application_id'] = 'missing-application'
    mutations.append(('unknown_action_target', bad))
    bad = deepcopy(dataset)
    bad['cases'][0]['expected']['preconfirmation_mutations'] = [{'update': 'application'}]
    mutations.append(('business_write_before_confirmation', bad))
    bad = deepcopy(dataset)
    bad['cases'][0]['expected']['evidence']['company_name']['quote'] = '原文不存在的公司证据'
    mutations.append(('invented_evidence', bad))
    bad = deepcopy(dataset)
    bad['cases'][5]['expected']['fields']['due_at'] = '2026-11-06T18:00:00+08:00'
    bad['cases'][5]['expected']['actions'][0]['changes']['due_at'] = '2026-11-06T18:00:00+08:00'
    mutations.append(('import_week_instead_of_sent_week', bad))
    bad = deepcopy(dataset)
    bad['cases'][7]['expected']['fields']['opens_at'] = '2026-10-30T19:00:00+08:00'
    bad['cases'][7]['expected']['actions'][0]['changes']['opens_at'] = '2026-10-30T19:00:00+08:00'
    mutations.append(('opening_after_deadline', bad))
    bad = deepcopy(dataset)
    bad['cases'][11]['expected']['actions'].append({'type': 'update_application', 'application_id': 'app-yunfan-ai-fall', 'changes': {'stage': 'assessment'}})
    mutations.append(('stage_regression', bad))
    bad = deepcopy(dataset)
    bad['cases'][6]['expected']['fields']['due_at'] = '2026-10-30T18:00:00+08:00'
    bad['cases'][6]['expected']['evidence']['due_at'] = {'source': 'notification', 'quote': '本周五18:00'}
    mutations.append(('relative_date_without_anchor', bad))
    return {name: bool(validate(contract, fixtures, value)) for name, value in mutations}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--self-check', action='store_true')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    base = args.root / 'data/examples/p1'
    contract, fixtures, dataset = [json.loads((base / name).read_text()) for name in ['contract.json', 'fixtures.json', 'cases.json']]
    errors = validate(contract, fixtures, dataset)
    negative = self_check(contract, fixtures, dataset) if args.self_check and not errors else {}
    for name, detected in negative.items():
        if not detected:
            errors.append(f'Checker failed to detect deliberate mutation: {name}')
    result = {
        'version': contract['version'], 'checked_on': '2026-10-27',
        'case_count': len(dataset['cases']), 'materials': dict(Counter(c['material_type'] for c in dataset['cases'])),
        'rule_count': len(contract['rules']), 'annotation_integrity': 'passed' if not errors else 'failed',
        'negative_self_checks': negative, 'findings': errors,
        'scope': 'Checks human-authored annotations only. No database, model, Agent, or business execution.',
    }
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered)
    print(rendered, end='')
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
