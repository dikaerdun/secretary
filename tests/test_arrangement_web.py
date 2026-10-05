"""Authenticated endpoints, persistent worker lifecycle and strict payloads."""
import asyncio
from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer
from deploy.training import build_training_app, training_config, PASSWORD, OWNER
from secretary.web import ARRANGEMENT_WORKER
from secretary.secretary_interpreter import SecretaryInterpreter
from tests.test_arrangement_flow_integration import NOW


def test_http_queue_operations_auth_validation_replay_and_assets(tmp_path):
    async def run():
        app = await build_training_app(training_config(root=tmp_path), clock=lambda:NOW)
        controller = next(r.handler.__self__ for r in app.router.routes() if getattr(r.handler,'__name__','') == 'dashboard')
        flow = controller.secretary_flow
        flow.interpreter = SecretaryInterpreter()
        client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
        await client.start_server()
        try:
            assert (await client.get('/api/secretary/arrangements')).status == 401
            login = await (await client.post('/api/login', json={'password':PASSWORD})).json()
            headers = {'X-CSRF-Token':login['csrf']}
            first = flow.submit(OWNER, {'text':'本周定下来，下月去拜访客户，周五再问', 'request_id':'web-one'})
            await flow.process_one()
            turn = flow.turn(OWNER, first['id'])
            assert turn['status'] == 'done', turn.get('error')
            plan = turn['plan']
            queue = await (await client.get('/api/secretary/arrangements?view=all&limit=200&offset=0')).json()
            assert plan['id'] in [item['plan_id'] for item in queue['items']]
            route = f"/api/secretary/plans/{plan['id']}/arrangement-decisions"
            body = {'operation':'pause', 'reason':'虚构行程调整', 'request_id':'pause', 'expected_revision':plan['revision']}
            assert (await client.post(route,json=body)).status == 403
            paused = await client.post(route,json=body,headers=headers)
            assert paused.status == 200, await paused.text()
            result = await paused.json()
            replay = await (await client.post(route,json=body,headers=headers)).json()
            assert replay == result
            conflict = await client.post(route,json={**body,'reason':'different'},headers=headers)
            assert conflict.status == 409
            invalid = await client.post(route,json={**body,'request_id':'invalid','expected_revision':result['revision'],'unexpected':1},headers=headers)
            assert invalid.status == 422
            assert (await client.get('/api/secretary/arrangements?offset=-1')).status == 422
            assert (await client.get('/api/secretary/arrangements?state=unknown')).status == 422
            assert (await client.get('/api/secretary/arrangements?customer_id=999999')).status == 404
            stale = await client.post(route,json={**body,'request_id':'stale'},headers=headers)
            assert stale.status == 409
            html = await (await client.get('/')).text()
            assert '/static/arrangement-queue.js?v=' in html and '/static/arrangement-queue.css?v=' in html
            for asset in ('arrangement-queue.js','arrangement-queue.css'):
                assert (await client.get('/static/'+asset)).status == 200
            assert app[ARRANGEMENT_WORKER] and not app[ARRANGEMENT_WORKER].done()
        finally:
            await client.close()
        assert app[ARRANGEMENT_WORKER].done()
    asyncio.run(run())
