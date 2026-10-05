"""Verify disclosed offline exercises, durable state and database isolation."""
import asyncio
from datetime import datetime, timedelta
from html import unescape
import json
from pathlib import Path
import re
import sqlite3

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer
import httpx
import pytest

from deploy.training import (DB_NAME, MARKER_NAME, NOTICE, OWNER, PASSWORD, OfflineCustomerParser, OfflineOrganizer,
    TrainingError, TrainingLock, build_training_app, training_config)
from secretary.sales_workspace import SalesWorkspace
from secretary.store import SHANGHAI


NOW = datetime(2026, 10, 3, 12, 0, tzinfo=SHANGHAI).timestamp()


def selected(tmp_path):
    return training_config(root=tmp_path)


def crm_from(app):
    return next(route.handler.__self__.crm for route in app.router.routes()
                if getattr(route.handler, '__name__', '') == 'dashboard')


async def client_for(tmp_path, now=NOW):
    app = await build_training_app(selected(tmp_path), clock=lambda: now)
    client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
    await client.start_server()
    login = await client.post('/api/login', json={'password': PASSWORD})
    assert login.status == 200
    return app, client, {'X-CSRF-Token': (await login.json())['csrf']}


@pytest.mark.parametrize('kwargs', [{'host': '0.0.0.0'}, {'host': 'localhost'}, {'port': 80},
    {'db_path': 'data/local-secretary.sqlite3'}, {'db_path': 'data/secretary.sqlite3'}, {'db_path': ':memory:'}])
def test_training_only_accepts_fixed_loopback_database(tmp_path, kwargs):
    with pytest.raises(TrainingError):
        training_config(root=tmp_path, **kwargs)
    assert not (tmp_path / 'data').exists()


def test_unknown_existing_database_is_not_opened_or_overwritten(tmp_path, monkeypatch):
    path = tmp_path / DB_NAME
    path.parent.mkdir()
    path.write_bytes(b'unknown-database-must-not-be-opened')
    original = sqlite3.connect
    def forbidden(*args, **kwargs):
        raise AssertionError('unknown database was opened')
    monkeypatch.setattr(sqlite3, 'connect', forbidden)
    with pytest.raises(TrainingError, match='未标记'):
        asyncio.run(build_training_app(selected(tmp_path), clock=lambda: NOW))
    assert path.read_bytes() == b'unknown-database-must-not-be-opened'
    monkeypatch.setattr(sqlite3, 'connect', original)


