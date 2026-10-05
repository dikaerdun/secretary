"""Matter flow through authenticated HTTP; synthetic data and no model network."""
import asyncio

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from deploy.training import build_training_app, training_config, PASSWORD, OWNER
from tests.test_secretary_flow import NOW, ScriptedInterpreter, initial


class Router:
    def __init__(self, service):
        self.service = service

    async def route(self, owner, text, scope=None, matter_id=None, mode='auto'):
        if matter_id:
            matter = self.service.get(owner, matter_id)
            action = matter['actions'][0]
            return {'kind': 'existing', 'matter_id': matter_id, 'base_revision': matter['revision'],
                'actions': [{'title': action['title'], 'existing_record_id': action['id'],
                    'content': text, 'evidence': text, 'status': 'done' if '做好' in text else 'following'}]}
        return {'kind': 'new', 'title': '虚构电力材料准备', 'objective': '准备一版可汇报的材料',
            'actions': [{'title': '调整PPT结构', 'content': text, 'evidence': '调整PPT结构'},
                        {'title': '补充装置材料', 'content': text, 'evidence': '补充装置材料'}]}


def test_two_steps_one_matter_continue_complete_does_not_end_objective(tmp_path):
    async def run():
        app = await build_training_app(training_config(root=tmp_path), clock=lambda: NOW)
        controller = next(r.handler.__self__ for r in app.router.routes() if getattr(r.handler, '__name__', '') == 'dashboard')
        controller.secretary_flow.interpreter = ScriptedInterpreter([{'intent': 'note', 'changes': {}}] * 3)
        controller.secretary_flow.matter_router = Router(controller.matters)
        client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
        await client.start_server()
        try:
            assert (await client.get('/api/matters')).status == 401
            session = await (await client.post('/api/login', json={'password': PASSWORD})).json()
            headers = {'X-CSRF-Token': session['csrf']}
            async def say(text, key, matter=None):
                body = {'text': text, 'request_id': key}
                if matter:
                    body.update(matter_id=matter['id'], matter_revision=matter['revision'])
                response = await client.post('/api/secretary/turns', json=body, headers=headers)
                assert response.status == 202, await response.text()
                turn = (await response.json())['turn']
                for _ in range(80):
                    turn = (await (await client.get('/api/secretary/turns/' + str(turn['id']))).json())['turn']
                    if turn['status'] not in ('queued', 'processing'):
                        break
                    await asyncio.sleep(.03)
                assert turn['status'] == 'done', turn.get('error')
                return turn
            first = await say('调整PPT结构，补充装置材料', 'one')
            matter = first['matter']
            assert matter['action_count'] == 2
            assert matter['status'] == 'following'
            assert len((await (await client.get('/api/matters')).json())['items']) == 1
            retry = await client.post('/api/secretary/turns', json={'text': '调整PPT结构，补充装置材料', 'request_id': 'one'}, headers=headers)
            assert (await retry.json())['turn']['id'] == first['id']
            second = await say('调整PPT结构时突出产品线', 'two', matter)
            assert second['matter']['id'] == matter['id']
            assert second['matter']['action_count'] == 2
            third = await say('PPT结构已经做好', 'three', second['matter'])
            assert third['matter']['completed_action_count'] == 1
            assert third['matter']['status'] == 'following'
            resolved = await (await client.get('/api/matters/resolve', params={'entity_type': 'record', 'entity_id': first['record_id']})).json()
            assert resolved['items'][0]['id'] == matter['id']
            original = await (await client.get('/api/records/' + str(first['record_id']))).json()
            assert original['matters'][0]['id'] == matter['id']
            assert controller.crm.get_record(OWNER, first['record_id'])['original_content'] == '调整PPT结构，补充装置材料'
            assert not controller.crm._db.execute('SELECT id FROM tasks WHERE title=?', ('虚构电力材料准备',)).fetchall()
            stale = await client.post('/api/secretary/turns', json={'text': '旧页面输入', 'request_id': 'stale',
                'matter_id': matter['id'], 'matter_revision': matter['revision']}, headers=headers)
            assert stale.status == 409
        finally:
            await client.close()
    asyncio.run(run())


def test_appointment_links_to_matter_without_calendar_duplication(tmp_path):
    async def run():
        app = await build_training_app(training_config(root=tmp_path), clock=lambda: NOW)
        controller = next(r.handler.__self__ for r in app.router.routes() if getattr(r.handler, '__name__', '') == 'dashboard')
        controller.secretary_flow.interpreter = ScriptedInterpreter([initial()])
        class SourceRouter:
            async def route(self, *args, **kwargs):
                return {'kind': 'source', 'reason': '先保留安排'}
        controller.secretary_flow.matter_router = SourceRouter()
        client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
        await client.start_server()
        try:
            session = await (await client.post('/api/login', json={'password': PASSWORD})).json()
            headers = {'X-CSRF-Token': session['csrf']}
            first = await (await client.post('/api/secretary/turns', json={'text': '10月8日约林博士吃饭', 'request_id': 'appointment'}, headers=headers)).json()
            for _ in range(80):
                turn = (await (await client.get('/api/secretary/turns/' + str(first['turn']['id']))).json())['turn']
                if turn['status'] not in ('queued', 'processing'):
                    break
                await asyncio.sleep(.03)
            assert turn['status'] == 'done', turn.get('error')
            assert turn['matter_id']
            agenda = await (await client.get('/api/agenda?date=2026-10-08')).json()
            plans = [p for p in agenda['secretary_plans'] if p['id'] == turn['plan_id']]
            assert len(plans) == 1
            assert plans[0]['matters'][0]['id'] == turn['matter_id']
            assert plans[0]['start_at'] is None
        finally:
            await client.close()
    asyncio.run(run())
