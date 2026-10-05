"""Adoption origins are real owned receipts, not new AI/customer evidence."""
import asyncio

import pytest

from secretary.action_provenance import action_origins
from test_ai_batches_web import context, ready, OWNER, NOW


def test_progress_adoption_detail_returns_exact_original_run_without_mutation(tmp_path):
    async def scenario():
        async with context(tmp_path) as (_, call, controller, unit):
            raw = '合成原复盘：我想先核对兼容边界；不是客户确认，不约时间。'
            run = (await call('POST', '/api/progress-workspaces', {'kind': 'recap', 'customer_id': unit['id'],
                'text': raw, 'request_id': 'origin-progress'}, 201))['run']
            run = await ready(call, run['id'])
            action = next(row for row in run['items'] if row['type'] == 'action')
            run = (await call('PATCH', f"/api/progress-runs/{run['id']}", {'expected_revision': run['revision'],
                'items': [{'id': action['id'], 'selected': True, 'draft': {'title': '人工核对后检查兼容边界',
                    'executor_kind': 'self', 'duration_minutes': None}}]}))['run']
            action = next(row for row in run['items'] if row['id'] == action['id'])
            receipt = await call('POST', f"/api/progress-runs/{run['id']}/confirm", {'expected_revision': run['revision'],
                'request_id': 'origin-confirm', 'items': [{'id': action['id'], 'expected_item_revision': action['revision'],
                'expected_snapshot': action['versions']['snapshot']}]})
            record_id = receipt['results'][0]['record_id']
            before = controller.crm._db.total_changes
            detail = await call('GET', f'/api/records/{record_id}')
            origin = next(row for row in detail['action_origins']['items'] if row['type'] == 'progress')
            assert origin['run_id'] == run['id'] and origin['text'] == raw
            assert origin['scope']['customer_id'] == unit['id'] and origin['same_customer'] is True
            assert detail['record']['title'] == '人工核对后检查兼容边界'
            assert controller.crm._db.total_changes == before
            assert controller.crm._db.execute('SELECT count(*) FROM crm_customer_facts').fetchone()[0] == 0
            assert controller.crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
            with pytest.raises(KeyError):
                action_origins(controller.crm, 'foreign-owner', record_id)
    asyncio.run(scenario())


def test_discussion_origin_points_to_adopted_message_and_preserves_user_question(tmp_path):
    async def scenario():
        async with context(tmp_path) as (_, call, controller, unit):
            class Advisor:
                async def reply(self, *_):
                    return {'answer': '合成 AI 判断，需要客户核实。', 'questions': [], 'risks': [],
                        'next_moves': [{'title': '核实试点评价标准', 'reason': '没有明确客户答复', 'contact_hint': '技术对接人待核实',
                            'preparation': '准备兼容性问题', 'success_signal': '收到明确测试范围'}]}
            controller.discussions.advisor = Advisor()
            thread = (await call('POST', '/api/sales-discussions', {'customer_id': unit['id'], 'request_id': 'origin-thread'}, 201))['thread']
            raw = '我自己的疑问：下一步该如何问清验收标准？'
            await call('POST', f"/api/sales-discussions/{thread['id']}/messages", {'text': raw, 'request_id': 'origin-question'})
            discussion = await call('GET', f"/api/sales-discussions/{thread['id']}")
            message = next(row for row in discussion['messages'] if row['role'] == 'assistant')
            record = (await call('POST', f"/api/sales-discussions/{thread['id']}/messages/{message['id']}/actions/1/adopt",
                {'expected_snapshot': message['snapshot']}))['record']
            before = controller.crm._db.total_changes
            origin = next(row for row in (await call('GET', f"/api/records/{record['id']}"))['action_origins']['items'] if row['type'] == 'discussion')
            assert (origin['thread_id'], origin['message_id'], origin['action_index']) == (thread['id'], message['id'], 1)
            assert origin['user_text'] == raw and '需要客户核实' in origin['answer_text']
            assert controller.crm._db.total_changes == before
            unrelated = controller.crm.create_record(OWNER, {'title': '普通合成记录', 'content': '无采用来源'}, NOW)
            assert action_origins(controller.crm, OWNER, unrelated['id'])['items'] == []
    asyncio.run(scenario())


def test_filing_use_never_creates_action_and_preserves_explicit_legacy_action(tmp_path):
    async def scenario():
        async with context(tmp_path) as (_, call, controller, unit):
            for purpose in ('action', 'schedule'):
                raw = '我答应整理清单，但采购反馈仍待核实；这是多个不同意思的来源。'
                capture = (await call('POST', '/api/captures', {'text': raw, 'request_id': 'origin-capture-' + purpose}, 202))['capture']
                result = await call('POST', f"/api/captures/{capture['id']}/classify", {'purpose': purpose, 'customer_id': unit['id'],
                    'expected_updated_at': capture['record']['updated_at']})
                assert result['record']['kind'] == 'note' and result['record']['original_content'] == raw
            assert controller.crm._db.execute('SELECT count(*) FROM crm_records WHERE kind=\'action\'').fetchone()[0] == 0
            controller.crm.update_record(OWNER, result['record']['id'], {'kind': 'action', 'title': '用户明确维护的旧行动'}, NOW + 5)
            changed = controller.captures.get(OWNER, result['capture']['id'])
            filed = controller.captures.classify(OWNER, changed['id'], {'purpose': 'action', 'customer_id': unit['id'],
                'expected_updated_at': changed['record']['updated_at']})
            assert filed['record']['kind'] == 'action' and filed['record']['title'] == '用户明确维护的旧行动'
    asyncio.run(scenario())
