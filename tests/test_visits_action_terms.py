import asyncio
from concurrent.futures import ThreadPoolExecutor
import re

import pytest

from secretary.customer_store import CustomerStore
from secretary.materials import MaterialService
from secretary.visits import VisitService


NOW = 1800000000.0


class Organizer:
    async def organize(self, text, now, context):
        actions = []
        for match in re.finditer(r'(我|客户|内部同事)答应([^。\n]+)', text):
            quote = match.group()
            title = match[2].split('，')[0]
            actions.append({'title': title, 'kind': 'commitment', 'reason': '明确口述',
                'evidence': quote, 'owner_hint': match[1],
                'remind_at': now + 86400 if '明天' in quote else None})
        return {'summary': text, 'actions': actions, 'key_points': [], 'open_questions': []}


def setup(path):
    crm = CustomerStore(path)
    materials = MaterialService(crm, asyncio.Lock(), organizer=Organizer(), clock=lambda: NOW)
    return crm, materials, VisitService(crm, materials, materials.lock)


@pytest.fixture
def services(tmp_path):
    result = setup(tmp_path / 'exchange.sqlite3')
    yield result
    result[0].close()


def add(visits, visit, text, **extra):
    return visits.add_material('alice', visit['id'], {'role': 'recording', 'provider': 'manual',
        'title': text[:80], 'text': text, **extra})


def process(materials):
    while asyncio.run(materials.process_one()):
        pass


def test_explicit_sixty_minutes_reaches_pending_and_confirmed_original_task(services):
    crm, materials, visits = services
    visit = visits.create('alice', {'title': '工作交流', 'occurred_at': NOW})
    add(visits, visit, '我答应拜访王工，明天，预计60分钟。')
    process(materials)
    detail = visits.detail('alice', visit['id'])
    action = detail['actions'][0]
    assert action['duration_minutes'] == 60 and action['executor_kind'] == 'self'
    adopted = visits.adopt('alice', visit['id'], action['key'], detail['visit']['revision'])
    assert adopted['proposal']['duration_minutes'] == 60
    assert adopted['record']['action_terms']['duration_minutes'] == 60
    assert not crm._db.execute('SELECT 1 FROM tasks').fetchone()
    crm.execute('alice', 'confirm-sixty', {'action': 'confirm', 'proposal_id': adopted['proposal']['id']}, NOW)
    task = crm.record_detail('alice', adopted['record']['id'])['task']
    assert task['duration_minutes'] == 60


def test_customer_and_team_promises_keep_terms_without_my_time_block(services):
    crm, materials, visits = services
    visit = visits.create('alice', {'title': '责任分工', 'occurred_at': NOW})
    add(visits, visit, '客户答应发送系统清单，明天。内部同事答应审查清单，明天。')
    process(materials)
    detail = visits.detail('alice', visit['id'])
    assert {item['executor_kind'] for item in detail['actions']} == {'customer', 'team'}
    for action in detail['actions']:
        result = visits.adopt('alice', visit['id'], action['key'], detail['visit']['revision'])
        assert result['proposal'] is None
        assert result['record']['action_terms']['executor_kind'] == action['executor_kind']
    assert crm._db.execute('SELECT COUNT(*) FROM proposals').fetchone()[0] == 0


def test_failed_source_requires_explicit_reversible_exclusion_and_old_revision_stays_invalid(services):
    crm, materials, visits = services
    visit = visits.create('alice', {'title': '来源恢复', 'occurred_at': NOW})
    known = add(visits, visit, '我答应发送方案，明天。')
    process(materials)
    old = visits.detail('alice', visit['id'])
    failed = visits.add_material('alice', visit['id'], {'role': 'recording', 'provider': 'listen_note', 'title': '不存在的录音'})
    process(materials)
    detail = visits.detail('alice', visit['id'])
    failed_id = failed['material']['id']
    assert materials.detail('alice', failed_id)['material']['status'] == 'failed'
    assert detail['blocked_source_ids'] == [failed_id]
    with pytest.raises(ValueError, match='核对'):
        visits.adopt('alice', visit['id'], detail['actions'][0]['key'], detail['visit']['revision'])
    with pytest.raises(ValueError, match='说明'):
        visits.decide_source('alice', visit['id'], failed_id, detail['visit']['revision'], 'excluded')
    chosen = visits.decide_source('alice', visit['id'], failed_id, detail['visit']['revision'], 'excluded', '录音定位待核实，本次仅采用已读原话')
    replay = visits.decide_source('alice', visit['id'], failed_id, detail['visit']['revision'], 'excluded', '录音定位待核实，本次仅采用已读原话')
    assert replay['visit']['revision'] == chosen['visit']['revision']
    assert chosen['excluded_sources'] == [failed_id] and not chosen['blocked_source_ids']
    with pytest.raises(ValueError, match='变化'):
        visits.adopt('alice', visit['id'], old['actions'][0]['key'], old['visit']['revision'])
    action = chosen['actions'][0]
    adopted = visits.adopt('alice', visit['id'], action['key'], chosen['visit']['revision'])
    assert adopted['proposal']['status'] == 'pending'
    crm.execute('alice', 'confirm-before-reinclude', {'action': 'confirm', 'proposal_id': adopted['proposal']['id']}, NOW)
    current = visits.detail('alice', visit['id'])
    restored = visits.decide_source('alice', visit['id'], failed_id, current['visit']['revision'], 'included', '重新定位核对')
    assert failed_id in restored['blocked_source_ids']
    assert crm.record_detail('alice', adopted['record']['id'])['task']['remind_at'] == NOW + 86400
    assert materials.detail('alice', known['material']['id'])['text'] == '我答应发送方案，明天。'
    assert crm._db.execute('SELECT COUNT(*) FROM crm_visit_source_choice_history').fetchone()[0] == 2


