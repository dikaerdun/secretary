"""Independent final review counterexamples; fresh DB and no real network."""
import asyncio

import pytest

from secretary.crm import analysis_fingerprint
from secretary.customer_store import CustomerStore
from secretary.customer_timeline import TimelineService
from secretary.exchange_workspace import ExchangeWorkspace
from secretary.profile_intelligence import ProfileIntelligence
from secretary.sales_discussion import DiscussionService
from secretary.sales_workspace import SalesWorkspace


NOW = 1791072000.0


@pytest.fixture
def review(tmp_path):
    crm = CustomerStore(tmp_path / 'independent-final-synthetic.sqlite3')
    clock = [NOW]
    sales = SalesWorkspace(crm, clock=lambda: clock[0])
    profile = ProfileIntelligence(crm, sales, clock=lambda: clock[0])
    discussion = DiscussionService(crm, sales, asyncio.Lock(), clock=lambda: clock[0])
    timeline = TimelineService(crm, sales, discussions=discussion, clock=lambda: clock[0])
    discussion.timeline = timeline
    discussion.profile_intelligence = profile
    exchange = ExchangeWorkspace(crm, sales, profile, timeline=timeline, clock=lambda: clock[0])
    unit = crm.create_customer('owner', {'name': '合成独立复审银行'}, NOW)
    person = crm.create_contact('owner', unit['id'], {'name': '合成王工', 'department': '科技部'}, NOW)
    yield crm, sales, discussion, timeline, exchange, unit, person, clock
    crm.close()


def packet(review):
    crm, _, _, _, exchange, unit, *_ = review
    record = crm.create_record('owner', {'customer_id': unit['id'], 'title': '合成现场承诺', 'content': '我答应发送接口清单。', 'kind': 'note'}, NOW)
    view = asyncio.run(exchange.prepare('owner', 'record', record['id']))
    return record, view


def request(view, *choices):
    return {'request_id': 'independent-batch', 'expected_revision': view['revision'], 'source_revision': view['source_revision'],
            'items': [{'id': item['id'], 'expected_version': item['version'], **extra} for item, extra in choices]}


