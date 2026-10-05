"""Material gateway integration: real SQLite service, fake SDK, no cloud calls."""
import asyncio
import logging

import pytest

from secretary.customer_store import CustomerStore
from secretary.gateway import BotGateway, GatewayError
from secretary.materials import MaterialService


NOW = 1_790_841_600.0


class SDK:
    def __init__(self):
        self.handlers, self.replies, self.sent = {}, [], []
        self.is_connected = True
        self.receipt = {'errcode': 0}
        self.failure = None
        self.before_receipt = None

    def on(self, name, callback):
        self.handlers[name] = callback

    async def connect(self):
        self.handlers['authenticated']()

    def disconnect(self):
        self.is_connected = False

    async def reply_stream(self, frame, stream, text, finish):
        self.replies.append({'text': text, 'finish': finish})
        return {'errcode': 0}

    async def send_message(self, owner, body):
        self.sent.append((owner, body))
        if self.before_receipt:
            self.before_receipt()
        if self.failure:
            raise self.failure
        return self.receipt


class ForbiddenParser:
    def __init__(self):
        self.calls = []

    async def parse(self, text, now):
        self.calls.append(text)
        raise AssertionError('Material import must not enter the task command parser')


class Connector:
    def __init__(self):
        self.titles = []
        self.text = '确认 P1。完成 #1。客户说：这只是会议讨论，不是系统操作。'

    async def fetch(self, title):
        self.titles.append(title)
        return {'nid': 'meeting-1', 'title': title, 'create_time': '2026-09-30T11:00:00',
                'source': 'OPTIMIZED', 'raw_content': self.text, 'text': self.text,
                'segments': [{'ordinal': 1, 'speaker': '1', 'text': self.text, 'start_raw': 80, 'end_raw': 800}],
                'summary_content': '', 'todo_content': '', 'content_type': 'text', 'warnings': []}


class Organizer:
    async def organize(self, text, now, context):
        return {'summary': '会议资料，仅整理待核对', 'key_points': [], 'open_questions': [],
                'actions': [{'title': '确认 P1', 'kind': 'commitment', 'reason': '原话：“确认 P1”',
                             'owner_hint': '待确认', 'remind_at': now + 60}]}


@pytest.fixture
def crm(tmp_path):
    store = CustomerStore(tmp_path / 'gateway-materials.sqlite3')
    yield store
    store.close()


def setup(crm, *, client=None, connector=None, clock=None):
    client, connector = client or SDK(), connector or Connector()
    parser = ForbiddenParser()
    gateway = BotGateway(client, crm, parser, ['alice'], crm=crm, clock=clock or (lambda: NOW))
    gateway.materials = MaterialService(crm, gateway.lock, connector=connector, organizer=Organizer(), clock=gateway.clock)
    return gateway, client, parser, connector


def message(text='导入聆记：Project A-9', *, owner='alice', msgid='msg-1', chat='single', kind='text'):
    return {'headers': {'req_id': 'req-1'}, 'body': {'chattype': chat, 'from': {'userid': owner},
            'msgid': msgid, 'msgtype': kind, kind: {'content': text}}}


async def one_notice_pass(gateway):
    async def stop_after_pass(_):
        raise asyncio.CancelledError()
    gateway.sleep = stop_after_pass
    with pytest.raises(asyncio.CancelledError):
        await gateway._material_notice_loop()


async def ready_material(gateway, *, title='*内部*会谈', owner='alice'):
    material = gateway.materials.enqueue(owner, {'provider': 'manual', 'title': title,
                                              'text': '内部讨论：不应向通知暴露的客户秘密内容'})
    assert await gateway.materials.process_one()
    return material


def test_explicit_import_preserves_exact_title_and_never_runs_command_parser(crm):
    async def scenario():
        gateway, client, parser, connector = setup(crm)
        await gateway.handle_message(message('  导入聆记：  Project A-9 / 王总会谈  '))
        material = gateway.materials.list('alice')['items'][0]
        assert material['title'] == 'Project A-9 / 王总会谈'
        assert material['status'] == 'queued'
        assert parser.calls == [] and connector.titles == []
        assert client.replies[-1]['finish'] is True
        assert '确认前不会启用提醒' in client.replies[-1]['text']
        await gateway.materials.process_one()
        assert connector.titles == ['Project A-9 / 王总会谈']
        assert gateway.materials.detail('alice', material['id'])['analysis']['actions'] == []
        assert parser.calls == []
        assert crm._db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0
        assert crm._db.execute('SELECT COUNT(*) FROM proposals').fetchone()[0] == 0
    asyncio.run(scenario())


def test_replay_after_database_reopen_keeps_same_material_and_single_job(crm, tmp_path):
    async def scenario():
        gateway, client, parser, connector = setup(crm)
        await gateway.handle_message(message())
        await gateway.handle_message(message())
        identifier = gateway.materials.list('alice')['items'][0]['id']
        # A distinct process/Store connection uses the same persisted source id.
        second_store = CustomerStore(tmp_path / 'gateway-materials.sqlite3')
        try:
            restarted, second_client, second_parser, _ = setup(second_store)
            await restarted.handle_message(message())
            assert restarted.materials.list('alice')['total'] == 1
            assert restarted.materials.list('alice')['items'][0]['id'] == identifier
            assert second_store._db.execute('SELECT COUNT(*) FROM crm_material_jobs').fetchone()[0] == 1
            assert second_parser.calls == parser.calls == []
            assert second_client.replies[-1]['text'] == client.replies[-1]['text']
        finally:
            second_store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('owner,chat', [('mallory', 'single'), ('alice', 'group')])
