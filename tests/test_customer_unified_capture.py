"""Capture returns one durable review result without silently enabling reminders."""
import asyncio
from datetime import datetime

import pytest

from secretary.customer_service import CustomerService
from secretary.customer_store import CustomerStore
from secretary.parser import SHANGHAI


NOW = datetime(2026, 9, 30, 10, tzinfo=SHANGHAI).timestamp()
WHEN = datetime(2026, 10, 1, 15, tzinfo=SHANGHAI).timestamp()
SOURCE = '补充客户星河医院，关注数据库加密，我答应明天下午三点发送方案。'


class Parser:
    def __init__(self, intent='update'):
        self.intent = intent
        self.calls = []

    async def parse(self, text, now, context=None):
        self.calls.append((text, context))
        return {'intent': self.intent, 'customer_name': '星河医院', 'contact_name': None,
                'basic': {}, 'basic_evidence': {}, 'contact': {}, 'contact_evidence': {},
                'attributes': ([{'target': 'account', 'key': 'crypto_needs', 'value': '数据库加密',
                                 'evidence': '数据库加密', 'basis': 'reported'}]
                               if self.intent in ('create', 'update') else []),
                'note_text': text, 'question': None}


class Organizer:
    def __init__(self, when=WHEN, kind='commitment', fail=False):
        self.when, self.kind, self.fail = when, kind, fail
        self.calls = []

    async def organize(self, text, now, context):
        self.calls.append((text, context))
        if self.fail:
            raise RuntimeError('private-provider-output')
        return {'summary': '客户关注数据库加密，约定发送方案。', 'key_points': [], 'open_questions': [],
                'actions': [{'title': '发送方案', 'kind': self.kind, 'reason': '原话明确约定',
                             'owner_hint': '我', 'remind_at': self.when,
                             'evidence': text, 'time_evidence': ('后天下午三点' if '后天下午三点' in text else '明天下午三点')}]}


@pytest.fixture
def crm(tmp_path):
    store = CustomerStore(tmp_path / 'unified.sqlite3')
    yield store
    store.close()


def service(crm, parser=None, organizer=None):
    return CustomerService(crm, parser or Parser(), asyncio.Lock(), clock=lambda: NOW,
                           organizer=organizer or Organizer())


def test_mixed_update_returns_profile_analysis_and_pending_precise_schedule(crm):
    async def run():
        customer = crm.create_customer('owner', {'name': '星河医院'}, NOW)
        organizer = Organizer()
        secretary = service(crm, organizer=organizer)
        result = await secretary.handle('owner', 'mixed', SOURCE, source='voice')
        assert len(organizer.calls) == 1
        assert result['draft']['status'] == 'pending'
        assert result['analysis']['actions'][0]['title'] == '发送方案'
        assert result['actions'][0]['adopted_record_id']
        assert result['proposals'][0]['status'] == 'pending'
        assert result['proposals'][0]['remind_at'] == WHEN
        assert '2026-10-01 15:00' in result['message'] and '待确认 P' in result['message']
        child = crm.get_record('owner', result['actions'][0]['adopted_record_id'])
        # Unique existing-customer attribution is saved before the C baseline;
        # profile facts themselves still await the separate confirmation.
        assert child['customer_id'] == customer['id']
        assert crm._db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0
        assert crm._db.execute('SELECT COUNT(*) FROM notifications').fetchone()[0] == 0
        assert crm.profile('owner', customer['id'])['fields'] == []
        secretary.decide('owner', result['draft']['id'], True)
        assert crm.get_record('owner', child['id'])['customer_id'] == customer['id']
    asyncio.run(run())


@pytest.mark.parametrize('when,kind', [(None, 'commitment'), (WHEN, 'suggestion'), (NOW - 1, 'commitment')])
def test_undated_suggestions_and_past_times_never_prepare_reminders(crm, when, kind):
    async def run():
        crm.create_customer('owner', {'name': '星河医院'}, NOW)
        result = await service(crm, organizer=Organizer(when, kind)).handle('owner', 'review', SOURCE)
        assert result['analysis'] and result['actions']
        assert result['proposals'] == []
        assert result['actions'][0]['adopted_record_id'] is None
        assert crm._db.execute('SELECT COUNT(*) FROM proposals').fetchone()[0] == 0
        assert crm._db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0
    asyncio.run(run())


