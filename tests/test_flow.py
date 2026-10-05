import asyncio
import json
from datetime import datetime

import httpx

from secretary.gateway import BotGateway
from secretary.parser import DeepSeekParser
from secretary.store import Store


def test_voice_proposal_confirmation_agenda_and_real_database(tmp_path):
    async def scenario():
        now = [datetime.fromisoformat('2026-09-30T12:00:00+08:00').timestamp()]

        class FakeClient:
            is_connected = True
            replies = []
            sent = []

            def on(self, *_):
                pass

            async def reply_stream(self, frame, stream, content, finish):
                if finish:
                    self.replies.append(content)
                return {'errcode': 0}

            async def send_message(self, owner, body):
                self.sent.append((owner, body))
                return {'errcode': 0}

        calls = []

        def model(request):
            text = json.loads(request.content)['messages'][1]['content']
            calls.append(text)
            if text == '研究一下新供应商':
                output = {'action': 'propose', 'title': '研究新供应商', 'remind_at': None}
            else:
                output = {'action': 'reschedule_proposal', 'proposal_id': 1,
                          'remind_at': '2026-09-30T15:00:00+08:00', 'time_evidence': '今天下午三点'}
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
                'content': json.dumps(output, ensure_ascii=False)}}]})

        def frame(identifier, text, kind='text'):
            return {'headers': {'req_id': identifier}, 'body': {'msgid': identifier,
                    'chattype': 'single', 'from': {'userid': 'me'}, 'msgtype': kind, kind: {'content': text}}}

        path = tmp_path / 'flow.sqlite3'
        store = Store(path)
        client = FakeClient()
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as http:
                parser = DeepSeekParser('test-key', client=http)
                gateway = BotGateway(client, store, parser, ['me'], clock=lambda: now[0])
                gateway.on_authenticated()
                incoming = frame('one', '研究一下新供应商', 'voice')
                await gateway.handle_message(incoming)
                await gateway.handle_message(incoming)
                assert calls == ['研究一下新供应商']
                assert 'P1' in client.replies[-1]
                assert store.claim_due(now[0] + 86400) is None
                await gateway.handle_message(frame('before', '本月安排'))
                assert '没有已确认安排' in client.replies[-1]
                await gateway.handle_message(frame('two', '把提案P1改到今天下午三点', 'voice'))
                assert store.claim_due(now[0] + 86400) is None
                await gateway.handle_message(frame('three', '确认提案一', 'voice'))
                assert '#1' in client.replies[-1]
                for index, phrase in enumerate(('今天安排', '本周安排', '本月安排')):
                    await gateway.handle_message(frame(f'agenda{index}', phrase))
                    assert '研究新供应商' in client.replies[-1]
                    assert '15:00' in client.replies[-1]
                store.close()
                store = Store(path)
                gateway.store = store
                now[0] += 3 * 3600
                assert await gateway.deliver_due_once()
                assert len(client.sent) == 1
                assert not await gateway.deliver_due_once()
                await gateway.handle_message(frame('four', '完成任务一', 'voice'))
                await gateway.handle_message(frame('after', '今天安排'))
                assert '已完成' in client.replies[-1]
                assert '研究新供应商' in client.replies[-1]
                await gateway.handle_message(frame('pending-after', '待办'))
                assert '当前没有未完成事项' in client.replies[-1]
        finally:
            store.close()

    asyncio.run(scenario())
