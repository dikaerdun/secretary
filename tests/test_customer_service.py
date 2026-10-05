import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer
import httpx

from secretary.customer_service import CustomerService
from secretary.customer_parser import CustomerVoiceParser, should_parse
from secretary.customer_store import CustomerStore
from secretary.gateway import BotGateway
from secretary.store import Store
from secretary.web import create_app, hash_password
from tests.test_gateway import FakeClient, FakeParser, message


class Parser:
    def __init__(self):
        self.calls = 0
        self.result = {'intent': 'create', 'customer_name': '星河医院', 'contact_name': '王总',
                       'basic': {}, 'basic_evidence': {}, 'contact': {'name': '王总'},
                       'contact_evidence': {'name': '王总'}, 'attributes': [
                           {'key': 'crypto_needs', 'value': '密钥管理', 'evidence': '密钥管理', 'basis': 'reported', 'target': 'account'},
                           {'key': 'communication_channel', 'value': '先微信沟通', 'evidence': '先微信沟通', 'basis': 'reported', 'target': 'contact'}]}

    async def parse(self, text, now):
        self.calls += 1
        return self.result


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / 'service.sqlite3'
        self.crm, self.store = CustomerStore(self.path), Store(self.path)
        self.lock, self.parser = asyncio.Lock(), Parser()
        self.service = CustomerService(self.crm, self.parser, self.lock)
        self.text = '新建客户星河医院，联系人王总，关注密钥管理，希望先微信沟通。'

    async def asyncTearDown(self):
        self.crm.close()
        self.store.close()
        self.folder.cleanup()

    async def draft(self):
        return (await self.service.handle('owner', 'voice1', self.text, source='voice'))['draft']

    async def test_voice_draft_then_exact_spoken_confirmation_no_schedule(self):
        draft = await self.draft()
        self.assertEqual(self.crm.list_customers('owner')['total'], 0)
        result = await self.service.handle('owner', 'confirm1', f"确认客户 C{draft['id']}")
        profile = self.crm.profile('owner', result['customer_id'])
        self.assertEqual(profile['customer']['name'], '星河医院')
        self.assertEqual(profile['fields'][0]['value'], '密钥管理')
        self.assertEqual(profile['contacts'][0]['fields'][0]['value'], '先微信沟通')
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)
        repeated = await self.service.handle('owner', 'confirm1', f"确认客户 C{draft['id']}")
        self.assertEqual(result, repeated)
        self.assertEqual(self.crm.list_customers('owner')['total'], 1)

    async def test_duplicate_voice_returns_original_without_second_parse(self):
        first = await self.draft()
        second = await self.draft()
        self.assertEqual(first, second)
        self.assertEqual(self.parser.calls, 1)

    async def test_crash_after_draft_commit_recovers_after_manual_confirmation(self):
        draft = await self.draft()
        self.service.decide('owner', draft['id'], True)
        with self.crm._transaction() as db:
            db.execute('DELETE FROM customer_command_results WHERE owner=? AND source_id=?', ('owner', 'voice1'))
        recovered = await self.draft()
        self.assertEqual(recovered['id'], draft['id'])
        self.assertEqual(recovered['status'], 'confirmed')
        self.assertEqual(self.parser.calls, 1)

    async def test_show_draft_is_local_and_owner_bound(self):
        draft = await self.draft()
        result = await self.service.handle('owner', 'show1', f"查看客户草稿 C{draft['id']}")
        self.assertEqual(result['draft']['id'], draft['id'])
        self.assertEqual(self.parser.calls, 1)
        other = await self.service.handle('other', 'show2', f"查看客户草稿 C{draft['id']}")
        self.assertIn('未找到', other['message'])

    async def test_customer_confirmation_never_confirms_task(self):
        reply = self.store.execute('owner', 'task1', {'action': 'propose', 'title': '不要误确认', 'remind_at': time.time()+3600}, time.time())
        self.assertIn('P1', reply)
        await self.service.handle('owner', 'confirm1', '确认客户 C1')
        self.assertEqual(self.crm.get_proposal('owner', 1)['status'], 'pending')

    async def test_original_task_command_bypasses_customer_parser(self):
        self.assertIsNone(await self.service.handle('owner', 't1', '明天下午三点提醒我拜访客户'))
        self.assertEqual(self.parser.calls, 0)

    async def test_gateway_customer_phone_reminder_falls_through_to_pending_p(self):
        text = '明天下午三点提醒我给客户王总打电话'
        self.assertTrue(should_parse(text))
        calls = []
        def model(request):
            calls.append('customer')
            payload = {'intent': 'none', 'customer_name': None, 'contact_name': None,
                       'basic': {}, 'basic_evidence': {}, 'contact': {}, 'contact_evidence': {},
                       'attributes': [], 'note_text': None, 'question': None}
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
                'content': json.dumps(payload)}}]})
        class TaskParser:
            async def parse(inner, source, now):
                calls.append('task')
                return {'action': 'propose', 'title': '给客户王总打电话', 'remind_at': now + 86400}
        async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as client:
            self.service.parser = CustomerVoiceParser('synthetic-key', client=client)
            wecom = FakeClient()
            gateway = BotGateway(wecom, self.store, TaskParser(), ['owner'], crm=self.crm, customer_service=self.service)
            gateway.lock = self.lock
            frame = message(msgid='customer-phone-reminder')
            frame['body']['voice']['content'] = text
            await gateway.handle_message(frame)
            await gateway.handle_message(frame)
        self.assertEqual(calls, ['customer', 'task'])
        self.assertIn('P1', wecom.replies[-1][0])
        self.assertEqual(self.crm.get_proposal('owner', 1)['status'], 'pending')
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)
        record = self.crm.list_records('owner')['items'][0]
        self.assertEqual(record['original_content'], text)
        self.assertEqual(record['proposal_id'], 1)

    async def test_gateway_undated_crm_none_keeps_note_without_task_model(self):
        text = '华辰那边希望先做数据库脱敏，预算还没有定'
        calls = []
        def model(request):
            calls.append('customer')
            payload = {'intent': 'none', 'customer_name': None, 'contact_name': None,
                       'basic': {}, 'basic_evidence': {}, 'contact': {}, 'contact_evidence': {},
                       'attributes': [], 'note_text': None, 'question': None}
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
                'content': json.dumps(payload)}}]})
        task_parser = FakeParser()
        async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as client:
            self.service.parser = CustomerVoiceParser('synthetic-key', client=client)
            wecom = FakeClient()
            gateway = BotGateway(wecom, self.store, task_parser, ['owner'], crm=self.crm, customer_service=self.service)
            gateway.lock = self.lock
            frame = message(msgid='undated-crm')
            frame['body']['voice']['content'] = text
            await gateway.handle_message(frame)
        self.assertEqual(calls, ['customer'])
        self.assertEqual(task_parser.calls, [])
        self.assertEqual(self.crm.list_records('owner')['items'][0]['original_content'], text)
        self.assertIn('已保存', wecom.replies[-1][0])
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM proposals').fetchone()[0], 0)

    async def test_customer_ambiguous_name_must_not_pick_latest(self):
        customer = self.crm.create_customer('owner', {'name': '星河医院'}, time.time())
        # New writes reject duplicates; migrated legacy databases can still
        # contain them and must never pick one customer's archive arbitrarily.
        with self.crm._transaction() as db:
            db.execute('INSERT INTO crm_customers(owner,name,contact,phone,stage,amount_cents,notes,created_at,updated_at) '
                       'SELECT owner,name,contact,phone,stage,amount_cents,notes,created_at,updated_at '
                       'FROM crm_customers WHERE id=?', (customer['id'],))
        self.parser.result['intent'] = 'update'
        result = await self.service.handle('owner', 'u1', self.text)
        self.assertIn('歧义', result['message'])
        self.assertEqual(len(result['candidates']), 2)
        self.assertEqual(self.crm.list_customer_drafts('owner')['items'], [])

    async def test_partial_match_only_suggests_no_draft(self):
        self.crm.create_customer('owner', {'name': '星河医院集团'}, time.time())
        self.parser.result['intent'] = 'update'
        result = await self.service.handle('owner', 'u1', self.text)
        self.assertIn('唯一匹配', result['message'])
        self.assertEqual(len(result['candidates']), 1)
        self.assertNotIn('draft', result)

    async def test_other_owner_cannot_confirm_or_see_candidates(self):
        draft = await self.draft()
        result = await self.service.handle('other', 'o1', f"确认客户 C{draft['id']}")
        self.assertIn('未找到', result['message'])
        self.assertEqual(self.crm.get_customer_draft('owner', draft['id'])['status'], 'pending')

    async def test_unrelated_manual_edit_keeps_new_value_and_allows_nonconflicting_draft(self):
        customer = self.crm.create_customer('owner', {'name': '星河医院'}, time.time())
        self.parser.result['intent'] = 'update'
        draft = await self.draft()
        self.crm.update_customer('owner', customer['id'], {'notes': '刚刚人工修改'}, time.time())
        result = await self.service.handle('owner', 'c1', f"确认客户 C{draft['id']}")
        self.assertIn('已确认', result['message'])
        self.assertEqual(self.crm.get_customer('owner', customer['id'])['notes'], '刚刚人工修改')
        self.assertEqual(self.crm.profile('owner', customer['id'])['fields'][0]['value'], '密钥管理')

    async def test_stale_same_field_manual_edit_preserves_the_new_fact(self):
        customer = self.crm.create_customer('owner', {'name': '星河医院'}, time.time())
        self.parser.result['intent'] = 'update'
        draft = await self.draft()
        self.crm.save_fact('owner', customer['id'], {'key': 'crypto_needs', 'value': '人工核实：只考虑数据脱敏',
                                                   'basis': 'reported'}, time.time())
        result = await self.service.handle('owner', 'same-field-c1', f"确认客户 C{draft['id']}")
        self.assertNotIn('已确认', result['message'])
        self.assertEqual(self.crm.profile('owner', customer['id'])['fields'][0]['value'], '人工核实：只考虑数据脱敏')

    async def test_note_auto_organizes_but_never_adopts(self):
        customer = self.crm.create_customer('owner', {'name': '星河医院'}, time.time())
        self.parser.result = {'intent': 'note', 'customer_name': '星河医院', 'contact_name': None}
        class Organizer:
            async def organize(inner, text, now, context):
                return {'summary': '讨论密钥管理', 'key_points': [], 'open_questions': [], 'actions': [
                    {'title': '准备技术说明', 'kind': 'suggestion', 'reason': '推进需求确认', 'owner_hint': '我', 'remind_at': None}]}
        self.service.organizer = Organizer()
        result = await self.service.handle('owner', 'note1', '记录客户星河医院交流，讨论密钥管理')
        self.assertIn('建议跟进', result['message'])
        record = self.crm.get_record('owner', result['record_id'])
        self.assertEqual(record['customer_id'], customer['id'])
        self.assertEqual(self.crm.list_records('owner')['total'], 1)
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM proposals').fetchone()[0], 0)

    async def test_note_replay_after_organization_interrupted_preserves_manual_edits(self):
        self.crm.create_customer('owner', {'name': '星河医院'}, time.time())
        other = self.crm.create_customer('owner', {'name': '另一客户'}, time.time())
        self.parser.result = {'intent': 'note', 'customer_name': '星河医院', 'contact_name': None}
        started = asyncio.Event()
        class Organizer:
            async def organize(inner, text, now, context):
                started.set()
                await asyncio.Future()
        self.service.organizer = Organizer()
        operation = asyncio.create_task(self.service.handle('owner', 'interrupted-note', '记录客户星河医院交流，讨论密钥管理'))
        await started.wait()
        operation.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await operation
        record = self.crm.list_records('owner')['items'][0]
        self.crm.update_record('owner', record['id'], {'customer_id': other['id'], 'content': '用户修正内容'}, time.time())
        result = await self.service.handle('owner', 'interrupted-note', '记录客户星河医院交流，讨论密钥管理')
        current = self.crm.get_record('owner', record['id'])
        self.assertIn('已保存', result['message'])
        self.assertEqual(current['customer_id'], other['id'])
        self.assertEqual(current['content'], '用户修正内容')
        self.assertIsNone(self.crm.get_analysis('owner', record['id']))

    async def test_gateway_voice_customer_path_and_replay(self):
        client, task_parser = FakeClient(), FakeParser()
        gateway = BotGateway(client, self.store, task_parser, ['owner'], crm=self.crm, customer_service=self.service)
        gateway.lock = self.lock
        frame = message()
        frame['body']['voice']['content'] = self.text
        await gateway.handle_message(frame)
        await gateway.handle_message(frame)
        self.assertEqual(self.parser.calls, 1)
        self.assertEqual(task_parser.calls, [])
        self.assertIn('客户资料待确认', client.replies[-1][0])

    async def test_gateway_whitespace_keeps_exact_original_evidence(self):
        self.crm.capture_message('owner', 'spaces', '  ' + self.text + '\n', 'voice', time.time())
        result = await self.service.handle('owner', 'spaces', '  ' + self.text + '\n', source='voice')
        self.assertEqual(result['draft']['source_text'], '  ' + self.text + '\n')


