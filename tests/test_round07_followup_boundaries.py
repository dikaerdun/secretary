"""Fresh real follow-up services/handlers; no runtime config/provider/server."""
import asyncio
import copy
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from secretary.crm import analysis_fingerprint
from secretary.customer_store import CustomerStore
from secretary.progress_workspace import ProgressConflict, ProgressWorkspace
from secretary.store import SHANGHAI, Store
from secretary.web import WebCRM, hash_password


NOW = datetime(2026, 10, 4, 10, tzinfo=SHANGHAI).timestamp()
OWNER = 'r07-formal-synthetic'


@pytest.fixture
def world(tmp_path):
    path = tmp_path / 'r07-fresh.sqlite3'
    crm, store = CustomerStore(path), Store(path)
    clock = [NOW]
    web = WebCRM(store, crm, asyncio.Lock(), OWNER, hash_password('public-r07-fixture'), clock=lambda: clock[0])
    unit = crm.create_customer(OWNER, {'name': 'R07合成安全银行'}, NOW)
    person = crm.create_contact(OWNER, unit['id'], {'name': '林工'}, NOW)
    a = web.sales_workspace.create_opportunity(OWNER, unit['id'], {'name': 'A数据库加密'})
    b = web.sales_workspace.create_opportunity(OWNER, unit['id'], {'name': 'B密钥管理'})
    w = SimpleNamespace(crm=crm, store=store, web=web, progress=web.progress_workspace,
                        sales=web.sales_workspace, unit=unit, person=person, a=a, b=b, clock=clock)
    yield w
    crm.close()
    store.close()


def action(w, title='提供产品资料并等待采购反馈', executor='customer'):
    record = w.crm.create_record(OWNER, {'title': title, 'content': title, 'kind': 'action',
                                 'status': 'following', 'customer_id': w.unit['id']}, NOW)
    return w.crm.save_action_terms(OWNER, record['id'], {'executor_kind': executor, 'duration_minutes': None}, NOW)


def proposal(w, row, *, when=NOW+7200, confirm=True):
    w.crm.execute(OWNER, 'proposal-'+str(row['id'])+'-'+str(when),
                  {'action': 'propose', 'title': row['title'], 'remind_at': when, 'duration_minutes': 30}, NOW)
    pid = w.crm._db.execute('SELECT max(id) FROM proposals WHERE owner=?', (OWNER,)).fetchone()[0]
    w.crm.link_proposal(OWNER, row['id'], pid, NOW)
    if confirm:
        w.crm.execute(OWNER, 'confirm-'+str(pid), {'action': 'confirm', 'proposal_id': pid}, NOW)
    return w.crm.get_proposal(OWNER, pid)


def business(w):
    return {name: [tuple(row) for row in w.crm._db.execute('SELECT * FROM '+name+' ORDER BY rowid')]
            for name in ('crm_records', 'crm_action_terms', 'tasks', 'proposals', 'notifications',
                         'crm_record_proposals', 'crm_activities', 'crm_action_outcomes',
                         'crm_opportunity_links', 'crm_opportunity_link_history',
                         'crm_timeline_contexts', 'crm_timeline_context_history', 'crm_progress_feedback')}


def request(identifier, body=None):
    class Request:
        match_info = {'id': str(identifier)}
        path = '/api/records/'+str(identifier)+'/complete-outcome'
        method = 'POST'

        async def read(self):
            return json.dumps(body, ensure_ascii=False).encode()

        async def json(self):
            return body
    return Request()


def get(w, row):
    return json.loads(asyncio.run(w.web.record(request(row['id']))).body)


def finish(w, row, **extra):
    body = {'request_id': 'finish-'+str(row['id']), 'result': '本轮资料已经交付',
            'expected_snapshot': get(w, row)['completion_snapshot'], **extra}
    return body, json.loads(asyncio.run(w.web.complete_outcome(request(row['id'], body))).body)


