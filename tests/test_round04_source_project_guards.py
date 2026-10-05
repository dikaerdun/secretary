"""Round04: source roles, reviewed project choices and actual adoption integrity."""
import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from secretary.customer_store import CustomerStore
from secretary.customer_timeline import TimelineService
from secretary.exchange_records import ExchangeRecords
from secretary.exchange_workspace import ExchangeWorkspace, ExchangeConflict
from secretary.materials import MaterialService
from secretary.profile_intelligence import ProfileIntelligence
from secretary.sales_workspace import SalesWorkspace
from secretary.visits import VisitService

NOW, OWNER = 1_800_100_000.0, 'round04-formal-synthetic'


class Organizer:
    async def organize(self, text, now, context):
        return {'summary': text, 'key_points': [], 'open_questions': [], 'actions': [
            {'title': '发送密钥清单', 'kind': 'commitment', 'reason': '原话落实',
             'evidence': '我答应发送密钥清单', 'owner_hint': '我', 'remind_at': None}
        ] if '我答应发送密钥清单' in text else []}


@pytest.fixture
def rig(tmp_path):
    crm = CustomerStore(tmp_path / 'source-project-guards.sqlite3')
    clock, lock = [NOW], asyncio.Lock()
    sales = SalesWorkspace(crm, clock=lambda: clock[0])
    materials = MaterialService(crm, lock, organizer=Organizer(), clock=lambda: clock[0])
    visits = VisitService(crm, materials, lock)
    profile = ProfileIntelligence(crm, sales, clock=lambda: clock[0])
    timeline = TimelineService(crm, sales, visits=visits, clock=lambda: clock[0])
    exchange = ExchangeWorkspace(crm, sales, profile, materials=materials, visits=visits,
                                 timeline=timeline, clock=lambda: clock[0])
    unit = crm.create_customer(OWNER, {'name': '合成用户已录入单位'}, NOW)
    yield SimpleNamespace(crm=crm, sales=sales, materials=materials, visits=visits, profile=profile,
        timeline=timeline, exchange=exchange, clock=clock, unit=unit,
        records=ExchangeRecords(crm, visits, lambda: clock[0]))
    crm.close()


def process(rig):
    while asyncio.run(rig.materials.process_one()):
        pass


def pair(rig, *, conflict=False, standalone=False):
    a = rig.sales.create_opportunity(OWNER, rig.unit['id'], {'name': '合成项目A'})
    b = rig.sales.create_opportunity(OWNER, rig.unit['id'], {'name': '合成项目B'})
    if standalone:
        second = rig.materials.enqueue(OWNER, {'title': '独立行动来源', 'provider': 'manual',
            'customer_id': rig.unit['id'], 'text': '我答应发送密钥清单。'})
        process(rig)
        rig.sales.link(OWNER, 'material', second['id'], b['id'])
        return None, None, second, a, b
    visit = rig.visits.create(OWNER, {'title': '合成多项目拜访', 'customer_id': rig.unit['id'], 'occurred_at': NOW - 86400})
    first = rig.visits.add_material(OWNER, visit['id'], {'title': '项目A来源', 'provider': 'manual',
        'role': 'recording', 'text': '我答应发送密钥清单。' if conflict else '客户说预算尚未审批。'})['material']
    second = rig.visits.add_material(OWNER, visit['id'], {'title': '项目B行动来源', 'provider': 'manual',
        'role': 'supplement', 'text': '我答应发送密钥清单。'})['material']
    process(rig)
    rig.sales.link(OWNER, 'material', first['id'], a['id'])
    rig.sales.link(OWNER, 'material', second['id'], b['id'])
    return visit, first, second, a, b


def action(view):
    return next(item for item in view['items'] if item['kind'] == 'action')


def save_scope(rig, kind, identifier, view, item, scope, **kwargs):
    return rig.exchange.edit_draft(OWNER, kind, identifier, {
        'expected_revision': view['revision'], 'source_revision': view['source_revision'],
        'items': [{'id': item['id'], 'expected_version': item['version'], 'scope': scope, **kwargs}]})


