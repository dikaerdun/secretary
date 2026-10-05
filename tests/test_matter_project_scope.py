"""Project goal scopes use fictional, isolated SQLite data only."""
import asyncio
import json

import pytest
from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from deploy.training import PASSWORD, build_training_app, training_config
from secretary.customer_store import CustomerStore
from secretary.matters import MatterService
from secretary.sales_workspace import SalesWorkspace

NOW = 1800000000.0
OWNER = 'fictional-project-owner'


def seed(crm, workspace, service, owner=OWNER):
    customer = crm.create_customer(owner, {'name': '虚构双项目单位'}, NOW)
    other = crm.create_customer(owner, {'name': '虚构另一单位'}, NOW)
    projects = [workspace.create_opportunity(owner, customer['id'], {'name': name})
                for name in ('虚构签名项目', '虚构加密项目')]
    other_project = workspace.create_opportunity(owner, other['id'], {'name': '虚构隔离项目'})
    foreign_customer = crm.create_customer('other-owner', {'name': '其他账号单位'}, NOW)
    foreign_project = workspace.create_opportunity('other-owner', foreign_customer['id'], {'name': '其他账号项目'})
    result = {'customer': customer, 'other': other, 'projects': projects,
              'other_project': other_project, 'foreign_project': foreign_project}

    def record(title, customer_id, kind='note', parent=None, record_owner=owner):
        return crm.create_record(record_owner, {'title': title, 'content': title, 'kind': kind,
            'customer_id': customer_id, 'parent_record_id': parent, 'status': 'following' if kind == 'action' else 'done'}, NOW)

    def matter(title, project, count=1, status='following', archived=False, matter_owner=owner):
        actions = [record(title + '步骤' + str(index), project['customer_id'], 'action', record_owner=matter_owner)
                   for index in range(count)]
        item = service.create(matter_owner, {'title': title, 'objective': title + '成果',
            'opportunity_id': project['id'], 'action_record_ids': [row['id'] for row in actions],
            'request_id': title})['matter']
        if count > 1:
            crm.update_record(matter_owner, actions[0]['id'], {'status': 'done'}, NOW + 1)
        if status != 'following':
            item = service.update(matter_owner, item['id'], {'status': status,
                'expected_revision': item['revision'], 'request_id': title + '-state'})['matter']
        if archived:
            item = service.lifecycle(matter_owner, item['id'], {'visibility': 'archived',
                'expected_revision': item['revision'], 'reminder_action': 'keep',
                'request_id': title + '-archive'})['matter']
        return item

    result['first_matters'] = [matter('签名方案准备', projects[0], count=2), matter('签名等待反馈', projects[0], status='waiting')]
    result['archived'] = matter('签名历史目标', projects[0], archived=True)
    result['second_matter'] = matter('加密试点推进', projects[1])
    result['other_matter'] = matter('其他单位目标', other_project)
    result['foreign_matter'] = matter('其他账号目标', foreign_project, matter_owner='other-owner')
    pending = []
    for index, project in enumerate(projects):
        source = record(project['name'] + '未归组原话', customer['id'])
        actions = [record(project['name'] + '未归组步骤' + str(n), customer['id'], 'action', source['id'])
                   for n in range(2 if index == 0 else 1)]
        for action in actions:
            workspace.link(owner, 'record', action['id'], project['id'])
        pending.append({'source': source, 'actions': actions})
    result['pending'] = pending
    result['unit_note'] = record('单位层面的想法', customer['id'])
    result['other_note'] = record('另一单位的想法', other['id'])
    return result


@pytest.fixture
def scoped(tmp_path):
    crm = CustomerStore(tmp_path / 'fictional-project-scopes.sqlite3')
    workspace = SalesWorkspace(crm, clock=lambda: NOW)
    service = MatterService(crm, clock=lambda: NOW)
    yield crm, workspace, service, seed(crm, workspace, service)
    crm.close()


