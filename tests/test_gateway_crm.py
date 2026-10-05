import asyncio
import tempfile
import unittest
from pathlib import Path

from secretary.crm import CRMStore
from secretary.gateway import BotGateway
from secretary.store import Store


class Client:
    is_connected = True
    def on(self, *args):
        pass
    async def reply_stream(self, frame, stream, text, finish):
        return {'errcode': 0}


class Parser:
    def __init__(self, command=None, fail=False):
        self.command, self.fail = command, fail
    async def parse(self, text, now):
        if self.fail:
            raise RuntimeError('provider-error-private')
        return self.command


def frame(source='voice-note', text='客户需要详细的实施计划', kind='voice', owner='owner'):
    return {'headers': {'req_id': source}, 'body': {'msgid': source, 'from': {'userid': owner},
            'chattype': 'single', 'msgtype': kind, kind: {'content': text}}}


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        path = Path(self.directory.name) / 'test.sqlite3'
        self.store, self.crm = Store(path), CRMStore(path)

    async def asyncTearDown(self):
        self.crm.close()
        self.store.close()
        self.directory.cleanup()

    async def test_failed_ai_keeps_original_without_enabling_reminder(self):
        gateway = BotGateway(Client(), self.store, Parser(fail=True), ['owner'], crm=self.crm)
        await gateway.handle_message(frame())
        await gateway.handle_message(frame())
        notes = self.crm.list_records('owner')
        self.assertEqual(notes['total'], 1)
        self.assertEqual(notes['items'][0]['original_content'], '客户需要详细的实施计划')
        self.assertEqual(self.store._db.execute('select count(*) from tasks').fetchone()[0], 0)

    async def test_voice_link_and_commands_are_not_duplicate_notes(self):
        gateway = BotGateway(Client(), self.store, Parser({'action': 'propose', 'title': '准备实施计划', 'remind_at': None}), ['owner'], crm=self.crm)
        await gateway.handle_message(frame())
        await gateway.handle_message(frame())
        record = self.crm.list_records('owner')['items'][0]
        self.assertEqual(self.crm.record_detail('owner', record['id'])['proposal']['title'], '准备实施计划')
        gateway.parser = Parser({'action': 'list'})
        await gateway.handle_message(frame('list', '待办', 'text'))
        self.assertEqual(self.crm.list_records('owner')['total'], 1)
        await gateway.handle_message(frame('outsider', owner='other'))
        self.assertEqual(self.crm.list_records('other')['total'], 0)

    async def test_cached_reply_recovers_after_classification_failure(self):
        gateway = BotGateway(Client(), self.store, Parser({'action': 'propose', 'title': '准备实施计划', 'remind_at': None}), ['owner'], crm=self.crm)
        original = self.crm.apply_command
        def fail_once(*args):
            raise RuntimeError('simulated interruption after task-store commit')
        self.crm.apply_command = fail_once
        await gateway.handle_message(frame())
        self.crm.apply_command = original
        self.crm.import_legacy('owner', gateway.clock())
        gateway.parser = Parser(fail=True)
        await gateway.handle_message(frame())
        records = self.crm.list_records('owner')
        self.assertEqual(records['total'], 1)
        self.assertEqual(records['items'][0]['original_content'], '客户需要详细的实施计划')
        self.assertIsNotNone(records['items'][0]['proposal_id'])
