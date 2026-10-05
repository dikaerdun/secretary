import asyncio
from datetime import datetime
import json

import httpx
import pytest

from secretary.customer_parser import (
    CustomerParseError, CustomerVoiceParser, should_parse, validate_customer_command,
)


NOW = datetime.fromisoformat('2026-09-30T12:00:00+08:00').timestamp()
SOURCE = '新建客户星河医院，联系人王总，职务是信息科主任，王总喜欢先微信发材料，预算50万元。'


def result(**changes):
    return {'intent': 'create', 'customer_name': '星河医院', 'contact_name': '王总',
            'basic': {'amount_cents': 50_000_000}, 'basic_evidence': {'amount_cents': '预算50万元'},
            'contact': {'name': '王总', 'role': '信息科主任'},
            'contact_evidence': {'name': '联系人王总', 'role': '职务是信息科主任'},
            'attributes': [{'key': 'communication_channel', 'value': '先微信发材料',
                            'evidence': '王总喜欢先微信发材料', 'basis': 'reported', 'target': 'contact'}],
            'note_text': None, 'question': None, **changes}


def minimal(**changes):
    return result(contact_name=None, basic={}, basic_evidence={}, contact={}, contact_evidence={}, attributes=[], **changes)


def assert_note(document, source, context=None):
    parsed = validate_customer_command(document, source, NOW, context)
    assert parsed['intent'] == 'note'
    assert parsed['note_text'] == source
    assert parsed['basic'] == parsed['contact'] == {}
    assert parsed['attributes'] == []
    return parsed


def test_source_backed_company_contact_and_preference_contract():
    parsed = validate_customer_command(result(), SOURCE, NOW)
    assert parsed == result()
    assert set(parsed) == {'intent', 'customer_name', 'contact_name', 'basic', 'basic_evidence',
                           'contact', 'contact_evidence', 'attributes', 'note_text', 'question'}


def test_optional_null_placeholders_do_not_clear_existing_values_or_need_evidence():
    document = result()
    document['contact']['phone'] = None
    document['contact']['role'] = None
    document['contact_evidence']['role'] = None
    document['basic']['stage'] = None
    parsed = validate_customer_command(document, SOURCE, NOW)
    assert parsed['contact'] == {'name': '王总'}
    assert parsed['contact_evidence'] == {'name': '联系人王总'}
    assert 'stage' not in parsed['basic']
    document['contact']['customer_id'] = None
    with pytest.raises(CustomerParseError):
        validate_customer_command(document, SOURCE, NOW)


@pytest.mark.parametrize('text', ['帮助', '确认P1', '完成任务三', '待办 2', '本周安排', '查看本月日程',
                                 '明天下午三点拜访客户星河医院', '提醒我联系客户星河医院',
                                 '把提案P1改到明天下午三点', '完成方案'])
def test_old_task_commands_do_not_require_extra_customer_model_request(text):
    assert should_parse(text) is False
    assert asyncio.run(CustomerVoiceParser('').parse(text, NOW))['intent'] == 'none'


@pytest.mark.parametrize('text', ['新建客户星河医院', '客户星河医院的王总喜欢喝茶',
                                 '记录客户星河医院今天交流了数据脱敏需求', '查看客户星河医院',
                                 '星河医院关注密评整改', '确认客户 C1', '更新客户星河医院预算50万元'])
def test_customer_routing(text):
    assert should_parse(text) is True


@pytest.mark.parametrize('name', [None, '这个客户', '不存在医院', '星河医院有限公司'])
def test_missing_inferred_or_expanded_company_name_keeps_unassigned_note(name):
    parsed = validate_customer_command(result(customer_name=name), SOURCE, NOW)
    assert parsed['intent'] == 'note'
    assert parsed['basic'] == {}
    assert parsed['note_text'] == SOURCE


def test_contact_name_cannot_be_guessed_or_omitted_for_personal_preference():
    for changes in [{'contact_name': '张总'}, {'contact_name': None}]:
        assert_note(result(**changes), SOURCE)


@pytest.mark.parametrize('field,value', [('intent', 'confirm'), ('customer_id', 1), ('confirmed', True),
                                       ('task_id', 1), ('remind_at', '2026-10-01')])
