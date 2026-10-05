import asyncio
import re

import pytest

from secretary.customer_store import CustomerStore
from secretary.materials import MaterialService
from secretary.visits import VisitService


NOW = 1_800_000_000.0


class Organizer:
    async def organize(self, text, now, context):
        actions = []
        for match in re.finditer(r'(我|客户)答应([^。\n]+)', text):
            quote = match.group()
            title = match[2].split('，')[0]
            when = now + 86400 if '明天' in quote else now + 172800 if '后天' in quote else None
            actions.append({'title': title, 'kind': 'commitment', 'evidence': quote,
                            'reason': '明确口述', 'owner_hint': match[1], 'remind_at': when})
        return {'summary': text, 'actions': actions, 'key_points': [], 'open_questions': []}


@pytest.fixture
def services(tmp_path):
    crm = CustomerStore(tmp_path / 'visits.sqlite3')
    lock = asyncio.Lock()
    materials = MaterialService(crm, lock, organizer=Organizer(), clock=lambda: NOW)
    visits = VisitService(crm, materials, lock)
    yield crm, materials, visits
    crm.close()


def create(visits, **kwargs):
    return visits.create('alice', {'title': '星海医院交流', **kwargs})


def add(visits, visit, text='我答应发送方案。', role='recording', **kwargs):
    return visits.add_material('alice', visit['id'], {'role': role, 'provider': 'manual',
        'title': '交流素材', 'text': text, **kwargs})


def process(materials):
    while asyncio.run(materials.process_one()):
        pass


def test_create_source_replay_owner_scope_and_reopen(services):
    crm, materials, visits = services
    customer = crm.create_customer('alice', {'name': '星海医院'}, NOW)
    visit = visits.create('alice', {'title': '首次交流', 'customer_id': customer['id'], 'occurred_at': NOW}, source_id='visit-msg')
    assert visits.create('alice', {'title': '首次交流', 'customer_id': customer['id'], 'occurred_at': NOW}, source_id='visit-msg')['id'] == visit['id']
    added = visits.add_material('alice', visit['id'], {'role': 'recap', 'provider': 'manual',
        'title': '我的复盘', 'text': '我观察客户担心预算。'}, source_id='recap-msg')
    again = visits.add_material('alice', visit['id'], {'role': 'recap', 'provider': 'manual',
        'title': '我的复盘', 'text': '我观察客户担心预算。'}, source_id='recap-msg')
    assert added['material']['id'] == again['material']['id']
    assert added['material']['customer_id'] == customer['id']
    assert added['material']['occurred_at'] == NOW
    assert added['material']['category'] == 'visit_review'
    assert visits.list('bob')['total'] == 0
    with pytest.raises(KeyError):
        visits.detail('bob', visit['id'])
    process(materials)
    reopened = VisitService(crm, materials, visits.lock)
    detail = reopened.detail('alice', visit['id'])
    assert detail['sources'][0]['role'] == 'recap'
    assert detail['sources'][0]['text'] == '我观察客户担心预算。'
    assert detail['visit']['revision']


def test_same_manual_text_in_different_visits_is_not_shared(services):
    _, _, visits = services
    first, second = create(visits), create(visits, title='第二次交流')
    a, b = add(visits, first), add(visits, second)
    assert a['material']['id'] != b['material']['id']


def test_multiple_sources_merge_and_adoption_is_one_pending_proposal(services):
    crm, materials, visits = services
    visit = create(visits, occurred_at=NOW)
    a = add(visits, visit, '我答应发送方案，明天。')
    b = add(visits, visit, '我答应发送方案，明天。', role='recap')
    process(materials)
    detail = visits.detail('alice', visit['id'])
    assert len(detail['actions']) == 1
    action = detail['actions'][0]
    assert {ref['material_id'] for ref in action['references']} == {a['material']['id'], b['material']['id']}
    assert {ref['role'] for ref in action['references']} == {'recording', 'recap'}
    adopted = visits.adopt('alice', visit['id'], action['key'], detail['visit']['revision'])
    assert adopted['proposal']['status'] == 'pending'
    assert visits.adopt('alice', visit['id'], action['key'], detail['visit']['revision']) == adopted
    assert crm._db.execute('SELECT COUNT(*) FROM proposals').fetchone()[0] == 1
    assert crm._db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0
    assert crm._db.execute('SELECT COUNT(*) FROM notifications').fetchone()[0] == 0
    source = detail['sources'][0]['material']
    raw = materials.detail('alice', source['id'])
    assert raw['analysis']['actions'][0]['adopted_record_id'] == adopted['record']['id']
    detail = visits.detail('alice', visit['id'])
    assert detail['actions'][0]['adopted_record_id'] == adopted['record']['id']


@pytest.mark.parametrize('second', ['我答应发送方案，后天。', '客户答应发送方案，明天。'])
def test_time_or_responsible_person_conflict_requires_review(services, second):
    crm, materials, visits = services
    visit = create(visits, occurred_at=NOW)
    add(visits, visit, '我答应发送方案，明天。')
    add(visits, visit, second, role='supplement')
    process(materials)
    detail = visits.detail('alice', visit['id'])
    action = detail['actions'][0]
    assert action['needs_review'] is True
    assert action['remind_at'] is None
    if second.startswith('客户'):
        assert len(detail['actions']) == 2
        assert len({item['key'] for item in detail['actions']}) == 2
        assert {item['executor_kind'] for item in detail['actions']} == {'self', 'customer'}
        assert all(len(item['references']) == 1 for item in detail['actions'])
    else:
        assert len(action['references']) == 2
    for item in detail['actions']:
        assert item['needs_review'] and item['remind_at'] is None
        with pytest.raises(ValueError, match='核对'):
            visits.adopt('alice', visit['id'], item['key'], detail['visit']['revision'])
    assert crm._db.execute('SELECT COUNT(*) FROM proposals').fetchone()[0] == 0