def prepare(w, row, text='已提供产品资料，采购意见尚未返回。', legacy=False):
    run = w.progress.create(OWNER, {'request_id': 'prepare-'+str(row['id']), 'kind': 'followup_result',
                   'customer_id': w.unit['id'], 'record_id': row['id'], 'text': text})
    if legacy:
        with w.crm._transaction() as db:
            db.execute('UPDATE crm_progress_runs SET followup_prefill_json=NULL WHERE id=?', (run['id'],))
    asyncio.run(w.progress.process_pending(OWNER))
    run = w.progress.get(OWNER, run['id'])
    assert run['status'] == 'ready'
    return run


def edit(w, run, **draft):
    item = next(item for item in run['items'] if item['type'] == 'outcome')
    return w.progress.edit_draft(OWNER, run['id'], {'expected_revision': run['revision'],
                  'items': [{'id': item['id'], 'selected': True, 'draft': draft}]})


def adoption(run, request_id='adopt'):
    return {'request_id': request_id, 'expected_revision': run['revision'],
            'items': [{'id': item['id'], 'expected_item_revision': item['revision'],
                       'expected_snapshot': item['versions']['snapshot']}
                      for item in run['items'] if item['selected']]}


def reply(title='核对试点验收口径', moves=3):
    return {'answer': '建议仍需核对，采购没有承诺。', 'questions': [], 'risks': [],
            'next_moves': [{'title': title+str(i), 'reason': '还有依赖条件', 'contact_hint': '具体责任待核对',
                            'preparation': '带接口边界清单', 'success_signal': '获得明确验收条件'}
                           for i in range(1, moves+1)]}


class Advisor:
    def __init__(self, value):
        self.value, self.calls = value, 0

    async def reply(self, *args):
        self.calls += 1
        return copy.deepcopy(self.value)


@pytest.mark.parametrize('change', ['terms', 'task', 'project', 'kind', 'status'])
def test_real_quick_completion_rejects_changed_business_atomically(world, change):
    w = world; row = action(w); p = proposal(w, row)
    w.sales.link(OWNER, 'record', row['id'], w.a['id'])
    token = get(w, row)['completion_snapshot']
    if change == 'terms':
        w.crm.save_action_terms(OWNER, row['id'], {'executor_kind': 'self', 'check_date': '2026-10-09'},
                NOW+1, expected_updated_at=row['terms_updated_at'], explicit=True)
    elif change == 'task':
        w.crm.execute(OWNER, 'move-current', {'action': 'snooze', 'task_id': p['task_id'], 'remind_at': NOW+14400}, NOW+1)
    elif change == 'project':
        w.sales.link(OWNER, 'record', row['id'], w.b['id'])
    else:
        w.crm.update_record(OWNER, row['id'], {change: 'note' if change == 'kind' else 'unfiled'}, NOW+1)
    assert get(w, row)['completion_snapshot'] != token
    before = business(w)
    with pytest.raises(ValueError):
        asyncio.run(w.web.complete_outcome(request(row['id'], {'request_id': 'old-view',
            'result': '按原来A项目完成', 'next_step': '继续A项目评审', 'expected_snapshot': token})))
    assert business(w) == before


def test_completion_token_includes_older_linked_task_and_pending_proposal(world):
    w = world; row = action(w)
    first = proposal(w, row)
    second = proposal(w, row, when=NOW+14400)
    token = get(w, row)['completion_snapshot']
    w.crm.execute(OWNER, 'move-older', {'action': 'snooze', 'task_id': first['task_id'], 'remind_at': NOW+21600}, NOW+1)
    assert get(w, row)['completion_snapshot'] != token
    before = business(w)
    with pytest.raises(ValueError):
        w.sales.complete_record(OWNER, row['id'], {'request_id': 'old-history', 'expected_completion_snapshot': token})
    assert business(w) == before and w.crm.get_task(OWNER, second['task_id'])['status'] == 'pending'
    pending = proposal(w, row, when=NOW+28800, confirm=False)
    token = get(w, row)['completion_snapshot']
    w.crm.execute(OWNER, 'revise-pending', {'action': 'reschedule_proposal', 'proposal_id': pending['id'],
                                         'remind_at': NOW+32400, 'duration_minutes': 45}, NOW+2)
    assert get(w, row)['completion_snapshot'] != token


