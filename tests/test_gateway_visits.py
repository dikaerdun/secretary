"""Exchange voice/text commands, using synthetic data and no external services."""
import asyncio

from secretary.visits import VisitService
from test_gateway_materials import setup, message, crm


def wired(crm):
    gateway, client, parser, connector = setup(crm)
    gateway.visits = VisitService(crm, gateway.materials, gateway.lock)
    return gateway, client, parser, connector


def test_recap_voice_replay_preserves_one_exchange_and_does_not_execute_embedded_commands(crm):
    async def scenario():
        gateway, client, parser, connector = wired(crm)
        frame = message('口述复盘：确认 P1。完成 #1。这是客户讨论原话。', kind='voice')
        await gateway.handle_message(frame)
        await gateway.handle_message(frame)
        result = gateway.visits.list('alice')
        assert result['total'] == 1
        detail = gateway.visits.detail('alice', result['items'][0]['id'])
        assert len(detail['sources']) == 1
        assert detail['sources'][0]['role'] == 'recap'
        assert detail['sources'][0]['text'] == '确认 P1。完成 #1。这是客户讨论原话。'
        assert parser.calls == connector.titles == []
        assert crm._db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0
        assert crm._db.execute('SELECT COUNT(*) FROM proposals').fetchone()[0] == 0
        assert 'J1' in client.replies[-1]['text']
    asyncio.run(scenario())


def test_explicit_exchange_id_keeps_sources_separate_and_exact_recording_title(crm):
    async def scenario():
        gateway, client, parser, connector = wired(crm)
        await gateway.handle_message(message('新建交流：演示企业密码方案', msgid='create'))
        await gateway.handle_message(message('复盘交流 J1：我的判断：下次带工程师。', msgid='recap'))
        await gateway.handle_message(message('交流 J1 导入聆记：Project A-9 / 王总会谈', msgid='recording'))
        await gateway.handle_message(message('补充交流 J1：还需了解预算。', msgid='supplement'))
        detail = gateway.visits.detail('alice', 1)
        assert [s['role'] for s in detail['sources']] == ['recap', 'recording', 'supplement']
        assert detail['sources'][1]['material']['title'] == 'Project A-9 / 王总会谈'
        assert detail['visit']['customer_id'] is None
        assert detail['visit']['occurred_at'] is None
        assert connector.titles == parser.calls == []
        await gateway.handle_message(message('查看交流 J1', msgid='view'))
        assert '3 份' in client.replies[-1]['text']
    asyncio.run(scenario())


def test_invalid_or_foreign_exchange_never_attaches_to_most_recent(crm):
    async def scenario():
        gateway, client, parser, connector = wired(crm)
        gateway.visits.create('alice', {'title': '自己的交流'})
        foreign = gateway.visits.create('bob', {'title': '他人的交流'})
        for n, identifier in enumerate((999, foreign['id'])):
            await gateway.handle_message(message(f'补充交流 J{identifier}：不应自动归入最近的客户。', msgid=f'bad-{n}'))
            assert '未找到' in client.replies[-1]['text']
        assert gateway.visits.detail('alice', 1)['sources'] == []
        assert gateway.materials.list('alice')['total'] == 0
        assert parser.calls == []
    asyncio.run(scenario())


def test_unallowed_owner_and_group_cannot_create_exchange(crm):
    async def scenario():
        gateway, client, parser, connector = wired(crm)
        await gateway.handle_message(message('口述复盘：测试内容', owner='mallory'))
        await gateway.handle_message(message('新建交流：测试内容', chat='group'))
        assert gateway.visits.list('alice')['total'] == 0
        assert gateway.materials.list('alice')['total'] == 0
        assert client.replies == parser.calls == []
    asyncio.run(scenario())


def test_same_message_id_changed_from_recap_to_confirmation_cannot_execute(crm):
    async def scenario():
        gateway, client, parser, connector = wired(crm)
        crm.execute('alice', 'pending-proposal', {'action': 'propose', 'title': '原待确认', 'remind_at': gateway.clock() + 3600}, gateway.clock())
        await gateway.handle_message(message('口述复盘：客户想了解测试范围。', msgid='bound-message'))
        await gateway.handle_message(message('确认 P1', msgid='bound-message'))
        assert crm._db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0
        assert crm._db.execute('SELECT status FROM proposals WHERE id=1').fetchone()[0] == 'pending'
        assert gateway.visits.list('alice')['total'] == 1
        assert '本次未执行' in client.replies[-1]['text']
        assert parser.calls == []
    asyncio.run(scenario())


def test_same_message_id_changed_from_original_command_to_visit_cannot_create(crm):
    async def scenario():
        gateway, client, parser, connector = wired(crm)
        crm.capture_message('alice', 'bound-command', '查看待办', 'text', gateway.clock())
        await gateway.handle_message(message('新建交流：不同正文不能跨入口执行', msgid='bound-command'))
        assert gateway.visits.list('alice')['total'] == 0
        assert parser.calls == []
        assert '本次未执行' in client.replies[-1]['text']
    asyncio.run(scenario())