def test_confirmed_reminder_survives_late_changed_commitment(services):
    crm, materials, visits = services
    visit = create(visits, occurred_at=NOW)
    add(visits, visit, '我答应发送方案，明天。')
    process(materials)
    detail = visits.detail('alice', visit['id'])
    old_revision = detail['visit']['revision']
    adopted = visits.adopt('alice', visit['id'], detail['actions'][0]['key'], old_revision)
    crm.execute('alice', 'confirm', {'action': 'confirm', 'proposal_id': adopted['proposal']['id']}, NOW)
    add(visits, visit, '我答应发送方案，后天。', role='supplement')
    process(materials)
    current = visits.detail('alice', visit['id'])
    assert any('原提醒' in warning or '已采纳' in warning for warning in current['warnings'])
    with pytest.raises(ValueError, match='变化|刷新'):
        visits.adopt('alice', visit['id'], current['actions'][0]['key'], old_revision)
    repeated = visits.adopt('alice', visit['id'], current['actions'][0]['key'], current['visit']['revision'])
    assert repeated['record']['id'] == adopted['record']['id']
    assert repeated['record']['remind_at'] == NOW + 86400
    assert crm._db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1
    assert crm._db.execute('SELECT COUNT(*) FROM proposals').fetchone()[0] == 1


def test_material_direct_adoption_uses_visit_guard(services):
    crm, materials, visits = services
    visit = create(visits, occurred_at=NOW)
    first = add(visits, visit, '我答应发送方案，明天。')
    second = add(visits, visit, '我答应发送方案，后天。', role='recap')
    process(materials)
    raw = materials.detail('alice', first['material']['id'])
    with pytest.raises(ValueError, match='核对'):
        materials.adopt('alice', raw['material']['id'], raw['analysis']['actions'][0]['id'], raw['material']['revision'])
    assert crm._db.execute('SELECT COUNT(*) FROM proposals').fetchone()[0] == 0
    assert second['material']['id'] != first['material']['id']


def test_material_direct_adoption_deduplicates_other_reference(services):
    crm, materials, visits = services
    visit = create(visits, occurred_at=NOW)
    first = add(visits, visit)
    second = add(visits, visit, role='recap')
    process(materials)
    results = []
    for material in [first, second]:
        raw = materials.detail('alice', material['material']['id'])
        results.append(materials.adopt('alice', raw['material']['id'], raw['analysis']['actions'][0]['id'], raw['material']['revision']))
    assert results[0]['record']['id'] == results[1]['record']['id']
    assert crm._db.execute("SELECT COUNT(*) FROM crm_records WHERE kind='action'").fetchone()[0] == 1


def test_material_change_invalidates_visit_revision_and_preserves_old_adoption(services):
    crm, materials, visits = services
    visit = create(visits)
    added = add(visits, visit)
    process(materials)
    detail = visits.detail('alice', visit['id'])
    action = detail['actions'][0]
    adopted = visits.adopt('alice', visit['id'], action['key'], detail['visit']['revision'])
    raw = materials.detail('alice', added['material']['id'])
    materials.update('alice', raw['material']['id'], {'revision': raw['material']['revision'], 'text': '我答应补充案例。'})
    with pytest.raises(ValueError, match='变化|刷新'):
        visits.adopt('alice', visit['id'], action['key'], detail['visit']['revision'])
    process(materials)
    current = visits.detail('alice', visit['id'])
    assert current['previous_adoptions'][0]['id'] == adopted['record']['id']
    assert crm._db.execute("SELECT COUNT(*) FROM crm_records WHERE kind='action'").fetchone()[0] == 1


def test_unknown_occurrence_is_not_guessed(services):
    _, materials, visits = services
    visit = create(visits)
    added = add(visits, visit, '我答应发送方案，明天。')
    assert added['material']['occurred_at'] is None
    process(materials)
    detail = visits.detail('alice', visit['id'])
    assert detail['actions'][0]['remind_at'] is None
    adopted = visits.adopt('alice', visit['id'], detail['actions'][0]['key'], detail['visit']['revision'])
    assert adopted['proposal'] is None


def test_link_mismatch_foreign_and_duplicate_are_guarded(services):
    crm, materials, visits = services
    ca = crm.create_customer('alice', {'name': '甲客户'}, NOW)
    cb = crm.create_customer('alice', {'name': '乙客户'}, NOW)
    visit = create(visits, customer_id=ca['id'], occurred_at=NOW)
    wrong = materials.enqueue('alice', {'provider': 'manual', 'title': '旧材料', 'text': '说明', 'customer_id': cb['id'], 'occurred_at': NOW})
    with pytest.raises(ValueError, match='客户'):
        visits.add_material('alice', visit['id'], {'role': 'recording', 'material_id': wrong['id']})
    foreign = materials.enqueue('bob', {'provider': 'manual', 'title': '外部材料', 'text': '说明'})
    with pytest.raises(KeyError):
        visits.add_material('alice', visit['id'], {'role': 'recording', 'material_id': foreign['id']})
    added = add(visits, visit)
    assert visits.add_material('alice', visit['id'], {'role': 'recording', 'material_id': added['material']['id']})['material']['id'] == added['material']['id']
    another = create(visits, customer_id=ca['id'], occurred_at=NOW)
    with pytest.raises(ValueError, match='归档|交流'):
        visits.add_material('alice', another['id'], {'role': 'recording', 'material_id': added['material']['id']})
    assert len(visits.detail('alice', visit['id'])['sources']) == 1


def test_date_mismatch_and_manual_override_are_rejected(services):
    _, materials, visits = services
    visit = create(visits, occurred_at=NOW)
    with pytest.raises(ValueError, match='时间|日期'):
        add(visits, visit, occurred_at=NOW + 86400)
    wrong = materials.enqueue('alice', {'provider': 'manual', 'title': '旧材料', 'text': '说明', 'occurred_at': NOW - 86400})
    with pytest.raises(ValueError, match='时间|日期'):
        visits.add_material('alice', visit['id'], {'role': 'recording', 'material_id': wrong['id']})