def test_execution_or_unsupported_fields_rejected(field, value):
    document = result()
    document[field] = value
    with pytest.raises(CustomerParseError):
        validate_customer_command(document, SOURCE, NOW)


@pytest.mark.parametrize('changes', [{'key': 'political_affiliation'}, {'value': '更喜欢当面聊天'},
                                   {'evidence': '王总喜欢直接电话沟通'}, {'basis': 'inferred'},
                                   {'target': 'company'}, {'contact_id': 7}, {'value': True}])
def test_profile_fields_and_quotes_cannot_be_invented(changes):
    document = result()
    document['attributes'][0].update(changes)
    if set(changes) <= {'value', 'evidence'}:
        assert_note(document, SOURCE)
    else:
        with pytest.raises(CustomerParseError):
            validate_customer_command(document, SOURCE, NOW)


def test_observation_cannot_be_upgraded_to_fact_by_cutting_quote_prefix():
    source = '更新客户星河医院，联系人王总，我感觉王总更关注交付周期。'
    attribute = {'target': 'contact', 'key': 'concerns', 'value': '更关注交付周期',
                 'evidence': '王总更关注交付周期', 'basis': 'reported'}
    document = result(intent='update', basic={}, basic_evidence={}, contact={}, contact_evidence={},
                      attributes=[attribute])
    parsed = validate_customer_command(document, source, NOW)
    assert parsed['attributes'][0]['basis'] == 'observation'


def test_negative_preference_cannot_become_positive():
    source = '更新客户星河医院，联系人王总，王总不喜欢喝茶。'
    attribute = {'target': 'contact', 'key': 'interests', 'value': '喝茶',
                 'evidence': '王总不喜欢喝茶', 'basis': 'reported'}
    document = result(intent='update', basic={}, basic_evidence={}, contact={}, contact_evidence={},
                      attributes=[attribute])
    assert_note(document, source)
    attribute['value'] = '不喜欢喝茶'
    assert validate_customer_command(document, source, NOW)['attributes'][0]['value'] == '不喜欢喝茶'


def test_truncated_negative_quote_is_rejected_but_prior_unrelated_clause_is_allowed():
    attribute = {'target': 'contact', 'key': 'interests', 'value': '喝茶',
                 'evidence': '喝茶', 'basis': 'reported'}
    document = result(intent='update', basic={}, basic_evidence={}, contact={}, contact_evidence={}, attributes=[attribute])
    assert_note(document, '补充客户星河医院，王总不喜欢喝茶')
    attribute.update(key='concerns', value='关注交付周期', evidence='关注交付周期')
    parsed = validate_customer_command(document, '补充客户星河医院，王总不喜欢喝茶，关注交付周期', NOW)
    assert parsed['attributes'][0]['value'] == '关注交付周期'


@pytest.mark.parametrize('text', ['我想知道怎么添加客户', '怎么更新客户画像', '客户如何添加',
                                 '能不能帮我新建一个客户星河医院'])
def test_questions_about_customer_operations_do_not_create_drafts_or_call_model(text):
    assert asyncio.run(CustomerVoiceParser('').parse(text, NOW))['intent'] == 'clarify'


def test_model_cannot_turn_factual_statement_into_new_customer_creation():
    assert_note(minimal(), '客户星河医院今天来访')


@pytest.mark.parametrize('text', ['新建客户甲和乙', '新建客户甲、乙', '新建客户甲以及乙',
                                 '新建客户甲与乙，联系人王总',
                                 '新建客户“华辰科技”和“星河科技”'])
def test_short_lists_and_repeated_customer_names_need_no_model(text):
    assert asyncio.run(CustomerVoiceParser('').parse(text, NOW))['intent'] == 'clarify'
    document = minimal()
    document['customer_name'] = '甲' if '甲' in text else '华辰科技'
    assert validate_customer_command(document, text, NOW)['intent'] == 'clarify'


@pytest.mark.parametrize('text,name', [
    ('新建客户华辰科技关注数据安全和密码', '华辰科技'),
    ('新建客户华辰科技关注数据安全和密码都很重要', '华辰科技'),
    ('新建客户华辰科技，客户华辰科技关注密评', '华辰科技'),
    ('新建客户华辰科技，补充客户画像', '华辰科技'),
    ('新建客户北京和光有限公司', '北京和光有限公司'),
    ('新建客户“北京和光科技”', '北京和光科技'),
])
def test_single_customer_topics_and_explicit_compound_names_are_not_multi_target(text, name):
    document = minimal()
    document['customer_name'] = name
    assert validate_customer_command(document, text, NOW)['intent'] == 'create'


