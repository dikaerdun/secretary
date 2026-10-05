import asyncio
import json
from datetime import datetime

import httpx
import pytest

from secretary.parser import DeepSeekParser, ParseError, validate_command


NOW = datetime.fromisoformat('2026-09-30T12:00:00+08:00').timestamp()


def test_time_requires_actual_quote_and_timezone():
    parsed = validate_command({
        'action': 'propose', 'title': '给张总打电话',
        'remind_at': '2026-10-01T15:00:00+08:00',
        'time_evidence': '明天下午三点',
    }, '明天下午三点提醒我给张总打电话', NOW)
    assert parsed['remind_at'] == datetime.fromisoformat('2026-10-01T15:00:00+08:00').timestamp()
    for time_string in ['2026-10-01T15:00:00', '2026-09-29T15:00:00+08:00']:
        with pytest.raises(ParseError):
            validate_command({**parsed, 'remind_at': time_string, 'time_evidence': '明天下午三点'},
                             '明天下午三点提醒我给张总打电话', NOW)


def test_unscheduled_idea_is_not_given_invented_deadline():
    result = validate_command({'action': 'propose', 'title': '研究新的供应商',
                               'remind_at': None, 'time_evidence': None}, '研究新的供应商', NOW)
    assert result['remind_at'] is None
    result = validate_command({'action': 'propose', 'title': '研究供应商',
                               'remind_at': '2026-10-01T15:00:00+08:00', 'time_evidence': '明天'},
                              '研究新的供应商', NOW)
    assert result['remind_at'] is None


def test_model_cannot_invent_task_identifier():
    with pytest.raises(ParseError):
        validate_command({'action': 'complete', 'task_id': 42}, '这件事完成了', NOW)
    assert validate_command({'action': 'complete', 'task_id': 42}, '任务42完成了', NOW)['task_id'] == 42
    with pytest.raises(ParseError):
        validate_command({'action': 'complete', 'task_id': True}, '完成1', NOW)


def test_spoken_chinese_identifiers_and_ambiguous_shorthand():
    parser = DeepSeekParser('')
    assert asyncio.run(parser.parse('完成任务一', NOW))['task_id'] == 1
    assert asyncio.run(parser.parse('取消任务十二', NOW))['task_id'] == 12
    assert asyncio.run(parser.parse('完成任务一百零二', NOW))['task_id'] == 102
    assert validate_command({'action': 'complete', 'task_id': 12}, '任务十二完成了', NOW)['task_id'] == 12
    assert validate_command({'action': 'complete', 'task_id': 200}, '任务两百完成了', NOW)['task_id'] == 200
    with pytest.raises(ParseError):
        asyncio.run(parser.parse('完成任务一百二', NOW))
    with pytest.raises(ParseError):
        validate_command({'action': 'complete', 'task_id': 42}, '任务十二完成了', NOW)


def test_ambiguity_and_recurring_reminders_do_not_create_one_off_task():
    with pytest.raises(ParseError, match='时间'):
        validate_command({'action': 'clarify', 'question': '请补充具体时间。'}, '下周提醒我', NOW)
    parser = DeepSeekParser('test')
    with pytest.raises(ParseError, match='重复'):
        asyncio.run(parser.parse('每天早上九点提醒我喝水', NOW))


def test_simple_commands_work_without_network():
    parser = DeepSeekParser('')
    assert asyncio.run(parser.parse('完成 3', NOW)) == {'action': 'complete', 'task_id': 3}
    assert asyncio.run(parser.parse('待办', NOW)) == {'action': 'list'}
    assert asyncio.run(parser.parse('待办 2', NOW)) == {'action': 'list', 'page': 2}
    with pytest.raises(ParseError, match='配置'):
        asyncio.run(parser.parse('明天下午三点给张总打电话', NOW))


