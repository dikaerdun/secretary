"""Local runtime boundaries: private config, durable capture, no bot sender."""
import asyncio
import json
import os
from pathlib import Path
import socket
import sqlite3
import time

from aiohttp import CookieJar
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import httpx
import pytest

from secretary.local import (LOCAL_OWNER, LocalError, build_local_app, check_port,
                             ensure_access_file, read_local_config, runtime_status, serve_local)
from secretary.web import hash_password, verify_password
from secretary.store import Store
import secretary.local as local_runtime


def config(tmp_path, **kwargs):
    return read_local_config(root=tmp_path, environ={}, **kwargs)


def test_local_config_reads_private_override_then_process_without_mutating_env(tmp_path, monkeypatch):
    (tmp_path / '.env').write_text(
        'DEEPSEEK_API_KEY=base-key\nDEEPSEEK_MODEL=base-model\nSECRETARY_DB_PATH=data/server.sqlite3\n'
        'SECRETARY_WEB_PORT=9123\nWECOM_BOT_ID=unneeded\nLOCAL_SECRETARY_TEST_SENTINEL=only-file\n', encoding='utf-8')
    (tmp_path / '.env.local').write_text(
        'DEEPSEEK_API_KEY=local-key\nLISTEN_NOTE_API_KEY=local-listen\n', encoding='utf-8')
    monkeypatch.delenv('LOCAL_SECRETARY_TEST_SENTINEL', raising=False)
    selected = read_local_config(root=tmp_path, environ={'DEEPSEEK_API_KEY': 'process-key'})
    assert selected['api_key'] == 'process-key'
    assert selected['listen_note_api_key'] == 'local-listen'
    assert selected['model'] == 'base-model'
    assert selected['db_path'] == tmp_path / 'data' / 'local-secretary.sqlite3'
    assert selected['port'] == 8765
    assert selected['owner'] == LOCAL_OWNER
    assert 'bot_id' not in selected and 'bot_secret' not in selected
    assert 'WECOM_BOT_ID' not in runtime_status(selected)
    assert 'local-key' in (tmp_path / '.env.local').read_text()
    assert not os.environ.get('LOCAL_SECRETARY_TEST_SENTINEL')


@pytest.mark.parametrize('kwargs', [
    {'host': '0.0.0.0'}, {'port': 80}, {'db_path': 'data/secretary.sqlite3'},
    {'environ': {'DEEPSEEK_BASE_URL': 'http://secret.invalid/?key=private-key'}},
    {'environ': {'LISTEN_NOTE_MCP_URL': 'https://private-key@secret.invalid/mcp'}},
])
def test_local_config_rejects_shared_db_public_host_and_unsafe_urls_without_echo(tmp_path, kwargs):
    arguments = {'root': tmp_path, 'environ': {}, **kwargs}
    with pytest.raises(LocalError) as caught:
        read_local_config(**arguments)
    assert 'private-key' not in str(caught.value)
    assert 'secret.invalid' not in str(caught.value)


def test_generated_access_is_private_persistent_and_never_displayed(tmp_path, capsys):
    selected = config(tmp_path)
    path = tmp_path / 'deploy' / 'local-access.local.json'
    first = ensure_access_file(path, selected)
    assert verify_password(first['password'], first['password_hash'])
    assert len(first['password']) >= 24
    second = ensure_access_file(path, {**selected, 'url': 'http://127.0.0.1:8875'})
    assert second['password'] == first['password']
    assert second['password_hash'] == first['password_hash']
    assert second['url'].endswith(':8875')
    assert first['password'] not in capsys.readouterr().out
    assert json.loads(path.read_text()) == second
    if os.name != 'nt':
        assert path.stat().st_mode & 0o777 == 0o600
    path.write_text('{"password": "invalid"}', encoding='utf-8')
    with pytest.raises(LocalError):
        ensure_access_file(path, selected)
    assert path.read_text() == '{"password": "invalid"}'


def test_private_json_temp_is_protected_while_empty_before_secret_write(tmp_path, monkeypatch):
    protected_contents = []
    protect = local_runtime.private_file

    def check_empty_then_protect(path):
        protected_contents.append(path.read_bytes())
        assert path.stat().st_size == 0
        protect(path)

    monkeypatch.setattr(local_runtime, 'private_file', check_empty_then_protect)
    destination = tmp_path / 'deploy' / 'access.local.json'
    payload = {'password': 'synthetic-private-password'}
    local_runtime._write_private_json(destination, payload)
    assert protected_contents == [b'']
    assert json.loads(destination.read_text(encoding='utf-8')) == payload
    assert list(destination.parent.glob(destination.name + '.*.local')) == []


def test_port_probe_never_reuses_an_existing_listener():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        port = listener.getsockname()[1]
        with pytest.raises(LocalError):
            check_port('127.0.0.1', port)
        assert listener.getsockname()[1] == port