def test_later_metadata_assignments_inherit_and_updates_are_revision_guarded(services):
    crm, materials, visits = services
    visit = create(visits)
    added = add(visits, visit)
    current = visits.detail('alice', visit['id'])['visit']
    customer = crm.create_customer('alice', {'name': '星海医院'}, NOW)
    updated = visits.update('alice', visit['id'], {'revision': current['revision'], 'customer_id': customer['id'], 'occurred_at': NOW})
    material = materials.detail('alice', added['material']['id'])['material']
    assert material['customer_id'] == customer['id'] and material['occurred_at'] == NOW
    with pytest.raises(ValueError, match='变化|刷新'):
        visits.update('alice', visit['id'], {'revision': current['revision'], 'title': '旧覆盖'})
    assert visits.list('alice', customer_id=customer['id'])['items'][0]['id'] == updated['id']


def test_unverified_quote_cannot_become_visit_commitment(services):
    crm, materials, visits = services
    visit = create(visits, occurred_at=NOW)
    added = add(visits, visit)
    process(materials)
    import json
    row = crm._db.execute('SELECT analysis_json FROM crm_materials WHERE id=?', (added['material']['id'],)).fetchone()
    analysis = json.loads(row[0])
    analysis['actions'][0]['evidence'] = '无来源的承诺'
    crm._db.execute('UPDATE crm_materials SET analysis_json=? WHERE id=?', (json.dumps(analysis), added['material']['id']))
    detail = visits.detail('alice', visit['id'])
    assert detail['actions'][0]['needs_review']
    with pytest.raises(ValueError, match='核对'):
        visits.adopt('alice', visit['id'], detail['actions'][0]['key'], detail['visit']['revision'])


def test_explicit_merge_preserves_references_and_survives_restart_and_reprocessing(services):
    crm, materials, visits = services
    visit = create(visits, occurred_at=NOW)
    first = add(visits, visit, '我答应发送方案，明天。')
    add(visits, visit, '我答应提交方案，明天。', role='recap')
    process(materials)
    detail = visits.detail('alice', visit['id'])
    assert len(detail['actions']) == 2
    merged = visits.merge_actions('alice', visit['id'], [item['key'] for item in detail['actions']], detail['visit']['revision'])
    assert len(merged['actions']) == 1 and len(merged['actions'][0]['references']) == 2
    assert merged['actions'][0]['needs_review'] is False
    merged_key = merged['actions'][0]['key']
    reopened = VisitService(crm, materials, visits.lock)
    raw = materials.detail('alice', first['material']['id'])['material']
    materials.update('alice', raw['id'], {'revision': raw['revision'], 'text': '补充背景。我答应发送方案，明天。'})
    process(materials)
    current = reopened.detail('alice', visit['id'])
    assert current['actions'][0]['key'] == merged_key
    assert len(current['actions']) == 1
    adopted = reopened.adopt('alice', visit['id'], merged_key, current['visit']['revision'])
    assert adopted['proposal']['status'] == 'pending'


def test_explicit_merge_keeps_time_conflict_and_refuses_adopted_rows(services):
    _, materials, visits = services
    visit = create(visits, occurred_at=NOW)
    add(visits, visit, '我答应发送方案，明天。')
    add(visits, visit, '我答应提交方案，后天。', role='recap')
    process(materials)
    detail = visits.detail('alice', visit['id'])
    merged = visits.merge_actions('alice', visit['id'], [item['key'] for item in detail['actions']], detail['visit']['revision'])
    assert merged['actions'][0]['needs_review']
    with pytest.raises(ValueError, match='核对'):
        visits.adopt('alice', visit['id'], merged['actions'][0]['key'], merged['visit']['revision'])
    second = create(visits)
    add(visits, second, '我答应发送案例。我答应提交报告。')
    process(materials)
    detail = visits.detail('alice', second['id'])
    visits.adopt('alice', second['id'], detail['actions'][0]['key'], detail['visit']['revision'])
    with pytest.raises(ValueError, match='原待办'):
        visits.merge_actions('alice', second['id'], [item['key'] for item in detail['actions']], detail['visit']['revision'])


def test_late_cancellation_references_block_new_adoption(services):
    crm, materials, visits = services
    visit = create(visits, occurred_at=NOW)
    add(visits, visit, '我答应发送脱敏方案，明天。')
    add(visits, visit, '客户补充说方案先别发了，等我通知。', role='supplement')
    process(materials)
    detail = visits.detail('alice', visit['id'])
    assert detail['actions'][0]['needs_review']
    assert any(ref.get('change') for ref in detail['actions'][0]['references'])
    with pytest.raises(ValueError, match='核对'):
        visits.adopt('alice', visit['id'], detail['actions'][0]['key'], detail['visit']['revision'])
    assert crm._db.execute('SELECT COUNT(*) FROM proposals').fetchone()[0] == 0


def test_recap_reported_customer_promise_requires_original_confirmation(services):
    crm, materials, visits = services
    visit = create(visits, occurred_at=NOW)
    add(visits, visit, '客户答应发送资料，明天。', role='recap')
    process(materials)
    detail = visits.detail('alice', visit['id'])
    assert detail['actions'][0]['kind'] == 'suggestion'
    assert detail['actions'][0]['needs_review']
    assert detail['actions'][0]['references'][0]['basis'] == 'observation'
    assert crm._db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0


def test_reverting_to_existing_material_version_is_verified(services):
    _, materials, visits = services
    visit = create(visits)
    source = add(visits, visit)
    process(materials)
    material = materials.detail('alice', source['material']['id'])['material']
    material = materials.update('alice', material['id'], {'revision': material['revision'], 'text': '我答应补充案例。'})
    process(materials)
    material = materials.update('alice', material['id'], {'revision': material['revision'], 'text': '我答应发送方案。'})
    process(materials)
    detail = visits.detail('alice', visit['id'])
    assert detail['actions'][0]['needs_review'] is False
    assert detail['actions'][0]['references'][0]['verified'] is True


def test_late_equal_source_returns_original_adoption_on_material_entry(services):
    crm, materials, visits = services
    visit = create(visits, occurred_at=NOW)
    add(visits, visit, '我答应发送方案，明天。')
    process(materials)
    detail = visits.detail('alice', visit['id'])
    adopted = visits.adopt('alice', visit['id'], detail['actions'][0]['key'], detail['visit']['revision'])
    late = add(visits, visit, '我答应发送方案，明天。', role='supplement')
    process(materials)
    raw = materials.detail('alice', late['material']['id'])
    result = materials.adopt('alice', raw['material']['id'], raw['analysis']['actions'][0]['id'], raw['material']['revision'])
    assert result['record']['id'] == adopted['record']['id']
    assert materials.detail('alice', late['material']['id'])['analysis']['actions'][0]['adopted_record_id'] == adopted['record']['id']
    assert crm._db.execute('SELECT COUNT(*) FROM proposals').fetchone()[0] == 1


