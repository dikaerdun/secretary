"""Archived sources never re-enter active AI work; all data is synthetic."""
import asyncio
import copy
import json

import pytest

from secretary.capture_inbox import CaptureService
from secretary.coaching_service import CoachingService
from secretary.customer_store import CustomerStore
from secretary.profile_intelligence import ProfileConflict, ProfileIntelligence
from secretary.progress_workspace import ProgressWorkspace
from secretary.review_queue import ReviewQueue
from secretary.record_lifecycle import RecordLifecycle
from secretary.sales_discussion import DiscussionService
from secretary.sales_workspace import SalesWorkspace


NOW = 1_800_000_000.0
REPLY = {'answer': '先核对试点范围。', 'questions': ['试点范围是否明确？'],
         'risks': ['仍需客户核实。'], 'next_moves': [{'title': '核对合成试点',
         'reason': '范围待核实', 'contact_hint': '技术负责人待核实',
         'preparation': '准备问题', 'success_signal': '明确范围'}]}


@pytest.fixture
def context(tmp_path):
    crm = CustomerStore(tmp_path / 'lifecycle-sources-synthetic.sqlite3')
    RecordLifecycle(crm, clock=lambda: NOW)
    workspace = SalesWorkspace(crm, clock=lambda: NOW)
    customer = crm.create_customer('owner', {'name': '合成归档银行'}, NOW)
    yield crm, workspace, customer
    crm.close()


def note(crm, customer, text='现有系统正在使用旧版密码设备。'):
    return crm.create_record('owner', {'title': '合成原话', 'content': text,
        'customer_id': customer['id'], 'kind': 'note'}, NOW)


def hide(crm, record):
    # Root's lifecycle service owns this transition; test its visibility contract.
    with crm._transaction() as db:
        db.execute('UPDATE crm_records SET hidden=1,updated_at=? WHERE owner=? AND id=?',
                   (NOW + 1, 'owner', record['id']))


def test_archived_customer_draft_leaves_review_queue_and_cannot_confirm(context):
    crm, _, customer = context
    original = '合成归档银行现有系统正在使用旧版密码设备。'
    source = note(crm, customer, original)
    draft = crm.create_customer_draft('owner', {'intent': 'update', 'customer_id': customer['id'],
        'customer_name': customer['name'], 'source_text': original,
        'attributes': [{'target': 'account', 'key': 'existing_systems',
            'value': '旧版密码设备', 'basis': 'reported', 'evidence': '旧版密码设备'}]},
        NOW, source_record_id=source['id'])
    queue = ReviewQueue(crm, clock=lambda: NOW)
    assert any(item['key'] == f"customer:C{draft['id']}" for item in queue.all_items('owner'))
    hide(crm, source)
    assert not any(item['key'] == f"customer:C{draft['id']}" for item in queue.all_items('owner'))
    with pytest.raises(KeyError):
        crm.confirm_customer_draft('owner', draft['id'], NOW + 2)


def test_archived_profile_candidates_disappear_but_confirmed_fact_survives(context):
    crm, workspace, customer = context
    original = note(crm, customer)
    service = ProfileIntelligence(crm, workspace, clock=lambda: NOW)
    asyncio.run(service.scan('owner', force=True))
    pending = service.list_candidates('owner')['items']
    assert pending
    fact = crm.save_fact('owner', customer['id'], {'key': 'requirements',
        'value': '已明确保存的独立需求', 'basis': 'reported',
        'source_record_id': original['id'], 'evidence': '旧版密码设备'}, NOW)
    hide(crm, original)
    assert service.list_candidates('owner')['items'] == []
    assert service.list_candidates('owner', status='stale')['items'] == []
    with pytest.raises(ProfileConflict):
        service.decide('owner', pending[0]['id'], {'decision': 'confirm'})
    assert any(item['id'] == fact['id'] for item in crm.profile('owner', customer['id'])['fields'])


def test_profile_inflight_hidden_source_cannot_publish_candidates(context):
    crm, workspace, customer = context
    original = note(crm, customer)

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        class Analyzer:
            async def extract(self, source, context):
                started.set()
                await release.wait()
                return [{'key': 'existing_systems', 'value': '旧版密码设备',
                         'evidence': '旧版密码设备', 'basis': 'reported'}]
        service = ProfileIntelligence(crm, workspace, analyzer=Analyzer(), clock=lambda: NOW)
        task = asyncio.create_task(service.scan('owner', force=True))
        await started.wait()
        hide(crm, original)
        release.set()
        await task
        assert service.list_candidates('owner', status='all')['items'] == []
    asyncio.run(scenario())


def test_archived_progress_scope_is_not_a_pending_preparation(context):
    crm, workspace, customer = context
    original = note(crm, customer)
    service = ProgressWorkspace(crm, workspace, clock=lambda: NOW)
    run = service.create('owner', {'request_id': 'progress-original', 'kind': 'visit_prepare',
        'customer_id': customer['id'], 'source_record_id': original['id'], 'text': '准备核对试点'})
    asyncio.run(service.process_pending('owner'))
    assert service.review_entries('owner')
    hide(crm, original)
    assert service.review_entries('owner') == []
    assert service.list_runs('owner')['runs'] == []
    archived = service.get('owner', run['id'])
    assert archived['source_unavailable'] and archived['items'] == []