def test_same_customer_project_scope_has_own_goals_summary_and_ungrouped_counts(scoped):
    crm, workspace, service, world = scoped
    first, second = world['projects']
    data = service.list(OWNER, customer_id=world['customer']['id'], opportunity_id=first['id'])
    assert {row['id'] for row in data['items']} == {row['id'] for row in world['first_matters']}
    assert data['total'] == data['summary']['active'] == 2
    assert data['summary']['following'] == data['summary']['waiting'] == 1
    assert data['summary']['archived'] == 1
    assert data['summary']['action_count'] == 3 and data['summary']['completed_action_count'] == 1
    assert (data['ungrouped_count'], data['ungrouped_action_count'], data['ungrouped_record_count']) == (1, 2, 3)
    second_data = service.list(OWNER, opportunity_id=second['id'])
    assert [row['id'] for row in second_data['items']] == [world['second_matter']['id']]
    assert second_data['summary']['active'] == 1 and second_data['summary']['archived'] == 0
    assert (second_data['ungrouped_count'], second_data['ungrouped_action_count'], second_data['ungrouped_record_count']) == (1, 1, 2)


def test_customer_scope_includes_its_projects_but_not_another_customer(scoped):
    _, _, service, world = scoped
    data = service.list(OWNER, customer_id=world['customer']['id'])
    assert data['total'] == 3 and data['summary']['active'] == 3 and data['summary']['archived'] == 1
    assert data['summary']['action_count'] == 4
    assert (data['ungrouped_count'], data['ungrouped_action_count'], data['ungrouped_record_count']) == (2, 3, 6)
    assert world['other_matter']['id'] not in {row['id'] for row in data['items']}


def test_unfiltered_semantics_and_existing_positional_parameters_remain(scoped):
    _, _, service, world = scoped
    data = service.list(OWNER)
    assert data['total'] == data['summary']['active'] == 4
    assert data['summary']['archived'] == 1 and data['summary']['action_count'] == 5
    assert (data['ungrouped_count'], data['ungrouped_record_count']) == (2, 7)
    positional = service.list(OWNER, '', world['customer']['id'], '', 'active', 1, 1, world['projects'][0]['id'])
    assert positional['total'] == 2 and len(positional['items']) == 1 and positional['pages'] == 2
    assert positional['summary']['active'] == 2
    legacy = service.list(OWNER, '', world['customer']['id'], '', 'active', 1, 1)
    assert legacy['total'] == 3 and legacy['page_size'] == 1


def test_scope_summary_is_complete_while_search_status_and_page_filter_items(scoped):
    _, _, service, world = scoped
    data = service.list(OWNER, q='等待', status='waiting', opportunity_id=world['projects'][0]['id'], page_size=1)
    assert data['total'] == 1 and data['items'][0]['title'] == '签名等待反馈'
    assert data['summary']['active'] == 2 and data['summary']['following'] == 1
    archived = service.list(OWNER, opportunity_id=world['projects'][0]['id'], visibility='archived')
    assert archived['total'] == 1 and archived['items'][0]['id'] == world['archived']['id']
    assert archived['summary']['action_count'] == 3


def test_foreign_owner_and_mismatched_customer_scopes_are_empty(scoped):
    _, _, service, world = scoped
    for owner, scope in [(OWNER, {'opportunity_id': world['foreign_project']['id']}),
                         ('other-owner', {'opportunity_id': world['projects'][0]['id']}),
                         (OWNER, {'customer_id': world['other']['id'], 'opportunity_id': world['projects'][0]['id']})]:
        data = service.list(owner, **scope)
        assert data['items'] == [] and data['total'] == 0
        assert all(value == 0 for value in data['summary'].values())
        assert data['ungrouped_count'] == data['ungrouped_record_count'] == 0