def test_local_http_saves_customer_and_material_without_credentials_or_fake_analysis(tmp_path, monkeypatch):
    # No bot or provider can be reached accidentally in this test.
    outbound_attempts = []
    async def forbidden_request(*args, **kwargs):
        outbound_attempts.append(True)
        raise AssertionError('local unconfigured test attempted an outbound request')
    monkeypatch.setattr('httpx.AsyncClient.post', forbidden_request)

    async def exercise():
        selected = config(tmp_path)
        app = await build_local_app(selected, hash_password('local-test-password'))
        client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
        await client.start_server()
        try:
            session = await (await client.get('/api/session')).json()
            assert session['runtime']['mode'] == 'local'
            assert session['runtime']['wecom_connected'] is False
            assert session['runtime']['model_configured'] is False
            assert session['demo'] is False
            login = await client.post('/api/login', json={'password': 'local-test-password'})
            csrf = (await login.json())['csrf']
            headers = {'X-CSRF-Token': csrf}
            created = await client.post('/api/customers', json={'name': '本机真实客户'}, headers=headers)
            assert created.status == 201
            customer = (await created.json())['customer']
            material = await client.post('/api/materials', json={
                'provider': 'manual', 'title': '本机交流原文', 'text': '我答应发送技术资料。',
                'customer_id': customer['id'], 'category': 'conversation'}, headers=headers)
            assert material.status == 202
            material_id = (await material.json())['material']['id']
            detail = None
            for _ in range(60):
                detail = await (await client.get(f'/api/materials/{material_id}')).json()
                if detail['material']['status'] == 'failed':
                    break
                await asyncio.sleep(.05)
            assert detail['material']['status'] == 'failed'
            assert detail['text'] == '我答应发送技术资料。'
            assert detail.get('analysis') is None
            assert (await (await client.get('/api/agenda?period=day')).json())['total'] == 0
            audio = await (await client.get('/api/audio/capabilities')).json()
            assert audio['configured'] is False
            assert audio['can_transcribe'] is False
            dashboard = await (await client.get('/api/dashboard')).json()
            assert dashboard['bot_connected'] is False
        finally:
            await client.close()

        # A new app opens the same database without seeding example customers.
        reopened = TestClient(TestServer(await build_local_app(selected, hash_password('local-test-password'))),
                              cookie_jar=CookieJar(unsafe=True))
        await reopened.start_server()
        try:
            await reopened.post('/api/login', json={'password': 'local-test-password'})
            customers = await (await reopened.get('/api/customers')).json()
            assert len(customers['items']) == 1
            assert customers['items'][0]['name'] == '本机真实客户'
            persisted = await (await reopened.get(f'/api/materials/{material_id}')).json()
            assert persisted['text'] == '我答应发送技术资料。'
        finally:
            await reopened.close()
    asyncio.run(exercise())
    assert outbound_attempts == []


def test_runtime_start_stop_request_closes_service_and_removes_only_its_state(tmp_path):
    async def exercise():
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        selected = config(tmp_path, port=port)
        access = tmp_path / 'deploy' / 'local-access.local.json'
        state = tmp_path / 'deploy' / 'local-process.local.json'
        stop = tmp_path / 'deploy' / 'local-stop-testinstance.local'
        task = asyncio.create_task(serve_local(selected, access_file=access, state_file=state,
                                  stop_file=stop, instance_id='testinstance'))
        try:
            for _ in range(100):
                if state.exists():
                    break
                if task.done():
                    await task
                await asyncio.sleep(.05)
            metadata = json.loads(state.read_text())
            assert metadata['status'] == 'ready'
            assert metadata['module'] == 'secretary.local'
            assert metadata['database'] == str(selected['db_path'])
            # A request for another instance cannot stop this service.
            stop.write_text(json.dumps({'instance_id': 'anotherinstance'}), encoding='utf-8')
            await asyncio.sleep(.35)
            assert not task.done()
            stop.write_text(json.dumps({'instance_id': 'testinstance'}), encoding='utf-8')
            await asyncio.wait_for(task, timeout=5)
            assert not state.exists() and not stop.exists()
            assert access.exists() and selected['db_path'].exists()
            check_port('127.0.0.1', port)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(exercise())