class ProfileWebTests(unittest.IsolatedAsyncioTestCase):
    draft = ServiceTests.draft

    async def asyncSetUp(self):
        await ServiceTests.asyncSetUp(self)
        app = create_app(self.store, self.crm, self.lock, 'owner', hash_password('test-secret-password'), customer_service=self.service)
        self.client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        result = await self.client.post('/api/login', json={'password': 'test-secret-password'})
        self.csrf = (await result.json())['csrf']

    async def asyncTearDown(self):
        await self.client.close()
        await ServiceTests.asyncTearDown(self)

    async def write(self, path, data):
        return await self.client.post(path, json=data, headers={'X-CSRF-Token': self.csrf})

    async def test_profile_api_confirm_contacts_fact_history(self):
        result = await self.write('/api/customer-command', {'text': self.text})
        self.assertEqual(result.status, 200)
        draft = (await result.json())['draft']
        result = await self.write(f"/api/customer-drafts/{draft['id']}/confirm", {})
        self.assertEqual(result.status, 200)
        cid = (await result.json())['customer_id']
        result = await self.write(f'/api/customers/{cid}/contacts', {'name': '陈经理', 'role': '采购'})
        self.assertEqual(result.status, 201)
        contact = (await result.json())['contact']
        for value in ('邮件', '微信'):
            result = await self.write(f'/api/customers/{cid}/facts', {'key': 'communication_channel', 'value': value,
                                     'basis': 'reported', 'contact_id': contact['id'], 'evidence': '人工确认'})
            self.assertEqual(result.status, 200)
        profile = await (await self.client.get(f'/api/customers/{cid}/profile')).json()
        self.assertEqual(len(profile['contacts']), 2)
        self.assertGreaterEqual(len(profile['history']), 2)

    async def test_profile_mutation_csrf_and_unknown_owner(self):
        draft = await self.draft()
        result = await self.client.post(f"/api/customer-drafts/{draft['id']}/confirm", json={})
        self.assertEqual(result.status, 403)
        other = self.crm.create_customer('other', {'name': '私有客户'}, time.time())
        result = await self.client.get(f"/api/customers/{other['id']}/profile")
        self.assertEqual(result.status, 404)
        result = await self.write('/api/customer-command', {'text': self.text, 'owner': 'other'})
        self.assertEqual(result.status, 400)
