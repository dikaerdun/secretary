"""Authenticated Round04 source/project routes, all data and APIs are synthetic."""
import asyncio
from contextlib import asynccontextmanager

import pytest
from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from secretary.customer_store import CustomerStore
from secretary.materials import MaterialService
from secretary.store import Store
from secretary.web import create_app, hash_password

NOW, OWNER = 1_800_100_000.0, 'round04-http-synthetic'


class Organizer:
    async def organize(self, text, now, context):
        return {'summary': text, 'key_points': [], 'open_questions': [], 'actions': [
            {'title': '发送密钥清单', 'kind': 'commitment', 'reason': '原话落实',
             'evidence': '我答应发送密钥清单', 'owner_hint': '我', 'remind_at': None}
        ] if '我答应发送密钥清单' in text else []}


@asynccontextmanager
async def context(tmp_path, *, conflict=False, standalone=False):
    path = tmp_path / 'round04-http.sqlite3'
    crm, store, lock = CustomerStore(path), Store(path), asyncio.Lock()
    materials = MaterialService(crm, lock, organizer=Organizer(), clock=lambda: NOW)
    app = create_app(store, crm, lock, OWNER, hash_password('isolated-only-password'),
                     materials=materials, clock=lambda: NOW)
    controller = app.middlewares[0].__self__
    client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
    await client.start_server()
    try:
        assert (await client.get('/api/visits')).status == 401
        login = await client.post('/api/login', json={'password': 'isolated-only-password'})
        csrf = (await login.json())['csrf']
        async def call(method, url, data=None, status=200):
            result = await client.request(method, url, json=data, headers={'X-CSRF-Token': csrf})
            body = await result.json()
            assert result.status == status, (url, result.status, body)
            return body
        unit = crm.create_customer(OWNER, {'name': '合成HTTP单位'}, NOW)
        a = controller.sales_workspace.create_opportunity(OWNER, unit['id'], {'name': 'HTTP项目A'})
        b = controller.sales_workspace.create_opportunity(OWNER, unit['id'], {'name': 'HTTP项目B'})
        visit = None
        if standalone:
            second = materials.enqueue(OWNER, {'title': '合成单份材料', 'provider': 'manual',
                'customer_id': unit['id'], 'text': '我答应发送密钥清单。'})
        else:
            visit = controller.visits.create(OWNER, {'title': '合成双项目交流', 'customer_id': unit['id'], 'occurred_at': NOW - 86400})
            first = controller.visits.add_material(OWNER, visit['id'], {'title': '首份原话', 'provider': 'manual', 'role': 'recording',
                'text': '我答应发送密钥清单。' if conflict else '客户说行业是金融。'})['material']
            second = controller.visits.add_material(OWNER, visit['id'], {'title': '第二份行动原话', 'provider': 'manual',
                'role': 'supplement', 'text': '我答应发送密钥清单。'})['material']
        while await materials.process_one():
            pass
        if visit:
            controller.sales_workspace.link(OWNER, 'material', first['id'], a['id'])
        controller.sales_workspace.link(OWNER, 'material', second['id'], b['id'])
        yield client, call, controller, unit, visit, second, a, b
    finally:
        await client.close()
        await materials.close()
        crm.close()
        store.close()


def test_http_material_detail_seen_scope_rejects_late_project_change(tmp_path):
    async def run():
        async with context(tmp_path) as (client, call, controller, _, visit, material, a, _):
            path = f"/api/materials/{material['id']}"
            detail = await call('GET', path)
            action = detail['analysis']['actions'][0]
            assert detail['visit_ref']['revision'] == action['visit_revision']
            assert action['project_scope']['opportunity_name'] == 'HTTP项目B'
            token = action['project_scope_revision']
            controller.sales_workspace.link(OWNER, 'material', material['id'], a['id'])
            body = {'revision': detail['material']['revision'], 'visit_revision': action['visit_revision'],
                    'project_scope_revision': token}
            adopt_url = path + f"/actions/{action['id']}/adopt"
            assert (await client.post(adopt_url, json=body)).status == 403
            await call('POST', adopt_url, body, 400)
            assert controller.crm._db.execute('SELECT count(*) FROM crm_visit_adoptions').fetchone()[0] == 0
            fresh = await call('GET', path)
            changed = fresh['analysis']['actions'][0]
            assert changed['project_scope_revision'] != token
            receipt = await call('POST', adopt_url, {'revision': fresh['material']['revision'],
                'visit_revision': changed['visit_revision'], 'project_scope_revision': changed['project_scope_revision']})
            assert receipt['project_scope']['opportunity_id'] == a['id']
            current_visit = await call('GET', f"/api/visits/{visit['id']}")
            assert receipt['record']['original_content'] == current_visit['actions'][0]['evidence']
            assert (await call('GET', path))['original_version']['text'] == '我答应发送密钥清单。'
            assert controller.crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
            outsider = controller.materials.enqueue('other-owner', {'title': '其他用户来源', 'provider': 'manual', 'text': '不能读取'})
            await call('GET', f"/api/materials/{outsider['id']}", status=404)
            assert (await call('GET', f"/api/visits/{visit['id']}"))['visit']['title'] == '合成双项目交流'
    asyncio.run(run())