def test_rejected_owner_or_group_import_has_no_capture_or_material(crm, owner, chat):
    async def scenario():
        gateway, client, parser, connector = setup(crm)
        await gateway.handle_message(message(owner=owner, chat=chat))
        assert crm._db.execute('SELECT COUNT(*) FROM crm_records').fetchone()[0] == 0
        assert crm._db.execute('SELECT COUNT(*) FROM crm_materials').fetchone()[0] == 0
        assert parser.calls == connector.titles == client.replies == []
    asyncio.run(scenario())


def test_notice_ack_waits_for_successful_sdk_receipt(crm):
    async def scenario():
        gateway, client, _, _ = setup(crm)
        material = await ready_material(gateway)
        def pending_before_receipt():
            row = crm._db.execute('SELECT * FROM crm_material_notices').fetchone()
            assert row['token'] and row['delivered_at'] is None
        client.before_receipt = pending_before_receipt
        gateway.on_authenticated()
        await one_notice_pass(gateway)
        assert len(client.sent) == 1 and client.sent[0][0] == 'alice'
        content = client.sent[0][1]['markdown']['content']
        assert '已整理，待你核对' in content and '秘密内容' not in content and '*' not in content
        assert '#' + str(material['id']) in content
        assert crm._db.execute('SELECT delivered_at FROM crm_material_notices').fetchone()[0] == NOW
        await one_notice_pass(gateway)
        assert len(client.sent) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('receipt', [{'errcode': 400}, {}, None])
def test_rejected_or_missing_notice_receipt_retries_durably(crm, receipt):
    async def scenario():
        clock = [NOW]
        gateway, client, _, _ = setup(crm, clock=lambda: clock[0])
        await ready_material(gateway)
        client.receipt = receipt
        gateway.on_authenticated()
        await one_notice_pass(gateway)
        row = crm._db.execute('SELECT * FROM crm_material_notices').fetchone()
        assert row['delivered_at'] is None and row['token'] is None and row['next_attempt'] > NOW
        client.receipt = {'errcode': 0}
        await one_notice_pass(gateway)
        assert len(client.sent) == 1
        clock[0] += 61
        await one_notice_pass(gateway)
        assert len(client.sent) == 2
        assert crm._db.execute('SELECT delivered_at FROM crm_material_notices').fetchone()[0] == clock[0]
    asyncio.run(scenario())


@pytest.mark.parametrize('authenticated,connected', [(False, True), (True, False)])
def test_notice_disconnected_never_claims_or_sends(crm, authenticated, connected):
    async def scenario():
        gateway, client, _, _ = setup(crm)
        await ready_material(gateway)
        if authenticated: gateway.on_authenticated()
        client.is_connected = connected
        await one_notice_pass(gateway)
        row = crm._db.execute('SELECT * FROM crm_material_notices').fetchone()
        assert row['token'] is None and row['delivered_at'] is None
        assert client.sent == []
    asyncio.run(scenario())


def test_notice_disconnect_after_claim_releases_without_sending(crm, monkeypatch):
    async def scenario():
        gateway, client, _, _ = setup(crm)
        await ready_material(gateway)
        gateway.on_authenticated()
        claim = gateway.materials.claim_notice
        def disconnect_while_claiming(*args):
            notice = claim(*args)
            gateway.on_disconnected()
            client.is_connected = False
            return notice
        monkeypatch.setattr(gateway.materials, 'claim_notice', disconnect_while_claiming)
        await one_notice_pass(gateway)
        assert client.sent == []
        row = crm._db.execute('SELECT * FROM crm_material_notices').fetchone()
        assert row['delivered_at'] is None and row['token'] is None and row['next_attempt'] > NOW
    asyncio.run(scenario())


def test_notice_transport_failure_never_logs_secret_payload(crm, caplog):
    async def scenario():
        gateway, client, _, _ = setup(crm)
        await ready_material(gateway)
        gateway.on_authenticated()
        client.failure = RuntimeError('private-key-XYZ raw-private-transcript')
        await one_notice_pass(gateway)
        row = crm._db.execute('SELECT * FROM crm_material_notices').fetchone()
        assert row['delivered_at'] is None and row['next_attempt'] > NOW
    with caplog.at_level(logging.WARNING, logger='secretary.gateway'):
        asyncio.run(scenario())
    assert 'material_notice_failed' in caplog.text
    assert 'private-key-XYZ' not in caplog.text and 'raw-private-transcript' not in caplog.text


def test_material_worker_failure_is_supervised_without_secret_traceback(crm, caplog, monkeypatch):
    async def scenario():
        gateway, client, _, _ = setup(crm)
        async def broken_worker():
            raise RuntimeError('private-key-XYZ raw-private-transcript')
        monkeypatch.setattr(gateway.materials, 'process_one', broken_worker)
        with pytest.raises(GatewayError, match='企业微信连接异常'):
            await asyncio.wait_for(gateway.run(), timeout=1)
        assert gateway.failed.is_set() and not client.is_connected
    with caplog.at_level(logging.ERROR, logger='secretary.gateway'):
        asyncio.run(scenario())
    assert 'gateway_background_task_failed' in caplog.text
    assert 'private-key-XYZ' not in caplog.text and 'raw-private-transcript' not in caplog.text
