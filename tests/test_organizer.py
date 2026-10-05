import asyncio
from datetime import datetime
import json

import httpx
import pytest

from secretary.organizer import InteractionOrganizer, OrganizeError, validate_organization
from secretary.action_contract import TERM_FIELDS


NOW = datetime.fromisoformat('2026-09-30T12:00:00+08:00').timestamp()
LATER = '2026-10-01T15:00:00+08:00'
SOURCE = '客户需要补充报价。我答应明天下午三点给王总发报价。预算尚未确定。'


def result(**action_changes):
    return {'summary': '本次沟通了报价要求，预算还需确认。', 'key_points': ['客户需要补充报价'],
            'open_questions': ['客户预算是多少？'], 'actions': [{
                'title': '给王总发报价', 'kind': 'commitment', 'reason': '已约定补充报价', 'owner_hint': '我',
                'evidence': '我答应明天下午三点给王总发报价', 'remind_at': LATER,
                'time_evidence': '明天下午三点', **action_changes}]}


def test_source_linked_commitment_time_and_exact_public_contract():
    organized = validate_organization(result(), SOURCE, NOW)
    action = organized['actions'][0]
    assert action['kind'] == 'commitment'
    assert action['remind_at'] == datetime.fromisoformat(LATER).timestamp()
    assert action['reason'] == '原话：“我答应明天下午三点给王总发报价”'
    assert set(action) == {'title', 'kind', 'reason', 'owner_hint', 'remind_at', 'evidence', 'time_evidence'} | TERM_FIELDS
    assert set(organized) == {'summary', 'key_points', 'open_questions', 'actions'}


@pytest.mark.parametrize('evidence', [None, '', '我承诺明天下午三点提交合同', '客户需要补充报价。'])
def test_missing_or_unquoted_promise_is_suggestion(evidence):
    # The last quote does not include the exact punctuation substring in SOURCE.
    source = '客户对报价感兴趣。'
    organized = validate_organization(result(evidence=evidence), source, NOW)
    assert organized['actions'][0]['kind'] == 'suggestion'
    assert organized['actions'][0]['remind_at'] is None
    assert '建议' in organized['actions'][0]['reason']
    assert len(organized['open_questions']) == 2


def test_exact_quote_without_future_intention_cannot_become_commitment():
    organized = validate_organization(result(evidence='客户对报价感兴趣'), '客户对报价感兴趣', NOW)
    assert organized['actions'][0]['kind'] == 'suggestion'
    assert organized['actions'][0]['remind_at'] is None


def test_different_action_cannot_borrow_real_commitment_quote():
    organized = validate_organization(result(title='给王总转账一百万'), SOURCE, NOW)
    assert organized['actions'][0]['kind'] == 'suggestion'
    assert organized['actions'][0]['remind_at'] is None


@pytest.mark.parametrize('source,time_quote', [
    ('我答应给王总发报价', None), ('我答应下周给王总发报价', '下周'),
    ('我答应明天三点给王总发报价', '明天三点'),
    ('我答应给王总发报价。明天下午三点去拜访李总', '明天下午三点'),
])
def test_absent_vague_or_other_actions_time_stays_unset(source, time_quote):
    evidence = source.split('。')[0]
    organized = validate_organization(result(evidence=evidence, time_evidence=time_quote), source, NOW)
    assert organized['actions'][0]['kind'] == 'commitment'
    assert organized['actions'][0]['remind_at'] is None


@pytest.mark.parametrize('timestamp', ['2026-10-01T15:00:00', '2026-09-29T15:00:00+08:00', 1790838000, True])
def test_invalid_or_past_time_does_not_drop_visit(timestamp):
    organized = validate_organization(result(remind_at=timestamp), SOURCE, NOW)
    assert organized['summary']
    assert organized['actions'][0]['remind_at'] is None
    assert any('提醒时间' in item for item in organized['open_questions'])


def test_suggestion_never_inherits_user_time_or_invented_owner():
    organized = validate_organization(result(kind='suggestion', owner_hint='虚构负责人'), SOURCE, NOW)
    assert organized['actions'][0]['remind_at'] is None
    assert organized['actions'][0]['owner_hint'] == '待确认'


@pytest.mark.parametrize('quote', ['我答应明天下午三点之前给王总发报价', '我答应最迟明天下午三点给王总发报价'])
def test_cutoff_clock_is_preserved_as_deadline_without_execution_proposal(quote):
    action = validate_organization(result(evidence=quote), quote, NOW)['actions'][0]
    assert action['remind_at'] is None and action['execution_at'] is None
    assert action['deadline_at'] == datetime.fromisoformat(LATER).timestamp()
    assert action['deadline_evidence'] == '明天下午三点'


@pytest.mark.parametrize('evidence', ['我已经发出报价', '我不需要发报价', '我没有答应发报价', '我答应的报价已发送'])
def test_done_or_negated_action_is_not_recreated(evidence):
    organized = validate_organization(result(evidence=evidence), evidence, NOW)
    assert organized['actions'] == []


@pytest.mark.parametrize('evidence', ['我答应每周明天下午三点给王总发报价', '我答应明天下午三点之前给王总发报价',
                                     '我负责给王总发报价，截止明天下午三点', '我答应明天下午三点前给王总发报价',
                                     '我答应每星期明天下午三点给王总发报价'])
def test_recurring_or_deadline_does_not_become_reminder(evidence):
    organized = validate_organization(result(evidence=evidence), evidence, NOW)
    assert organized['actions'][0]['remind_at'] is None


