"""Synthetic HTTP research flows, with no real providers or deployment data."""
import asyncio
import hashlib
import re
from contextlib import asynccontextmanager

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from secretary.customer_store import CustomerStore
from secretary.coaching_service import CoachingService
from secretary.profile_intelligence import _rule_extract
from secretary.store import Store
from secretary.web import create_app, hash_password


class SyntheticAnalyzer:
    async def extract(self, source, context):
        return _rule_extract(source, context)


class SyntheticResearcher:
    def __init__(self):
        self.calls = []

    async def research(self, context):
        self.calls.append(context)
        name = context['customer']['name']
        return [{'url': 'https://official.example/unit', 'title': name + '简介',
                 'text': name + '，行业是金融。总部位于杭州。', 'entity_name': name,
                 'published_at': '2023-01-02', 'fetched_at': 1800000000}]


@asynccontextmanager
async def context(tmp_path, *, configured=True):
    path = tmp_path / 'isolated-research-web.sqlite3'
    crm, store = CustomerStore(path), Store(path)
    researcher = SyntheticResearcher() if configured else None
    app = create_app(store, crm, asyncio.Lock(), 'research-owner',
                     hash_password('synthetic-research-password'),
                     profile_analyzer=SyntheticAnalyzer(), public_researcher=researcher)
    controller = app.middlewares[0].__self__
    client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
    await client.start_server()
    login = await client.post('/api/login', json={'password': 'synthetic-research-password'})
    token = (await login.json())['csrf']

    async def call(method, path, data=None, status=200):
        result = await client.request(method, path, json=data, headers={'X-CSRF-Token': token})
        payload = await result.json()
        assert result.status == status, (path, result.status, payload)
        return payload

    try:
        unit = (await call('POST', '/api/customers', {'name': '合成研究银行'}, 201))['customer']
        await call('PATCH', f"/api/customers/{unit['id']}/profile-intelligence/settings",
                   {'official_domains': ['official.example']})
        yield client, call, controller, unit, researcher
    finally:
        await client.close()
        crm.close()
        store.close()


async def ready(call, run_id):
    for _ in range(100):
        run = (await call('GET', f'/api/research-runs/{run_id}'))['run']
        if run['status'] not in ('queued', 'searching', 'analyzing'):
            return run
        await asyncio.sleep(.02)
    raise AssertionError('Background research did not finish')


def test_http_quick_deep_draft_confirmation_and_resume(tmp_path):
    async def run():
        async with context(tmp_path) as (_, call, controller, unit, researcher):
            route = f"/api/customers/{unit['id']}/research-workspace"
            assert (await call('GET', route))['capabilities']['research_configured'] is True
            started = await call('POST', route, {'mode': 'quick', 'request_id': 'http-quick'})
            run_id = started['run']['id']
            first = await ready(call, run_id)
            assert first['status'] == 'ready' and first['items']
            assert controller.crm.profile('research-owner', unit['id'])['fields'] == []
            repeated = await call('POST', route, {'mode': 'quick', 'request_id': 'http-quick'})
            assert repeated['run']['id'] == run_id
            assert (await call('GET', route))['runs'][0]['id'] == run_id
            item = next(i for i in first['items'] if i['scope'] == 'account')
            candidate_id = item.get('candidate_id', item['id'])
            edited = (await call('PATCH', f'/api/research-runs/{run_id}', {
                'expected_revision': first['revision'],
                'items': [{'candidate_id': candidate_id, 'value': '用户修改后的行业说明', 'selected': True}]
            }))['run']
            assert controller.crm.profile('research-owner', unit['id'])['fields'] == []
            result = await call('POST', f'/api/research-runs/{run_id}/confirm', {
                'request_id': 'http-confirm', 'expected_revision': edited['revision'],
                'items': [{'candidate_id': candidate_id, 'expected_candidate_revision': item['revision'],
                           'expected_fact_id': item['current_fact_id'], 'value': '用户修改后的行业说明',
                           'verify_public': True, 'verify_entity': True}]
            })
            assert result['results']
            assert len(controller.crm.profile('research-owner', unit['id'])['fields']) == 1
            public_profile = await call('GET', f"/api/customers/{unit['id']}/profile")
            assert public_profile['fields'][0]['source']['type'] == 'public'
            assert public_profile['fields'][0]['source']['published_at'] == '2023-01-02'
            # The real discussion and coaching adapters must retain publication
            # dates, so historical public observations cannot become current promises.
            coach = CoachingService(controller.crm, None, asyncio.Lock())
            coach.profile_intelligence = controller.profile_intelligence
            fact = coach._profile('research-owner', unit['id'])['fields'][0]
            assert fact['basis'] == 'observation'
            assert fact['source']['published_at'] == '2023-01-02'
            assert fact['source']['url'] == 'https://official.example/unit'
            thread = controller.discussions.create_thread('research-owner', {'customer_id': unit['id']})['thread']
            discussion_context = controller.discussions._raw_context('research-owner', thread)
            assert '2023-01-02' in str(discussion_context)
            await coach.close()
            deep = await call('POST', route, {'mode': 'deep', 'request_id': 'http-deep'})
            assert deep['run']['id'] != run_id
            deeper = await ready(call, deep['run']['id'])
            assert deeper['status'] == 'ready'
            assert len(researcher.calls) >= 2
            assert (await call('GET', f'/api/research-runs/{run_id}'))['run']['id'] == run_id
            for table in ('tasks', 'proposals', 'notifications'):
                assert controller.store._db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
    asyncio.run(run())


