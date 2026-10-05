"""Exercise the authenticated HTTP confirmation boundary with an isolated DB."""
import asyncio
import tempfile
import unittest
from pathlib import Path

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from secretary.customer_store import CustomerStore
from secretary.materials import MaterialService
from secretary.store import Store
from secretary.web import create_app, hash_password


class Organizer:
    async def organize(self, text, now, context):
        return {'summary': '保留来源，整理后等待确认。', 'key_points': [], 'open_questions': [],
                'actions': [{'title': '发送方案', 'kind': 'commitment', 'reason': '原话：“我答应一小时后发送方案”',
                             'owner_hint': '我', 'remind_at': now + 3600}]}


class MaterialWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        path = Path(self.folder.name) / 'web.sqlite3'
        self.crm, self.store = CustomerStore(path), Store(path)
        self.lock = asyncio.Lock()
        self.now = 1800000000
        self.materials = MaterialService(self.crm, self.lock, organizer=Organizer(), clock=lambda: self.now)
        owner_crm = self.crm
        class CustomerService:
            async def handle(self, owner, source_id, text, **kwargs):
                r = owner_crm.capture_message(owner, source_id, text, 'web', self_time)
                if kwargs.get('category') is not None:
                    owner_crm.update_record(owner, r['id'], {'category': kwargs['category']}, self_time)
                return {'record_id': r['id'], 'message': '已保存'}
        self_time = self.now
        app = create_app(self.store, self.crm, self.lock, 'alice', hash_password('material-test-password'),
                         materials=self.materials, customer_service=CustomerService(), clock=lambda: self.now)
        self.client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        login = await self.client.post('/api/login', json={'password': 'material-test-password'})
        self.csrf = (await login.json())['csrf']

    async def asyncTearDown(self):
        await self.client.close()
        await self.materials.close()
        self.crm.close()
        self.store.close()
        self.folder.cleanup()

    async def write(self, method, path, data):
        return await self.client.request(method, path, json=data, headers={'X-CSRF-Token': self.csrf})

    async def test_long_transcript_source_and_explicit_confirmation(self):
        text = '我答应一小时后发送方案。\n' + '会议背景内容。' * 8000
        created = await self.write('POST', '/api/materials', {'provider': 'manual', 'title': '现场交流',
            'category': 'meeting', 'text': text, 'occurred_at': self.now})
        self.assertEqual(created.status, 202)
        material = (await created.json())['material']
        await self.materials.process_one()
        detail = await (await self.client.get('/api/materials/' + str(material['id']))).json()
        self.assertEqual(detail['text'], text)
        self.assertEqual(detail['material']['status'], 'review')
        self.assertEqual(self.crm._db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 0)
        action = detail['analysis']['actions'][0]
        adopted = await self.write('POST', f"/api/materials/{material['id']}/actions/{action['id']}/adopt",
                                  {'revision': detail['material']['revision']})
        self.assertEqual(adopted.status, 200)
        result = await adopted.json()
        self.assertEqual(result['proposal']['status'], 'pending')
        self.assertEqual(self.crm._db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 0)
        record = await (await self.client.get('/api/records/' + str(result['record']['id']))).json()
        self.assertEqual(record['material_ref']['id'], material['id'])
        for linked_record_id in (result['record']['id'], detail['material']['record_id']):
            for operation, data in (('organize', {}), ('reinterpret', {'text': '我答应发送方案。'})):
                blocked = await self.write('POST', f'/api/records/{linked_record_id}/{operation}', data)
                self.assertEqual(blocked.status, 409)
                self.assertEqual((await blocked.json())['material_ref']['id'], material['id'])
        p = result['proposal']
        confirmed = await self.write('POST', f"/api/records/{result['record']['id']}/confirm",
                                    {'proposal_id': p['id'], 'updated_at': p['updated_at']})
        self.assertEqual(confirmed.status, 200)
        self.assertEqual(self.crm._db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)
        for period in ('day', 'week', 'month'):
            agenda = await self.client.get('/api/agenda?period=' + period)
            self.assertEqual(agenda.status, 200)
        changed = await self.write('PATCH', f"/api/materials/{material['id']}",
                                  {'revision': detail['material']['revision'], 'text': '我答应发送方案。新进展另行核对。'})
        self.assertEqual(changed.status, 200)
        await self.materials.process_one()
        revised = self.materials.detail('alice', material['id'])
        self.assertEqual(revised['previous_adoptions'][0]['id'], result['record']['id'])
        self.assertEqual(revised['previous_adoptions'][0]['task_status'], 'pending')
        self.assertEqual(len(revised['versions']), 2)

    async def test_private_material_csrf_and_stale_page(self):
        private = self.materials.enqueue('bob', {'provider': 'manual', 'title': '外人材料', 'text': '私人内容'})
        self.assertEqual((await self.client.get('/api/materials/' + str(private['id']))).status, 404)
        missing_csrf = await self.client.post('/api/materials', json={'provider': 'manual', 'title': '伪造', 'text': '不会入库'})
        self.assertEqual(missing_csrf.status, 403)
        payload = {'provider': 'manual', 'title': '我的想法', 'category': 'idea', 'text': '我答应发送方案。'}
        material = (await (await self.write('POST', '/api/materials', payload)).json())['material']
        await self.materials.process_one()
        await self.materials.process_one()
        before = self.materials.detail('alice', material['id'])
        updated = await self.write('PATCH', '/api/materials/' + str(material['id']),
                                  {'revision': material['revision'], 'category': 'memo'})
        self.assertEqual(updated.status, 200)
        stale = await self.write('POST', f"/api/materials/{material['id']}/actions/{before['analysis']['actions'][0]['id']}/adopt",
                                {'revision': material['revision']})
        self.assertEqual(stale.status, 400)
        too_big_elsewhere = await self.write('POST', '/api/records', {'title': '正常记录', 'content': '字' * 24000})
        self.assertEqual(too_big_elsewhere.status, 413)
        illegal = await self.write('POST', '/api/materials', {**payload, 'owner': 'bob'})
        self.assertEqual(illegal.status, 400)

    async def test_explicit_category_is_saved_with_customer_record_id_contract(self):
        result = await self.write('POST', '/api/customer-command', {'text': '客户提出 POC 需求。', 'category': 'idea'})
        self.assertEqual(result.status, 200)
        data = await result.json()
        self.assertEqual(self.crm.get_record('alice', data['record_id'])['category'], 'idea')
        items = await (await self.client.get('/api/records?category=idea')).json()
        self.assertEqual(items['total'], 1)