def test_excluded_verified_source_cannot_bypass_exchange_through_material_adoption(services):
    _, materials, visits = services
    visit = visits.create('alice', {'title': '排除来源', 'occurred_at': NOW})
    saved = add(visits, visit, '我答应发送方案，明天。')
    process(materials)
    raw = materials.detail('alice', saved['material']['id'])
    detail = visits.detail('alice', visit['id'])
    chosen = visits.decide_source('alice', visit['id'], raw['material']['id'], detail['visit']['revision'], False, '来源归属待核实')
    assert not chosen['actions']
    with pytest.raises(ValueError, match='重新纳入'):
        materials.adopt('alice', raw['material']['id'], raw['analysis']['actions'][0]['id'], raw['material']['revision'])
    with pytest.raises(KeyError):
        visits.decide_source('bob', visit['id'], raw['material']['id'], chosen['visit']['revision'], True)


def test_deferred_source_remains_blocking_and_choices_survive_reopen(services):
    crm, materials, visits = services
    visit = visits.create('alice', {'title': '稍后核对', 'occurred_at': NOW})
    saved = add(visits, visit, '我答应发送方案，明天。')
    process(materials)
    detail = visits.detail('alice', visit['id'])
    changed = visits.decide_source('alice', visit['id'], saved['material']['id'], detail['visit']['revision'], 'deferred', '稍后核对原话')
    reopened = VisitService(crm, materials, materials.lock).detail('alice', visit['id'])
    assert changed['visit']['revision'] == reopened['visit']['revision']
    assert saved['material']['id'] in reopened['blocked_source_ids']
    assert reopened['actions'][0]['needs_review']


def test_recording_time_difference_requires_explicit_association_and_preserves_both_times(services):
    _, materials, visits = services
    visit = visits.create('alice', {'title': '14点交流', 'occurred_at': NOW})
    source = materials.enqueue('alice', {'provider': 'manual', 'title': '14点03录音',
        'text': '我答应发送方案，明天。', 'category': 'conversation', 'occurred_at': NOW + 180})
    with pytest.raises(ValueError, match='发生时间'):
        visits.add_material('alice', visit['id'], {'role': 'recording', 'material_id': source['id']})
    payload = {'role': 'recording', 'material_id': source['id'], 'confirm_time_difference': True}
    visits.add_material('alice', visit['id'], payload, source_id='explicit-time-link')
    process(materials)
    detail = visits.detail('alice', visit['id'])
    assert detail['visit']['occurred_at'] == NOW
    assert detail['sources'][0]['material']['occurred_at'] == NOW + 180
    assert detail['sources'][0]['time_association_confirmed']
    assert not detail['actions'][0]['needs_review']
    visits.add_material('alice', visit['id'], payload)
    assert visits.detail('alice', visit['id'])['visit']['revision'] == detail['visit']['revision']
    changed = materials.update('alice', source['id'], {'revision': detail['sources'][0]['material']['revision'], 'occurred_at': NOW + 300})
    process(materials)
    current = visits.detail('alice', visit['id'])
    assert not current['sources'][0]['time_association_confirmed']
    assert current['actions'][0]['needs_review']
    assert materials.detail('alice', changed['id'])['material']['occurred_at'] == NOW + 300


def test_two_connections_keep_real_conflicting_source_use_decisions(tmp_path):
    path = tmp_path / 'race.sqlite3'
    first = setup(path)
    second = setup(path)
    try:
        visit = first[2].create('alice', {'title': '并发核对', 'occurred_at': NOW})
        source = add(first[2], visit, '我答应发送方案。')
        process(first[1])
        detail = first[2].detail('alice', visit['id'])
        def decide(item):
            service, use = item
            try:
                service.decide_source('alice', visit['id'], source['material']['id'], detail['visit']['revision'], use, '本次来源选择')
                return 'saved'
            except ValueError:
                return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(decide, [(first[2], 'excluded'), (second[2], 'deferred')]))
        assert sorted(result) == ['conflict', 'saved']
        assert first[0]._db.execute('SELECT COUNT(*) FROM crm_visit_source_choice_history').fetchone()[0] == 1
    finally:
        first[0].close()
        second[0].close()