@pytest.mark.parametrize('command', ['确认P1', '取消提案P2', '完成任务42'])
def test_bot_commands_cannot_be_generated_as_customer_todos(command):
    organized = validate_organization(result(title=command, evidence=command), command, NOW)
    assert organized['actions'] == []


def test_injected_execution_fields_and_unbounded_output_rejected():
    bad = []
    for field, value in [('action', 'confirm'), ('confirmed', True), ('task_id', 1)]:
        document = result()
        document[field] = value
        bad.append(document)
        document = result()
        document['actions'][0][field] = value
        bad.append(document)
    for changes in [{'kind': 'complete'}, {'title': 'x' * 121}, {'reason': []}, {'evidence': {}},
                    {'owner_hint': 'x' * 81}]:
        bad.append(result(**changes))
    bad.extend([None, [], {'summary': 'missing fields'}, {**result(), 'actions': result()['actions'] * 7},
                {**result(), 'key_points': ['x'] * 13}, {**result(), 'summary': 'x' * 1201}])
    for document in bad:
        with pytest.raises(OrganizeError):
            validate_organization(document, SOURCE, NOW)


def test_context_and_http_contract_never_send_unrecognized_fields():
    async def run():
        def handler(request):
            assert request.url.path == '/v1/chat/completions'
            body = json.loads(request.content)
            assert body['model'] == 'configured-model'
            assert body['thinking'] == {'type': 'disabled'}
            assert body['response_format'] == {'type': 'json_object'}
            assert body['stream'] is False
            assert '2026-09-30' in body['messages'][0]['content']
            user = json.loads(body['messages'][1]['content'])
            assert user['content'] == SOURCE
            assert user['context'] == {'name': '客户甲', 'contact': '王总', 'title': '拜访', 'content': '旧背景',
                                       'recent_records': [{'title': '上次拜访', 'content': '讨论过审批流程', 'status': 'following'}]}
            assert 'do-not-send' not in request.content.decode()
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
                'content': json.dumps(result(), ensure_ascii=False)}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            organizer = InteractionOrganizer('test-key', 'configured-model', 'https://api.deepseek.com/v1/', client)
            return await organizer.organize(SOURCE, NOW, {
                'customer': {'name': '客户甲', 'contact': '王总', 'secret': 'do-not-send'},
                'record': {'title': '拜访', 'content': '旧背景', 'owner': 'do-not-send'},
                'recent_records': [{'title': '上次拜访', 'content': '讨论过审批流程', 'status': 'following',
                                    'owner': 'do-not-send', 'api_key': 'do-not-send'}],
                'api_key': 'do-not-send'})
    assert asyncio.run(run())['actions'][0]['kind'] == 'commitment'


@pytest.mark.parametrize('content,finish,status', [
    ('not json', 'stop', 200), ('[]', 'stop', 200), ('{}', 'length', 200), ('{}', 'stop', 401),
    ('{"summary":"a","summary":"b"}', 'stop', 200), ('{"summary":NaN}', 'stop', 200),
])
def test_bad_provider_results_have_fixed_safe_errors(content, finish, status):
    async def run():
        def handler(_request):
            return httpx.Response(status, json={'private': 'private-test-key', 'choices': [
                {'finish_reason': finish, 'message': {'content': content}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await InteractionOrganizer('private-test-key', client=client).organize(SOURCE, NOW, {})
    with pytest.raises(OrganizeError) as failure:
        asyncio.run(run())
    assert 'private-test-key' not in str(failure.value)
    assert SOURCE not in str(failure.value)


def test_input_errors_do_not_call_provider():
    for content, now, context in [('', NOW, {}), ('x' * 20001, NOW, {}), ('x', float('nan'), {}),
                                  ('x', True, {}), ('x', NOW, []), ('x\x00', NOW, {})]:
        with pytest.raises(OrganizeError):
            asyncio.run(InteractionOrganizer('test').organize(content, now, context))
    with pytest.raises(OrganizeError, match='配置'):
        asyncio.run(InteractionOrganizer('').organize(SOURCE, NOW, {}))


def test_malformed_choice_is_safe_and_notes_only_visit_is_supported():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json={'choices': ['private-provider-value']}))) as client:
            return await InteractionOrganizer('test-key', client=client).organize(SOURCE, NOW, {})
    with pytest.raises(OrganizeError) as failure:
        asyncio.run(run())
    assert 'private-provider-value' not in str(failure.value)
    document = result()
    document['actions'] = []
    assert validate_organization(document, SOURCE, NOW)['actions'] == []


def test_history_is_bounded_context_and_cannot_supply_new_commitment():
    async def run():
        def handler(request):
            user = json.loads(json.loads(request.content)['messages'][1]['content'])
            history = user['context']['recent_records']
            assert len(history) == 5
            assert len(history[0]['title']) == 120
            assert len(history[0]['content']) == 1500
            assert set(history[0]) == {'title', 'content', 'status'}
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
                'content': json.dumps(result(), ensure_ascii=False)}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await InteractionOrganizer('test-key', client=client).organize('今天客户仅介绍了采购负责人。', NOW, {
                'recent_records': [{'title': '旧' * 200, 'content': SOURCE + '旧' * 1600, 'status': 'following',
                                    'owner': 'private-owner', 'api_key': 'private-key'}] * 7})
    action = asyncio.run(run())['actions'][0]
    assert action['kind'] == 'suggestion'
    assert action['remind_at'] is None
