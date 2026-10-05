"""Exercise the user-visible correction/confirmation/reminder lifecycle."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

import httpx
from aiohttp import CookieJar, FormData
from aiohttp.test_utils import TestClient, TestServer

from secretary.audio import AudioService
from secretary.coaching_service import CoachingService
from secretary.customer_service import CustomerService
from secretary.customer_store import CustomerStore
from secretary.store import Store
from secretary.web import create_app, hash_password


class RevisionAPI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        path = Path(self.folder.name) / 'review.sqlite3'
        self.store, self.crm = Store(path), CustomerStore(path)
        self.now = time.time()
        self.lock = asyncio.Lock()
        class Parser:
            async def parse(inner, text, now, context=None):
                return {'intent': 'note', 'customer_name': None, 'basic': {}, 'contact': {}, 'attributes': []}
        class Organizer:
            async def organize(inner, text, now, context=None):
                return {'summary': text, 'key_points': [], 'open_questions': [], 'actions': [
                    {'title': '准备交流后的材料', 'kind': 'suggestion', 'reason': '需要核对', 'owner_hint': '我', 'remind_at': None}]}
        self.customer_service = CustomerService(self.crm, Parser(), self.lock, organizer=Organizer())
        app = create_app(self.store, self.crm, self.lock, 'alice', hash_password('review-password-123'),
                         organizer=Organizer(), customer_service=self.customer_service)
        self.web = app.middlewares[0].__self__
        self.client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        login = await self.client.post('/api/login', json={'password': 'review-password-123'})
        self.csrf = (await login.json())['csrf']

    async def asyncTearDown(self):
        await self.client.close()
        self.crm.close()
        self.store.close()
        self.folder.cleanup()

    async def write(self, method, path, data):
        if path.startswith('/api/proposals/') and data == {}:
            proposal = self.crm.get_proposal('alice', int(path.split('/')[3]))
            if proposal:
                data = {'updated_at': proposal['updated_at']}
        r = await self.client.request(method, path, json=data, headers={'X-CSRF-Token': self.csrf})
        return r.status, await r.json()

    def record(self):
        c = self.crm.create_customer('alice', {'name': '星河医院', 'aliases': ['星河'], 'contact': '王经理'}, self.now)
        return self.crm.create_record('alice', {'title': '提交原方案', 'content': '原始交流', 'customer_id': c['id'], 'kind': 'action'}, self.now)

    async def schedule(self, record, offset=3600):
        seen = await (await self.client.get(f"/api/records/{record['id']}")).json()
        self.assertRegex(seen['schedule_snapshot'], r'^[0-9a-f]{64}$')
        status, detail = await self.write('POST', f"/api/records/{record['id']}/schedule", {'remind_at': self.now + offset, 'expected_schedule_snapshot': seen['schedule_snapshot']})
        self.assertEqual(status, 200, detail)
        return detail

    async def test_active_change_requires_exact_confirmation_and_replaces_notification(self):
        record = self.record()
        detail = await self.schedule(record)
        old_p = detail['proposal']['id']
        status, _ = await self.write('POST', f'/api/proposals/{old_p}/confirm', {})
        self.assertEqual(status, 200)
        old_task = self.crm.record_detail('alice', record['id'])['task']
        seen = await (await self.client.get(f"/api/records/{record['id']}")).json()
        self.assertRegex(seen['schedule_snapshot'], r'^[0-9a-f]{64}$')
        status, detail = await self.write('POST', f"/api/records/{record['id']}/schedule",
                                         {'title': '提交修正版', 'remind_at': self.now + 7200, 'expected_schedule_snapshot': seen['schedule_snapshot']})
        self.assertEqual(status, 200, detail)
        new_p = detail['proposal']['id']
        self.assertEqual(detail['task']['id'], old_task['id'])
        self.assertEqual(detail['task']['title'], '提交原方案')
        self.assertEqual(detail['task']['remind_at'], self.now + 3600)
        status, _ = await self.write('POST', f"/api/records/{record['id']}/confirm", {'proposal_id': old_p})
        self.assertEqual(status, 409)
        status, _ = await self.write('POST', f'/api/proposals/{new_p}/confirm', {})
        self.assertEqual(status, 200)
        current = self.crm.record_detail('alice', record['id'])
        self.assertEqual(current['task']['id'], old_task['id'])
        self.assertEqual(current['task']['title'], '提交修正版')
        self.assertEqual(current['record']['title'], '提交修正版')
        self.assertEqual(current['task']['revision'], 2)
        self.assertIsNone(self.store.claim_due(self.now + 3600))
        notification = self.store.claim_due(self.now + 7200)
        self.assertIn('星河医院', notification['text'])
        self.assertIn('王经理', notification['text'])

    async def test_done_or_cancel_cannot_reactivate_old_proposals(self):
        record = self.record()
        detail = await self.schedule(record)
        p = detail['proposal']['id']
        status, _ = await self.write('PATCH', f"/api/records/{record['id']}", {'status': 'done'})
        self.assertEqual(status, 200)
        status, _ = await self.write('POST', f'/api/proposals/{p}/confirm', {})
        self.assertEqual(status, 409)
        self.assertIsNone(self.store.claim_due(self.now + 100000))
        next_status, next_detail = await self.write('POST', f"/api/records/{record['id']}/next-visit", {'title': '再拜访客户'})
        self.assertEqual(next_status, 201)
        self.assertEqual(next_detail['record']['parent_record_id'], record['id'])
        self.assertIsNone(next_detail['proposal'])

    async def test_invalid_correction_never_finishes_or_modifies_the_original(self):
        record = self.record()
        detail = await self.schedule(record)
        await self.write('POST', f"/api/proposals/{detail['proposal']['id']}/confirm", {})
        status, _ = await self.write('POST', f"/api/records/{record['id']}/reinterpret",
            {'content': 'x' * 7000, 'status': 'done', 'mode': 'note_only'})
        self.assertEqual(status, 400)
        saved = self.crm.record_detail('alice', record['id'])
        self.assertEqual(saved['record']['content'], '原始交流')
        self.assertNotEqual(saved['record']['status'], 'done')
        self.assertEqual(saved['task']['status'], 'pending')
        status, _ = await self.write('POST', f"/api/records/{record['id']}/reinterpret",
            {'content': '新的文字', 'mode': 'sync_reminder', 'remind_at': self.now - 100})
        self.assertEqual(status, 400)
        self.assertEqual(self.crm.get_record('alice', record['id'])['content'], '原始交流')

    async def test_old_proposal_snapshot_cannot_confirm_revised_time(self):
        record = self.record()
        detail = await self.schedule(record)
        p = detail['proposal']
        await self.schedule(record, 7200)
        result = await self.client.post(f"/api/proposals/{p['id']}/confirm", json={'updated_at': p['updated_at']},
                                        headers={'X-CSRF-Token': self.csrf})
        self.assertEqual(result.status, 409)
        self.assertEqual(self.crm.get_proposal('alice', p['id'])['status'], 'pending')
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)

    async def test_snooze_validates_conflict_past_and_own_task_exclusion(self):
        for source, offset in [('a', 3600), ('b', 10800)]:
            self.store.execute('alice', source, {'action': 'create', 'title': source, 'remind_at': self.now + offset}, self.now)
        for offset in [-1, 10801]:
            reply = self.store.execute('alice', 'change:' + str(offset),
                {'action': 'snooze', 'task_id': 1, 'remind_at': self.now + offset}, self.now)
            self.assertIn('未调整', reply)
        reply = self.store.execute('alice', 'own', {'action': 'snooze', 'task_id': 1, 'remind_at': self.now + 3601}, self.now)
        self.assertIn('已调整', reply)
        self.assertEqual(self.crm.get_task('alice', 1)['revision'], 2)

    async def test_full_reinterpret_clears_customer_and_preserves_original(self):
        record = self.record()
        status, result = await self.write('POST', f"/api/records/{record['id']}/reinterpret",
            {'title': '修正标题', 'content': '修正后的交流', 'customer_id': None, 'status': 'following', 'kind': 'note', 'mode': 'note_only'})
        self.assertEqual(status, 200, result)
        saved = self.crm.get_record('alice', record['id'])
        self.assertEqual(saved['title'], '修正标题')
        self.assertEqual(saved['content'], '修正后的交流')
        self.assertEqual(saved['original_content'], '原始交流')
        self.assertIsNone(saved['customer_id'])
        self.assertEqual(saved['kind'], 'note')

    async def test_activity_is_new_exchange_and_review_actions_link_to_source(self):
        record = self.record()
        status, result = await self.write('POST', f"/api/records/{record['id']}/activities/organize", {'body': '今天新的交流内容'})
        self.assertEqual(status, 201, result)
        self.assertNotEqual(result['record']['id'], record['id'])
        self.assertEqual(result['record']['parent_record_id'], record['id'])
        self.assertEqual(self.crm.get_record('alice', record['id'])['content'], '原始交流')
        inbox = await (await self.client.get('/api/review-inbox')).json()
        self.assertTrue(any(item['record_id'] == result['record']['id'] for item in inbox['actions']))
        self.assertEqual(inbox['proposals'], [])

    async def test_voice_settings_without_provider_are_real_and_owner_isolated(self):
        record = self.record()
        capabilities = await (await self.client.get('/api/audio/capabilities')).json()
        self.assertFalse(capabilities['can_transcribe'])
        status, settings = await self.write('PATCH', '/api/voice-settings', {'hotwords': ['密评', '密评', '证书管理']})
        self.assertEqual(status, 200)
        self.assertEqual(settings['hotwords'], ['密评', '证书管理'])
        self.assertIn('星河', settings['automatic_hotwords'])
        self.assertNotIn('密评', self.web.audio.settings('bob')['hotwords'][14:])
        form = FormData()
        form.add_field('file', b'fake audio', filename='audio.wav', content_type='audio/wav')
        result = await self.client.post('/api/audio/transcribe', data=form, headers={'X-CSRF-Token': self.csrf})
        self.assertEqual(result.status, 503)
        self.assertEqual(self.crm.list_records('alice')['total'], 1)

    async def test_optional_asr_payload_has_vocabulary_and_returns_reviewable_text_only(self):
        self.record()
        captured = []
        def handle(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': '原始转写，明天再核对。'}}]})
        self.web.audio = AudioService(self.crm, 'synthetic-key', transport=httpx.MockTransport(handle))
        form = FormData()
        form.add_field('file', b'RIFF mock', filename='audio.wav', content_type='audio/wav')
        result = await self.client.post('/api/audio/transcribe', data=form, headers={'X-CSRF-Token': self.csrf})
        self.assertEqual(result.status, 200)
        self.assertTrue((await result.json())['needs_review'])
        self.assertIn('星河医院', captured[0]['messages'][0]['content'])
        self.assertTrue(captured[0]['messages'][1]['content'][0]['input_audio']['data'].startswith('data:audio/wav;base64,'))
        self.assertEqual(self.crm.list_records('alice')['total'], 1)

    async def test_incomplete_asr_is_not_presented_as_a_complete_transcript(self):
        self.web.audio = AudioService(self.crm, 'synthetic-key', transport=httpx.MockTransport(lambda request:
            httpx.Response(200, json={'choices': [{'finish_reason': 'length', 'message': {'content': '截断的私人文本'}}]})))
        form = FormData()
        form.add_field('file', b'RIFF mock', filename='audio.wav', content_type='audio/wav')
        result = await self.client.post('/api/audio/transcribe', data=form, headers={'X-CSRF-Token': self.csrf})
        self.assertEqual(result.status, 502)
        self.assertNotIn('私人文本', await result.text())
        self.assertEqual(self.crm.list_records('alice')['total'], 0)

    async def test_feedback_completes_adopted_reminder_and_reaches_new_advice(self):
        customer = self.crm.create_customer('alice', {'name': '反馈客户'}, self.now)
        profiles = []
        class Coach:
            async def advise(inner, profile, now):
                profiles.append(profile)
                return {'summary': '建议', 'objective': '目标', 'rationale': '依据', 'questions': [], 'risks': [],
                        'next_moves': [{'title': '补充材料', 'reason': '客户要求', 'contact_hint': '技术负责人',
                            'preparation': '材料', 'talk_track': '核对内容', 'success_signal': '完成核对'}]}
        coaching = CoachingService(self.crm, Coach(), self.lock)
        self.web.coaching = coaching
        try:
            coaching.schedule('alice', customer['id'])
            await asyncio.gather(*list(coaching.running.values()))
            advice = coaching.view('alice', customer['id'])['recommendation']
            record = coaching.adopt('alice', customer['id'], advice['version'], 1)
            detail = await self.schedule(record)
            await self.write('POST', f"/api/proposals/{detail['proposal']['id']}/confirm", {})
            status, _ = await self.write('POST', f"/api/customers/{customer['id']}/coach-feedback",
                {'version': advice['version'], 'index': 1, 'status': 'completed', 'note': '客户确认完成'})
            self.assertEqual(status, 200)
            await asyncio.gather(*list(coaching.running.values()))
            self.assertEqual(self.crm.get_record('alice', record['id'])['status'], 'done')
            self.assertIsNone(self.store.claim_due(self.now + 100000))
            self.assertEqual(profiles[-1]['coaching_feedback'][0]['status'], 'completed')
        finally:
            await coaching.close()