def test_linked_original_record_is_visible_versioned_and_blocks_unreviewed_sources(services):
    crm, materials, visits = services
    crm._db.execute('CREATE TABLE IF NOT EXISTS crm_visit_records(owner TEXT NOT NULL,visit_id INTEGER NOT NULL,'
        'record_id INTEGER NOT NULL,role TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(owner,record_id))')
    visit = visits.create('alice', {'title': '短记录归档', 'occurred_at': NOW})
    add(visits, visit, '我答应发送方案，明天。')
    process(materials)
    old = visits.detail('alice', visit['id'])
    action = old['actions'][0]
    adopted = visits.adopt('alice', visit['id'], action['key'], old['visit']['revision'])
    crm.execute('alice', 'confirm-before-raw-source', {'action': 'confirm', 'proposal_id': adopted['proposal']['id']}, NOW)
    raw = crm.create_record('alice', {'title': '追加原话', 'content': '方案先别发送，等新清单后再约。'}, NOW)
    crm._db.execute('INSERT INTO crm_visit_records VALUES (?,?,?,?,?)', ('alice', visit['id'], raw['id'], 'supplement', NOW))
    detail = visits.detail('alice', visit['id'])
    assert detail['visit']['revision'] != old['visit']['revision']
    assert detail['visit']['source_count'] == 2
    assert detail['blocked_record_ids'] == [raw['id']]
    assert detail['record_sources'][0]['record']['id'] == raw['id']
    assert any(ref.get('record_id') == raw['id'] for ref in detail['actions'][0]['references'])
    assert detail['actions'][0]['needs_review']
    context = visits.record_action_context('alice', raw['id'], {'title': '发送方案'})
    assert context['blocked'] and context['revision'] == detail['visit']['revision']
    assert any('取消' in reason for reason in context['reasons'])
    assert crm.record_detail('alice', adopted['record']['id'])['task']['status'] == 'pending'
    assert crm.record_detail('alice', adopted['record']['id'])['task']['remind_at'] == NOW + 86400
    with pytest.raises(ValueError, match='变化'):
        visits.adopt('alice', visit['id'], action['key'], old['visit']['revision'])


def test_conflicting_date_only_deadlines_remain_reviewable_without_clock(services):
    _, materials, visits = services
    visit = visits.create('alice', {'title': '截止日冲突', 'occurred_at': NOW})
    add(visits, visit, '我答应发送方案，10月12日前。')
    add(visits, visit, '我答应发送方案，10月13日前。')
    process(materials)
    detail = visits.detail('alice', visit['id'])
    action = detail['actions'][0]
    assert action['needs_review']
    assert action['execution_at'] is None and action['deadline_at'] is None
    assert len({ref['deadline_date'] for ref in action['references']}) == 2
    with pytest.raises(ValueError, match='核对'):
        visits.adopt('alice', visit['id'], action['key'], detail['visit']['revision'])


def test_source_choice_uses_original_link_id_after_provider_duplicate_resolution(services):
    _, materials, visits = services
    class Connector:
        async def fetch(self, title):
            text = '我答应发送方案，明天。'
            return {'nid': 'one-original-recording', 'title': title, 'source': 'OPTIMIZED', 'text': text}
    materials.connector = Connector()
    visit = visits.create('alice', {'title': '重复定位', 'occurred_at': NOW})
    first = visits.add_material('alice', visit['id'], {'role': 'recording', 'provider': 'listen_note', 'title': '录音原名'})
    process(materials)
    alias = visits.add_material('alice', visit['id'], {'role': 'recording', 'provider': 'listen_note', 'title': '录音别名'})
    process(materials)
    detail = visits.detail('alice', visit['id'])
    assert {source['material_id'] for source in detail['sources']} == {first['material']['id']}
    excluded = visits.decide_source('alice', visit['id'], alias['material']['id'], detail['visit']['revision'], 'excluded', '本次不用重复定位链接')
    choices = {source['linked_material_id']: source['source_use'] for source in excluded['sources']}
    assert choices[first['material']['id']] == 'included'
    assert choices[alias['material']['id']] == 'excluded'
    assert len(excluded['actions']) == 1 and not excluded['actions'][0]['needs_review']
    action = excluded['actions'][0]
    assert visits.adopt('alice', visit['id'], action['key'], excluded['visit']['revision'])['proposal']['status'] == 'pending'