@pytest.mark.parametrize('kind', ['note', 'action', 'legacy_action'])
def test_coaching_archived_evidence_is_not_shown_and_independent_adoption_survives(context, kind):
    crm, workspace, customer = context
    original = crm.create_record('owner', {'title': '合成来源', 'content': '合成范围待核对',
        'customer_id': customer['id'], 'kind': 'action' if kind == 'legacy_action' else kind, 'status': 'following'}, NOW)

    async def scenario():
        class Coach:
            async def advise(self, profile, now):
                advice = {'summary': '合成建议', 'objective': '明确试点', 'rationale': '范围待核对',
                          **copy.deepcopy(REPLY)}
                advice['next_moves'][0]['talk_track'] = '核对范围'
                return advice
        service = CoachingService(crm, Coach(), asyncio.Lock(), clock=lambda: NOW)
        service.workspace = workspace
        service.schedule('owner', customer['id'])
        await asyncio.gather(*list(service.running.values()))
        recommendation = service.view('owner', customer['id'])['recommendation']
        child = service.adopt('owner', customer['id'], recommendation['version'], 1)
        if kind == 'legacy_action':
            with crm._transaction() as db:
                legacy = json.loads(db.execute('SELECT data_json FROM crm_coaching WHERE owner=? AND customer_id=? AND version=?',
                    ('owner', customer['id'], recommendation['version'])).fetchone()[0])
                legacy['sources'] = []  # Old caches recorded recent notes only.
                legacy.pop('sources_complete', None)
                db.execute('UPDATE crm_coaching SET data_json=? WHERE owner=? AND customer_id=? AND version=?',
                    (json.dumps(legacy), 'owner', customer['id'], recommendation['version']))
        hide(crm, original)
        if kind == 'legacy_action':
            with crm._transaction() as db:
                db.execute('INSERT INTO crm_record_lifecycle VALUES (?,?,?,?,?,?,?)',
                    ('owner', original['id'], original['id'], 'archived', 1, NOW + 1, NOW + 1))
        assert service.view('owner', customer['id'])['recommendation'] is None
        assert service.list_views('owner')['items'] == []
        assert crm.get_record('owner', child['id']) is not None
        assert service.adopt('owner', customer['id'], recommendation['version'], 1)['id'] == child['id']
        await service.close()
    asyncio.run(scenario())


def test_progress_confirm_hidden_source_cannot_create_action(context):
    crm, workspace, customer = context
    original = note(crm, customer)
    service = ProgressWorkspace(crm, workspace, clock=lambda: NOW)
    run = service.create('owner', {'request_id': 'prepare-before-archive', 'kind': 'visit_prepare',
        'customer_id': customer['id'], 'source_record_id': original['id'], 'text': '合成准备'})
    asyncio.run(service.process_pending('owner'))
    run = service.get('owner', run['id'])
    item = run['items'][0]
    run = service.edit_draft('owner', run['id'], {'expected_revision': run['revision'],
        'items': [{'id': item['id'], 'selected': True, 'draft': {'title': '用户采纳的合成行动',
            'content': '明确核对试点范围', 'executor_kind': 'self'}}]})
    item = run['items'][0]
    hide(crm, original)
    with pytest.raises((KeyError, ValueError)):
        service.confirm('owner', run['id'], {'request_id': 'stale-confirm',
            'expected_revision': run['revision'], 'items': [{'id': item['id'],
            'expected_item_revision': item['revision'], 'expected_snapshot': item['versions']['snapshot']}]})
    assert crm._db.execute("SELECT count(*) FROM crm_records WHERE kind='action'").fetchone()[0] == 0
    assert crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0


def test_progress_completed_adoption_remains_independent_after_source_archive(context):
    crm, workspace, customer = context
    original = note(crm, customer)
    service = ProgressWorkspace(crm, workspace, clock=lambda: NOW)
    run = service.create('owner', {'request_id': 'prepared-and-adopted', 'kind': 'visit_prepare',
        'customer_id': customer['id'], 'source_record_id': original['id'], 'text': '合成准备'})
    asyncio.run(service.process_pending('owner'))
    run = service.get('owner', run['id'])
    run = service.edit_draft('owner', run['id'], {'expected_revision': run['revision'],
        'items': [{'id': run['items'][0]['id'], 'selected': True, 'draft': {'title': '独立合成行动',
            'content': '用户已采纳的核对行动', 'executor_kind': 'self'}}]})
    item = run['items'][0]
    data = {'request_id': 'adoption-before-archive', 'expected_revision': run['revision'],
        'items': [{'id': item['id'], 'expected_item_revision': item['revision'],
                   'expected_snapshot': item['versions']['snapshot']}]}
    adopted = service.confirm('owner', run['id'], data)['results'][0]['record_id']
    hide(crm, original)
    assert crm.get_record('owner', adopted) is not None
    replay = service.confirm('owner', run['id'], data)
    assert replay['replayed'] and replay['results'][0]['record_id'] == adopted
    assert crm._db.execute("SELECT count(*) FROM crm_records WHERE kind='action'").fetchone()[0] == 1