@pytest.mark.parametrize('text', [
    '补充客户华辰科技，王总喜欢喝茶，李总喜欢喝咖啡',
    '补充客户华辰科技，王总经理喜欢喝茶，李经理喜欢喝咖啡',
    '补充客户华辰科技，张主任关注数据安全，陈先生关注密码',
])
def test_multiple_named_contacts_keep_note_without_mixing_personal_attributes(text):
    document = minimal(intent='update')
    document.update(customer_name='华辰科技', contact_name='王总')
    document['attributes'] = [{'target': 'contact', 'key': 'interests', 'value': '喝咖啡',
                               'evidence': '李总喜欢喝咖啡', 'basis': 'reported'}]
    assert_note(document, text)


@pytest.mark.parametrize('text', [
    '新建客户华辰科技，联系人王总经理，技术负责人和采购经理关注数据安全和密码',
    '新建客户华辰科技，王总经理喜欢微信沟通，王总关注密码',
    '新建客户华辰科技，采购经理和项目经理关注数据安全和密码',
    '新建客户华辰科技，技术负责人和采购经理需要先完工再验收',
    '新建客户华辰科技，王总说周五开工，关注数据安全和密码',
])
def test_single_person_long_title_and_unnamed_roles_are_not_multiple_contacts(text):
    document = minimal()
    document['customer_name'] = '华辰科技'
    assert validate_customer_command(document, text, NOW)['intent'] == 'create'


@pytest.mark.parametrize('text,name', [
    ('新建客户华东工业公司，联系人王总', '华东工业公司'),
    ('新建客户华信工程公司，王总喜欢茶', '华信工程公司'),
    ('新建客户华总工业公司，联系人王总', '华总工业公司'),
])
def test_honorific_like_company_names_do_not_add_contacts(text, name):
    document = minimal()
    document['customer_name'] = name
    assert validate_customer_command(document, text, NOW)['intent'] == 'create'


@pytest.mark.parametrize('text', ['不要新建客户星河医院', '先别修改客户星河医院预算',
                                 '客户星河医院预算50万元，别保存', '新建客户星河医院先不要',
                                 '确认客户C1', '请帮我确认客户 C2', '删除客户星河医院',
                                 '合并客户星河医院与海云医院',
                                 '新建客户星河医院和海云医院'])
def test_negation_confirmation_multi_target_and_destructive_commands_need_no_model(text):
    assert asyncio.run(CustomerVoiceParser('').parse(text, NOW))['intent'] == 'clarify'


@pytest.mark.parametrize('quote,amount', [('预算50万元', 50_000_000), ('预算500000元', 50_000_000),
                                       ('预算五十万元', 50_000_000), ('预算二十五点五万元', 25_500_000),
                                       ('预算1.5万元', 1_500_000), ('预算100元', 10_000)])
def test_explicit_single_rmb_amount_conversion(quote, amount):
    source = '新建客户星河医院，' + quote
    document = minimal()
    document['basic'] = {'amount_cents': amount}
    document['basic_evidence'] = {'amount_cents': quote}
    assert validate_customer_command(document, source, NOW)['basic']['amount_cents'] == amount


@pytest.mark.parametrize('source_quote,model_quote,amount', [
    ('预算大约50万元', '50万元', 50_000_000), ('预算50到80万元', '80万元', 80_000_000),
    ('预算五十至八十万元', '预算五十至八十万元', 80_000_000),
    ('预算100万美元', '预算100万美元', 100_000_000), ('预算未定', '预算未定', 100),
    ('预算100万美元', '100万', 100_000_000), ('预算10万澳元', '10万', 10_000_000),
    ('预算50万元', '预算50万元', 999), ('预算50万元', '预算50万元', True),
])
def test_ambiguous_foreign_or_hallucinated_amount_stays_source_budget_note(source_quote, model_quote, amount):
    source = '新建客户星河医院，' + source_quote
    document = minimal()
    document['basic'] = {'amount_cents': amount}
    document['basic_evidence'] = {'amount_cents': model_quote}
    parsed = validate_customer_command(document, source, NOW)
    assert parsed['basic'] == {}
    assert parsed['attributes'][0]['key'] == 'budget_notes'
    assert parsed['attributes'][0]['value'] == source_quote