def test_concurrent_adoption_from_two_database_connections_is_idempotent(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = tmp_path / 'concurrent.sqlite3'
    first_crm = CustomerStore(path)
    second_crm = CustomerStore(path)
    try:
        first_materials = MaterialService(first_crm, asyncio.Lock(), organizer=Organizer(), clock=lambda: NOW)
        second_materials = MaterialService(second_crm, asyncio.Lock(), organizer=Organizer(), clock=lambda: NOW)
        first = VisitService(first_crm, first_materials, first_materials.lock)
        second = VisitService(second_crm, second_materials, second_materials.lock)
        visit = create(first, occurred_at=NOW)
        add(first, visit, '我答应发送方案，明天。')
        process(first_materials)
        detail = first.detail('alice', visit['id'])
        def adopt_one(service):
            return service.adopt('alice', visit['id'], detail['actions'][0]['key'], detail['visit']['revision'])
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(adopt_one, [first, second]))
        assert results[0]['record']['id'] == results[1]['record']['id']
        assert first_crm._db.execute('SELECT COUNT(*) FROM proposals').fetchone()[0] == 1
    finally:
        first_crm.close()
        second_crm.close()


def test_source_message_cannot_be_replayed_into_other_visit(services):
    _, _, visits = services
    first, second = create(visits), create(visits, title='下次交流')
    payload = {'role': 'recap', 'provider': 'manual', 'title': '复盘', 'text': '我的观察'}
    visits.add_material('alice', first['id'], payload, source_id='same-msg')
    with pytest.raises(ValueError, match='归档|不同内容'):
        visits.add_material('alice', second['id'], payload, source_id='same-msg')


def test_material_customer_reassignment_warns_and_invalidates_revision(services):
    crm, materials, visits = services
    customer = crm.create_customer('alice', {'name': '星海医院'}, NOW)
    visit = create(visits, customer_id=customer['id'])
    source = add(visits, visit)
    process(materials)
    old = visits.detail('alice', visit['id'])
    different = crm.create_customer('alice', {'name': '其他医院'}, NOW)
    raw = materials.detail('alice', source['material']['id'])['material']
    materials.update('alice', raw['id'], {'revision': raw['revision'], 'customer_id': different['id']})
    process(materials)
    current = visits.detail('alice', visit['id'])
    assert current['visit']['customer_id'] == customer['id']
    assert current['actions'][0]['needs_review']
    assert any('客户' in warning for warning in current['warnings'])
    with pytest.raises(ValueError, match='变化|刷新'):
        visits.adopt('alice', visit['id'], old['actions'][0]['key'], old['visit']['revision'])


def test_changed_delivery_verb_after_adoption_requires_original_task_review(services):
    crm, materials, visits = services
    visit = create(visits, occurred_at=NOW)
    source = add(visits, visit, '我答应发送方案，明天。')
    process(materials)
    detail = visits.detail('alice', visit['id'])
    adopted = visits.adopt('alice', visit['id'], detail['actions'][0]['key'], detail['visit']['revision'])
    raw = materials.detail('alice', source['material']['id'])['material']
    materials.update('alice', raw['id'], {'revision': raw['revision'], 'text': '我答应提交方案，后天。'})
    process(materials)
    changed = visits.detail('alice', visit['id'])
    assert changed['actions'][0]['needs_review']
    assert changed['previous_adoptions'][0]['id'] == adopted['record']['id']
    with pytest.raises(ValueError, match='核对'):
        visits.adopt('alice', visit['id'], changed['actions'][0]['key'], changed['visit']['revision'])
    assert crm._db.execute('SELECT COUNT(*) FROM proposals').fetchone()[0] == 1


def test_late_provider_duplicate_across_visits_blocks_both_archives(services):
    crm, materials, visits = services
    class Connector:
        async def fetch(self, title):
            return {'nid': 'same-recording', 'title': title, 'source': 'OPTIMIZED',
                    'text': '我答应发送方案。', 'raw_content': '我答应发送方案。'}
    materials.connector = Connector()
    first, second = create(visits), create(visits, title='另一场交流')
    a = visits.add_material('alice', first['id'], {'role': 'recording', 'provider': 'listen_note', 'title': '录音原标题A'})
    process(materials)
    old = visits.detail('alice', first['id'])
    visits.add_material('alice', second['id'], {'role': 'recording', 'provider': 'listen_note', 'title': '录音原标题B'})
    process(materials)
    for visit in (first, second):
        detail = visits.detail('alice', visit['id'])
        assert detail['actions'][0]['needs_review']
        assert any('另一场交流' in warning for warning in detail['warnings'])
        with pytest.raises(ValueError, match='核对'):
            visits.adopt('alice', visit['id'], detail['actions'][0]['key'], detail['visit']['revision'])
    assert visits.detail('alice', first['id'])['visit']['revision'] != old['visit']['revision']
    assert materials.detail('alice', a['material']['id'])['material']['title'] == '录音原标题A'
    assert crm._db.execute('SELECT COUNT(*) FROM proposals').fetchone()[0] == 0


def test_cold_reopen_recovers_archive_and_existing_adoption(tmp_path):
    path = tmp_path / 'reopen.sqlite3'
    crm = CustomerStore(path)
    materials = MaterialService(crm, asyncio.Lock(), organizer=Organizer(), clock=lambda: NOW)
    visits = VisitService(crm, materials, materials.lock)
    visit = create(visits)
    add(visits, visit)
    process(materials)
    detail = visits.detail('alice', visit['id'])
    adopted = visits.adopt('alice', visit['id'], detail['actions'][0]['key'], detail['visit']['revision'])
    crm.close()
    reopened_crm = CustomerStore(path)
    try:
        reopened_materials = MaterialService(reopened_crm, asyncio.Lock(), organizer=Organizer(), clock=lambda: NOW)
        reopened = VisitService(reopened_crm, reopened_materials, reopened_materials.lock)
        detail = reopened.detail('alice', visit['id'])
        assert detail['actions'][0]['adopted_record_id'] == adopted['record']['id']
        assert reopened.adopt('alice', visit['id'], detail['actions'][0]['key'], detail['visit']['revision'])['record']['id'] == adopted['record']['id']
        assert reopened_crm._db.execute("SELECT COUNT(*) FROM crm_records WHERE kind='action'").fetchone()[0] == 1
    finally:
        reopened_crm.close()


