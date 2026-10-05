"""Synthetic attribution oracles: inference suggests, the user still decides."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from secretary.customer_resolution import (
    CustomerResolutionService, CustomerResolver, MAX_CONTEXT_CHARS, MAX_MODEL_TEXT,
)
from secretary.customer_store import CustomerStore
from secretary.sales_workspace import SalesWorkspace


NOW = 1791000000.0


class StubResolver:
    available = True

    def __init__(self, result=None, error=None, callback=None):
        self.result, self.error, self.callback = result, error, callback
        self.calls = []

    async def resolve(self, text, context, now):
        self.calls.append((text, context, now))
        if self.callback:
            self.callback()
        if self.error:
            raise self.error
        return self.result


def candidate(customer, project=None, confidence='high', reasons=None):
    return {'customer_id': customer['id'], 'opportunity_id': project['id'] if project else None,
            'confidence': confidence, 'reasons': reasons or ['联系人和签名项目描述一致，仍需核对。']}


class ResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.crm = CustomerStore(Path(self.folder.name) / 'synthetic.sqlite3')
        self.workspace = SalesWorkspace(self.crm, clock=lambda: NOW)
        self.lock = asyncio.Lock()
        self.hospital = self.crm.create_customer('me', {
            'name': '星浦医疗科技集团', 'aliases': ['星浦医院'],
            'contact': '陈工（信息中心负责人）', 'phone': '13800138000',
            'notes': '电子病历场景；联系人邮箱 chen@example.invalid，电话 13800138000'}, NOW-100)
        contact = self.crm.profile('me', self.hospital['id'])['contacts'][0]
        self.crm.update_contact('me', self.hospital['id'], contact['id'], {'role': '信息中心项目负责人'}, NOW)
        self.sign = self.workspace.create_opportunity('me', self.hospital['id'], {
            'name': '电子病历签名验签', 'scope': '病历签署和医嘱签名，密评整改',
            'notes': '昨日现场沟通过签名流程，邮箱 chen@example.invalid', 'contact_ids': [contact['id']]})
        self.database = self.workspace.create_opportunity('me', self.hospital['id'], {
            'name': '数据库透明加密试点', 'scope': '数据库加密性能验证'})
        self.bank = self.crm.create_customer('me', {'name': '澄川银行', 'contact': '陈工'}, NOW-50)
        self.bank_sign = self.workspace.create_opportunity('me', self.bank['id'], {'name': '支付签名验签平台'})
        self.foreign = self.crm.create_customer('other', {'name': '外部医院', 'contact': '陈工'}, NOW)
        self.foreign_project = self.workspace.create_opportunity('other', self.foreign['id'], {'name': '秘密签名项目'})
        self.crm.create_record('me', {'title': '昨天现场讨论', 'content': '医嘱签名要确认流程，电话13800138000',
                                     'customer_id': self.hospital['id']}, NOW-86400)
        self.crm.create_record('other', {'title': '秘密记录', 'content': '不应发送给模型',
                                        'customer_id': self.foreign['id']}, NOW)

    async def asyncTearDown(self):
        self.crm.close()
        self.folder.cleanup()

    def service(self, resolver=None):
        return CustomerResolutionService(self.crm, self.workspace, self.lock, resolver, clock=lambda: NOW)

    async def test_vague_hospital_contact_project_model_suggestion_never_links(self):
        stub = StubResolver({'items': [candidate(self.hospital, self.sign)], 'question': '是星浦的病历签名项目吗？'})
        result = await self.service(stub).resolve('me', '昨天医院陈工那个签名项目，需要把流程补充下。')
        self.assertEqual(result['method'], 'model')
        self.assertEqual(result['status'], 'single')
        self.assertEqual(result['items'][0]['customer_name'], self.hospital['name'])
        self.assertEqual(result['items'][0]['opportunity_name'], self.sign['name'])
        self.assertEqual(result['items'][0]['confidence'], 'high')
        self.assertIsNone(result['selected_customer_id'])
        self.assertTrue(result['requires_confirmation'])
        self.assertEqual(self.crm._db.execute('SELECT count(*) FROM crm_opportunity_links').fetchone()[0], 0)
        self.assertEqual(self.crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)

    async def test_same_contact_ambiguity_and_two_projects_are_not_silently_selected(self):
        stub = StubResolver({'items': [candidate(self.hospital, self.sign, 'medium'),
                                      candidate(self.bank, self.bank_sign, 'medium')],
                             'question': '你说的陈工是医院还是银行的联系人？'})
        result = await self.service(stub).resolve('me', '陈工那个签名项目')
        self.assertEqual(result['status'], 'ambiguous')
        self.assertEqual(len(result['items']), 2)
        self.assertIsNone(result['selected_customer_id'])
        stub.result = {'items': [candidate(self.hospital, self.sign), candidate(self.hospital, self.database)],
                       'question': '是哪个项目？'}
        result = await self.service(stub).resolve('me', '医院项目')
        self.assertEqual(result['status'], 'ambiguous')

    async def test_context_is_owner_scoped_bounded_and_redacts_embedded_contact_details(self):
        stub = StubResolver({'items': [], 'question': '还没有找到匹配归属。'})
        result = await self.service(stub).resolve('me', '找陈工，chen@example.invalid 13800138000')
        text, context, now = stub.calls[0]
        serialized = json.dumps(context, ensure_ascii=False)
        self.assertEqual(now, NOW)
        self.assertNotIn('13800138000', serialized + text)
        self.assertNotIn('chen@example.invalid', serialized + text)
        self.assertNotIn('外部医院', serialized)
        self.assertNotIn('秘密签名', serialized)
        self.assertNotIn('不应发送', serialized)
        self.assertNotIn('owner', serialized)
        self.assertLessEqual(len(serialized), MAX_CONTEXT_CHARS)
        self.assertIn('陈工', serialized)
        self.assertIn('医嘱签名', serialized)
        self.assertEqual(result['method'], 'model')

    async def test_model_runs_outside_shared_lock_and_revalidates_archived_project(self):
        def archive():
            self.assertFalse(self.lock.locked())
            self.workspace.update_opportunity('me', self.hospital['id'], self.sign['id'], {
                'expected_revision': self.sign['revision'], 'archived': True})
        stub = StubResolver({'items': [candidate(self.hospital, self.sign)], 'question': '请核对'}, callback=archive)
        result = await self.service(stub).resolve('me', '医院陈工签名')
        self.assertFalse(any(item['opportunity_id'] == self.sign['id'] for item in result['items']))
        self.assertNotEqual(result['method'], 'model')
        self.assertIn('核对', result['warning'])

    async def test_context_customer_is_an_unselected_hint_not_authority(self):
        stub = StubResolver({'items': [candidate(self.bank)], 'question': '是银行吗？'})
        result = await self.service(stub).resolve('me', '刚才陈工那件事', context_customer_id=self.hospital['id'])
        self.assertEqual(stub.calls[0][1]['context_customer_id'], self.hospital['id'])
        self.assertIsNone(result['selected_customer_id'])
        self.assertEqual(result['items'][0]['customer_id'], self.bank['id'])
        with self.assertRaises(KeyError):
            await self.service(stub).resolve('me', '陈工', context_customer_id=self.foreign['id'])

    async def test_no_model_rule_fallback_accepts_alias_and_abbreviated_contact(self):
        result = await self.service().resolve('me', '星浦医院陈工的签名项目')
        self.assertEqual(result['method'], 'rules')
        self.assertTrue(any(item['customer_id'] == self.hospital['id'] for item in result['items']))
        self.assertIsNone(result['selected_customer_id'])
        self.assertFalse(self.service().available)
        unknown = await self.service().resolve('me', '想到了一个新主意，回头再补')
        self.assertEqual(unknown['status'], 'none')
        self.assertEqual(unknown['method'], 'unavailable')
        self.assertIn('保存', unknown['question'])

    async def test_unconfigured_customer_resolver_is_not_called(self):
        resolver = CustomerResolver('', 'https://model.example.invalid', 'synthetic')
        self.assertFalse(resolver.available)
        result = await self.service(resolver).resolve('me', '星浦医院')
        self.assertEqual(result['method'], 'rules')

    async def test_provider_failure_falls_back_without_leaking_response(self):
        stub = StubResolver(error=RuntimeError('secret synthetic provider payload'))
        result = await self.service(stub).resolve('me', '星浦医院陈工')
        self.assertEqual(result['method'], 'rules')
        self.assertNotIn('secret', json.dumps(result))
        self.assertIn('暂', result['warning'])

    async def test_malformed_hallucinated_and_cross_owner_output_cannot_escape_validation(self):
        bad_results = [
            {'items': [candidate(self.foreign, self.foreign_project)], 'question': 'x'},
            {'items': [{**candidate(self.hospital), 'customer_id': 999999}], 'question': 'x'},
            {'items': [candidate(self.hospital, self.bank_sign)], 'question': 'x'},
            {'items': [{**candidate(self.hospital), 'customer_id': True}], 'question': 'x'},
            {'items': [{**candidate(self.hospital), 'confidence': 0.9}], 'question': 'x'},
            {'items': [candidate(self.hospital, reasons=['x'*241])], 'question': 'x'},
            {'items': [candidate(self.hospital), candidate(self.hospital)], 'question': 'x'},
            {'items': [candidate(self.hospital)], 'question': 'x', 'selected_customer_id': self.hospital['id']},
            {'items': [{**candidate(self.hospital), 'confirmed': True}], 'question': 'x'},
            {'items': [candidate(self.hospital)]*9, 'question': 'x'},
            {'items': [], 'question': None}, [], None,
        ]
        for data in bad_results:
            with self.subTest(data=data):
                result = await self.service(StubResolver(data)).resolve('me', '随便记一个想法')
                self.assertNotEqual(result['method'], 'model')
                self.assertIsNone(result['selected_customer_id'])
                self.assertFalse(any(item['customer_id'] in (self.foreign['id'], 999999) for item in result['items']))

    async def test_large_source_and_context_are_explicitly_truncated(self):
        for number in range(75):
            customer = self.crm.create_customer('me', {'name': f'合成客户{number}', 'notes': '材料上下文'*700}, NOW-number)
            for project in range(3):
                self.workspace.create_opportunity('me', customer['id'], {
                    'name': f'项目{project}', 'scope': '独立项目范围'*400, 'notes': '测试方案'*500})
        stub = StubResolver({'items': [], 'question': '请补充一点上下文。'})
        result = await self.service(stub).resolve('me', '想到'*10000, context_customer_id=self.hospital['id'])
        text, context, _ = stub.calls[0]
        self.assertLessEqual(len(text), MAX_MODEL_TEXT)
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), MAX_CONTEXT_CHARS)
        self.assertTrue(context['truncated'])
        self.assertIn('部分', result['warning'])
        self.assertEqual(context['customers'][0]['customer_id'], self.hospital['id'])

    async def test_invalid_input_never_reaches_provider(self):
        stub = StubResolver({'items': [], 'question': 'x'})
        for source in ('', ' '*5, 'x'*20001, 'bad\x00text', None):
            with self.subTest(source=str(source)[:20]):
                with self.assertRaises(ValueError):
                    await self.service(stub).resolve('me', source)
        self.assertEqual(stub.calls, [])


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_openai_compatible_transport_has_bounded_json_contract(self):
        received = []
        def handler(request):
            received.append(json.loads(request.content))
            self.assertEqual(request.url.path, '/v1/chat/completions')
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
                'content': json.dumps({'items': [], 'question': '需要再补充客户线索。'}, ensure_ascii=False)}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            resolver = CustomerResolver('synthetic-key', 'https://model.example.invalid/v1/', 'synthetic',
                                        timeout=3, client=client)
            self.assertTrue(resolver.available)
            result = await resolver.resolve('医院陈工', {'customers': [], 'truncated': False}, NOW)
        self.assertEqual(result['items'], [])
        self.assertEqual(received[0]['response_format'], {'type': 'json_object'})
        self.assertFalse(received[0]['stream'])
        self.assertIn('不得确认', received[0]['messages'][0]['content'])
        self.assertIn('2026-', received[0]['messages'][0]['content'])

    async def test_truncated_duplicate_nonfinite_and_oversize_json_are_rejected(self):
        choices = [
            {'finish_reason': 'length', 'message': {'content': '{"items":[],"question":"x"}'}},
            {'finish_reason': 'stop', 'message': {'content': '{"items":[],"items":[],"question":"x"}'}},
            {'finish_reason': 'stop', 'message': {'content': '{"items":[],"question":NaN}'}},
            {'finish_reason': 'stop', 'message': {'content': 'x'*16001}},
        ]
        for choice in choices:
            with self.subTest(choice=str(choice)[:50]):
                async with httpx.AsyncClient(transport=httpx.MockTransport(
                        lambda request: httpx.Response(200, json={'choices': [choice]}))) as client:
                    resolver = CustomerResolver('synthetic', 'https://model.example.invalid', 'synthetic', client=client)
                    with self.assertRaises(ValueError):
                        await resolver.resolve('客户', {'customers': []}, NOW)


if __name__ == '__main__':
    unittest.main()
