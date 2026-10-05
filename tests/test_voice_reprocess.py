"""Natural visit capture and explicit, owner-bound correction/reprocessing."""
import asyncio
import json
import time

import httpx
import pytest

from secretary.customer_parser import CustomerVoiceParser, validate_customer_command, should_parse
from secretary.customer_service import CustomerService
from secretary.customer_store import CustomerStore


def document(intent='note', name='星河医院'):
    return {'intent': intent, 'customer_name': name, 'contact_name': None, 'basic': {}, 'basic_evidence': {},
            'contact': {}, 'contact_evidence': {}, 'attributes': [], 'note_text': None, 'question': None}


def update_document(name='星河医院', value='数据库加密'):
    data = document('update', name)
    data['attributes'] = [{'target': 'account', 'key': 'crypto_needs', 'value': value,
                           'evidence': value, 'basis': 'reported'}]
    return data


@pytest.mark.parametrize('source', [
    '记录客户星河医院，客户说希望下周发方案。',
    '记录客户星河医院，王总要求不要修改原系统，只补充接口层。',
    '今天拜访星河医院，王总关注加密，李经理负责采购，我答应明天下午三点发报价。',
    '不是王总，是李总，星河医院的联系人说希望先发微信。',
])
def test_natural_visits_reach_model_and_preserve_raw_note(source):
    async def run():
        calls = []
        def handle(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
                'content': json.dumps(document(), ensure_ascii=False)}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await CustomerVoiceParser('synthetic-key', client=client).parse(source, time.time())
        assert len(calls) == 1
        assert result['intent'] == 'note'
        assert result['note_text'] == source
        assert result['attributes'] == []
    asyncio.run(run())


@pytest.mark.parametrize('source', ['华辰那边说数据库脱敏先做起来，下周我再过去一趟。',
                                  '帮我记一下，今天去了星河，谈了数据库加密。',
                                  '他说预算改成八十万，不是五十万。'])
def test_short_company_and_followup_are_capture_candidates(source):
    assert should_parse(source)
    result = validate_customer_command(document('none', None), source)
    assert result['intent'] == 'note'
    assert result['customer_name'] is None


def test_selected_customer_context_is_sanitized_and_only_selected_name_can_be_inferred():
    source = '他需要数据库加密。'
    async def run():
        def handle(request):
            body = json.loads(request.content)
            assert 'private-phone' not in request.content.decode()
            assert 'private-token' not in request.content.decode()
            incoming = json.loads(body['messages'][1]['content'])
            assert incoming['context'] == {'customer': {'name': '星河医院'}, 'customers': [{'name': '其他医院'}]}
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
                'content': json.dumps(update_document(), ensure_ascii=False)}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await CustomerVoiceParser('synthetic-key', client=client).parse(source, time.time(), {
                'customer': {'id': 1, 'name': '星河医院', 'phone': 'private-phone'},
                'customers': [{'name': '其他医院', 'phone': 'private-phone'}], 'token': 'private-token'})
    assert asyncio.run(run())['intent'] == 'update'
    unselected = validate_customer_command(update_document(), source, context={'customers': [{'name': '星河医院'}]})
    assert unselected['intent'] == 'note'
    assert unselected['customer_name'] is None


class Parser:
    def __init__(self, data=None):
        self.data = data
        self.calls = []

    async def parse(self, text, now, context=None):
        self.calls.append((text, context))
        data = self.data or update_document(context['customer']['name'])
        return validate_customer_command(data, text, now, context)


def test_explicit_selection_edited_content_and_confirmation_preserve_original(tmp_path):
    async def run():
        crm = CustomerStore(tmp_path / 'selected.sqlite3')
        try:
            customer = crm.create_customer('owner', {'name': '星河医院'}, time.time())
            raw = crm.capture_message('owner', 'voice-original', '他说要数据库脱敏', 'voice', time.time())
            crm.update_record('owner', raw['id'], {'content': '他说要数据库加密'}, time.time())
            parser = Parser()
            service = CustomerService(crm, parser, asyncio.Lock())
            result = await service.handle('owner', 'reprocess-1', 'ignored stale client text', force=True,
                                          selected_customer_id=customer['id'], record_id=raw['id'])
            assert result['record_id'] == raw['id']
            assert result['draft']['status'] == 'pending'
            assert parser.calls[0][0] == '他说要数据库加密'
            assert result['draft']['source_text'] == '他说要数据库脱敏'
            assert result['draft']['source_content'] == '他说要数据库加密'
            assert result['draft']['selected_customer'] is True
            assert crm.profile('owner', customer['id'])['fields'] == []
            service.decide('owner', result['draft']['id'], True)
            assert crm.profile('owner', customer['id'])['fields'][0]['value'] == '数据库加密'
            assert crm.get_record('owner', raw['id'])['original_content'] == '他说要数据库脱敏'
            assert crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
        finally:
            crm.close()
    asyncio.run(run())