def confirm(rig, kind, identifier, view, item, request='one-adoption', **kwargs):
    body = {'request_id': request, 'expected_revision': view['revision'], 'source_revision': view['source_revision'],
            'items': [{'id': item['id'], 'expected_version': item['version'], **kwargs}]}
    return rig.exchange.confirm(OWNER, kind, identifier, body), body


def saved_project(rig, identifier):
    row = rig.crm._db.execute("SELECT * FROM crm_opportunity_links WHERE owner=? AND entity_type='record' AND entity_id=?",
                              (OWNER, identifier)).fetchone()
    return dict(row) if row else None


@pytest.mark.parametrize('kind', ['visit', 'material'])
def test_explicit_single_cross_project_action_is_previewed_then_adopted_once(rig, kind):
    visit, _, material, _, b = pair(rig, conflict=True)
    identifier = visit['id'] if kind == 'visit' else material['id']
    view = asyncio.run(rig.exchange.prepare(OWNER, kind, identifier))
    old = action(view)
    assert old['status'] == 'blocked' and old['scope']['opportunity_id'] is None
    original_text = rig.materials.detail(OWNER, material['id'])['text']
    with pytest.raises(ValueError, match='跨项目'):
        save_scope(rig, kind, identifier, view, old, {'opportunity_id': b['id']})
    assert rig.crm._db.execute("SELECT count(*) FROM crm_records WHERE kind='action'").fetchone()[0] == 0
    edited = save_scope(rig, kind, identifier, view, old,
        {'opportunity_id': b['id'], 'confirm_single_action': True}, selected=True,
        draft={'title': '按用户核对后的跨项目主事项'})
    chosen = action(edited)
    assert chosen['status'] == 'pending' and chosen['scope']['opportunity_name'] == b['name']
    assert chosen['project_scope']['explicit'] and chosen['project_scope']['confirm_single_action']
    assert len(chosen['project_scope']['references']) == 2
    assert len(chosen['current']['action']['references']) == 2
    result, body = confirm(rig, kind, identifier, edited, chosen)
    assert result['status'] == 'complete', result['results']
    record_id = result['results'][0]['result']['record_id']
    assert saved_project(rig, record_id)['opportunity_id'] == b['id']
    assert rig.crm.get_record(OWNER, record_id)['title'] == '按用户核对后的跨项目主事项'
    assert rig.visits._source_project(rig.crm._db, OWNER, 'record', record_id)['valid']
    assert rig.exchange.confirm(OWNER, kind, identifier, body) == result
    assert rig.crm._db.execute("SELECT count(*) FROM crm_records WHERE kind='action'").fetchone()[0] == 1
    assert rig.crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
    assert rig.materials.detail(OWNER, material['id'])['text'] == original_text


def test_clearing_saved_main_project_restores_conflict_without_changing_sources(rig):
    visit, _, material, _, b = pair(rig, conflict=True)
    view = asyncio.run(rig.exchange.prepare(OWNER, 'visit', visit['id']))
    edited = save_scope(rig, 'visit', visit['id'], view, action(view),
        {'opportunity_id': b['id'], 'confirm_single_action': True}, selected=True)
    cleared = save_scope(rig, 'visit', visit['id'], edited, action(edited), {}, selected=False)
    item = action(cleared)
    assert item['status'] == 'blocked' and not item['selected']
    assert item['scope']['opportunity_id'] is None and not item['project_scope']['explicit']
    assert rig.visits._source_project(rig.crm._db, OWNER, 'material', material['id'])['opportunity_id'] == b['id']
    assert rig.crm._db.execute("SELECT count(*) FROM crm_records WHERE kind='action'").fetchone()[0] == 0


@pytest.mark.parametrize('entry', ['visit', 'material'])
def test_unique_evidence_project_is_identical_through_both_exchange_entries(rig, entry):
    visit, first, material, a, b = pair(rig)
    identifier = visit['id'] if entry == 'visit' else material['id']
    view = asyncio.run(rig.exchange.prepare(OWNER, entry, identifier))
    chosen = action(view)
    assert chosen['scope']['opportunity_id'] == b['id']
    assert {state['id'] for state in chosen['project_scope']['references']} == {material['id']}
    receipt, _ = confirm(rig, entry, identifier, view, chosen)
    assert receipt['status'] == 'complete', receipt['results']
    child = receipt['results'][0]['result']['record_id']
    assert saved_project(rig, child)['opportunity_id'] == b['id']
    assert rig.visits._source_project(rig.crm._db, OWNER, 'material', first['id'])['opportunity_id'] == a['id']
    assert rig.crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0