def test_unprocessed_late_source_defers_all_new_adoptions(services):
    crm, materials, visits = services
    visit = create(visits, occurred_at=NOW)
    add(visits, visit, '我答应发送方案，明天。')
    process(materials)
    add(visits, visit, '方案先别发了。', role='supplement')
    current = visits.detail('alice', visit['id'])
    assert current['actions'][0]['needs_review']
    with pytest.raises(ValueError, match='核对'):
        visits.adopt('alice', visit['id'], current['actions'][0]['key'], current['visit']['revision'])
    assert crm._db.execute('SELECT COUNT(*) FROM proposals').fetchone()[0] == 0


def test_customer_drafts_are_collected_with_roles_and_keep_c_confirmation(services):
    crm, materials, visits = services
    class Parser:
        async def parse(self, text, now, context=None):
            return {'intent': 'create', 'customer_name': '星海医院', 'contact_name': None,
                    'basic': {}, 'basic_evidence': {}, 'contact': {}, 'contact_evidence': {},
                    'attributes': [{'key': 'pain_points', 'target': 'account', 'value': '担心预算',
                                    'evidence': '担心预算', 'basis': 'reported'}]}
    materials.customer_parser = Parser()
    visit = create(visits)
    add(visits, visit, '星海医院担心预算。')
    add(visits, visit, '我的观察是星海医院担心预算。', role='recap')
    process(materials)
    detail = visits.detail('alice', visit['id'])
    assert len(detail['customer_drafts']) == 2
    assert {ref['role'] for draft in detail['customer_drafts'] for ref in draft['references']} == {'recording', 'recap'}
    assert all(draft['status'] == 'pending' for draft in detail['customer_drafts'])
    confirmed = crm.confirm_customer_draft('alice', detail['customer_drafts'][0]['id'], NOW)
    assert confirmed['status'] == 'confirmed'
    current = visits.detail('alice', visit['id'])
    assert current['visit']['customer_id'] is None
    assert current['visit']['revision'] != detail['visit']['revision']
    assert any('客户' in warning for warning in current['warnings'])
    assert crm._db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0


def test_recap_only_explicit_own_promise_can_be_adopted(services):
    crm, materials, visits = services
    visit = create(visits, occurred_at=NOW)
    add(visits, visit, '我答应发送方案，明天。', role='recap')
    process(materials)
    detail = visits.detail('alice', visit['id'])
    action = detail['actions'][0]
    assert action['kind'] == 'commitment'
    assert action['needs_review'] is False
    adopted = visits.adopt('alice', visit['id'], action['key'], detail['visit']['revision'])
    assert adopted['proposal']['status'] == 'pending'
    assert adopted['proposal']['remind_at'] == NOW + 86400
    assert crm._db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0


def test_recap_named_customer_promise_is_observation(services):
    _, materials, visits = services
    class NamedOrganizer:
        async def organize(self, text, now, context):
            return {'summary': text, 'key_points': [], 'open_questions': [], 'actions': [
                {'title': '发送资料', 'kind': 'commitment', 'evidence': '王总答应发送资料，明天',
                 'owner_hint': '王总', 'remind_at': now + 86400, 'reason': '转述'}]}
    materials.organizer = NamedOrganizer()
    visit = create(visits, occurred_at=NOW)
    add(visits, visit, '王总答应发送资料，明天。', role='recap')
    process(materials)
    action = visits.detail('alice', visit['id'])['actions'][0]
    assert action['kind'] == 'suggestion' and action['needs_review']


def test_recap_direct_timed_own_intention_preserves_proposal(services):
    _, materials, visits = services
    class TimedOrganizer:
        async def organize(self, text, now, context):
            return {'summary': text, 'key_points': [], 'open_questions': [], 'actions': [
                {'title': '去拜访', 'kind': 'commitment', 'evidence': '我明天去拜访',
                 'owner_hint': '我', 'remind_at': now + 86400, 'reason': '明确我的计划'}]}
    materials.organizer = TimedOrganizer()
    visit = create(visits, occurred_at=NOW)
    add(visits, visit, '我明天去拜访。', role='recap')
    process(materials)
    detail = visits.detail('alice', visit['id'])
    action = detail['actions'][0]
    assert action['kind'] == 'commitment' and action['needs_review'] is False
    adopted = visits.adopt('alice', visit['id'], action['key'], detail['visit']['revision'])
    assert adopted['proposal']['remind_at'] == NOW + 86400


def test_conflicted_new_material_action_does_not_link_old_adoption(services):
    crm, materials, visits = services
    ca = crm.create_customer('alice', {'name': '甲医院'}, NOW)
    cb = crm.create_customer('alice', {'name': '乙医院'}, NOW)
    visit = create(visits, customer_id=ca['id'], occurred_at=NOW)
    added = add(visits, visit, '我答应发送方案，明天。')
    process(materials)
    detail = visits.detail('alice', visit['id'])
    adopted = visits.adopt('alice', visit['id'], detail['actions'][0]['key'], detail['visit']['revision'])
    crm.execute('alice', 'confirm-conflict', {'action': 'confirm', 'proposal_id': adopted['proposal']['id']}, NOW)
    raw = materials.detail('alice', added['material']['id'])['material']
    materials.update('alice', raw['id'], {'revision': raw['revision'], 'customer_id': cb['id']})
    process(materials)
    detail = visits.detail('alice', visit['id'])
    assert detail['actions'][0]['needs_review']
    result = visits.adopt('alice', visit['id'], detail['actions'][0]['key'], detail['visit']['revision'])
    assert result['record']['id'] == adopted['record']['id']
    assert result['record']['customer_id'] == ca['id']
    assert result['record']['remind_at'] == NOW + 86400
    raw = materials.detail('alice', raw['id'])
    assert raw['analysis']['actions'][0]['adopted_record_id'] is None
    assert crm._db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1


