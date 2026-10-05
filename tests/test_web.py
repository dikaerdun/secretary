import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from secretary.crm import CRMStore
from secretary.store import Store
from secretary.web import create_app, hash_password, verify_password


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        path = Path(self.directory.name) / 'test.sqlite3'
        self.store = Store(path)
        self.crm = CRMStore(path)
        self.lock = asyncio.Lock()
        app = create_app(self.store, self.crm, self.lock, 'owner', hash_password('test-secret-password'))
        self.client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        self.csrf = None

    async def asyncTearDown(self):
        await self.client.close()
        self.crm.close()
        self.store.close()
        self.directory.cleanup()

    async def login(self):
        result = await self.client.post('/api/login', json={'password': 'test-secret-password'})
        self.assertEqual(result.status, 200)
        self.assertIn('HttpOnly', result.headers.get('Set-Cookie'))
        self.assertIn('SameSite=Strict', result.headers.get('Set-Cookie'))
        self.csrf = (await result.json())['csrf']

    async def write(self, method, path, data):
        if path.startswith('/api/records/') and path.endswith('/confirm') and data == {}:
            record_id = int(path.split('/')[3])
            proposal = self.crm.record_detail('owner', record_id).get('proposal')
            if proposal:
                data = {'proposal_id': proposal['id'], 'updated_at': proposal['updated_at']}
        return await self.client.request(method, path, json=data, headers={'X-CSRF-Token': self.csrf})

    async def new_record(self):
        result = await self.write('POST', '/api/records', {'title': '给客户寄方案', 'content': '需要补充实施计划'})
        self.assertEqual(result.status, 201)
        return (await result.json())['record']

    async def test_private_api_and_static_paths_require_safe_access(self):
        for path in ('/api/dashboard', '/api/customers', '/api/records', '/api/agenda'):
            result = await self.client.get(path)
            self.assertEqual(result.status, 401)
        result = await self.client.get('/static/.env')
        self.assertEqual(result.status, 404)
        self.assertEqual(result.headers['Cache-Control'], 'no-store')
        self.assertIn("frame-ancestors 'none'", result.headers['Content-Security-Policy'])

    async def test_csrf_origin_logout_and_expired_session(self):
        await self.login()
        result = await self.client.post('/api/records', json={'title': 'injected'})
        self.assertEqual(result.status, 403)
        result = await self.client.post('/api/records', json={'title': 'injected'}, headers={
            'X-CSRF-Token': self.csrf, 'Origin': 'https://untrusted.invalid'})
        self.assertEqual(result.status, 403)
        result = await self.client.post('/api/login', data={'password': 'test-secret-password'})
        self.assertEqual(result.status, 415)
        await self.write('POST', '/api/logout', {})
        result = await self.client.get('/api/records')
        self.assertEqual(result.status, 401)

    async def test_concurrent_login_attempts_are_bounded_before_hashing(self):
        calls = []
        def bad_password(*args):
            calls.append(1)
            time.sleep(.04)
            return False
        with patch('secretary.web.verify_password', bad_password):
            results = await asyncio.gather(*(self.client.post('/api/login', json={'password': 'bad'}) for _ in range(12)))
        self.assertEqual(len(calls), 5)
        self.assertEqual(sum(r.status == 429 for r in results), 7)

    async def test_customer_record_activity_flow_and_owner_isolation(self):
        other = self.crm.create_customer('other', {'name': 'private customer'}, time.time())
        private_record = self.crm.create_record('other', {'title': 'private record', 'content': 'private'}, time.time())
        await self.login()
        result = await self.write('POST', '/api/customers', {'name': 'injected', 'owner': 'other'})
        self.assertEqual(result.status, 400)
        result = await self.write('POST', '/api/customers', {'name': '青禾', 'contact': '王女士',
                                 'phone': '01012345678', 'stage': 'proposal', 'amount_cents': 123456})
        self.assertEqual(result.status, 201)
        customer = (await result.json())['customer']
        self.assertNotIn('owner', customer)
        record = await self.new_record()
        result = await self.write('PATCH', f"/api/records/{record['id']}",
                                 {'customer_id': customer['id'], 'status': 'following', 'content': '补充后的计划'})
        self.assertEqual(result.status, 200)
        await self.write('POST', f"/api/records/{record['id']}/activities", {'content': '客户确认下周讨论'})
        detail = await (await self.client.get(f"/api/records/{record['id']}")).json()
        self.assertEqual(detail['record']['original_content'], '需要补充实施计划')
        self.assertEqual(detail['activities'][-1]['content'], '客户确认下周讨论')
        self.assertEqual((await self.client.get(f"/api/customers/{other['id']}")).status, 404)
        self.assertEqual((await self.client.get(f"/api/records/{private_record['id']}")).status, 404)
        result = await self.write('PATCH', f"/api/records/{record['id']}", {'customer_id': other['id']})
        self.assertIn(result.status, (400, 404))
        self.assertEqual(self.store._db.execute('select count(*) from notifications').fetchone()[0], 0)

    async def test_schedule_requires_confirmation_and_double_confirm_is_safe(self):
        await self.login()
        record = await self.new_record()
        seen = await (await self.client.get(f"/api/records/{record['id']}")).json()
        self.assertRegex(seen['schedule_snapshot'], r'^[0-9a-f]{64}$')
        result = await self.write('POST', f"/api/records/{record['id']}/schedule",
                                 {'remind_at': time.time() + 3600, 'duration_minutes': 30, 'expected_schedule_snapshot': seen['schedule_snapshot']})
        self.assertEqual(result.status, 200)
        self.assertEqual(self.store._db.execute('select count(*) from notifications').fetchone()[0], 0)
        for _ in range(2):
            result = await self.write('POST', f"/api/records/{record['id']}/confirm", {})
            self.assertEqual(result.status, 200)
        task = (await result.json())['task']
        self.assertEqual(self.store._db.execute('select count(*) from tasks').fetchone()[0], 1)
        self.assertEqual(self.store._db.execute('select count(*) from notifications').fetchone()[0], 1)
        result = await self.write('POST', f"/api/tasks/{task['id']}/complete", {})
        self.assertEqual(result.status, 200)
        self.assertEqual(self.crm.get_task('owner', task['id'])['status'], 'completed')
        self.assertIsNone(self.store.claim_due(time.time() + 7200))

    async def test_conflict_does_not_silently_confirm(self):
        await self.login()
        when = time.time() + 3600
        for index in range(2):
            record = await self.new_record()
            seen = await (await self.client.get(f"/api/records/{record['id']}")).json()
            self.assertRegex(seen['schedule_snapshot'], r'^[0-9a-f]{64}$')
            await self.write('POST', f"/api/records/{record['id']}/schedule", {'remind_at': when, 'expected_schedule_snapshot': seen['schedule_snapshot']})
            result = await self.write('POST', f"/api/records/{record['id']}/confirm", {})
            self.assertEqual(result.status, 200 if index == 0 else 409)
        self.assertEqual(self.store._db.execute('select count(*) from tasks').fetchone()[0], 1)

    async def test_completing_record_stops_its_active_reminder(self):
        await self.login()
        record = await self.new_record()
        seen = await (await self.client.get(f"/api/records/{record['id']}")).json()
        self.assertRegex(seen['schedule_snapshot'], r'^[0-9a-f]{64}$')
        await self.write('POST', f"/api/records/{record['id']}/schedule", {'remind_at': time.time() + 3600, 'expected_schedule_snapshot': seen['schedule_snapshot']})
        await self.write('POST', f"/api/records/{record['id']}/confirm", {})
        result = await self.write('PATCH', f"/api/records/{record['id']}", {'status': 'done'})
        self.assertEqual(result.status, 200)
        self.assertEqual((await result.json())['record']['task_status'], 'completed')
        self.assertIsNone(self.store.claim_due(time.time() + 7200))

    async def test_shared_lock_serializes_web_mutation_with_reminder_delivery(self):
        await self.login()
        await self.lock.acquire()
        try:
            request = asyncio.create_task(self.write('POST', '/api/records', {'title': '等待派发结束', 'content': ''}))
            await asyncio.sleep(.02)
            self.assertFalse(request.done())
        finally:
            self.lock.release()
        self.assertEqual((await request).status, 201)

    async def test_organize_and_adopt_require_explicit_confirmation(self):
        from datetime import datetime
        from secretary.store import SHANGHAI
        when = int((time.time()+3600)//60)*60
        spoken_time = datetime.fromtimestamp(when,SHANGHAI).strftime('%Y年%m月%d日%H:%M')
        source = f'我答应{spoken_time}准备实施方案，预计60分钟。'
        class Organizer:
            async def organize(self, content, now, context):
                return {'summary': '讨论交付计划', 'key_points': ['需要补充计划'], 'open_questions': [],
                        'actions': [{'title': '准备实施方案', 'kind': 'commitment', 'reason': '测试承诺依据',
                                     'owner_hint': '我', 'remind_at': when,
                                     'evidence':source, 'time_evidence':spoken_time,
                                     'duration_minutes':60, 'duration_evidence':'预计60分钟'}]}
        # The bound middleware owns the injectable organizer for this isolated app.
        self.client.app.middlewares[0].__self__.organizer = Organizer()
        await self.login()
        created = await self.write('POST','/api/records',{'title':'交付计划','content':source})
        record = (await created.json())['record']
        result = await self.write('POST', f"/api/records/{record['id']}/organize", {})
        self.assertEqual(result.status, 200)
        action = (await result.json())['analysis']['actions'][0]
        adopted = []
        for _ in range(2):
            result = await self.write('POST', f"/api/records/{record['id']}/actions/{action['id']}/adopt", {})
            self.assertEqual(result.status, 200)
            adopted.append((await result.json())['record'])
        self.assertEqual(adopted[0]['id'], adopted[1]['id'])
        self.assertIsNotNone(adopted[0]['proposal_id'])
        self.assertEqual(self.crm.get_proposal('owner',adopted[0]['proposal_id'])['duration_minutes'],60)
        self.assertEqual(self.store._db.execute('select count(*) from tasks').fetchone()[0], 0)
        result = await self.write('POST', f"/api/records/{record['id']}/organize", {})
        self.assertEqual(result.status, 409)
        result = await self.write('POST', f"/api/records/{adopted[0]['id']}/confirm", {})
        self.assertEqual(result.status, 200)
        self.assertEqual(self.store._db.execute('select count(*) from tasks').fetchone()[0], 1)

    async def test_edit_during_ai_call_does_not_apply_stale_result(self):
        started, proceed = asyncio.Event(), asyncio.Event()
        class Organizer:
            async def organize(self, content, now, context):
                started.set()
                await proceed.wait()
                return {'summary': 'old', 'key_points': [], 'open_questions': [], 'actions': []}
        self.client.app.middlewares[0].__self__.organizer = Organizer()
        await self.login()
        record = await self.new_record()
        pending = asyncio.create_task(self.write('POST', f"/api/records/{record['id']}/organize", {}))
        await started.wait()
        await self.write('PATCH', f"/api/records/{record['id']}", {'content': 'new content'})
        proceed.set()
        self.assertEqual((await pending).status, 400)
        self.assertIsNone(self.crm.get_analysis('owner', record['id']))


def test_password_hash_is_salted_and_validated():
    first = hash_password('long-random-password')
    second = hash_password('long-random-password')
    assert first != second
    assert verify_password('long-random-password', first)
    assert not verify_password('wrong', first)
    assert not verify_password('long-random-password', 'broken')