@pytest.mark.parametrize('token', [None, '', 'bad', '0'*64])
def test_new_web_completion_requires_current_dedicated_seen_token(world, token):
    w = world; row = action(w); before = business(w)
    with pytest.raises(ValueError):
        asyncio.run(w.web.complete_outcome(request(row['id'], {'request_id': 'invalid', 'result': '资料发了', 'expected_snapshot': token})))
    assert business(w) == before


def test_source_fingerprint_stays_internal_and_owner_snapshot_is_read_only(world):
    w = world; row = action(w); p = proposal(w, row)
    before = business(w); token = get(w, row)['completion_snapshot']
    assert token != analysis_fingerprint(row) and business(w) == before
    with pytest.raises(KeyError):
        w.sales.completion_snapshot('another-owner', row['id'])
    with pytest.raises(ValueError):
        asyncio.run(w.web.complete_outcome(request(row['id'], {'request_id': 'source-bypass',
                'expected_snapshot': analysis_fingerprint(row)})))
    assert business(w) == before
    done = w.sales.complete_record(OWNER, row['id'], {'request_id': 'legacy-internal',
                'expected_snapshot': analysis_fingerprint(row), 'result': '原内部调用仍明确完成'})
    assert done['record']['status'] == 'done' and w.crm.get_task(OWNER, p['task_id'])['status'] == 'completed'


@pytest.mark.parametrize('executor', ['self', 'customer', 'team'])
def test_quick_next_responsibility_is_explicit_child_only_and_replay_safe(world, executor):
    w = world; row = action(w); original_terms = row['action_terms']
    body, done = finish(w, row, next_step='下一步核对验收条件', next_executor_kind=executor)
    child = done['next_record']
    assert child['action_terms']['executor_kind'] == executor and child['proposal_id'] is None
    assert w.crm.get_record(OWNER, row['id'])['action_terms'] == original_terms
    w.crm.save_action_terms(OWNER, child['id'], {'executor_kind': 'unknown', 'check_date': '2026-10-10'},
            NOW+2, explicit=True, expected_updated_at=child['terms_updated_at'])
    before = business(w)
    replay = json.loads(asyncio.run(w.web.complete_outcome(request(row['id'], body))).body)
    assert replay['next_record']['action_terms']['executor_kind'] == 'unknown' and business(w) == before
    with pytest.raises(ValueError):
        asyncio.run(w.web.complete_outcome(request(row['id'], {**body, 'next_executor_kind': 'unknown'})))
    assert business(w) == before


def test_unknown_next_executor_keeps_legacy_signature_and_natural_replay(world):
    w = world; row = action(w)
    body, done = finish(w, row, next_step='继续核对技术边界')
    assert done['next_record']['action_terms'] == {}
    replay = json.loads(asyncio.run(w.web.complete_outcome(request(row['id'],
                {**body, 'request_id': 'another-click', 'next_executor_kind': 'unknown'}))).body)
    assert replay['next_record']['id'] == done['next_record']['id']
    assert w.crm._db.execute('SELECT COUNT(*) FROM crm_action_outcomes').fetchone()[0] == 1


@pytest.mark.parametrize('executor', ['self', 'customer', True, None, 'manager'])
def test_invalid_next_responsibility_without_content_is_atomic(world, executor):
    w = world; row = action(w); proposal(w, row); before = business(w)
    with pytest.raises(ValueError):
        finish(w, row, next_executor_kind=executor)
    assert business(w) == before