def test_reprocess_marks_prior_draft_stale_and_later_edit_blocks_confirmation(tmp_path):
    async def run():
        crm = CustomerStore(tmp_path / 'stale.sqlite3')
        try:
            customer = crm.create_customer('owner', {'name': '星河医院'}, time.time())
            record = crm.create_record('owner', {'title': '沟通', 'content': '需要数据库加密', 'customer_id': customer['id']}, time.time())
            service = CustomerService(crm, Parser(), asyncio.Lock())
            first = await service.handle('owner', 'r1', '', record_id=record['id'], force=True)
            second = await service.handle('owner', 'r2', '', record_id=record['id'], force=True)
            assert first['draft']['id'] != second['draft']['id']
            assert crm.get_customer_draft('owner', first['draft']['id'])['status'] == 'stale'
            with pytest.raises(ValueError):
                service.decide('owner', first['draft']['id'], True)
            crm.update_record('owner', record['id'], {'content': '刚才说错了，暂不考虑'}, time.time())
            with pytest.raises(ValueError):
                service.decide('owner', second['draft']['id'], True)
            assert crm.profile('owner', customer['id'])['fields'] == []
        finally:
            crm.close()
    asyncio.run(run())


def test_owner_isolation_selected_customer_and_record_both_required(tmp_path):
    async def run():
        crm = CustomerStore(tmp_path / 'owners.sqlite3')
        try:
            a = crm.create_customer('alice', {'name': '星河医院'}, time.time())
            b = crm.create_customer('bob', {'name': '机密医院'}, time.time())
            record = crm.create_record('alice', {'title': '沟通', 'content': '需要数据库加密'}, time.time())
            service = CustomerService(crm, Parser(), asyncio.Lock())
            with pytest.raises(KeyError):
                await service.handle('bob', 'r1', '', selected_customer_id=b['id'], record_id=record['id'], force=True)
            with pytest.raises(KeyError):
                await service.handle('alice', 'r2', '', selected_customer_id=b['id'], record_id=record['id'], force=True)
            result = await service.handle('alice', 'r3', '', selected_customer_id=a['id'], record_id=record['id'], force=True)
            assert result.get('draft')
            assert '机密医院' not in json.dumps(service.parser.calls[0][1], ensure_ascii=False)
        finally:
            crm.close()
    asyncio.run(run())


def test_manual_edit_during_inference_is_not_overwritten(tmp_path):
    async def run():
        crm = CustomerStore(tmp_path / 'concurrent.sqlite3')
        try:
            customer = crm.create_customer('owner', {'name': '星河医院'}, time.time())
            record = crm.create_record('owner', {'title': '沟通', 'content': '需要数据库加密', 'customer_id': customer['id']}, time.time())
            class Concurrent(Parser):
                async def parse(self, text, now, context=None):
                    crm.update_record('owner', record['id'], {'content': '人工修正后的内容'}, time.time())
                    return update_document()
            service = CustomerService(crm, Concurrent(), asyncio.Lock())
            result = await service.handle('owner', 'r1', '', record_id=record['id'], force=True)
            assert result['record_id'] == record['id'] and '已被修改' in result['message']
            assert not result.get('draft')
            assert crm.get_record('owner', record['id'])['content'] == '人工修正后的内容'
        finally:
            crm.close()
    asyncio.run(run())


def test_model_failure_and_unknown_customer_return_durable_record_and_candidates(tmp_path):
    async def run():
        crm = CustomerStore(tmp_path / 'failure.sqlite3')
        try:
            crm.create_customer('owner', {'name': '华辰科技有限公司'}, time.time())
            class Failure:
                async def parse(self, text, now, context=None):
                    raise ValueError('private-provider-payload')
            service = CustomerService(crm, Failure(), asyncio.Lock())
            result = await service.handle('owner', 'f1', '华辰那边要数据库加密', force=True)
            assert crm.get_record('owner', result['record_id'])['original_content'] == '华辰那边要数据库加密'
            assert 'private-provider-payload' not in result['message']
            assert result['candidates'][0]['name'] == '华辰科技有限公司'
            service.parser = Parser(update_document('华辰'))
            unresolved = await service.handle('owner', 'f2', '补充客户华辰，需要数据库加密', force=True)
            assert unresolved['record_id']
            assert not unresolved.get('draft')
            assert crm.get_record('owner', unresolved['record_id'])['customer_id'] is None
        finally:
            crm.close()
    asyncio.run(run())