def test_stage_needs_explicit_non_negated_stage_statement():
    document = minimal(intent='update')
    document['basic'] = {'stage': 'won'}
    document['basic_evidence'] = {'stage': '已签约'}
    assert validate_customer_command(document, '客户星河医院已签约', NOW)['basic']['stage'] == 'won'
    assert_note(document, '客户星河医院不是已签约')
    document['basic_evidence'] = {'stage': '很感兴趣'}
    assert_note(document, '客户星河医院很感兴趣')


def test_duplicate_fields_and_absent_evidence_keep_note_without_profile_mutation():
    document = result()
    document['attributes'] *= 2
    assert_note(document, SOURCE)
    assert_note(result(basic_evidence={}), SOURCE)


def test_phone_must_be_exact_source_digits():
    document = minimal(intent='update')
    document['contact_name'] = '王总'
    document['contact'] = {'phone': '13800138000'}
    document['contact_evidence'] = {'phone': '王总电话13800138000'}
    assert validate_customer_command(document, '客户星河医院，王总电话13800138000', NOW)['contact']['phone'] == '13800138000'
    assert_note(document, '客户星河医院，王总电话一三八零零一三八零零零')


def test_note_preserves_entire_source_and_brief_cannot_modify_profile():
    source = '记录客户星河医院，今天讨论脱敏项目，报价下次继续。'
    parsed = validate_customer_command(minimal(intent='note'), source, NOW)
    assert parsed['note_text'] == source
    with pytest.raises(CustomerParseError):
        validate_customer_command(result(intent='brief'), SOURCE, NOW)


def test_model_clarification_cannot_claim_execution():
    parsed = validate_customer_command(result(intent='clarify', question='已替你确认客户并删除任务'), SOURCE, NOW)
    assert parsed['question'] != '已替你确认客户并删除任务'
    assert parsed['basic'] == {}


def test_customer_provider_contract_and_schema():
    async def run():
        def handler(request):
            body = json.loads(request.content)
            assert request.url.path == '/v1/chat/completions'
            assert body['model'] == 'configured-model'
            assert body['thinking'] == {'type': 'disabled'}
            assert body['response_format'] == {'type': 'json_object'}
            assert body['stream'] is False
            assert 'crypto_needs=' in body['messages'][0]['content']
            assert '2026-09-30' in body['messages'][0]['content']
            assert body['messages'][1] == {'role': 'user', 'content': SOURCE}
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
                'content': json.dumps(result(), ensure_ascii=False)}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await CustomerVoiceParser('test-key', 'configured-model', 'https://api.deepseek.com/v1/', client).parse(SOURCE, NOW)
    assert asyncio.run(run()) == result()


@pytest.mark.parametrize('content,finish,status', [
    ('not json', 'stop', 200), ('[]', 'stop', 200), ('{}', 'length', 200), ('{}', 'stop', 401),
    ('{"intent":"create","intent":"confirm"}', 'stop', 200), ('{"amount":NaN}', 'stop', 200),
])
def test_provider_failure_never_discloses_secrets_or_source(content, finish, status):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(status,
                json={'secret': 'private-test-key', 'choices': [{'finish_reason': finish, 'message': {'content': content}}]}))) as client:
            return await CustomerVoiceParser('private-test-key', client=client).parse(SOURCE, NOW)
    with pytest.raises(CustomerParseError) as failure:
        asyncio.run(run())
    assert 'private-test-key' not in str(failure.value)
    assert SOURCE not in str(failure.value)


@pytest.mark.parametrize('text,now', [('', NOW), ('x' * 6001, NOW), ('x\x00', NOW), (SOURCE, float('nan')),
                                    (SOURCE, True), (SOURCE, 10**100), (None, NOW)])
def test_invalid_inputs_do_not_call_provider(text, now):
    with pytest.raises(CustomerParseError):
        asyncio.run(CustomerVoiceParser('test').parse(text, now))