def test_only_complete_slot_requires_recheck_but_does_not_complete_business(world):
    w = world; row = action(w); p = proposal(w, row)
    seen = get(w, row)['completion_snapshot']
    w.crm.execute(OWNER, 'slot-only', {'action': 'complete', 'task_id': p['task_id']}, NOW)
    assert w.crm.get_record(OWNER, row['id'])['status'] == 'following'
    assert any(item.get('record_id') == row['id'] for item in w.sales.priorities(OWNER)['items'])
    with pytest.raises(ValueError):
        w.sales.complete_record(OWNER, row['id'], {'request_id': 'old-slot', 'expected_completion_snapshot': seen})
    _, done = finish(w, row, next_step='保持下一步未排期')
    assert done['record']['status'] == 'done' and done['next_record']['proposal_id'] is None


@pytest.mark.parametrize('length', [4000, 4001, 19999])
def test_long_result_preserves_full_original_and_tail_with_read_only_metadata(world, length):
    w = world; row = action(w)
    text = '头'+('核'*(length-8))+'仍未完成原项。'
    run = prepare(w, row, text)
    item = run['items'][0]
    assert run['text'] == text and len(item['draft']['result']) <= 4000 and item['draft']['decision'] == 'continue'
    if len(text) > 4000:
        assert item['draft']['result'].endswith('仍未完成原项。')
        assert item['result_excerpt']['strategy'] == 'head_tail'
    else:
        assert item['draft']['result'] == text and 'result_excerpt' not in item
    changes = w.crm._db.total_changes
    assert w.progress.get(OWNER, run['id']) == run and w.crm._db.total_changes == changes
    edited = edit(w, run, result='我手工核对：只完成产品资料，采购仍未反馈。')
    assert 'result_excerpt' not in edited['items'][0]
    queued = w.progress.retry(OWNER, run['id'], {'expected_revision': edited['revision']})
    asyncio.run(w.progress.process_pending(OWNER))
    assert w.progress.get(OWNER, queued['id'])['items'][0]['draft']['result'] == edited['items'][0]['draft']['result']


def test_legacy_prefix_is_disclosed_without_rewriting_and_oversize_edit_preserved(world):
    w = world; row = action(w); text = '核'*4500+'仍未完成，继续核对'
    run = prepare(w, row, text)
    items = run['items']; items[0]['draft']['result'] = text[:4000]
    with w.crm._transaction() as db:
        db.execute('UPDATE crm_progress_runs SET items_json=? WHERE id=?', (json.dumps(items), run['id']))
    before = business(w); changes = w.crm._db.total_changes
    legacy = w.progress.get(OWNER, run['id'])
    assert legacy['items'][0]['result_excerpt']['strategy'] == 'legacy_prefix'
    assert legacy['items'][0]['draft']['result'] == text[:4000] and w.crm._db.total_changes == changes
    with pytest.raises(ValueError):
        edit(w, legacy, result='核'*4001)
    assert w.progress.get(OWNER, run['id'])['items'][0]['draft'] == legacy['items'][0]['draft']
    assert business(w) == before and legacy['text'] == text


def test_new_followup_first_ai_move_prefills_outcome_without_duplicate_action(world):
    w = world; row = action(w); w.progress._advisor = Advisor(reply())
    before = business(w); run = prepare(w, row)
    assert business(w) == before
    assert [item['id'] for item in run['items']] == ['outcome:'+str(row['id']), 'move:2', 'move:3']
    outcome = run['items'][0]
    assert not outcome['selected'] and outcome['draft']['decision'] == 'continue'
    assert outcome['draft']['next_title'] == '核对试点验收口径1'
    assert 'AI建议' in outcome['draft']['next_step'] and outcome['draft']['executor_kind'] == 'unknown'
    assert all(outcome['draft'][key] is None for key in ('remind_at', 'deadline_at', 'check_at', 'duration_minutes'))
    assert run['followup_prefill']['nature'] == 'model_suggestion'
    run = edit(w, run, decision='waiting', executor_kind='self')
    payload = adoption(run)
    result = w.progress.confirm(OWNER, run['id'], payload)
    assert result['status'] == 'complete' and w.crm.get_record(OWNER, row['id'])['status'] == 'following'
    child = result['results'][0]['next_record_id']
    w.progress.confirm(OWNER, run['id'], payload)
    assert w.crm._db.execute('SELECT COUNT(*) FROM crm_records WHERE parent_record_id=?', (row['id'],)).fetchone()[0] == 1
    assert w.crm.get_record(OWNER, child)['proposal_id'] is None


