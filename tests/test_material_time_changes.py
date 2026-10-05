"""Independent source oracles for later changes to an earlier sales promise."""

import asyncio
from datetime import datetime

import pytest

from secretary.customer_store import CustomerStore
from secretary.materials import MaterialService
from secretary.store import SHANGHAI


NOW = datetime(2026, 10, 1, 10, tzinfo=SHANGHAI).timestamp()
OLD_TIME = datetime(2026, 10, 2, 15, tzinfo=SHANGHAI).timestamp()
PROMISE = '我答应明天下午三点给王总发报价'


class PromiseExtractor:
    """First-pass extraction only; the later change is deliberately separate."""

    def __init__(self):
        self.calls = []

    async def organize(self, text, now, context):
        self.calls.append(text)
        actions = []
        if PROMISE in text:
            actions.append({'title': '给王总发报价', 'kind': 'commitment',
                            'reason': '原话：“' + PROMISE + '”',
                            'owner_hint': '我', 'remind_at': OLD_TIME})
        return {'summary': '交流记录', 'key_points': [], 'open_questions': [],
                'actions': actions}


class ChangeReviewer(PromiseExtractor):
    def __init__(self, change_clause):
        super().__init__()
        self.change_clause = change_clause
        self.review_calls = []

    async def reconcile_material(self, payload):
        self.review_calls.append(payload)
        assert any(clause['text'] == self.change_clause
                   for clause in payload['later_clauses'])
        assert any(action['title'] == '给王总发报价'
                   and action['evidence'] == PROMISE
                   for action in payload['actions'])
        return {'decisions': [{'action_key': action['action_key'],
                               'decision': 'review',
                               'resolution_evidence': self.change_clause}
                              for action in payload['actions']]}


@pytest.mark.parametrize('change', [
    '改到后天下午三点再发报价',
    '调整到后天下午三点再发报价',
    '推迟到后天下午三点再发报价',
    '提前到今天下午三点再发报价',
    '改一下时间，后天下午三点再发报价',
])
@pytest.mark.parametrize('has_reviewer', [True, False])
def test_later_time_change_cannot_keep_original_scheduled_promise(tmp_path, change, has_reviewer):
    clause = '王总说' + change
    source = PROMISE + '。' + '背景。' * 1600 + clause + '。'
    organizer = ChangeReviewer(clause) if has_reviewer else PromiseExtractor()
    crm = CustomerStore(tmp_path / 'time-change.sqlite3')

    async def scenario():
        service = MaterialService(crm, asyncio.Lock(), organizer=organizer,
                                  clock=lambda: NOW)
        material = service.enqueue('alice', {'provider': 'manual', 'title': '报价改期记录',
                                            'text': source, 'occurred_at': NOW})
        assert await service.process_one() is True
        detail = service.detail('alice', material['id'])
        assert detail['material']['status'] == 'review'
        assert detail['text'] == source
        # The source oracle requires distinct extraction calls, so this cannot
        # accidentally pass solely through within-paragraph model handling.
        assert any(PROMISE in text for text in organizer.calls)
        assert any(clause in text for text in organizer.calls)
        assert all(not (PROMISE in text and clause in text) for text in organizer.calls)
        actions = detail['analysis']['actions']
        assert len(actions) == 1
        assert actions[0]['title'] == '给王总发报价'
        assert actions[0]['kind'] == 'suggestion'
        assert actions[0]['remind_at'] is None
        if has_reviewer:
            assert organizer.review_calls
            assert actions[0]['review_evidence'] == clause
        else:
            assert any('语义复核未完成' in warning
                       for warning in detail['analysis']['warnings'])
        for table in ('tasks', 'proposals', 'notifications'):
            assert crm._db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] == 0
        await service.close()

    try:
        asyncio.run(scenario())
    finally:
        crm.close()
