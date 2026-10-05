"""HTTP journey oracles for one customer exchange with multiple source materials."""
import asyncio
from pathlib import Path
import tempfile
import unittest

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from secretary.customer_store import CustomerStore
from secretary.materials import MaterialService
from secretary.store import Store
from secretary.web import create_app, hash_password


class Organizer:
    async def organize(self, text, now, context):
        return {'summary': '客户需要部署方案。', 'key_points': ['核对试点边界'],
                'open_questions': [], 'actions': [{
                    'title': '发送部署方案', 'kind': 'commitment', 'owner_hint': '我',
                    'reason': '原话：“我答应发送部署方案”', 'remind_at': None}]}


class VisitWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        path = Path(self.folder.name) / 'visit.sqlite3'
        self.store, self.crm = Store(path), CustomerStore(path)
        self.lock = asyncio.Lock()
        self.materials = MaterialService(self.crm, self.lock, organizer=Organizer())
        self.client = TestClient(TestServer(create_app(
            self.store, self.crm, self.lock, 'alice', hash_password('visit-test-password'),
            materials=self.materials, runtime={'mode': 'local', 'wecom_connected': False,
                'reminders': 'web'})), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        login = await self.client.post('/api/login', json={'password': 'visit-test-password'})
        self.login_result = await login.json()
        self.csrf = self.login_result['csrf']

    async def asyncTearDown(self):
        await self.client.close()
        await self.materials.close()
        self.crm.close()
        self.store.close()
        self.folder.cleanup()

    async def write(self, method, path, data):
        return await self.client.request(method, path, json=data, headers={'X-CSRF-Token': self.csrf})

    async def test_fresh_login_exposes_same_local_runtime_as_session(self):
        session = await (await self.client.get('/api/session')).json()
        self.assertEqual(self.login_result['runtime'], session['runtime'])
        self.assertEqual(self.login_result['runtime']['mode'], 'local')
        self.assertFalse(self.login_result['runtime']['wecom_connected'])

    async def test_recap_then_recording_one_exchange_one_action_and_source_navigation(self):
        created = await self.write('POST', '/api/visits', {'title': '方案交流'})
        self.assertEqual(created.status, 201)
        visit = (await created.json())['visit']
        materials = []
        for role in ('recap', 'recording'):
            saved = await self.write('POST', f"/api/visits/{visit['id']}/materials", {
                'role': role, 'provider': 'manual', 'title': role,
                'text': '我答应发送部署方案。客户需要先确认试点范围。'})
            self.assertEqual(saved.status, 202)
            materials.append((await saved.json())['material'])
            await self.materials.process_one()
        detail = await (await self.client.get(f"/api/visits/{visit['id']}")).json()
        self.assertEqual(len(detail['sources']), 2)
        self.assertEqual(len(detail['actions']), 1)
        self.assertEqual(len(detail['actions'][0]['references']), 2)
        for material in materials:
            source = await (await self.client.get(f"/api/materials/{material['id']}")).json()
            self.assertEqual(source['visit_ref']['id'], visit['id'])
        action = detail['actions'][0]
        adopted = await self.write('POST', f"/api/visits/{visit['id']}/actions/{action['key']}/adopt",
                                   {'revision': detail['visit']['revision']})
        self.assertEqual(adopted.status, 200)
        record = (await adopted.json())['record']
        record_detail = await (await self.client.get(f"/api/records/{record['id']}")).json()
        self.assertEqual(record_detail['visit_ref']['id'], visit['id'])
        # Neither adoption nor matching a second source enables a reminder.
        self.assertEqual(self.crm._db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 0)
        for material in materials:
            original = self.materials.detail('alice', material['id'])
            result = await self.write('POST',
                f"/api/materials/{material['id']}/actions/{original['analysis']['actions'][0]['id']}/adopt",
                {'revision': original['material']['revision']})
            self.assertEqual(result.status, 200)
            self.assertEqual((await result.json())['record']['id'], record['id'])
        listing = await (await self.client.get('/api/visits')).json()
        self.assertEqual(listing['total'], 1)

    async def test_stale_exchange_and_csrf_are_rejected_without_adoption(self):
        created = await self.write('POST', '/api/visits', {'title': '待核对交流'})
        self.assertEqual(created.status, 201)
        visit = (await created.json())['visit']
        await self.write('POST', f"/api/visits/{visit['id']}/materials", {
            'role': 'recap', 'provider': 'manual', 'title': '复盘', 'text': '我答应发送部署方案。'})
        await self.materials.process_one()
        before = await (await self.client.get(f"/api/visits/{visit['id']}")).json()
        await self.write('POST', f"/api/visits/{visit['id']}/materials", {
            'role': 'supplement', 'provider': 'manual', 'title': '后续补充', 'text': '技术范围仍待核实。'})
        action = before['actions'][0]
        stale = await self.write('POST', f"/api/visits/{visit['id']}/actions/{action['key']}/adopt",
                                {'revision': before['visit']['revision']})
        self.assertEqual(stale.status, 400)
        forged = await self.client.post('/api/visits', json={'title': '跨站提交'})
        self.assertEqual(forged.status, 403)
        invalid = await self.write('POST', '/api/visits', {'title': '越界', 'owner': 'bob'})
        self.assertEqual(invalid.status, 400)
        self.assertEqual((await self.client.get('/api/visits/99999')).status, 404)