def test_local_confirmed_due_items_stay_visible_without_claiming_delivery(tmp_path):
    async def exercise():
        selected = config(tmp_path)
        store = Store(selected['db_path'])
        before = time.time() - 120
        try:
            store.execute(LOCAL_OWNER, 'due-proposal', {'action': 'propose', 'title': '本机待检查事项',
                          'remind_at': before + 60}, before)
            store.execute(LOCAL_OWNER, 'due-confirm', {'action': 'confirm', 'proposal_id': 1}, before)
        finally:
            store.close()
        client = TestClient(TestServer(await build_local_app(selected, hash_password('local-test-password'))),
                            cookie_jar=CookieJar(unsafe=True))
        await client.start_server()
        try:
            await client.post('/api/login', json={'password': 'local-test-password'})
            await asyncio.sleep(.6)
            dashboard = await (await client.get('/api/dashboard')).json()
            assert dashboard['stats']['overdue'] == 1
            assert dashboard['queues']['overdue']['items'][0]['title'] == '本机待检查事项'
            agenda = await (await client.get('/api/agenda?period=day')).json()
            assert agenda['total'] == 1
            with sqlite3.connect(selected['db_path']) as database:
                status, attempts, sent_at = database.execute('SELECT status,attempts,sent_at FROM notifications').fetchone()
                assert status == 'queued' and attempts == 0 and sent_at is None
        finally:
            await client.close()
    asyncio.run(exercise())


def test_local_shared_model_client_bypasses_broken_proxy_and_closes(tmp_path, monkeypatch):
    """Exercise the real HTTP transport against synthetic loopback model data."""
    observed_clients, providers, requests = [], {}, []
    real_client = httpx.AsyncClient

    def capture_client(*args, **kwargs):
        client = real_client(*args, **kwargs)
        observed_clients.append((client, kwargs))
        return client

    for name in ('InteractionOrganizer', 'CustomerVoiceParser', 'SalesCoach'):
        provider_type = getattr(local_runtime, name)
        def capture_provider(*args, _type=provider_type, _name=name, **kwargs):
            provider = _type(*args, **kwargs)
            providers[_name] = provider
            return provider
        monkeypatch.setattr(local_runtime, name, capture_provider)
    monkeypatch.setattr(local_runtime.httpx, 'AsyncClient', capture_client)

    async def exercise(proxy_url):
        async def synthetic_model(request):
            requests.append(await request.json())
            return web.json_response({'choices': [{'finish_reason': 'stop', 'message': {
                'content': json.dumps({'summary': '合成材料已整理', 'key_points': [],
                                      'open_questions': [], 'actions': []}, ensure_ascii=False)}}]})
        synthetic = web.Application()
        synthetic.router.add_post('/chat/completions', synthetic_model)
        server = TestServer(synthetic)
        await server.start_server()
        app_client = None
        try:
            base_url = str(server.make_url('')).rstrip('/')
            # This proves the test proxy would break the same loopback request
            # if trust_env were accidentally restored on the shared client.
            async with real_client(trust_env=True, timeout=.5) as inherited_proxy_client:
                with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):
                    await inherited_proxy_client.post(base_url + '/chat/completions', json={})
            assert requests == []
            selected = config(tmp_path)
            # Only this synthetic test endpoint uses HTTP. Production config
            # still requires HTTPS; no credential file is read by this test.
            selected.update(api_key='synthetic-local-test-key', base_url=base_url)
            app = await build_local_app(selected, hash_password('local-test-password'))
            assert len(observed_clients) == 1
            shared, settings = observed_clients[0]
            assert settings['trust_env'] is False
            assert settings['follow_redirects'] is False
            assert all(provider.client is shared for provider in providers.values())
            app_client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
            await app_client.start_server()
            result = await providers['InteractionOrganizer'].organize('这是合成材料原文。', time.time(), {})
            assert result['summary'] == '合成材料已整理'
            assert len(requests) == 1
            assert shared.is_closed is False
            # The app preference did not remove or rewrite this process's proxy.
            assert os.environ['HTTP_PROXY'] == proxy_url
            await app_client.close()
            app_client = None
            assert shared.is_closed is True
        finally:
            if app_client is not None:
                await app_client.close()
            await server.close()

    with socket.socket() as unavailable_proxy:
        unavailable_proxy.bind(('127.0.0.1', 0))
        proxy_url = f'http://127.0.0.1:{unavailable_proxy.getsockname()[1]}'
        for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
            monkeypatch.setenv(name, proxy_url)
        for name in ('NO_PROXY', 'no_proxy'):
            monkeypatch.setenv(name, '')
        asyncio.run(exercise(proxy_url))


def test_shared_model_client_closes_when_app_construction_fails(tmp_path, monkeypatch):
    real_client = httpx.AsyncClient
    clients = []
    def capture_client(*args, **kwargs):
        client = real_client(*args, **kwargs)
        clients.append(client)
        return client
    monkeypatch.setattr(local_runtime.httpx, 'AsyncClient', capture_client)
    def fail_app(*args, **kwargs):
        raise ValueError('synthetic construction failure')
    monkeypatch.setattr(local_runtime, 'create_app', fail_app)

    async def exercise():
        with pytest.raises(ValueError, match='synthetic construction failure'):
            await build_local_app(config(tmp_path), hash_password('local-test-password'))
        assert len(clients) == 1 and clients[0].is_closed
        # SQLite handles must also have been released on the same failure path.
        config(tmp_path)['db_path'].unlink()
    asyncio.run(exercise())
