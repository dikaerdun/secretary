"""Actual offline Advisor inputs: bounded evidence, selected history and privacy."""
import asyncio
import json

from test_timeline_web import timeline_app, customer, contact, capture, discuss, last_assistant, NOW
from test_timeline_trial_journeys import trial_app, adopt_move, terms


def test_selected_old_evidence_survives_budget_and_sources_reference_sent_events(tmp_path):
    async def run():
        async with timeline_app(tmp_path) as (_, call, _, _, _, advisor, _):
            unit = await customer(call, '大资料量合成单位', notes='人工背景说明' * 500)
            person = await contact(call, unit['id'], department='技术部')
            chosen = []
            for index in range(6):
                source = await capture(call, {'contact_id': person['id']},
                    f'重要历史{index}：以前明确的接口范围。' + '一般技术背景。' * 550 + f'\n结尾修正{index}：原承诺需重新核对。',
                    occurred_at=NOW-30*86400+index, request_id=f'budget-old-{index}')
                chosen.append(source['event']['key'])
            for index in range(12):
                latest = await capture(call, {'contact_id': person['id']},
                    f'近期交流{index}：上线进度。' + '普通现状说明。' * 550 + f'\n最新结尾{index}：审批暂未确定。',
                    occurred_at=NOW-3600+index, request_id=f'budget-new-{index}')
            thread = await discuss(call, unit['id'], contact_id=person['id'], event_keys=chosen, request_id='budget-thread')
            reply = await call('POST', f"/api/sales-discussions/{thread['id']}/messages",
                {'text': '结合我明确选择的旧资料和最新进展，讨论下一步。', 'request_id': 'budget-message'})
            context = advisor.calls[-1]['context']
            evidence = context['timeline']['events']
            keys = {item['key'] for item in evidence}
            assert set(chosen) <= keys
            assert latest['event']['key'] in keys
            assert all(item['selected'] for item in evidence if item['key'] in chosen)
            assert context['truncated'] and context['timeline']['omitted_event_count'] > 0
            assert len(json.dumps(context, ensure_ascii=False, separators=(',', ':'))) <= 32_000
            references = {item['event_key'] for item in last_assistant(reply)['sources'] if 'event_key' in item}
            assert references <= keys and set(chosen) <= references
            for index, key in enumerate(chosen):
                assert f'结尾修正{index}' in next(item['text'] for item in evidence if item['key'] == key)
    asyncio.run(run())


def test_confirmed_and_cancelled_action_have_consistent_schedule_context(tmp_path):
    async def run():
        async with trial_app(tmp_path) as (_, call, _, _, _, advisor, _, clock, _):
            unit = await customer(call, '日程上下文合成单位')
            person = await contact(call, unit['id'])
            await capture(call, {'contact_id': person['id']}, '我负责核对上线边界。', request_id='same-action-source')
            thread = await discuss(call, unit['id'], contact_id=person['id'], request_id='same-action-thread')
            _, action = await adopt_move(call, thread, request_id='same-action-message')
            await terms(call, action, executor_kind='self', execution_at=clock()+3600, execution_evidence='人工拟定一小时后核对')
            calendar_seen = await call('GET', f"/api/records/{action['id']}")
            assert len(calendar_seen['schedule_snapshot']) == 64
            scheduled = await call('POST', f"/api/records/{action['id']}/schedule", {'remind_at': clock()+3600, 'duration_minutes': 30, 'expected_schedule_snapshot': calendar_seen['schedule_snapshot']})
            await call('POST', f"/api/records/{action['id']}/confirm",
                {'proposal_id': scheduled['proposal']['id'], 'updated_at': scheduled['proposal']['updated_at']})
            for active in (True, False):
                if not active:
                    calendar_seen = await call('GET', f"/api/records/{action['id']}")
                    assert len(calendar_seen['schedule_snapshot']) == 64
                    await call('POST', f"/api/records/{action['id']}/cancel", {'expected_schedule_snapshot': calendar_seen['schedule_snapshot']})
                review = await discuss(call, unit['id'], contact_id=person['id'], request_id=f'same-action-review-{active}')
                await call('POST', f"/api/sales-discussions/{review['id']}/messages",
                    {'text': '核对当前有效安排', 'request_id': f'same-action-review-msg-{active}'})
                context = advisor.calls[-1]['context']
                top = next(item for item in context['open_actions'] if item['id'] == action['id'])
                focused = next(item for item in context['timeline']['open_actions'] if item['id'] == action['id'])
                assert top == focused
                assert focused['active_schedule'] is active
                assert focused['remind_at'] == (clock()+3600 if active else None)
                assert focused['status'] != 'done'
    asyncio.run(run())


def test_long_evidence_redacts_head_and_tail_without_changing_original(tmp_path):
    async def run():
        async with timeline_app(tmp_path) as (_, call, _, _, _, advisor, _):
            unit = await customer(call, '隐私合成单位')
            person = await contact(call, unit['id'], phone='13812345678')
            exact = '开头联系电话13812345678，先讨论材料。\n' + '交流中的背景信息。' * 800 + '\n最后通知：项目暂停，不要再发方案；sample@example.com。'
            saved = await capture(call, {'contact_id': person['id']}, exact, request_id='privacy-record')
            thread = await discuss(call, unit['id'], contact_id=person['id'], event_keys=[saved['event']['key']], request_id='privacy-thread')
            await call('POST', f"/api/sales-discussions/{thread['id']}/messages",
                {'text': '依据最后的变更讨论', 'request_id': 'privacy-message'})
            evidence = next(item for item in advisor.calls[-1]['context']['timeline']['events'] if item['key'] == saved['event']['key'])
            assert evidence['text_truncated'] and evidence['text_length'] == len(exact)
            assert '项目暂停' in evidence['text'] and '不要再发方案' in evidence['text']
            serialized = json.dumps(advisor.calls[-1]['context'], ensure_ascii=False)
            assert '13812345678' not in serialized and 'sample@example.com' not in serialized
            assert len(evidence['text']) <= 2200 and '中间内容省略' in evidence['text']
            record = (await call('GET', f"/api/records/{saved['record']['id']}"))['record']
            assert record['content'] == record['original_content'] == exact
    asyncio.run(run())
