"""New goal actions inherit project attribution; legacy records stay intact."""
import asyncio

import pytest
from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from deploy.training import PASSWORD, OWNER as WEB_OWNER, build_training_app, training_config
from secretary.customer_store import CustomerStore
from secretary.matter_flow_integration import apply_decision
from secretary.matters import MatterService
from secretary.overview import OverviewService
from secretary.sales_workspace import SalesWorkspace
from secretary.secretary_flow import SecretaryFlow
from tests.test_secretary_flow import NOW, ScriptedInterpreter

OWNER = 'fictional-action-project-owner'


class Router:
    def __init__(self, service, matter_id, actions):
        self.service, self.matter_id, self.actions = service, matter_id, actions

    async def route(self, owner, *args, **kwargs):
        matter = self.service.get(owner, self.matter_id)
        return {'kind': 'existing', 'matter_id': matter['id'],
                'base_revision': matter['revision'], 'actions': self.actions}


def setup(crm, workspace, service, flow, owner=OWNER):
    service.secretary_flow = flow
    crm.secretary_flow = flow
    flow.matters = service
    customer = crm.create_customer(owner, {'name': '虚构双项目客户'}, NOW)
    projects = [workspace.create_opportunity(owner, customer['id'], {'name': title})
                for title in ('虚构签名项目', '虚构加密项目')]
    matter = service.create(owner, {'title': '准备签名方案', 'customer_id': customer['id'],
        'opportunity_id': projects[0]['id'], 'request_id': 'initial-goal'})['matter']
    return {'crm': crm, 'workspace': workspace, 'service': service, 'flow': flow,
            'customer': customer, 'projects': projects, 'matter': matter, 'owner': owner}


@pytest.fixture
def world(tmp_path):
    crm = CustomerStore(tmp_path / 'fictional-action-project.sqlite3')
    workspace = SalesWorkspace(crm, clock=lambda: NOW)
    service = MatterService(crm, clock=lambda: NOW)
    flow = SecretaryFlow(crm, workspace, asyncio.Lock(), clock=lambda: NOW)
    yield setup(crm, workspace, service, flow)
    crm.close()


def action(title, **extra):
    return {'title': title, 'content': title, 'evidence': title, **extra}


def turn_input(w, text, key='new-step'):
    matter = w['service'].get(w['owner'], w['matter']['id'])
    return {'text': text, 'matter_id': matter['id'], 'matter_revision': matter['revision'],
            'request_id': key}


def process(w, text, actions, key='new-step'):
    flow = w['flow']
    flow.interpreter = ScriptedInterpreter([{'intent': 'note', 'changes': {}}])
    flow.matter_router = Router(w['service'], w['matter']['id'], actions)
    payload = turn_input(w, text, key)
    submitted = flow.submit(w['owner'], payload)
    assert asyncio.run(flow.process_one())
    turn = flow.turn(w['owner'], submitted['id'])
    assert turn['status'] == 'done', turn.get('error')
    return turn, payload


def link(w, identifier):
    return w['crm']._db.execute("SELECT * FROM crm_opportunity_links WHERE owner=? AND entity_type='record' AND entity_id=?",
        (w['owner'], identifier)).fetchone()


def test_process_one_new_action_is_visible_in_same_project_and_replay_is_single(world):
    text = '准备签名方案PPT'
    turn, payload = process(world, text, [action(text)])
    created = turn['matter_route']['changes'][0]
    assert created['change'] == 'created'
    row = link(world, created['record_id'])
    raw = world['crm'].get_record(OWNER, created['record_id'])
    assert row['opportunity_id'] == world['projects'][0]['id']
    assert row['owner'] == OWNER and row['customer_id'] == world['customer']['id']
    assert row['source_snapshot'] == SalesWorkspace._entity_snapshot(raw)
    assert raw['original_content'] == text
    overview = OverviewService(world['crm'], world['workspace'], clock=lambda: NOW).get(OWNER)
    projects = {project['id']: project for project in overview['projects']}
    assert projects[world['projects'][0]['id']]['progress']['total'] == 1
    assert projects[world['projects'][0]['id']]['open_actions'] == 1
    assert projects[world['projects'][1]['id']]['progress']['total'] == 0
    step = next(item for item in overview['my_actions'] if item['id'] == raw['id'])
    assert step['opportunity_id'] == world['projects'][0]['id']
    db = world['crm']._db
    history_count = db.execute('SELECT COUNT(*) FROM crm_opportunity_link_history').fetchone()[0]
    replayed = world['flow'].submit(OWNER, payload)
    assert replayed['id'] == turn['id']
    assert not asyncio.run(world['flow'].process_one())
    assert db.execute('SELECT COUNT(*) FROM crm_opportunity_link_history').fetchone()[0] == history_count
    assert len(world['service'].get(OWNER, world['matter']['id'])['actions']) == 1
    assert db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0
    assert db.execute('SELECT COUNT(*) FROM notifications').fetchone()[0] == 0