@pytest.mark.parametrize('target', ['other_unit', 'other_owner', 'unreferenced'])
def test_main_project_cannot_select_unseen_unrelated_project(rig, target):
    visit, _, _, _, _ = pair(rig, conflict=True)
    owner = 'foreign-owner' if target == 'other_owner' else OWNER
    unit = rig.crm.create_customer(owner, {'name': '合成外部单位'}, NOW) if target != 'unreferenced' else rig.unit
    project = rig.sales.create_opportunity(owner, unit['id'], {'name': '不在原引用的项目'})
    view = asyncio.run(rig.exchange.prepare(OWNER, 'visit', visit['id']))
    with pytest.raises(ValueError, match='当前有效来源'):
        save_scope(rig, 'visit', visit['id'], view, action(view),
            {'opportunity_id': project['id'], 'confirm_single_action': True})
    assert rig.crm._db.execute('SELECT count(*) FROM crm_visit_adoptions').fetchone()[0] == 0


def test_changed_reference_scope_rejects_seen_direct_tokens_before_write(rig):
    visit, _, material, a, _ = pair(rig)
    scopes = rig.visits.material_action_scopes(OWNER, material['id'])
    action_id, seen = next(iter(scopes.items()))
    revision = rig.materials.detail(OWNER, material['id'])['material']['revision']
    rig.sales.link(OWNER, 'material', material['id'], a['id'])
    assert rig.materials.detail(OWNER, material['id'])['material']['revision'] == revision
    with pytest.raises(ValueError, match='已有变化'):
        rig.materials.adopt(OWNER, material['id'], action_id, revision,
            expected_visit_revision=seen['visit_revision'], project_scope_revision=seen['project_scope_revision'])
    latest = rig.visits.detail(OWNER, visit['id'])
    with pytest.raises(ValueError, match='项目范围已有变化'):
        rig.visits.adopt(OWNER, visit['id'], latest['actions'][0]['key'], latest['visit']['revision'],
                        project_scope_revision=seen['project_scope_revision'])
    assert rig.crm._db.execute('SELECT count(*) FROM crm_visit_adoptions').fetchone()[0] == 0


def test_standalone_source_project_token_guards_change_and_links_actual_child(rig):
    _, _, material, a, _ = pair(rig, standalone=True)
    action_id, seen = next(iter(rig.visits.material_action_scopes(OWNER, material['id']).items()))
    revision = rig.materials.detail(OWNER, material['id'])['material']['revision']
    rig.sales.link(OWNER, 'material', material['id'], a['id'])
    with pytest.raises(ValueError, match='项目范围已变化'):
        rig.materials.adopt(OWNER, material['id'], action_id, revision, project_scope_revision=seen['project_scope_revision'])
    assert rig.crm._db.execute("SELECT count(*) FROM crm_records WHERE kind='action'").fetchone()[0] == 0
    current = rig.visits.material_action_scopes(OWNER, material['id'])[action_id]
    adopted = rig.materials.adopt(OWNER, material['id'], action_id, revision,
                                project_scope_revision=current['project_scope_revision'])
    assert saved_project(rig, adopted['record']['id'])['opportunity_id'] == a['id']
    assert not rig.timeline.get_event(OWNER, 'record:' + str(adopted['record']['id']))['contact_relations']
    assert rig.crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0


