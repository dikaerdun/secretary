import asyncio
import copy
from pathlib import Path
import tempfile
import time
import unittest

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from secretary.coaching_service import CoachingService
from secretary.customer_store import CustomerStore
from secretary.store import Store
from secretary.web import create_app, hash_password


ADVICE = {'summary': '客户关注密钥管理，系统范围待核实', 'objective': '确认一个验证范围',
          'rationale': '需要先核实业务范围再提出方案', 'next_moves': [
              {'title': '确认应用系统与接口边界', 'reason': '需求尚未明确', 'contact_hint': '技术负责人（姓名待确认）',
               'preparation': '准备接口问题清单', 'talk_track': '哪些系统需要接入？', 'success_signal': '确定试点系统'},
              {'title': '核实试点验收指标', 'reason': '试点需要明确评价方式', 'contact_hint': '业务负责人（姓名待确认）',
               'preparation': '准备指标示例', 'talk_track': '如何判断试点效果？', 'success_signal': '得到验收指标'}],
          'questions': ['由谁确认测试范围？'], 'risks': ['预算尚未核实']}


class Coach:
    def __init__(self):
        self.calls = 0
        self.started, self.release = asyncio.Event(), None

    async def advise(self, profile, now):
        self.calls += 1
        self.started.set()
        if self.release:
            await self.release.wait()
        return copy.deepcopy(ADVICE)


class CoachingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.crm = CustomerStore(Path(self.folder.name)/'test.sqlite3')
        self.customer = self.crm.create_customer('owner', {'name': '星河医院'}, time.time())
        self.model, self.lock = Coach(), asyncio.Lock()
        self.coaching = CoachingService(self.crm, self.model, self.lock)

    async def asyncTearDown(self):
        await self.coaching.close()
        self.crm.close()
        self.folder.cleanup()

    async def generate(self):
        self.coaching.schedule('owner', self.customer['id'])
        await asyncio.gather(*list(self.coaching.running.values()))
        return self.coaching.view('owner', self.customer['id'])['recommendation']

    async def test_advice_does_not_create_tasks_until_adopted(self):
        advice = await self.generate()
        self.assertEqual(advice['objective'], ADVICE['objective'])
        self.assertEqual(self.crm.list_records('owner')['total'], 0)
        first = self.coaching.adopt('owner', self.customer['id'], advice['version'], 1)
        again = self.coaching.adopt('owner', self.customer['id'], advice['version'], 1)
        second = self.coaching.adopt('owner', self.customer['id'], advice['version'], 2)
        self.assertEqual(first['id'], again['id'])
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(first['status'], 'following')
        self.assertEqual(self.crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)
        self.assertEqual(self.crm._db.execute('SELECT count(*) FROM notifications').fetchone()[0], 0)

    async def test_manual_update_makes_suggestions_stale_and_blocks_adoption(self):
        advice = await self.generate()
        self.crm.save_fact('owner', self.customer['id'], {'key':'requirements','value':'范围已变','basis':'reported'}, time.time())
        self.assertTrue(self.coaching.view('owner', self.customer['id'])['recommendation']['stale'])
        with self.assertRaises(ValueError):
            self.coaching.adopt('owner', self.customer['id'], advice['version'], 1)

    async def test_owner_isolation_for_read_generate_and_adopt(self):
        advice = await self.generate()
        self.assertEqual(self.coaching.list_views('other')['items'], [])
        for action in (lambda:self.coaching.view('other',self.customer['id']),
                       lambda:self.coaching.schedule('other',self.customer['id']),
                       lambda:self.coaching.adopt('other',self.customer['id'],advice['version'],1)):
            with self.assertRaises(KeyError):
                action()

    async def test_model_wait_does_not_block_lock_and_changed_context_retried(self):
        self.model.release = asyncio.Event()
        self.coaching.schedule('owner', self.customer['id'])
        await self.model.started.wait()
        self.coaching.schedule('owner', self.customer['id'])
        async with self.lock:
            self.crm.update_customer('owner', self.customer['id'], {'notes':'人工新信息'}, time.time())
        self.model.release.set()
        await asyncio.gather(*list(self.coaching.running.values()))
        self.assertEqual(self.model.calls, 2)
        self.assertFalse(self.coaching.view('owner', self.customer['id'])['recommendation']['stale'])

    async def test_provider_error_not_exposed_or_published(self):
        async def fail(*args):
            raise RuntimeError('private provider payload')
        self.model.advise = fail
        self.assertIsNone(await self.generate())
        self.assertNotIn('private', self.coaching.view('owner', self.customer['id'])['error'])


class CoachingWebTests(CoachingTests):
    async def test_http_refresh_poll_adopt_and_csrf(self):
        app = create_app(self.crm, self.crm, self.lock, 'owner', hash_password('test-secret-password'), coaching=self.coaching)
        async with TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True)) as client:
            self.assertEqual((await client.get('/api/coaching')).status, 401)
            login = await client.post('/api/login', json={'password':'test-secret-password'})
            headers = {'X-CSRF-Token': (await login.json())['csrf']}
            url = f"/api/customers/{self.customer['id']}/coaching"
            self.assertEqual((await client.post(url, json={})).status, 403)
            result = await client.post(url, json={}, headers=headers)
            self.assertEqual(result.status, 202)
            await asyncio.gather(*list(self.coaching.running.values()))
            advice = (await (await client.get(url)).json())['recommendation']
            result = await client.post(url+f"/{advice['version']}/actions/1/adopt", json={}, headers=headers)
            self.assertEqual(result.status, 200)
            self.assertEqual((await result.json())['record']['customer_id'], self.customer['id'])