def test_note_includes_multiple_customers_without_silently_assigning_first(tmp_path):
    async def run():
        crm = CustomerStore(tmp_path / 'ambiguous.sqlite3')
        try:
            for name in ('星河医院', '海云医院'):
                crm.create_customer('owner', {'name': name}, time.time())
            service = CustomerService(crm, Parser(document()), asyncio.Lock())
            result = await service.handle('owner', 'n1', '今天交流了星河医院和海云医院的项目，王总关注加密，李总关注采购。')
            assert result['customer_id'] is None
            assert len(result['candidates']) == 2
            assert crm.get_record('owner', result['record_id'])['customer_id'] is None
        finally:
            crm.close()
    asyncio.run(run())


def test_reprocessing_saved_control_text_cannot_confirm_customer(tmp_path):
    async def run():
        crm = CustomerStore(tmp_path / 'controls.sqlite3')
        try:
            service = CustomerService(crm, Parser(document()), asyncio.Lock())
            draft = crm.create_customer_draft('owner', {'intent': 'create', 'customer_name': '星河医院',
                                                       'source_text': '新建客户星河医院'}, time.time())
            record = crm.create_record('owner', {'title': '引用命令', 'content': '确认客户 C1'}, time.time())
            await service.handle('owner', 'r1', '', record_id=record['id'], force=True)
            assert crm.get_customer_draft('owner', draft['id'])['status'] == 'pending'
            assert crm.list_customers('owner')['total'] == 0
        finally:
            crm.close()
    asyncio.run(run())


def test_data_layer_explicit_selection_requires_owned_link_and_exact_content_snapshot(tmp_path):
    crm = CustomerStore(tmp_path / 'data-boundary.sqlite3')
    try:
        customer = crm.create_customer('owner', {'name': '星河医院'}, time.time())
        other = crm.create_customer('owner', {'name': '其他医院'}, time.time())
        record = crm.create_record('owner', {'title': '原话', 'content': '他需要数据库加密', 'customer_id': customer['id']}, time.time())
        payload = {**update_document(), 'customer_id': customer['id'], 'source_text': record['original_content'],
                   'source_content': record['content'], 'source_snapshot': crm.record_snapshot(record), 'selected_customer': True}
        for field in ('note_text', 'question'):
            payload.pop(field)
        with pytest.raises(ValueError):
            crm.create_customer_draft('owner', payload, time.time())
        with pytest.raises(KeyError):
            crm.create_customer_draft('other-owner', payload, time.time(), source_record_id=record['id'])
        with pytest.raises(ValueError):
            crm.create_customer_draft('owner', {**payload, 'customer_id': other['id'], 'customer_name': other['name']}, time.time(), source_record_id=record['id'])
        with pytest.raises(ValueError):
            crm.create_customer_draft('owner', {**payload, 'source_content': '他需要数据脱敏'}, time.time(), source_record_id=record['id'])
        with pytest.raises(ValueError):
            crm.create_customer_draft('owner', {**payload, 'source_snapshot': '0' * 64}, time.time(), source_record_id=record['id'])
        accepted = crm.create_customer_draft('owner', payload, time.time(), source_record_id=record['id'])
        assert accepted['status'] == 'pending'
    finally:
        crm.close()


def test_coach_runs_after_note_and_confirmation_without_waiting_for_inference(tmp_path):
    async def run():
        crm = CustomerStore(tmp_path / 'coach-hook.sqlite3')
        try:
            customer = crm.create_customer('owner', {'name': '星河医院'}, time.time())
            class Coach:
                def __init__(self):
                    self.calls = []
                def view(self, owner, customer_id):
                    return {'recommendation': None}
                def schedule(self, owner, customer_id):
                    self.calls.append((owner, customer_id))
            coach = Coach()
            service = CustomerService(crm, Parser(document()), asyncio.Lock(), coach=coach)
            await service.handle('owner', 'n1', '今天拜访星河医院，聊了数据库加密')
            assert coach.calls == [('owner', customer['id'])]
            service.parser = Parser()
            drafted = await service.handle('owner', 'n2', '需要数据库加密', selected_customer_id=customer['id'])
            service.decide('owner', drafted['draft']['id'], True)
            assert len(coach.calls) == 2
        finally:
            crm.close()
    asyncio.run(run())