@pytest.mark.parametrize('next_step', ['', '手工确认只问一个具体试点边界'])
def test_ai_prefill_retry_preserves_manual_clear_initial_metadata_and_indices(world, next_step):
    w = world; row = action(w); w.progress._advisor = Advisor(reply())
    run = prepare(w, row); initial = run['followup_prefill']
    edited = edit(w, run, next_title='' if not next_step else '手工下一步', next_step=next_step)
    w.progress._advisor = Advisor(reply('这次新的AI提议'))
    w.progress.retry(OWNER, run['id'], {'expected_revision': edited['revision']})
    asyncio.run(w.progress.process_pending(OWNER))
    reopened = ProgressWorkspace(w.crm, w.sales, w.web.discussions, clock=lambda: NOW)
    current = reopened.get(OWNER, run['id'])
    assert current['items'][0]['draft'] == edited['items'][0]['draft'] and not current['items'][0]['selected']
    assert current['followup_prefill'] == initial
    assert [item['id'] for item in current['items'][1:]] == ['move:2', 'move:3']
    assert w.crm.list_records(OWNER)['total'] == 1


def test_rules_no_move_has_no_fake_ai_and_legacy_adopted_first_move_is_retained(world):
    w = world; empty = prepare(w, action(w))
    assert empty['followup_prefill']['initial_next_move'] is None
    assert empty['followup_prefill']['nature'] == 'rule_preparation'
    assert empty['items'][0]['draft']['next_step'] == ''
    row = action(w, '旧准备任务'); w.progress._advisor = Advisor(reply())
    legacy = prepare(w, row, legacy=True)
    assert 'followup_prefill' not in legacy and legacy['items'][1]['id'] == 'move:1'
    move = legacy['items'][1]
    legacy = w.progress.edit_draft(OWNER, legacy['id'], {'expected_revision': legacy['revision'],
             'items': [{'id': move['id'], 'selected': True}]})
    result = w.progress.confirm(OWNER, legacy['id'], adoption(legacy))
    adopted = next(item for item in result['run']['items'] if item['id'] == 'move:1')
    record_id = adopted['receipt']['record_id']
    before = business(w)
    w.progress._advisor = Advisor(reply('新的独立建议'))
    w.progress.retry(OWNER, legacy['id'], {'expected_revision': result['run']['revision']})
    asyncio.run(w.progress.process_pending(OWNER))
    current = w.progress.get(OWNER, legacy['id'])
    assert 'followup_prefill' not in current
    assert next(item for item in current['items'] if item['id'] == 'move:1')['receipt']['record_id'] == record_id
    assert current['items'][0]['draft']['next_step'] == '' and business(w) == before


def split(w, row, **extra):
    run = prepare(w, row)
    return edit(w, run, decision='partial', completed_title='已兑现产品资料', completed_part='产品资料已交付。',
                remaining_title='等待采购反馈', remaining_part='采购仍需反馈试点采购牵头人。', **extra)