def test_editing_aligned_visit_metadata_updates_sources_preserves_confirmed_history(services):
    crm, materials, visits = services
    ca = crm.create_customer('alice', {'name': '甲医院'}, NOW)
    cb = crm.create_customer('alice', {'name': '乙医院'}, NOW)
    visit = create(visits, customer_id=ca['id'], occurred_at=NOW)
    added = add(visits, visit, '我答应发送方案，明天。')
    process(materials)
    detail = visits.detail('alice', visit['id'])
    adopted = visits.adopt('alice', visit['id'], detail['actions'][0]['key'], detail['visit']['revision'])
    crm.execute('alice', 'confirm-before-edit', {'action': 'confirm', 'proposal_id': adopted['proposal']['id']}, NOW)
    updated = visits.update('alice', visit['id'], {'revision': detail['visit']['revision'],
        'customer_id': cb['id'], 'occurred_at': NOW + 86400 * 3})
    assert updated['customer_id'] == cb['id'] and updated['occurred_at'] == NOW + 86400 * 3
    raw = materials.detail('alice', added['material']['id'])['material']
    assert raw['customer_id'] == cb['id'] and raw['occurred_at'] == NOW + 86400 * 3
    assert raw['status'] == 'queued'
    old = crm.get_record('alice', adopted['record']['id'])
    assert old['customer_id'] == ca['id'] and old['remind_at'] == NOW + 86400
    process(materials)
    current = visits.detail('alice', visit['id'])
    assert current['actions'][0]['needs_review']
    assert any('客户与交流不同' in warning for warning in current['warnings'])
    result = visits.adopt('alice', visit['id'], current['actions'][0]['key'], current['visit']['revision'])
    assert result['record']['customer_id'] == ca['id']
    assert materials.detail('alice', added['material']['id'])['analysis']['actions'][0]['adopted_record_id'] is None
    assert crm._db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1


def test_visit_update_does_not_absorb_independently_changed_source(services):
    crm, materials, visits = services
    ca = crm.create_customer('alice', {'name': '甲医院'}, NOW)
    cb = crm.create_customer('alice', {'name': '乙医院'}, NOW)
    visit = create(visits, customer_id=ca['id'], occurred_at=NOW)
    added = add(visits, visit)
    raw = materials.detail('alice', added['material']['id'])['material']
    materials.update('alice', raw['id'], {'revision': raw['revision'], 'customer_id': cb['id'], 'occurred_at': NOW + 86400})
    detail = visits.detail('alice', visit['id'])
    with pytest.raises(ValueError, match='独立变化'):
        visits.update('alice', visit['id'], {'revision': detail['visit']['revision'], 'customer_id': cb['id'], 'occurred_at': NOW + 86400})
    assert visits.detail('alice', visit['id'])['visit']['customer_id'] == ca['id']


def test_explicit_visit_assignment_to_c_confirmed_customer_is_allowed(services):
    crm, materials, visits = services
    class Parser:
        async def parse(self, text, now, context=None):
            return {'intent': 'create', 'customer_name': '星海医院', 'contact_name': None,
                'basic': {}, 'basic_evidence': {}, 'contact': {}, 'contact_evidence': {}, 'attributes': []}
    materials.customer_parser = Parser()
    visit = create(visits)
    add(visits, visit, '星海医院。我答应发送方案。')
    process(materials)
    detail = visits.detail('alice', visit['id'])
    confirmed = crm.confirm_customer_draft('alice', detail['customer_drafts'][0]['id'], NOW)
    current = visits.detail('alice', visit['id'])
    updated = visits.update('alice', visit['id'], {'revision': current['visit']['revision'], 'customer_id': confirmed['customer_id']})
    assert updated['customer_id'] == confirmed['customer_id']


def test_date_only_visit_edit_keeps_new_action_unlinked_to_original_reminder(services):
    crm, materials, visits = services
    visit = create(visits, occurred_at=NOW)
    source = add(visits, visit, '我答应发送方案，明天。')
    process(materials)
    detail = visits.detail('alice', visit['id'])
    adopted = visits.adopt('alice', visit['id'], detail['actions'][0]['key'], detail['visit']['revision'])
    crm.execute('alice', 'confirm-before-date-edit', {'action': 'confirm', 'proposal_id': adopted['proposal']['id']}, NOW)
    visits.update('alice', visit['id'], {'revision': detail['visit']['revision'], 'occurred_at': NOW + 86400})
    process(materials)
    current = visits.detail('alice', visit['id'])
    assert current['actions'][0]['needs_review']
    result = visits.adopt('alice', visit['id'], current['actions'][0]['key'], current['visit']['revision'])
    assert result['record']['remind_at'] == NOW + 86400
    raw = materials.detail('alice', source['material']['id'])
    assert raw['analysis']['actions'][0]['adopted_record_id'] is None


def test_create_request_key_is_bound_to_normalized_original_payload(services):
    _, _, visits = services
    first = visits.create('alice', {'title': '  original visit  ', 'occurred_at': NOW}, source_id='create-retry-key')
    assert visits.create('alice', {'title': 'original visit', 'occurred_at': int(NOW)}, source_id='create-retry-key')['id'] == first['id']
    with pytest.raises(ValueError, match='不同内容'):
        visits.create('alice', {'title': 'changed visit', 'occurred_at': NOW}, source_id='create-retry-key')
    assert visits.list('alice')['total'] == 1
    assert visits.create('bob', {'title': 'other owner'}, source_id='create-retry-key')['id'] != first['id']


def test_source_response_lost_equal_retry_recovers_but_edited_ui_draft_is_rejected(services):
    _, materials, visits = services
    visit = create(visits)
    first_payload = {'role': 'recap', 'provider': 'manual', 'title': '  source title  ', 'text': 'first source text'}
    first = visits.add_material('alice', visit['id'], first_payload, source_id='ui-request-key')
    equal = visits.add_material('alice', visit['id'], {**first_payload, 'title': 'source title'}, source_id='ui-request-key')
    assert equal['material']['id'] == first['material']['id']
    edited_draft = {**first_payload, 'text': 'first source text plus important new feedback'}
    with pytest.raises(ValueError, match='不同内容'):
        visits.add_material('alice', visit['id'], edited_draft, source_id='ui-request-key')
    assert materials.detail('alice', first['material']['id'])['text'] == 'first source text'
    assert len(visits.detail('alice', visit['id'])['sources']) == 1