@pytest.mark.parametrize('existing_project', [None, 1])
def test_existing_action_progress_does_not_rewrite_legacy_project(world, existing_project):
    crm, service = world['crm'], world['service']
    raw = crm.create_record(OWNER, {'title': '准备已有方案', 'content': '保留已有原文',
        'kind': 'action', 'status': 'following', 'customer_id': world['customer']['id']}, NOW)
    service.attach(OWNER, world['matter']['id'], 'record', raw['id'], role='action')
    if existing_project is not None:
        world['workspace'].link(OWNER, 'record', raw['id'], world['projects'][existing_project]['id'])
    before = dict(link(world, raw['id'])) if link(world, raw['id']) else None
    history_count = crm._db.execute('SELECT COUNT(*) FROM crm_opportunity_link_history').fetchone()[0]
    text = '准备已有方案'
    turn, _ = process(world, text, [action(text, existing_record_id=raw['id'])])
    assert turn['matter_route']['changes'][0]['change'] == 'updated'
    after = dict(link(world, raw['id'])) if link(world, raw['id']) else None
    assert after == before
    assert crm._db.execute('SELECT COUNT(*) FROM crm_opportunity_link_history').fetchone()[0] == history_count + 1
    # The turn's new evidence may be attributed; the old action is never moved.
    assert crm.get_record(OWNER, raw['id'])['original_content'] == '保留已有原文'


def test_new_action_and_project_link_roll_back_with_outer_turn(world):
    flow, crm, service = world['flow'], world['crm'], world['service']
    text = '整理虚构演示材料'
    queued = flow.submit(OWNER, turn_input(world, text))
    row = crm._db.execute('SELECT * FROM crm_secretary_turns WHERE owner=? AND id=?', (OWNER, queued['id'])).fetchone()
    decision = {'kind': 'existing', 'matter_id': world['matter']['id'],
        'base_revision': service.get(OWNER, world['matter']['id'])['revision'], 'actions': [action(text)]}
    tables = ('crm_records', 'crm_opportunity_links', 'crm_opportunity_link_history', 'crm_matter_links', 'crm_matters')
    before = {table: [dict(item) for item in crm._db.execute('SELECT * FROM ' + table)] for table in tables}
    with pytest.raises(RuntimeError, match='outer failure'):
        with crm._transaction() as db:
            result = apply_decision(service, db, row, decision, text=text, now=NOW,
                scope={'customer_id': world['customer']['id'], 'opportunity_id': world['projects'][0]['id']})
            assert link(world, result['changes'][0]['record_id'])['opportunity_id'] == world['projects'][0]['id']
            raise RuntimeError('outer failure')
    assert {table: [dict(item) for item in crm._db.execute('SELECT * FROM ' + table)] for table in tables} == before


def test_http_new_action_raw_record_and_project_overview_agree(tmp_path):
    async def run():
        app = await build_training_app(training_config(root=tmp_path), clock=lambda: NOW)
        controller = next(route.handler.__self__ for route in app.router.routes()
            if getattr(route.handler, '__name__', '') == 'dashboard')
        w = setup(controller.crm, controller.sales_workspace, controller.matters, controller.secretary_flow, WEB_OWNER)
        text = '准备签名项目汇报材料'
        w['flow'].interpreter = ScriptedInterpreter([{'intent': 'note', 'changes': {}}])
        w['flow'].matter_router = Router(w['service'], w['matter']['id'], [action(text)])
        client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
        await client.start_server()
        try:
            assert (await client.get('/api/overview')).status == 401
            session = await (await client.post('/api/login', json={'password': PASSWORD})).json()
            response = await client.post('/api/secretary/turns', json=turn_input(w, text),
                headers={'X-CSRF-Token': session['csrf']})
            assert response.status == 202, await response.text()
            turn = (await response.json())['turn']
            for _ in range(100):
                turn = (await (await client.get('/api/secretary/turns/' + str(turn['id']))).json())['turn']
                if turn['status'] not in ('queued', 'processing'):
                    break
                await asyncio.sleep(.03)
            assert turn['status'] == 'done', turn.get('error')
            identifier = turn['matter_route']['changes'][0]['record_id']
            detail = await (await client.get('/api/records/' + str(identifier))).json()
            assert detail['record']['opportunity_id'] == w['projects'][0]['id']
            assert detail['record']['opportunity_name'] == w['projects'][0]['name']
            assert detail['record']['project_link_stale'] is False
            assert detail['record']['original_content'] == text
            assert detail['matters'][0]['id'] == w['matter']['id']
            overview = await (await client.get('/api/overview')).json()
            projects = {project['id']: project for project in overview['projects']}
            assert projects[w['projects'][0]['id']]['open_actions'] == 1
            assert projects[w['projects'][0]['id']]['matters']['items'][0]['action_count'] == 1
            assert projects[w['projects'][1]['id']]['open_actions'] == 0
            assert projects[w['projects'][1]['id']]['matters']['total'] == 0
            assert (await client.get('/api/records/' + str(identifier + 100000))).status == 404
        finally:
            await client.close()
    asyncio.run(run())