def interrupt_checkpoint(exchange, monkeypatch, stage):
    original = exchange._checkpoint
    def checkpoint(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[3] == stage:
            raise KeyboardInterrupt('synthetic process interruption at durable checkpoint')
        return result
    monkeypatch.setattr(exchange, '_checkpoint', checkpoint)
    return original


def test_exchange_new_proposal_changed_after_interrupt_is_not_blindly_confirmed(review, monkeypatch):
    crm, _, _, _, exchange, _, _, clock = review
    source, view = packet(review)
    action = next(item for item in view['items'] if item['kind'] == 'action')
    schedule = next(item for item in view['items'] if item['kind'] == 'schedule')
    body = request(view, (action, {}), (schedule, {'draft': {'remind_at': NOW+3600, 'duration_minutes': 30}}))
    original = interrupt_checkpoint(exchange, monkeypatch, 'proposed')
    with pytest.raises(KeyboardInterrupt): exchange.confirm('owner', 'record', source['id'], body)
    monkeypatch.setattr(exchange, '_checkpoint', original)
    child = crm._db.execute('SELECT child_record_id FROM crm_analysis_actions').fetchone()[0]
    proposal = crm.record_detail('owner', child)['proposal']
    clock[0] += 1
    crm.execute('owner', 'human-revised-proposal', {'action': 'reschedule_proposal', 'proposal_id': proposal['id'], 'remind_at': NOW+7200}, clock[0])
    result = exchange.confirm('owner', 'record', source['id'], body)
    schedule_result = next(item for item in result['results'] if item['item_id'] == schedule['id'])
    assert schedule_result['status'] == 'conflict', schedule_result
    assert crm.record_detail('owner', child)['task'] is None
    assert crm.get_proposal('owner', proposal['id'])['remind_at'] == NOW+7200
    assert crm.get_record('owner', child)['status'] == 'following'


def test_exchange_action_terms_added_after_schedule_interrupt_are_not_blindly_used(review, monkeypatch):
    crm, _, _, _, exchange, _, _, clock = review
    source, view = packet(review)
    action = next(item for item in view['items'] if item['kind'] == 'action')
    schedule = next(item for item in view['items'] if item['kind'] == 'schedule')
    body = request(view, (action, {}), (schedule, {'draft': {'remind_at': NOW+3600, 'duration_minutes': 30}}))
    original = interrupt_checkpoint(exchange, monkeypatch, 'schedule_started')
    with pytest.raises(KeyboardInterrupt): exchange.confirm('owner', 'record', source['id'], body)
    monkeypatch.setattr(exchange, '_checkpoint', original)
    child = crm._db.execute('SELECT child_record_id FROM crm_analysis_actions').fetchone()[0]
    current = crm.get_record('owner', child)
    clock[0] += 1
    crm.save_action_terms('owner', child, {'executor_kind': 'customer', 'check_date': '2026-10-08'}, clock[0],
                          expected_updated_at=current['terms_updated_at'], explicit=True)
    result = exchange.confirm('owner', 'record', source['id'], body)
    schedule_result = next(item for item in result['results'] if item['item_id'] == schedule['id'])
    assert schedule_result['status'] == 'conflict', schedule_result
    assert crm.record_detail('owner', child)['task'] is None
    assert crm.get_record('owner', child)['action_terms']['executor_kind'] == 'customer'


def test_exchange_untouched_new_schedule_resumes_once_at_reviewed_time(review, monkeypatch):
    crm, _, _, _, exchange, _, _, _ = review
    source, view = packet(review)
    action = next(item for item in view['items'] if item['kind'] == 'action')
    schedule = next(item for item in view['items'] if item['kind'] == 'schedule')
    body = request(view, (action, {}), (schedule, {'draft': {'remind_at': NOW+3600, 'duration_minutes': 30}}))
    original = interrupt_checkpoint(exchange, monkeypatch, 'proposed')
    with pytest.raises(KeyboardInterrupt): exchange.confirm('owner', 'record', source['id'], body)
    monkeypatch.setattr(exchange, '_checkpoint', original)
    result = exchange.confirm('owner', 'record', source['id'], body)
    schedule_result = next(item for item in result['results'] if item['item_id'] == schedule['id'])
    assert schedule_result['status'] in ('confirmed', 'already_confirmed'), schedule_result
    assert crm.get_task('owner', schedule_result['result']['task_id'])['remind_at'] == NOW+3600
    assert exchange.confirm('owner', 'record', source['id'], body) == result
    assert crm._db.execute('SELECT count(*) FROM proposals').fetchone()[0] == 1
    assert crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1
    assert crm._db.execute('SELECT count(*) FROM crm_analysis_actions').fetchone()[0] == 1


def test_exchange_old_analysis_action_cannot_be_repurposed_after_interrupt(review, monkeypatch):
    crm, _, _, _, exchange, _, _, clock = review
    source, view = packet(review)
    action = next(item for item in view['items'] if item['kind'] == 'action')
    body = request(view, (action, {}))
    original = interrupt_checkpoint(exchange, monkeypatch, 'adopting')
    with pytest.raises(KeyboardInterrupt): exchange.confirm('owner', 'record', source['id'], body)
    monkeypatch.setattr(exchange, '_checkpoint', original)
    clock[0] += 1
    crm.save_analysis('owner', source['id'], {'summary': '重新核对后建议', 'input_fingerprint': analysis_fingerprint(crm.get_record('owner', source['id'])),
        'actions': [{'title': '新的建议先核对验收人', 'kind': 'suggestion', 'reason': '原话仍需核对', 'remind_at': None}]}, clock[0])
    result = exchange.confirm('owner', 'record', source['id'], body)
    assert result['results'][0]['status'] in ('conflict', 'blocked')
    assert crm._db.execute('SELECT count(*) FROM crm_analysis_actions').fetchone()[0] == 0
    assert crm._db.execute("SELECT count(*) FROM crm_records WHERE kind='action'").fetchone()[0] == 0


def test_discussion_edited_adoption_preserves_original_person_project_and_replay(review):
    crm, sales, discussion, timeline, _, unit, person, clock = review
    project = sales.create_opportunity('owner', unit['id'], {'name': '合成复审密码改造', 'contact_ids': [person['id']]})
    class Advisor:
        async def reply(self, context, history, text, now):
            return {'answer': '先核对范围，仅为建议。', 'questions': [], 'risks': [], 'next_moves': [
                {'title': '原建议先核对接口', 'reason': '范围尚需确认', 'contact_hint': '合成王工', 'preparation': '带接口清单', 'success_signal': '明确试点范围'}]}
    discussion.advisor = Advisor()
    thread = discussion.create_thread('owner', {'customer_id': unit['id'], 'contact_id': person['id'], 'opportunity_id': project['id'], 'title': '合成个人推进'})
    result = asyncio.run(discussion.send_message('owner', thread['thread']['id'], {'request_id': 'synthetic-question', 'text': '如何更好推进这次试点？'}))
    reply = next(item for item in result['messages'] if item['role'] == 'assistant')
    draft = {'title': '用户改成先核对验收负责人', 'executor_kind': 'self'}
    body = {'request_id': 'edited-adoption', 'expected_snapshot': reply['snapshot'], 'draft': draft}
    adopted = discussion.adopt('owner', thread['thread']['id'], reply['id'], 1, body)
    assert adopted['title'] == draft['title'] and '原建议先核对接口' in adopted['original_content']
    assert adopted['action_terms']['executor_kind'] == 'self'
    link = crm._db.execute("SELECT opportunity_id,customer_id FROM crm_opportunity_links WHERE owner='owner' AND entity_type='record' AND entity_id=?", (adopted['id'],)).fetchone()
    assert link['opportunity_id'] == project['id'] and link['customer_id'] == unit['id']
    assert any(item['id'] == adopted['id'] for item in timeline.view('owner', {'contact_id': person['id'], 'opportunity_id': project['id']})['summary']['open_actions'])
    crm.update_record('owner', adopted['id'], {'title': '之后人工修订的正式待办'}, NOW+1)
    replay = discussion.adopt('owner', thread['thread']['id'], reply['id'], 1, body)
    assert replay['id'] == adopted['id'] and replay['title'] == '之后人工修订的正式待办'
    with pytest.raises(ValueError): discussion.adopt('owner', thread['thread']['id'], reply['id'], 1, {**body, 'draft': {**draft, 'title': '旧稿重新覆盖'}})
    assert crm._db.execute("SELECT count(*) FROM crm_records WHERE kind='action'").fetchone()[0] == 1


def test_exchange_schedule_selection_cannot_confirm_pending_cancellation(review):
    crm, _, _, _, exchange, _, _, clock = review
    source, view = packet(review)
    action = next(item for item in view['items'] if item['kind'] == 'action')
    adopted = exchange.confirm('owner', 'record', source['id'], request(view, (action, {})))
    child = adopted['results'][0]['result']['record_id']
    crm.execute('owner', 'synthetic-initial-time', {'action': 'propose', 'title': crm.get_record('owner', child)['title'], 'remind_at': NOW+3600}, NOW)
    proposal = crm._db.execute("SELECT id FROM proposals WHERE owner='owner' ORDER BY id DESC LIMIT 1").fetchone()[0]
    crm.link_proposal('owner', child, proposal, NOW)
    crm.execute('owner', 'synthetic-initial-confirm', {'action': 'confirm', 'proposal_id': proposal}, NOW)
    task = crm.record_detail('owner', child)['task']
    clock[0] += 1
    crm.execute('owner', 'synthetic-propose-cancel', {'action': 'propose_cancel', 'task_id': task['id'], 'title': crm.get_record('owner', child)['title']}, clock[0])
    cancel = crm._db.execute("SELECT id FROM proposals WHERE owner='owner' ORDER BY id DESC LIMIT 1").fetchone()[0]
    crm.link_proposal('owner', child, cancel, clock[0])
    view = exchange.get('owner', 'record', source['id'])
    schedule = next(item for item in view['items'] if item['kind'] == 'schedule')
    assert schedule['current']['proposal']['change_kind'] == 'cancel'
    body = request(view, (schedule, {})); body['request_id'] = 'cannot-cancel-from-schedule'
    result = exchange.confirm('owner', 'record', source['id'], body)
    assert result['results'][0]['status'] in ('blocked', 'conflict'), result['results'][0]
    assert crm.get_task('owner', task['id'])['status'] == 'pending'
    assert crm.get_proposal('owner', cancel)['status'] == 'pending'