def test_original_source_retry_after_visit_metadata_change_does_not_change_payload_identity(services):
    crm, _, visits = services
    visit = create(visits)
    payload = {'role': 'recap', 'provider': 'manual', 'title': 'recap', 'text': 'synthetic original source'}
    first = visits.add_material('alice', visit['id'], payload, source_id='metadata-retry-key')
    customer = crm.create_customer('alice', {'name': 'synthetic customer'}, NOW)
    detail = visits.detail('alice', visit['id'])
    visits.update('alice', visit['id'], {'revision': detail['visit']['revision'], 'customer_id': customer['id'], 'occurred_at': NOW})
    retried = visits.add_material('alice', visit['id'], payload, source_id='metadata-retry-key')
    assert retried['material']['id'] == first['material']['id']
    assert retried['material']['customer_id'] == customer['id']


def test_existing_material_request_key_cannot_change_role_or_material(services):
    _, materials, visits = services
    visit = create(visits)
    first = materials.enqueue('alice', {'provider': 'manual', 'title': 'first', 'text': 'one'})
    second = materials.enqueue('alice', {'provider': 'manual', 'title': 'second', 'text': 'two'})
    payload = {'role': 'recording', 'material_id': first['id']}
    visits.add_material('alice', visit['id'], payload, source_id='existing-material-key')
    for changed in ({**payload, 'role': 'recap'}, {**payload, 'material_id': second['id']}):
        with pytest.raises(ValueError, match='不同内容'):
            visits.add_material('alice', visit['id'], changed, source_id='existing-material-key')
    assert len(visits.detail('alice', visit['id'])['sources']) == 1


def test_unquoted_native_ai_suggestion_can_be_explicitly_adopted_without_schedule(services):
    crm, materials, visits = services
    class SuggestionOrganizer:
        async def organize(self, text, now, context):
            return {'summary': '个人复盘', 'key_points': [], 'open_questions': [], 'actions': [
                {'title': '建议核实客户是否真正关心运维成本', 'kind': 'suggestion', 'evidence': '',
                 'reason': '秘书建议：根据我的观察核实客户实际关切', 'owner_hint': '待确认', 'remind_at': None}]}
    materials.organizer = SuggestionOrganizer()
    visit = create(visits, occurred_at=NOW)
    add(visits, visit, '我感觉客户对运维成本没有说清楚。', role='recap')
    process(materials)
    detail = visits.detail('alice', visit['id'])
    action = detail['actions'][0]
    assert action['kind'] == 'suggestion' and action['needs_review'] is False
    assert action['reason']
    assert action['remind_at'] is None
    adopted = visits.adopt('alice', visit['id'], action['key'], detail['visit']['revision'])
    assert adopted['record']['kind'] == 'action'
    assert '秘书建议' in adopted['record']['content']
    assert adopted['proposal'] is None
    assert crm._db.execute('SELECT COUNT(*) FROM proposals').fetchone()[0] == 0
    assert crm._db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0
    assert crm._db.execute('SELECT COUNT(*) FROM notifications').fetchone()[0] == 0
    raw = materials.detail('alice', detail['sources'][0]['material']['id'])
    assert raw['analysis']['actions'][0]['adopted_record_id'] == adopted['record']['id']


def test_native_suggestion_from_superseded_saved_action_is_not_adoptable(services):
    crm, materials, visits = services
    class SuggestionOrganizer:
        async def organize(self, text, now, context):
            return {'summary': text, 'key_points': [], 'open_questions': [], 'actions': [
                {'title': '核实运维成本', 'kind': 'suggestion', 'reason': '秘书建议', 'owner_hint': '待确认', 'remind_at': None}]}
    materials.organizer = SuggestionOrganizer()
    visit = create(visits)
    added = add(visits, visit, '我的观察是运维成本需核实。', role='recap')
    process(materials)
    raw = materials.detail('alice', added['material']['id'])
    stale_action_id = raw['analysis']['actions'][0]['id']
    materials.update('alice', added['material']['id'], {'revision': raw['material']['revision'], 'text': '更正观察：需要再了解运维成本。'})
    process(materials)
    # An obsolete action is not allowed just because it is an AI suggestion.
    import json
    row = crm._db.execute('SELECT analysis_json FROM crm_materials WHERE id=?', (added['material']['id'],)).fetchone()
    analysis = json.loads(row[0])
    analysis['actions'][0]['id'] = stale_action_id
    crm._db.execute('UPDATE crm_materials SET analysis_json=? WHERE id=?', (json.dumps(analysis), added['material']['id']))
    detail = visits.detail('alice', visit['id'])
    assert detail['actions'][0]['needs_review']
    with pytest.raises(ValueError, match='核对'):
        visits.adopt('alice', visit['id'], detail['actions'][0]['key'], detail['visit']['revision'])


def test_request_payload_hash_survives_cold_reopen(tmp_path):
    path = tmp_path / 'request-reopen.sqlite3'
    crm = CustomerStore(path)
    materials = MaterialService(crm, asyncio.Lock(), clock=lambda: NOW)
    visits = VisitService(crm, materials, materials.lock)
    create_payload = {'title': 'request replay synthetic'}
    source_payload = {'role': 'recap', 'provider': 'manual', 'title': 'recap', 'text': 'first source text'}
    visit = visits.create('alice', create_payload, source_id='durable-create-key')
    added = visits.add_material('alice', visit['id'], source_payload, source_id='durable-source-key')
    crm.close()
    reopened_crm = CustomerStore(path)
    try:
        reopened_materials = MaterialService(reopened_crm, asyncio.Lock(), clock=lambda: NOW)
        reopened = VisitService(reopened_crm, reopened_materials, reopened_materials.lock)
        assert reopened.create('alice', create_payload, source_id='durable-create-key')['id'] == visit['id']
        assert reopened.add_material('alice', visit['id'], source_payload, source_id='durable-source-key')['material']['id'] == added['material']['id']
        with pytest.raises(ValueError, match='不同内容'):
            reopened.add_material('alice', visit['id'], {**source_payload, 'text': 'edited feedback'}, source_id='durable-source-key')
        assert all(len(row[0]) == 64 for row in reopened_crm._db.execute('SELECT payload_hash FROM crm_visit_messages'))
    finally:
        reopened_crm.close()