def test_project_link_failure_rolls_back_all_adoption_writes(rig):
    visit, _, material, _, _ = pair(rig)
    detail = rig.visits.detail(OWNER, visit['id'])
    before = [dict(row) for row in rig.crm._db.execute('SELECT * FROM crm_records ORDER BY id')]
    rig.crm._db.execute("CREATE TRIGGER synthetic_block_project BEFORE INSERT ON crm_opportunity_link_history WHEN NEW.entity_type='record' BEGIN SELECT RAISE(ABORT,'synthetic-project-failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='synthetic-project-failure'):
        rig.visits.adopt(OWNER, visit['id'], detail['actions'][0]['key'], detail['visit']['revision'])
    assert [dict(row) for row in rig.crm._db.execute('SELECT * FROM crm_records ORDER BY id')] == before
    assert rig.crm._db.execute('SELECT count(*) FROM crm_visit_adoptions').fetchone()[0] == 0
    assert rig.crm._db.execute('SELECT count(*) FROM crm_material_actions WHERE record_id IS NOT NULL').fetchone()[0] == 0
    assert rig.materials.detail(OWNER, material['id'])['text'] == '我答应发送密钥清单。'


def test_project_edit_during_interrupted_adoption_is_preserved(rig, monkeypatch):
    visit, _, _, a, _ = pair(rig)
    view = asyncio.run(rig.exchange.prepare(OWNER, 'visit', visit['id']))
    item = action(view)
    original = rig.exchange._checkpoint
    def stop_after_adopt(owner, batch_id, item_id, stage, values):
        result = original(owner, batch_id, item_id, stage, values)
        if stage == 'adopted':
            raise KeyboardInterrupt('synthetic interruption after adoption')
        return result
    monkeypatch.setattr(rig.exchange, '_checkpoint', stop_after_adopt)
    with pytest.raises(KeyboardInterrupt):
        confirm(rig, 'visit', visit['id'], view, item, draft={'title': '旧准备稿标题'})
    record_id = rig.crm._db.execute('SELECT record_id FROM crm_visit_adoptions').fetchone()[0]
    rig.sales.link(OWNER, 'record', record_id, a['id'])
    before = saved_project(rig, record_id)
    monkeypatch.setattr(rig.exchange, '_checkpoint', original)
    receipt, _ = confirm(rig, 'visit', visit['id'], view, item, draft={'title': '旧准备稿标题'})
    assert receipt['status'] == 'partial' and receipt['results'][0]['status'] == 'conflict'
    assert saved_project(rig, record_id) == before
    assert rig.crm.get_record(OWNER, record_id)['title'] == '发送密钥清单'
    assert rig.crm._db.execute('SELECT count(*) FROM crm_visit_adoptions').fetchone()[0] == 1


def test_adopted_manual_project_is_read_back_and_retry_never_overwrites(rig):
    visit, _, _, a, _ = pair(rig)
    detail = rig.visits.detail(OWNER, visit['id'])
    created = rig.visits.adopt(OWNER, visit['id'], detail['actions'][0]['key'], detail['visit']['revision'])
    rig.sales.link(OWNER, 'record', created['record']['id'], a['id'])
    current = rig.visits.detail(OWNER, visit['id'])
    replay = rig.visits.adopt(OWNER, visit['id'], current['actions'][0]['key'], current['visit']['revision'])
    assert replay['record']['id'] == created['record']['id']
    assert replay['project_scope']['opportunity_id'] == a['id']
    assert saved_project(rig, created['record']['id'])['opportunity_id'] == a['id']
    exchange = rig.exchange.get(OWNER, 'visit', visit['id'])
    assert action(exchange)['scope']['opportunity_id'] == a['id']


def test_invalid_source_scope_cannot_be_repaired_by_picking_a_main_project(rig):
    visit, _, material, _, b = pair(rig, conflict=True)
    current = rig.materials.detail(OWNER, material['id'])['material']
    rig.materials.update(OWNER, material['id'], {'revision': current['revision'], 'title': '纠正后材料'})
    process(rig)
    view = asyncio.run(rig.exchange.prepare(OWNER, 'visit', visit['id']))
    item = action(view)
    assert item['project_scope']['status'] == 'needs_review'
    assert any(state['invalid'] for state in item['project_scope']['references'])
    with pytest.raises(ValueError, match='当前有效来源'):
        save_scope(rig, 'visit', visit['id'], view, item, {'opportunity_id': b['id'], 'confirm_single_action': True})
    assert rig.crm._db.execute('SELECT count(*) FROM crm_visit_adoptions').fetchone()[0] == 0