@pytest.mark.parametrize('entry', ['visit', 'material', 'standalone'])
def test_http_legacy_adoption_body_remains_compatible_and_returns_actual_project(tmp_path, entry):
    async def run():
        async with context(tmp_path, standalone=entry == 'standalone') as (_, call, controller, _, visit, material, _, b):
            if entry == 'visit':
                path = f"/api/visits/{visit['id']}"
                detail = await call('GET', path)
                action = detail['actions'][0]
                result = await call('POST', path + f"/actions/{action['key']}/adopt", {'revision': detail['visit']['revision']})
            else:
                path = f"/api/materials/{material['id']}"
                detail = await call('GET', path)
                action = detail['analysis']['actions'][0]
                assert action['project_scope_revision']
                result = await call('POST', path + f"/actions/{action['id']}/adopt", {'revision': detail['material']['revision']})
            link = controller.visits._source_project(controller.crm._db, OWNER, 'record', result['record']['id'])
            assert link['valid'] and link['opportunity_id'] == b['id']
            assert controller.crm._db.execute("SELECT count(*) FROM crm_records WHERE kind='action'").fetchone()[0] == 1
            assert controller.crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
    asyncio.run(run())


@pytest.mark.parametrize('entry', ['visit', 'material'])
def test_http_conflict_choice_requires_preview_and_does_not_split_or_write_unselected_facts(tmp_path, entry):
    async def run():
        async with context(tmp_path, conflict=True) as (_, call, controller, _, visit, material, _, b):
            identifier = visit['id'] if entry == 'visit' else material['id']
            path = f'/api/exchange-workspaces/{entry}/{identifier}'
            view = (await call('POST', path + '/prepare', {}))['workspace']
            item = next(row for row in view['items'] if row['kind'] == 'action')
            assert item['status'] == 'blocked' and len(item['project_scope']['options']) == 2
            body = {'expected_revision': view['revision'], 'source_revision': view['source_revision'],
                'items': [{'id': item['id'], 'expected_version': item['version'], 'selected': True,
                    'scope': {'opportunity_id': b['id'], 'confirm_single_action': True}}]}
            edited = (await call('PATCH', path, body))['workspace']
            chosen = next(row for row in edited['items'] if row['kind'] == 'action')
            assert chosen['scope']['opportunity_name'] == b['name'] and chosen['status'] == 'pending'
            await call('PATCH', path, body, 409)
            request = {'request_id': 'http-one-cross-project-action', 'expected_revision': edited['revision'],
                'source_revision': edited['source_revision'], 'items': [{'id': chosen['id'], 'expected_version': chosen['version']}]}
            receipt = await call('POST', path + '/confirm', request)
            assert receipt['status'] == 'complete', receipt['results']
            child = receipt['results'][0]['result']['record_id']
            assert controller.visits._source_project(controller.crm._db, OWNER, 'record', child)['opportunity_id'] == b['id']
            assert await call('POST', path + '/confirm', request) == receipt
            assert controller.crm._db.execute('SELECT count(DISTINCT record_id) FROM crm_material_actions WHERE record_id IS NOT NULL').fetchone()[0] == 1
            assert controller.crm._db.execute('SELECT count(*) FROM crm_customer_facts').fetchone()[0] == 0
            assert controller.crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
    asyncio.run(run())


def test_http_solo_seen_scope_token_is_real_and_malformed_token_has_no_write(tmp_path):
    async def run():
        async with context(tmp_path, standalone=True) as (_, call, controller, _, _, material, a, _):
            path = f"/api/materials/{material['id']}"
            view = await call('GET', path)
            item = view['analysis']['actions'][0]
            assert view['visit_ref'] is None and item['project_scope']['references'][0]['id'] == material['id']
            adopt_url = path + f"/actions/{item['id']}/adopt"
            await call('POST', adopt_url, {'revision': view['material']['revision'], 'project_scope_revision': 'bad'}, 400)
            controller.sales_workspace.link(OWNER, 'material', material['id'], a['id'])
            await call('POST', adopt_url, {'revision': view['material']['revision'], 'project_scope_revision': item['project_scope_revision']}, 400)
            assert controller.crm._db.execute("SELECT count(*) FROM crm_records WHERE kind='action'").fetchone()[0] == 0
            fresh = await call('GET', path)
            result = await call('POST', adopt_url, {'revision': fresh['material']['revision'],
                'project_scope_revision': fresh['analysis']['actions'][0]['project_scope_revision']})
            assert controller.visits._source_project(controller.crm._db, OWNER, 'record', result['record']['id'])['opportunity_id'] == a['id']
    asyncio.run(run())