def test_deepseek_contract_and_invalid_results_are_rejected():
    async def run_case(content, finish='stop', status=200):
        def handler(request):
            body = json.loads(request.content)
            assert request.url.path == '/chat/completions'
            assert body['response_format'] == {'type': 'json_object'}
            assert body['thinking'] == {'type': 'disabled'}
            assert '2026-09-30' in body['messages'][0]['content']
            return httpx.Response(status, json={'choices': [{
                'finish_reason': finish, 'message': {'content': content}
            }]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            parser = DeepSeekParser('private-test-key', client=client)
            return await parser.parse('明天下午三点提醒我打电话', NOW)

    good = json.dumps({'action': 'propose', 'title': '打电话',
                       'remind_at': '2026-10-01T15:00:00+08:00', 'time_evidence': '明天下午三点'})
    assert asyncio.run(run_case(good))['action'] == 'propose'
    for content, finish, status in [('not json', 'stop', 200), (good, 'length', 200),
                                    (good, 'stop', 401), ('', 'stop', 200), ('[]', 'stop', 200)]:
        with pytest.raises(ParseError) as failure:
            asyncio.run(run_case(content, finish, status))
        assert 'private-test-key' not in str(failure.value)


def test_confirmation_and_agenda_work_without_model():
    parser = DeepSeekParser('')
    for text, expected in [
        ('确认 P1', {'action': 'confirm', 'proposal_id': 1}),
        ('确认提案一', {'action': 'confirm', 'proposal_id': 1}),
        ('取消提案 P2', {'action': 'reject', 'proposal_id': 2}),
        ('待确认', {'action': 'proposals'}),
        ('待确认 2', {'action': 'proposals', 'page': 2}),
        ('看今天安排', {'action': 'agenda', 'period': 'day', 'page': 1}),
        ('本周安排 2', {'action': 'agenda', 'period': 'week', 'page': 2}),
        ('本月的安排', {'action': 'agenda', 'period': 'month', 'page': 1}),
    ]:
        assert asyncio.run(parser.parse(text, NOW)) == expected


def test_proposal_time_is_user_supplied_and_create_cannot_skip_confirmation():
    text = '把提案P1改到明天下午三点'
    command = validate_command({'action': 'reschedule_proposal', 'proposal_id': 1,
                                'remind_at': '2026-10-01T15:00:00+08:00',
                                'time_evidence': '明天下午三点'}, text, NOW)
    assert command['proposal_id'] == 1
    with pytest.raises(ParseError):
        validate_command({'action': 'create', 'title': '直接生成正式任务'}, '记录这个事', NOW)
    with pytest.raises(ParseError):
        validate_command({'action': 'confirm', 'proposal_id': 23}, '确认这个', NOW)
    proposal = validate_command({'action': 'propose', 'title': '准备方案',
                                 'remind_at': None, 'time_evidence': '周五前',
                                 'schedule_note': '周五前交方案，具体时间待补充'}, '周五前准备方案', NOW)
    assert proposal['remind_at'] is None
    assert proposal['schedule_note']
    duration = validate_command({'action': 'reschedule_proposal', 'proposal_id': 1,
                                 'duration_minutes': 15, 'duration_evidence': '15分钟'},
                                '把提案P1用时改为15分钟', NOW)
    assert duration == {'action': 'reschedule_proposal', 'proposal_id': 1, 'duration_minutes': 15}
    with pytest.raises(ParseError):
        validate_command({'action': 'reschedule_proposal', 'proposal_id': 1,
                          'duration_minutes': 15, 'duration_evidence': '15分钟'}, '把提案P1改到明天三点', NOW)


def test_model_cannot_confirm_negation_or_bypass_explicit_confirmation():
    for text in ['提案P1先别确认', '我在考虑提案P1', '确认P1']:
        with pytest.raises(ParseError):
            validate_command({'action': 'confirm', 'proposal_id': 1}, text, NOW)
    assert asyncio.run(DeepSeekParser('').parse('确认P1', NOW)) == {'action': 'confirm', 'proposal_id': 1}
    with pytest.raises(ParseError):
        validate_command({'action': 'reject', 'proposal_id': 1}, '不要取消提案P1', NOW)
    with pytest.raises(ParseError):
        validate_command({'action': 'complete', 'task_id': 1}, '任务1还没完成', NOW)


@pytest.mark.parametrize('text,evidence', [('研究新供应商', '研究'), ('明天研究新供应商', '明天'),
                                         ('明天三点研究新供应商', '明天三点')])
def test_vague_time_cannot_become_model_chosen_clock(text, evidence):
    result = validate_command({'action': 'propose', 'title': '研究供应商',
                               'remind_at': '2026-10-01T15:00:00+08:00', 'time_evidence': evidence}, text, NOW)
    assert result['remind_at'] is None