@pytest.mark.parametrize('executor', ['', 'self', 'unknown'])
def test_partial_has_one_remaining_obligation_and_done_child_preserves_source_people(world, executor):
    w = world; row = action(w); w.sales.link(OWNER, 'record', row['id'], w.a['id'])
    w.crm.save_transcript(OWNER, row['id'], row['original_content'], row['content'], NOW)
    event = w.web.timeline.get_event(OWNER, 'record:'+str(row['id']))
    w.web.timeline.save_context(OWNER, event['key'], {'expected_revision': event['revision'], 'kind': 'reflection',
        'occurred_at': None, 'contact_relations': [{'contact_id': w.person['id'], 'relation': 'about'}]})
    run = split(w, row, remaining_executor_kind=executor, next_step='AI另一个提议不得当第三事项')
    payload = adoption(run); result = w.progress.confirm(OWNER, run['id'], payload)
    assert result['status'] == 'complete'
    receipt = result['results'][0]; parent = w.crm.get_record(OWNER, row['id'])
    done = w.crm.get_record(OWNER, receipt['completed_record_id'])
    assert receipt['remaining_record_id'] == row['id'] and parent['status'] == 'following'
    assert parent['title'] == '等待采购反馈' and parent['original_content'] == row['original_content']
    assert parent['content'] == '采购仍需反馈试点采购牵头人。'
    assert parent['action_terms']['executor_kind'] == (executor or 'customer')
    assert done['status'] == 'done' and done['parent_record_id'] == row['id']
    assert done['action_terms']['duration_minutes'] is None and done['proposal_id'] is None
    current_event = w.web.timeline.get_event(OWNER, event['key'])
    done_event = w.web.timeline.get_event(OWNER, 'record:'+str(done['id']))
    assert not current_event['needs_review'] and current_event['opportunity_id'] == w.a['id']
    assert current_event['contact_relations'][0]['contact_id'] == w.person['id']
    assert done_event['kind'] == 'result' and done_event['occurred_at'] is None
    assert done_event['contact_relations'][0]['relation'] == 'about'
    assert w.crm.list_records(OWNER)['total'] == 2
    transcript = w.crm._db.execute('SELECT * FROM crm_record_transcripts WHERE record_id=?', (row['id'],)).fetchone()
    assert transcript['original_text'] == row['original_content'] and transcript['corrected_text'] == parent['content']
    assert parent['updated_at'] > row['updated_at']
    w.crm.update_record(OWNER, row['id'], {'content': '后续用户亲自更正剩余范围'}, NOW+3)
    before = business(w)
    replay = w.progress.confirm(OWNER, run['id'], payload)
    assert replay['replayed'] and business(w) == before
    assert w.crm.get_record(OWNER, row['id'])['content'] == '后续用户亲自更正剩余范围'


@pytest.mark.parametrize('confirmed', [False, True])
def test_partial_blocks_pending_old_arrangement_with_useful_receipt(world, confirmed):
    w = world; row = action(w); proposal(w, row, confirm=confirmed)
    run = split(w, row); before = business(w)
    result = w.progress.confirm(OWNER, run['id'], adoption(run))
    assert result['status'] == 'blocked' and '先核对旧安排' in result['results'][0]['message']
    assert business(w) == before


def test_partial_second_write_failure_rolls_back_and_same_request_recovers(world, monkeypatch):
    w = world; row = action(w); run = split(w, row); payload = adoption(run)
    before = business(w); real = w.web.timeline.save_context; calls = [0]
    def interrupt(*args, **kwargs):
        calls[0] += 1
        if calls[0] == 2:
            raise RuntimeError('Synthetic failure after parent/context write')
        return real(*args, **kwargs)
    monkeypatch.setattr(w.web.timeline, 'save_context', interrupt)
    first = w.progress.confirm(OWNER, run['id'], payload)
    assert first['results'][0]['status'] == 'failed' and business(w) == before
    monkeypatch.setattr(w.web.timeline, 'save_context', real)
    recovered = w.progress.confirm(OWNER, run['id'], payload)
    assert recovered['status'] == 'complete' and w.crm.list_records(OWNER)['total'] == 2
    w.progress.confirm(OWNER, run['id'], payload)
    assert w.crm.list_records(OWNER)['total'] == 2


def test_partial_invalid_two_parts_or_stale_source_cannot_change_parent(world):
    w = world; row = action(w); run = split(w, row)
    invalid = edit(w, run, completed_part=''); before = business(w)
    result = w.progress.confirm(OWNER, run['id'], adoption(invalid))
    assert result['status'] == 'blocked' and business(w) == before
    repaired = edit(w, result['run'], completed_part='实际交付资料')
    w.crm.update_record(OWNER, row['id'], {'content': '另一页更正了复合原事项'}, NOW+1)
    before = business(w)
    with pytest.raises(ProgressConflict):
        w.progress.confirm(OWNER, run['id'], adoption(repaired, 'stale-partial'))
    assert business(w) == before