def test_legacy_message_schema_migrates_without_guessing_original_payload(tmp_path):
    crm = CustomerStore(tmp_path / 'legacy-requests.sqlite3')
    try:
        materials = MaterialService(crm, asyncio.Lock(), clock=lambda: NOW)
        crm._db.execute('CREATE TABLE crm_visit_messages(owner TEXT NOT NULL,source_id TEXT NOT NULL,visit_id INTEGER NOT NULL,'
                        'material_id INTEGER,PRIMARY KEY(owner,source_id))')
        visits = VisitService(crm, materials, materials.lock)
        assert 'payload_hash' in {row['name'] for row in crm._db.execute('PRAGMA table_info(crm_visit_messages)')}
        visit = create(visits)
        crm._db.execute('INSERT INTO crm_visit_messages(owner,source_id,visit_id,material_id) VALUES (?,?,?,NULL)',
                        ('alice', 'legacy-key', visit['id']))
        with pytest.raises(ValueError, match='旧请求编号'):
            visits.create('alice', {'title': visit['title']}, source_id='legacy-key')
        assert visits.list('alice')['total'] == 1
        payload = {'role': 'recap', 'provider': 'manual', 'title': 'recap', 'text': 'first source text'}
        source = visits.add_material('alice', visit['id'], payload)
        crm._db.execute('INSERT INTO crm_visit_messages(owner,source_id,visit_id,material_id) VALUES (?,?,?,?)',
                        ('alice', 'legacy-source-key', visit['id'], source['material']['id']))
        with pytest.raises(ValueError, match='旧请求编号'):
            visits.add_material('alice', visit['id'], payload, source_id='legacy-source-key')
        assert len(visits.detail('alice', visit['id'])['sources']) == 1
    finally:
        crm.close()


def test_http_response_lost_retry_with_changed_draft_returns_error_and_keeps_old_source(services):
    from aiohttp import CookieJar
    from aiohttp.test_utils import TestClient, TestServer
    from secretary.web import create_app, hash_password
    crm, materials, visits = services
    async def scenario():
        client = TestClient(TestServer(create_app(crm, crm, materials.lock, 'alice',
            hash_password('synthetic-password-only'), materials=materials)), cookie_jar=CookieJar(unsafe=True))
        await client.start_server()
        try:
            login = await client.post('/api/login', json={'password': 'synthetic-password-only'})
            headers = {'X-CSRF-Token': (await login.json())['csrf']}
            created = await client.post('/api/visits', json={'title': 'retry flow', 'request_key': 'http-create-key'}, headers=headers)
            visit = (await created.json())['visit']
            create_retry = await client.post('/api/visits', json={'title': 'retry flow', 'request_key': 'http-create-key'}, headers=headers)
            assert create_retry.status == 201 and (await create_retry.json())['visit']['id'] == visit['id']
            changed_create = await client.post('/api/visits', json={'title': 'edited retry title', 'request_key': 'http-create-key'}, headers=headers)
            assert changed_create.status == 400
            assert visits.list('alice')['total'] == 1
            payload = {'role': 'recap', 'provider': 'manual', 'title': 'recap', 'text': 'first source text', 'request_key': 'http-source-key'}
            saved = await client.post(f"/api/visits/{visit['id']}/materials", json=payload, headers=headers)
            assert saved.status == 202
            first = (await saved.json())['material']
            # The UI keeps its draft/key when the first response is lost.
            equal_retry = await client.post(f"/api/visits/{visit['id']}/materials", json=payload, headers=headers)
            assert equal_retry.status == 202
            assert (await equal_retry.json())['material']['id'] == first['id']
            edited_retry = await client.post(f"/api/visits/{visit['id']}/materials",
                json={**payload, 'text': 'first source text plus important new feedback'}, headers=headers)
            assert edited_retry.status == 400
            error = (await edited_retry.json())['error']
            assert '不同内容' in error and '草稿' in error
            assert 'first source text' not in error
            assert materials.detail('alice', first['id'])['text'] == 'first source text'
            assert len(visits.detail('alice', visit['id'])['sources']) == 1
        finally:
            await client.close()
    asyncio.run(scenario())


def test_concurrent_equal_source_requests_from_two_connections_are_idempotent(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = tmp_path / 'request-concurrent.sqlite3'
    stores = [CustomerStore(path), CustomerStore(path)]
    try:
        services = []
        for store in stores:
            materials = MaterialService(store, asyncio.Lock(), clock=lambda: NOW)
            services.append(VisitService(store, materials, materials.lock))
        visit = create(services[0])
        payload = {'role': 'recap', 'provider': 'manual', 'title': 'recap', 'text': 'same synthetic source'}
        def save(service):
            return service.add_material('alice', visit['id'], payload, source_id='same-concurrent-key')
        with ThreadPoolExecutor(max_workers=2) as pool:
            saved = list(pool.map(save, services))
        assert saved[0]['material']['id'] == saved[1]['material']['id']
        assert len(services[0].detail('alice', visit['id'])['sources']) == 1
    finally:
        for store in stores:
            store.close()


def test_supplement_category_defaults_to_content_inference_and_explicit_category_survives(services):
    _, materials, visits = services
    visit = create(visits)
    automatic = add(visits, visit, '客户说需要先了解现有数据库的兼容性。', role='supplement')
    assert automatic['material']['category'] == 'auto'
    explicit = add(visits, visit, '客户补充了系统边界。', role='supplement', category='memo')
    assert explicit['material']['category'] == 'memo'
    process(materials)
    assert materials.detail('alice', automatic['material']['id'])['material']['category'] == 'conversation'
    assert materials.detail('alice', explicit['material']['id'])['material']['category'] == 'memo'