def test_completed_note_replay_keeps_summary_actions_and_ids_without_reparsing(crm):
    async def run():
        crm.create_customer('owner', {'name': '星河医院'}, NOW)
        parser, organizer = Parser('note'), Organizer()
        secretary = service(crm, parser, organizer)
        first = await secretary.handle('owner', 'note', SOURCE, source='voice')
        replay = await secretary.handle('owner', 'note', SOURCE, source='voice')
        assert first == replay
        assert len(parser.calls) == len(organizer.calls) == 1
        assert replay['analysis']['summary'] in replay['message']
        assert replay['actions'][0]['proposal_id'] == replay['proposals'][0]['id']
        assert crm._db.execute('SELECT COUNT(*) FROM proposals').fetchone()[0] == 1
        assert crm._db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0
    asyncio.run(run())


def test_failed_organization_final_status_is_durable_and_safe(crm):
    async def run():
        crm.create_customer('owner', {'name': '星河医院'}, NOW)
        organizer = Organizer(fail=True)
        secretary = service(crm, Parser('note'), organizer)
        result = await secretary.handle('owner', 'failure', SOURCE)
        assert '自动整理暂未完成' in result['message']
        assert 'private-provider-output' not in result['message']
        assert await secretary.handle('owner', 'failure', SOURCE) == result
        assert len(organizer.calls) == 1
        assert crm.get_record('owner', result['record_id'])['original_content'] == SOURCE
        assert crm._db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0
    asyncio.run(run())


def test_omitted_customer_uses_existing_and_explicit_none_clears_without_reattaching(crm):
    async def run():
        customer = crm.create_customer('owner', {'name': '星河医院'}, NOW)
        record = crm.create_record('owner', {'title': '现场交流', 'content': SOURCE,
                                            'customer_id': customer['id']}, NOW)
        secretary = service(crm, Parser('note'), Organizer(None))
        unchanged = await secretary.handle('owner', 'omitted', '', record_id=record['id'], force=True)
        assert unchanged['customer_id'] == customer['id']
        cleared = await secretary.handle('owner', 'clear', '', selected_customer_id=None,
                                         record_id=record['id'], force=True)
        assert cleared['customer_id'] is None
        assert crm.get_record('owner', record['id'])['customer_id'] is None
        assert crm.get_record('owner', record['id'])['original_content'] == SOURCE
        assert not cleared.get('draft')
    asyncio.run(run())


def test_explicit_clear_still_checks_record_owner_before_mutation(crm):
    async def run():
        customer = crm.create_customer('alice', {'name': '星河医院'}, NOW)
        record = crm.create_record('alice', {'title': '客户交流', 'content': SOURCE,
                                            'customer_id': customer['id']}, NOW)
        with pytest.raises(KeyError):
            await service(crm).handle('bob', 'clear', '', selected_customer_id=None,
                                      record_id=record['id'], force=True)
        assert crm.get_record('alice', record['id'])['customer_id'] == customer['id']
    asyncio.run(run())


def test_corrected_active_reminder_is_disclosed_and_not_duplicated(crm):
    async def run():
        customer = crm.create_customer('owner', {'name': '星河医院'}, NOW)
        record = crm.create_record('owner', {'title': '发送原方案', 'content': SOURCE,
                                            'customer_id': customer['id']}, NOW)
        crm.execute('owner', 'p', {'action': 'propose', 'title': record['title'], 'remind_at': WHEN}, NOW)
        crm.link_proposal('owner', record['id'], 1, NOW)
        crm.execute('owner', 'confirm', {'action': 'confirm', 'proposal_id': 1}, NOW)
        crm.update_record('owner', record['id'], {'content': '更正：约定后天下午四点发送方案'}, NOW + 1)
        result = await service(crm, Parser('note'), Organizer(WHEN + 90000)).handle(
            'owner', 'correct', '', record_id=record['id'], force=True)
        assert result['active_reminder']['title'] == '发送原方案'
        assert result['active_reminder']['record_id'] == record['id']
        assert result['requires_reminder_review'] is True
        assert '现存提醒尚未更正' in result['message']
        assert '2026-10-01 15:00' in result['message']
        assert result['proposals'] == []
        assert crm._db.execute('SELECT COUNT(*) FROM proposals').fetchone()[0] == 1
        assert crm.record_detail('owner', record['id'])['task']['remind_at'] == WHEN
    asyncio.run(run())