def test_web_profile_scan_queues_the_shared_research_worker(tmp_path):
    async def run():
        async with context(tmp_path) as (_, call, controller, unit, researcher):
            intelligence = controller.profile_intelligence
            intelligence.configure('research-owner', {'enabled': True, 'auto_research': True})
            # Hold worker execution while proving that a profile scan queues an
            # existing deep run rather than making a second outbound search.
            workspace = controller.research_workspace
            original = workspace.process_pending

            async def paused(*args, **kwargs):
                return {'processed': 0, 'run_ids': []}

            workspace.process_pending = paused
            route = f"/api/customers/{unit['id']}/research-workspace"
            deep = (await call('POST', route, {'mode': 'deep', 'request_id': 'scan-deep'}))['run']
            scanned = await intelligence.scan('research-owner', unit['id'], force=True)
            assert scanned['research']['run']['id'] == deep['id']
            assert scanned['research']['reused'] is True
            assert researcher.calls == []
            workspace.process_pending = original
            assert (await ready(call, deep['id']))['status'] == 'ready'
            assert len(researcher.calls) == 3
    asyncio.run(run())


def test_http_research_auth_csrf_owner_and_strict_input(tmp_path):
    async def run():
        async with context(tmp_path) as (client, call, controller, unit, _):
            route = f"/api/customers/{unit['id']}/research-workspace"
            no_csrf = await client.post(route, json={'mode': 'quick', 'request_id': 'no-csrf'})
            assert no_csrf.status == 403
            await call('POST', route, {'mode': 'unbounded', 'request_id': 'bad'}, 400)
            await call('POST', route, {'mode': 'quick', 'request_id': 'bad', 'secret': 'internal'}, 400)
            stranger = controller.crm.create_customer('other-owner', {'name': '另一所有者合成单位'}, 1800000000)
            await call('GET', f"/api/customers/{stranger['id']}/research-workspace", status=404)
            await call('GET', '/api/research-runs/999999', status=404)
            await call('POST', '/api/research-runs/999999/cancel', {'expected_revision': 1}, 404)
            await call('POST', '/api/logout', {})
            result = await client.get(route)
            assert result.status == 401
    asyncio.run(run())


def test_http_unconfigured_research_keeps_existing_facts(tmp_path):
    async def run():
        async with context(tmp_path, configured=False) as (_, call, controller, unit, _):
            route = f"/api/customers/{unit['id']}/research-workspace"
            assert (await call('GET', route))['capabilities']['research_configured'] is False
            await call('GET', route)
            assert controller.crm.profile('research-owner', unit['id'])['fields'] == []
    asyncio.run(run())


def test_research_goal_preview_never_saves_a_customer_exchange(tmp_path):
    async def run():
        async with context(tmp_path) as (_, call, controller, unit, _):
            before = controller.crm._db.execute('SELECT COUNT(*) FROM crm_records').fetchone()[0]
            preview = await call('POST', '/api/secretary-goals/preview', {
                'text': '帮我研究合成研究银行，做单位画像，准备拜访'
            })
            assert preview['intent'] == 'research'
            assert any(c['customer_id'] == unit['id'] for c in preview['resolution']['items'])
            ordinary = await call('POST', '/api/secretary-goals/preview', {'text': '客户说预算暂未审批'})
            assert ordinary['intent'] == 'record'
            quoted = await call('POST', '/api/secretary-goals/preview', {'text': '王工说：帮我查一下接口文档'})
            assert quoted['intent'] == 'record'
            assert controller.crm._db.execute('SELECT COUNT(*) FROM crm_records').fetchone()[0] == before
    asyncio.run(run())


def test_index_asset_versions_change_with_source_without_restarting(tmp_path, monkeypatch):
    import secretary.web as web_module
    static = tmp_path / 'public-synthetic-assets'
    static.mkdir()
    assets = ('app.js', 'app.css', 'account-intelligence.js', 'account-intelligence.css',
              'customer-timeline.js', 'customer-timeline.css')
    (static / 'index.html').write_text(''.join(f'<script src="/static/{asset}"></script>' for asset in assets))
    for asset in assets:
        (static / asset).write_bytes(b'/* synthetic public source */')
    monkeypatch.setattr(web_module, 'STATIC', static)

    async def run():
        async with context(tmp_path) as (client, _, _, _, _):
            first = await client.get('/')
            old = await first.text()
            assert first.headers['Cache-Control'] == 'no-store'
            assert len(re.findall(r'\?v=[a-f0-9]{16}', old)) == len(assets)
            (static / 'app.js').write_bytes(b'/* updated public source */')
            newer = await (await client.get('/')).text()
            version = hashlib.sha256((static / 'app.js').read_bytes()).hexdigest()[:16]
            assert f'/static/app.js?v={version}' in newer
            assert newer != old
            resource = await client.get('/static/app.js?v=' + version)
            assert await resource.read() == b'/* updated public source */'
    asyncio.run(run())