def test_only_selected_project_details_are_loaded_and_list_is_read_only(scoped, monkeypatch):
    crm, _, service, world = scoped
    original = service._detail
    ids = []
    def traced(db, owner, identifier):
        ids.append(identifier)
        return original(db, owner, identifier)
    monkeypatch.setattr(service, '_detail', traced)
    writes = crm._db.total_changes
    service.list(OWNER, opportunity_id=world['projects'][0]['id'])
    assert set(ids) == {row['id'] for row in world['first_matters']} | {world['archived']['id']}
    assert crm._db.total_changes == writes


def test_stale_project_snapshot_is_not_a_scoped_candidate_or_record(scoped):
    crm, _, service, world = scoped
    action = world['pending'][1]['actions'][0]
    crm.update_record(OWNER, action['id'], {'content': '归属依据已经修改'}, NOW + 2)
    data = service.list(OWNER, opportunity_id=world['projects'][1]['id'])
    assert data['ungrouped_count'] == data['ungrouped_action_count'] == data['ungrouped_record_count'] == 0
    assert service.list(OWNER)['ungrouped_record_count'] == 7


def insert_plan(crm, record, data):
    with crm._transaction() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS crm_secretary_plans(id INTEGER PRIMARY KEY,owner TEXT,record_id INTEGER,
            visit_id INTEGER,data_json TEXT,revision INTEGER,created_at REAL,updated_at REAL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS crm_secretary_turns(id INTEGER PRIMARY KEY,owner TEXT,plan_id INTEGER,record_id INTEGER)''')
        return db.execute('INSERT INTO crm_secretary_plans(owner,record_id,data_json,revision,created_at,updated_at) VALUES(?,?,?,?,?,?)',
            (OWNER, record['id'], json.dumps({'title': '虚构项目拜访', 'date': '2026-10-08', 'status': 'draft', **data}), 1, NOW, NOW)).lastrowid


def test_shared_source_plan_attaches_only_to_its_matching_project(scoped):
    crm, workspace, service, world = scoped
    source = world['pending'][0]['source']
    action = crm.create_record(OWNER, {'title': '同次交流的项目二行动', 'content': '同一原话支持项目二',
        'kind': 'action', 'customer_id': world['customer']['id'], 'parent_record_id': source['id']}, NOW)
    workspace.link(OWNER, 'record', action['id'], world['projects'][1]['id'])
    plan_id = insert_plan(crm, source, {'customer_id': world['customer']['id'], 'opportunity_id': world['projects'][1]['id']})
    writes = crm._db.total_changes
    candidates = service.candidates(OWNER)['items']
    with_plan = [row for row in candidates if plan_id in row['plan_ids']]
    assert len(with_plan) == 1 and with_plan[0]['opportunity_id'] == world['projects'][1]['id']
    assert with_plan[0]['action_record_ids'] == [action['id']]
    assert service.list(OWNER, opportunity_id=world['projects'][0]['id'])['ungrouped_plan_count'] == 0
    assert service.list(OWNER, opportunity_id=world['projects'][1]['id'])['ungrouped_plan_count'] == 1
    assert crm._db.total_changes == writes


def test_project_plan_without_matching_source_bucket_stays_independent(scoped):
    crm, _, service, world = scoped
    plan_id = insert_plan(crm, world['pending'][0]['source'], {
        'customer_id': world['customer']['id'], 'opportunity_id': world['projects'][1]['id']})
    with_plan = next(row for row in service.candidates(OWNER)['items'] if plan_id in row['plan_ids'])
    assert with_plan['key'] == 'plan:' + str(plan_id)
    assert with_plan['action_record_ids'] == []
    assert with_plan['opportunity_id'] == world['projects'][1]['id']


@pytest.mark.parametrize('invalid', ['other-customer', 'other-owner', 'archived', 'malformed'])
def test_invalid_plan_project_degrades_to_no_project_without_writes(scoped, invalid):
    crm, _, service, world = scoped
    if invalid == 'other-customer':
        project_id = world['other_project']['id']
    elif invalid == 'other-owner':
        project_id = world['foreign_project']['id']
    elif invalid == 'malformed':
        project_id = {'id': world['projects'][0]['id']}
    else:
        project_id = world['projects'][0]['id']
        with crm._transaction() as db:
            db.execute('UPDATE crm_opportunities SET archived=1 WHERE owner=? AND id=?', (OWNER, project_id))
    plan_id = insert_plan(crm, world['pending'][0]['source'], {
        'customer_id': world['customer']['id'], 'opportunity_id': project_id})
    writes = crm._db.total_changes
    with_plan = next(row for row in service.candidates(OWNER)['items'] if plan_id in row['plan_ids'])
    assert with_plan['customer_id'] == world['customer']['id'] and with_plan['opportunity_id'] is None
    assert crm._db.total_changes == writes


@pytest.mark.parametrize('foreign', [False, True])
def test_corrupt_project_customer_or_owner_link_cannot_leak_scoped_counts(scoped, foreign):
    crm, workspace, service, world = scoped
    raw = crm.create_record(OWNER, {'title': '虚构原话项目归属', 'content': '测试损坏关联',
        'customer_id': world['customer']['id']}, NOW)
    workspace.link(OWNER, 'record', raw['id'], world['projects'][1]['id'])
    target = world['foreign_project'] if foreign else world['other_project']
    ids = [raw['id'], world['pending'][1]['actions'][0]['id']]
    # Simulate a legacy/corrupt link in this disposable SQLite fixture. New
    # writes also have foreign keys; the read boundary still validates scope.
    crm._db.execute('PRAGMA foreign_keys=OFF')
    try:
        with crm._transaction() as db:
            for identifier in ids:
                db.execute('UPDATE crm_opportunity_links SET opportunity_id=? WHERE owner=? AND entity_id=?',
                           (target['id'], OWNER, identifier))
    finally:
        crm._db.execute('PRAGMA foreign_keys=ON')
    data = service.list(OWNER, opportunity_id=target['id'])
    assert data['ungrouped_count'] == data['ungrouped_record_count'] == 0
    original = service.list(OWNER, opportunity_id=world['projects'][1]['id'])
    assert original['ungrouped_count'] == original['ungrouped_record_count'] == 0


@pytest.mark.parametrize('value', [True, 0, -1, 'invalid'])
def test_invalid_project_scope_rejected(scoped, value):
    with pytest.raises(ValueError):
        scoped[2].list(OWNER, opportunity_id=value)


def test_authenticated_project_scope_and_overview_are_consistent(tmp_path):
    async def run():
        app = await build_training_app(training_config(root=tmp_path), clock=lambda: NOW)
        controller = next(route.handler.__self__ for route in app.router.routes()
                          if getattr(route.handler, '__name__', '') == 'dashboard')
        from deploy.training import OWNER as training_owner
        world = seed(controller.crm, controller.sales_workspace, controller.matters, training_owner)
        client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
        await client.start_server()
        try:
            assert (await client.get('/api/matters?opportunity_id=' + str(world['projects'][0]['id']))).status == 401
            assert (await client.post('/api/login', json={'password': PASSWORD})).status == 200
            scopes = []
            for project in world['projects']:
                request = await client.get('/api/matters', params={'customer_id': world['customer']['id'], 'opportunity_id': project['id'], 'page_size': 6})
                assert request.status == 200, await request.text()
                scopes.append(await request.json())
            overview = await (await client.get('/api/overview')).json()
            projects = {project['id']: project for project in overview['projects']}
            for project, scope in zip(world['projects'], scopes):
                assert projects[project['id']]['matters'] == scope
            assert {row['id'] for row in scopes[0]['items']}.isdisjoint(row['id'] for row in scopes[1]['items'])
            foreign = await (await client.get('/api/matters', params={'opportunity_id': world['foreign_project']['id']})).json()
            assert foreign['total'] == foreign['summary']['active'] == foreign['ungrouped_record_count'] == 0
            assert world['foreign_project']['id'] not in projects
            bad = await client.get('/api/matters?opportunity_id=invalid')
            assert bad.status == 400
        finally:
            await client.close()
    asyncio.run(run())