def test_confirmed_child_reminder_is_disclosed_when_correcting_parent_note(crm):
    async def run():
        crm.create_customer('owner', {'name': '星河医院'}, NOW)
        secretary = service(crm, Parser('note'), Organizer())
        result = await secretary.handle('owner', 'original', SOURCE)
        proposal = result['proposals'][0]
        crm.execute('owner', 'confirm', {'action': 'confirm', 'proposal_id': proposal['id']}, NOW)
        crm.update_record('owner', result['record_id'], {'content': SOURCE + '更正时间。'}, NOW + 1)
        correction = await secretary.handle('owner', 'correction', '', record_id=result['record_id'], force=True)
        assert correction['active_reminder']['title'] == '发送方案'
        assert correction['active_reminder']['record_id'] == result['actions'][0]['record_id']
        assert len(correction['active_reminders']) == 1
        assert '现存提醒尚未更正' in correction['message']
        assert crm._db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1
        assert crm._db.execute('SELECT COUNT(*) FROM proposals').fetchone()[0] == 1
    asyncio.run(run())


def test_corrected_pending_schedule_replaces_old_review_version_without_enabling_tasks(crm):
    async def run():
        crm.create_customer('owner', {'name': '星河医院'}, NOW)
        organizer = Organizer()
        secretary = service(crm, Parser('note'), organizer)
        original = await secretary.handle('owner', 'original', SOURCE)
        old_proposal = original['proposals'][0]
        old_child = original['actions'][0]['record_id']
        # The corrected action's time evidence must occur in the same source
        # clause as its title; an isolated appended date is not sufficient.
        crm.update_record('owner', original['record_id'], {'content': SOURCE.replace('明天下午三点', '后天下午三点')}, NOW + 1)
        organizer.when = WHEN + 86400
        corrected = await secretary.handle('owner', 'corrected', '', record_id=original['record_id'], force=True)
        assert corrected['analysis']['version'] > original['analysis']['version']
        assert corrected['actions'][0]['id'] != original['actions'][0]['id']
        assert corrected['proposals'][0]['id'] != old_proposal['id']
        assert corrected['proposals'][0]['status'] == 'pending'
        assert corrected['proposals'][0]['remind_at'] == WHEN + 86400
        assert crm.get_proposal('owner', old_proposal['id'])['status'] == 'rejected'
        assert crm.get_record('owner', old_child)['status'] == 'done'
        assert crm._db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0
        assert crm._db.execute('SELECT COUNT(*) FROM notifications').fetchone()[0] == 0
    asyncio.run(run())


def test_new_customer_confirmation_keeps_same_exchange_actions_reviewable(crm):
    async def run():
        secretary = service(crm, Parser('create'), Organizer(None))
        result = await secretary.handle('owner', 'new-account', SOURCE.replace('补充', '新建'))
        assert result['draft']['status'] == 'pending'
        confirmed = secretary.decide('owner', result['draft']['id'], True)
        analysis = crm.get_analysis('owner', result['record_id'])
        assert analysis['stale'] is False
        child = crm.adopt_action('owner', result['record_id'], analysis['actions'][0]['id'], NOW)
        assert child['customer_id'] == confirmed['customer_id']
        assert crm._db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0
    asyncio.run(run())


def test_visit_brief_filters_archived_contacts_before_taking_active_contacts(crm):
    customer = crm.create_customer('owner', {'name': '星河医院'}, NOW)
    for index in range(6):
        old = crm.create_contact('owner', customer['id'], {'name': f'旧联系人{index}'}, NOW)
        crm.update_contact('owner', customer['id'], old['id'], {'archived': True}, NOW + 1)
    crm.create_contact('owner', customer['id'], {'name': '当前技术负责人'}, NOW + 2)
    brief = CustomerService.render_brief(crm.profile('owner', customer['id']))
    assert '旧联系人' not in brief
    assert '当前技术负责人' in brief