def test_offline_seed_never_reads_credentials_or_creates_provider_clients(tmp_path, monkeypatch):
    (tmp_path / '.env').write_text('sentinel-do-not-read', encoding='utf-8')
    (tmp_path / '.env.local').write_text('sentinel-do-not-read', encoding='utf-8')
    private_db = tmp_path / 'data' / 'local-secretary.sqlite3'
    private_db.parent.mkdir()
    private_db.write_bytes(b'formal-data-sentinel')
    read = Path.read_text
    def guarded(path, *args, **kwargs):
        assert path.name not in ('.env', '.env.local', 'local-access.local.json')
        return read(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', guarded)
    def forbidden_client(*args, **kwargs):
        raise AssertionError('provider client was constructed')
    monkeypatch.setattr(httpx.AsyncClient, '__init__', forbidden_client)
    async def exercise():
        app, client, headers = await client_for(tmp_path)
        try:
            session = await (await client.get('/api/session')).json()
            assert session['demo'] is True
            runtime = session['runtime']
            assert runtime['mode'] == 'local' and runtime['training'] is True
            assert runtime['database'] == 'training' and runtime['wecom_connected'] is False
            assert runtime['model_configured'] is False and '非真实' in runtime['model_status']
            assert len(runtime['training_scenarios']) == 6
            audio = await (await client.get('/api/audio/capabilities')).json()
            assert audio['configured'] is False and audio['can_transcribe'] is False
            material = await (await client.get('/api/materials/capabilities')).json()
            assert material['configured'] is False
            manifest = await (await client.get('/api/training')).json()
            assert manifest['base_date'] == '2026-10-03'
            crm = crm_from(app)
            assert crm.list_customers(OWNER)['total'] == 3
            assert crm.list_customers('local-user')['total'] == 0
            assert crm._db.execute("SELECT count(*) FROM notifications WHERE status='sent'").fetchone()[0] == 0
        finally:
            await client.close()
    asyncio.run(exercise())
    assert private_db.read_bytes() == b'formal-data-sentinel'


def test_actual_scenario_states_use_real_store_and_services(tmp_path):
    async def exercise():
        app, client, headers = await client_for(tmp_path)
        try:
            crm = crm_from(app)
            manifest = await (await client.get('/api/training')).json()
            scenes = {s['key']: s for s in manifest['scenarios']}
            lead = scenes['lead']
            todo = crm.get_record(OWNER, lead['todo_id'])
            assert todo['kind'] == 'action' and todo['title'] == '发送数据库加密产品资料'
            assert todo['task_id'] is None and todo['proposal_id'] is None and todo['remind_at'] is None
            profile = crm.profile(OWNER, lead['customer_id'])
            facts = {f['key']: f for f in profile['fields']}
            assert facts['crypto_needs']['basis'] == 'reported'
            assert facts['blockers']['basis'] == 'observation'
            assert all(f['evidence'] in crm.get_record(OWNER, f['source_record_id'])['original_content'] for f in facts.values())
            visit = await (await client.get('/api/visits/' + str(scenes['meeting']['visit_id']))).json()
            assert {s['role'] for s in visit['sources']} == {'recording', 'recap'}
            assert len(visit['actions']) == 3
            actions = {a['title']: a for a in visit['actions']}
            assert actions['提供接口清单']['executor_kind'] == 'customer'
            assert actions['提供接口清单']['deadline_date'] == '2026-10-06'
            assert actions['提供接口清单']['execution_at'] is None
            assert actions['演示脱敏网关']['executor_kind'] == 'self'
            assert actions['演示脱敏网关']['duration_minutes'] == 45
            assert datetime.fromtimestamp(actions['演示脱敏网关']['execution_at'], SHANGHAI).isoformat().startswith('2026-10-05T15:00')
            assert actions['完成兼容验证']['executor_kind'] == 'team'
            assert actions['完成兼容验证']['deadline_date'] == '2026-10-08'
            assert actions['完成兼容验证']['check_date'] == '2026-10-07'
            assert all(a.get('adopted_record_id') is None for a in actions.values())
            sales = SalesWorkspace(crm, clock=lambda: NOW)
            ops = sales.opportunities(OWNER, scenes['projects']['customer_id'])['items']
            assert {(o['amount_cents'], o['amount_type'], o['approval']) for o in ops} == {
                (18000000, 'estimate', 'unconfirmed'), (46000000, 'budget', 'approved')}
            assert crm.get_customer(OWNER, scenes['projects']['customer_id'])['amount_cents'] is None
            overdue = crm.get_record(OWNER, scenes['waiting']['record_ids'][0])
            waiting = crm.get_record(OWNER, scenes['waiting']['record_ids'][1])
            assert overdue['task_status'] == 'pending' and overdue['remind_at'] < NOW
            assert waiting['action_terms']['executor_kind'] == 'customer' and waiting['task_id'] is None
            assert waiting['action_terms']['check_date'] == '2026-10-06'
            changes = scenes['changes']
            for task_id, proposal_id in zip(changes['task_ids'], changes['proposal_ids']):
                task, proposal = crm.get_task(OWNER, task_id), crm.get_proposal(OWNER, proposal_id)
                assert task['status'] == 'pending' and task['revision'] == 1
                assert proposal['status'] == 'pending' and proposal['target_task_id'] == task_id
            assert crm.get_proposal(OWNER, changes['proposal_ids'][1])['change_kind'] == 'cancel'
            result = scenes['outcome']
            assert crm.get_record(OWNER, result['record_id'])['status'] == 'done'
            followup = crm.get_record(OWNER, result['next_record_id'])
            assert followup['parent_record_id'] == result['record_id']
            assert followup['title'] == '确认试点采购负责人'
            assert followup['task_id'] is None and followup['proposal_id'] is None
            link = crm._db.execute('SELECT opportunity_id FROM crm_opportunity_links WHERE owner=? AND entity_type=? AND entity_id=?',
                                   (OWNER, 'record', followup['id'])).fetchone()
            assert link['opportunity_id'] == result['opportunity_ids'][0]
        finally:
            await client.close()
    asyncio.run(exercise())


def test_restart_is_idempotent_and_preserves_practice_and_first_day(tmp_path):
    async def exercise():
        app, client, headers = await client_for(tmp_path)
        crm = crm_from(app)
        before = await (await client.get('/api/training')).json()
        practiced = await client.post('/api/customer-command', json={'text': '任意演练原话：请记下需要核对的事项。'}, headers=headers)
        assert practiced.status == 200
        result = await practiced.json()
        assert '演练' in result['message'] and '只理解预设' in result['message']
        stored_id = result['record_id']
        tables = ('crm_customers','crm_customer_facts','crm_records','tasks','proposals','crm_materials','crm_visits','crm_opportunities','crm_action_outcomes')
        counts = {t: crm._db.execute('SELECT count(*) FROM ' + t).fetchone()[0] for t in tables}
        await client.close()
        app2, client2, headers2 = await client_for(tmp_path, NOW + 2 * 86400)
        try:
            crm2 = crm_from(app2)
            after = await (await client2.get('/api/training')).json()
            assert after == before
            assert crm2.get_record(OWNER, stored_id)['original_content'] == '任意演练原话：请记下需要核对的事项。'
            assert {t: crm2._db.execute('SELECT count(*) FROM ' + t).fetchone()[0] for t in tables} == counts
            assert json.loads((tmp_path / MARKER_NAME).read_text())['status'] == 'ready'
        finally:
            await client2.close()
    asyncio.run(exercise())


def test_selected_original_task_changes_only_after_confirmation(tmp_path):
    async def exercise():
        app, client, headers = await client_for(tmp_path)
        try:
            crm = crm_from(app)
            manifest = await (await client.get('/api/training')).json()
            scene = next(s for s in manifest['scenarios'] if s['key'] == 'changes')
            for i, proposal_id in enumerate(scene['proposal_ids']):
                task_id = scene['task_ids'][i]
                assert crm.get_task(OWNER, task_id)['status'] == 'pending'
                proposal = crm.get_proposal(OWNER, proposal_id)
                response = await client.post(f'/api/proposals/{proposal_id}/confirm', json={'updated_at': proposal['updated_at']}, headers=headers)
                assert response.status == 200, await response.text()
                task = crm.get_task(OWNER, task_id)
                assert task['status'] == ('pending' if i == 0 else 'cancelled')
                if i == 0:
                    assert datetime.fromtimestamp(task['remind_at'], SHANGHAI).strftime('%Y-%m-%d %H:%M') == '2026-10-07 15:00'
                    assert task['duration_minutes'] == 45
            assert crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 3
        finally:
            await client.close()
    asyncio.run(exercise())


def test_database_instance_lock_prevents_second_training_process(tmp_path):
    path = tmp_path / 'training.lock'
    with TrainingLock(path):
        with pytest.raises(TrainingError):
            with TrainingLock(path):
                pass
    with TrainingLock(path):
        pass


def test_guide_exposes_only_fixed_document_with_exact_inline_policy(tmp_path, monkeypatch):
    import deploy.training as training
    guide_root = tmp_path / 'guide-root'
    (guide_root / 'deploy').mkdir(parents=True)
    guide = guide_root / 'deploy' / '使用指南与案例.html'
    guide.write_text('<!doctype html><style>body{color:#123}</style><script>window.trainingGuide=true;</script>演练指南', encoding='utf-8')
    monkeypatch.setattr(training, 'APP_ROOT', guide_root)
    async def exercise():
        app, client, headers = await client_for(tmp_path)
        try:
            page = await client.get('/guide')
            assert page.status == 200 and '演练指南' in await page.text()
            policy = page.headers['Content-Security-Policy']
            assert policy.count('sha256-') == 2 and 'unsafe-inline' not in policy
            assert (await client.get('/deploy/.env')).status == 404
            assert (await client.get('/guide/local-access.local.json')).status == 404
            main = await client.get('/')
            assert 'sha256-' not in main.headers['Content-Security-Policy']
        finally:
            await client.close()
    asyncio.run(exercise())


def test_free_text_never_inherits_preset_actions():
    result = asyncio.run(OfflineOrganizer().organize('今天讨论其他供应商，没有给出承诺。', NOW, {}))
    assert result['actions'] == []
    assert NOTICE in result['summary'] and '只理解预设' in result['summary']


@pytest.mark.parametrize('text', [
    '我没有答应发送数据库加密产品资料。',
    '我方团队赵工负责发送数据库加密产品资料。',
    '我观察客户可能需要发送数据库加密产品资料。',
    '我观察团队负责发送数据库加密产品资料。',
    '我答应发送数据库加密产品资料，没有约定执行时间？',
    '客户说我答应发送数据库加密产品资料，没有约定执行时间。',
    '我答应演示脱敏网关，执行时间2026-10-05 15:00，预计45分钟，但尚未确认。',
    '我方团队赵工负责完成兼容验证，截止2026-02-30，检查2026-02-29。',
])
def test_unsupported_negation_subject_and_observation_do_not_create_commitments(text):
    async def exercise():
        organized = await OfflineOrganizer().organize(text, NOW, {})
        assert organized['actions'] == []
        assert NOTICE in organized['summary'] and '只理解预设' in organized['summary']
        parsed = await OfflineCustomerParser().parse(text, NOW, {'customer': {'name': '澄川精密制造集团'}})
        assert parsed['intent'] == 'clarify' and '只理解预设' in parsed['question']
    asyncio.run(exercise())


def test_unsupported_inputs_are_saved_without_tasks_or_proposals(tmp_path):
    async def exercise():
        app, client, headers = await client_for(tmp_path)
        try:
            crm = crm_from(app)
            before = {table: crm._db.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                      for table in ('tasks', 'proposals', 'crm_analysis_actions')}
            for text in ('我没有答应发送数据库加密产品资料。',
                         '我方团队赵工负责发送数据库加密产品资料。',
                         '我观察客户可能需要发送数据库加密产品资料。'):
                response = await client.post('/api/customer-command', json={'text': text, 'customer_id': 1}, headers=headers)
                assert response.status == 200
                result = await response.json()
                assert '只理解预设' in result['message'] and NOTICE in result['message']
                assert crm.get_record(OWNER, result['record_id'])['original_content'] == text
                assert crm.get_analysis(OWNER, result['record_id']) is None
            assert {table: crm._db.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                    for table in before} == before
        finally:
            await client.close()
    asyncio.run(exercise())


def test_nine_public_guide_quotes_keep_their_supported_exercise_behavior():
    from secretary.spoken_changes import parse_change
    guide = Path(__file__).resolve().parents[1] / 'deploy' / '使用指南与案例.html'
    source = guide.read_text(encoding='utf-8-sig')
    quotes = {key: unescape(text) for key, text in re.findall(r'<pre id="(quote-[^"]+)"[^>]*>(.*?)</pre>', source, re.S)}
    assert len(quotes) == 9
    current = datetime.fromtimestamp(NOW, SHANGHAI).date()
    quotes = {key: re.sub(r'\{\{date:(\d+)\}\}', lambda match: (current + timedelta(days=int(match[1]))).isoformat(), text)
              for key, text in quotes.items()}
    async def exercise():
        organizer, parser = OfflineOrganizer(), OfflineCustomerParser()
        assert (await parser.parse(quotes['quote-lead'], NOW))['intent'] == 'create'
        assert (await parser.parse(quotes['quote-new-customer'], NOW))['intent'] == 'create'
        assert len((await organizer.organize(quotes['quote-lead'], NOW, {}))['actions']) == 1
        meeting = (await organizer.organize(quotes['quote-meeting'], NOW, {}))['actions']
        assert [(action['title'], action['executor_kind']) for action in meeting] == [
            ('提供接口清单', 'customer'), ('演示脱敏网关', 'self'), ('完成兼容验证', 'team')]
        waiting = (await organizer.organize(quotes['quote-waiting'], NOW, {}))['actions']
        assert len(waiting) == 1 and waiting[0]['executor_kind'] == 'customer' and waiting[0]['remind_at'] is None
        for key in ('quote-recap', 'quote-projects', 'quote-overdue', 'quote-new-customer'):
            assert (await organizer.organize(quotes[key], NOW, {}))['actions'] == []
    asyncio.run(exercise())
    task = {'id': 1, 'status': 'pending', 'title': '原定演示', 'duration_minutes': 45}
    changed = parse_change(quotes['quote-change'], NOW, task)
    assert changed['action'] == 'propose_change' and changed['remind_at'] == NOW + 4 * 86400 + 3 * 3600
    assert parse_change(quotes['quote-cancel'], NOW, task)['action'] == 'propose_cancel'


def test_exact_practice_input_creates_disclosed_draft_not_a_customer(tmp_path):
    async def exercise():
        app, client, headers = await client_for(tmp_path)
        try:
            manifest = await (await client.get('/api/training')).json()
            created = await client.post('/api/customer-command', json={'text': manifest['practice_customer_input']}, headers=headers)
            assert created.status == 200
            result = await created.json()
            assert NOTICE in result['message']
            assert result['draft']['customer_name'] == '霁星数据研究院' and result['draft']['status'] == 'pending'
            assert crm_from(app).find_customers_exact(OWNER, '霁星数据研究院') == []
            confirmed = await client.post('/api/customer-drafts/' + str(result['draft']['id']) + '/confirm', json={}, headers=headers)
            assert confirmed.status == 200
            assert crm_from(app).find_customers_exact(OWNER, '霁星数据研究院')
        finally:
            await client.close()
    asyncio.run(exercise())