def test_discussion_inflight_archived_source_drops_new_actions_and_profile_evidence(context):
    crm, workspace, customer = context
    original = note(crm, customer)

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        class Advisor:
            async def reply(self, context, history, text, now):
                started.set()
                await release.wait()
                return copy.deepcopy(REPLY)
        service = DiscussionService(crm, workspace, asyncio.Lock(), Advisor(), clock=lambda: NOW)
        thread = service.create_thread('owner', {'customer_id': customer['id'],
            'source_record_id': original['id'], 'title': '合成讨论'})['thread']
        task = asyncio.create_task(service.send_message('owner', thread['id'],
            {'request_id': 'message-one', 'text': '现有系统正在使用旧版密码设备。'}))
        await started.wait()
        hide(crm, original)
        release.set()
        result = await task
        assert all(not (message.get('data') or {}).get('next_moves') for message in result['messages'])
        assert service.list_threads('owner')['items'] == []
        intelligence = ProfileIntelligence(crm, workspace, clock=lambda: NOW)
        await intelligence.scan('owner', force=True)
        assert intelligence.list_candidates('owner', status='all')['items'] == []
        await service.close()
    asyncio.run(scenario())


def test_capture_hidden_source_cannot_be_classified_or_retried(context):
    crm, workspace, customer = context
    capture = CaptureService(crm, workspace, asyncio.Lock(), clock=lambda: NOW)
    saved = capture.capture('owner', {'request_id': 'capture-one',
        'text': '临时合成想法', 'customer_id': customer['id']})
    hide(crm, saved['record'])
    assert capture.list('owner')['items'] == []
    with pytest.raises(KeyError):
        capture.retry('owner', saved['id'])
    with pytest.raises(KeyError):
        capture.classify('owner', saved['id'], {'purpose': 'note', 'customer_id': customer['id'],
            'expected_updated_at': saved['record']['updated_at']})


@pytest.mark.parametrize('operation', ['replay', 'feedback'])
def test_coaching_archived_adopted_action_cannot_be_replayed_or_completed(context, operation):
    crm, _, customer = context
    async def scenario():
        class Coach:
            async def advise(self, profile, now):
                advice = {'summary': '合成建议', 'objective': '核对范围', 'rationale': '仍需核对',
                          **copy.deepcopy(REPLY)}
                advice['next_moves'][0]['talk_track'] = '核对范围'
                return advice
        service = CoachingService(crm, Coach(), asyncio.Lock(), clock=lambda: NOW)
        service.schedule('owner', customer['id'])
        await asyncio.gather(*list(service.running.values()))
        version = service.view('owner', customer['id'])['recommendation']['version']
        child = service.adopt('owner', customer['id'], version, 1)
        hide(crm, child)
        with pytest.raises(KeyError):
            if operation == 'replay':
                service.adopt('owner', customer['id'], version, 1)
            else:
                service.feedback('owner', customer['id'], version, 1, 'completed')
        assert crm._db.execute('SELECT status FROM crm_records WHERE id=?', (child['id'],)).fetchone()[0] == 'following'
        await service.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('case', ['technical_hidden', 'foreign_archive', 'created_later'])
def test_legacy_coaching_guard_requires_a_plausible_explicit_customer_source(context, case):
    from secretary.coaching_service import fingerprint
    crm, _, customer = context
    service = CoachingService(crm, None, asyncio.Lock(), clock=lambda: NOW)
    advice = {'summary': '旧版合成建议', 'objective': '核对范围', 'rationale': '待核对',
              **copy.deepcopy(REPLY), 'sources': []}
    stamp = fingerprint(service._profile('owner', customer['id']))
    with crm._transaction() as db:
        db.execute('INSERT INTO crm_coaching VALUES (?,?,?,?,?,?)',
            ('owner', customer['id'], 1, stamp, json.dumps(advice), NOW))
    target = customer
    if case == 'foreign_archive':
        target = crm.create_customer('owner', {'name': '合成另一单位'}, NOW)
    created = NOW + 2 if case == 'created_later' else NOW
    source = crm.create_record('owner', {'title': '不属于旧上下文的合成记录', 'content': '合成原话',
        'customer_id': target['id'], 'kind': 'action', 'status': 'following'}, created)
    hide(crm, source)
    if case != 'technical_hidden':
        with crm._transaction() as db:
            db.execute('INSERT INTO crm_record_lifecycle VALUES (?,?,?,?,?,?,?)',
                ('owner', source['id'], source['id'], 'archived', 1, NOW + 3, NOW + 3))
    # A separate legitimate edit makes the cache stale, but must not qualify
    # unrelated/technical/later records as archived historical coach evidence.
    crm.update_customer('owner', customer['id'], {'notes': '用户新补充的合成背景'}, NOW + 4)
    assert service.view('owner', customer['id'])['recommendation'] is not None