def test_partial_rejects_identical_obligation_without_half_split(world):
    w = world; row = action(w); run = split(w, row)
    run = edit(w, run, completed_part='同一采购回复', remaining_part='同一采购回复')
    before = business(w)
    result = w.progress.confirm(OWNER, run['id'], adoption(run))
    assert result['status'] == 'blocked' and '不能完全相同' in result['results'][0]['message']
    assert business(w) == before


def test_partial_keeps_reviewed_parent_dates_but_done_child_has_no_dates(world):
    w = world; row = action(w)
    row = w.crm.save_action_terms(OWNER, row['id'], {'executor_kind': 'customer', 'check_date': '2026-10-08',
             'deadline_date': '2026-10-10', 'check_evidence': '原义务检查日期'}, NOW+1,
             expected_updated_at=row['terms_updated_at'], explicit=True)
    run = split(w, row)
    assert run['items'][0]['current']['terms']['check_date'] == '2026-10-08'
    result = w.progress.confirm(OWNER, run['id'], adoption(run))
    assert result['status'] == 'complete'
    parent = w.crm.get_record(OWNER, row['id'])
    done = w.crm.get_record(OWNER, result['results'][0]['completed_record_id'])
    assert parent['action_terms'] == row['action_terms']
    assert not any(key.endswith('_at') or key.endswith('_date') for key in done['action_terms'])
    assert w.crm._db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0


def test_cancelled_late_ai_reply_does_not_persist_initial_prefill(world):
    w = world; row = action(w)
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        class SlowAdvisor:
            async def reply(self, *args):
                entered.set(); await release.wait(); return reply()
        w.progress._advisor = SlowAdvisor()
        run = w.progress.create(OWNER, {'request_id': 'cancel-prefill', 'kind': 'followup_result',
                 'customer_id': w.unit['id'], 'record_id': row['id'], 'text': '原动作未全部兑现。'})
        processing = asyncio.create_task(w.progress.process_pending(OWNER))
        await asyncio.wait_for(entered.wait(), 2)
        visible = w.progress.get(OWNER, run['id'])
        w.progress.cancel(OWNER, run['id'], {'expected_revision': visible['revision']})
        release.set(); await processing
        cancelled = w.progress.get(OWNER, run['id'])
        assert cancelled['status'] == 'cancelled' and cancelled['items'] == []
        assert cancelled['followup_prefill']['initial_captured'] is False
    before = business(w); asyncio.run(scenario())
    assert business(w) == before


@pytest.mark.parametrize('change,phrase', [('content', '版本'), ('customer', '单位'), ('archive', '归档')])
def test_web_project_stale_reason_is_owner_safe_and_does_not_guess_field_delta(world, change, phrase):
    w = world; row = action(w); w.sales.link(OWNER, 'record', row['id'], w.a['id'])
    if change == 'content':
        w.crm.update_record(OWNER, row['id'], {'content': '当前用户改了推进说明'}, NOW+1)
    elif change == 'customer':
        unit = w.crm.create_customer(OWNER, {'name': '本owner另一个单位'}, NOW)
        w.crm.update_record(OWNER, row['id'], {'customer_id': unit['id']}, NOW+1)
    else:
        w.sales.update_opportunity(OWNER, w.unit['id'], w.a['id'], {'expected_revision': 1, 'archived': True})
    before = business(w); data = get(w, row)
    assert data['record']['project_link_stale'] is True
    assert phrase in data['project_link_stale_reason'] and 'opportunity_name' not in data['record']
    assert data['record']['project_link_stale_reason'] == data['project_link_stale_reason']
    assert business(w) == before
    with pytest.raises(KeyError):
        w.sales.completion_snapshot('other-owner', row['id'])
