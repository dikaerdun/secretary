import asyncio

import pytest

from secretary.customer_store import CustomerStore
from secretary.materials import MaterialService


NOW = 1800000000.0


class Organizer:
    async def organize(self, text, now, context):
        quote = text.rstrip('。')
        owner = '客户' if text.startswith('客户') else '我'
        return {'summary': text, 'key_points': [], 'open_questions': [], 'actions': [
            {'title': '发送方案', 'kind': 'commitment', 'evidence': quote, 'reason': '原话明确约定',
             'owner_hint': owner, 'remind_at': now + 86400 if '明天' in text else None}]}


@pytest.fixture
def services(tmp_path):
    crm = CustomerStore(tmp_path / 'materials.sqlite3')
    materials = MaterialService(crm, asyncio.Lock(), organizer=Organizer(), clock=lambda: NOW)
    yield crm, materials
    crm.close()


def material(materials, text):
    saved = materials.enqueue('alice', {'provider': 'manual', 'title': '行动原话', 'text': text, 'occurred_at': NOW})
    while asyncio.run(materials.process_one()):
        pass
    return materials.detail('alice', saved['id'])


def test_material_explicit_duration_is_atomic_and_survives_reopen(services):
    crm, materials = services
    detail = material(materials, '我答应发送方案，明天，预计60分钟。')
    action = detail['analysis']['actions'][0]
    result = materials.adopt('alice', detail['material']['id'], action['id'], detail['material']['revision'])
    assert result['proposal']['duration_minutes'] == 60
    assert result['record']['action_terms']['duration_minutes'] == 60
    assert materials.adopt('alice', detail['material']['id'], action['id'], detail['material']['revision']) == result
    reopened = MaterialService(crm, materials.lock, organizer=Organizer(), clock=lambda: NOW)
    assert reopened.detail('alice', detail['material']['id'])['analysis']['actions'][0]['duration_minutes'] == 60


def test_material_customer_commitment_does_not_create_personal_proposal(services):
    crm, materials = services
    detail = material(materials, '客户答应发送方案，明天，预计60分钟。')
    action = detail['analysis']['actions'][0]
    result = materials.adopt('alice', detail['material']['id'], action['id'], detail['material']['revision'])
    assert result['proposal'] is None
    assert result['record']['action_terms']['executor_kind'] == 'customer'
    assert crm._db.execute('SELECT COUNT(*) FROM proposals').fetchone()[0] == 0


def test_missing_duration_keeps_explicit_default_distinct_from_source_fact(services):
    _, materials = services
    detail = material(materials, '我答应发送方案，明天。')
    action = detail['analysis']['actions'][0]
    result = materials.adopt('alice', detail['material']['id'], action['id'], detail['material']['revision'])
    assert action['duration_minutes'] is None
    assert result['proposal']['duration_minutes'] == 30
    assert result['record']['action_terms']['duration_minutes'] is None


def test_original_transcript_and_each_version_are_immutable_and_owner_scoped(services):
    _, materials = services
    original = material(materials, '我答应发送方案。')
    updated = materials.update('alice', original['material']['id'],
        {'revision': original['material']['revision'], 'text': '更正：我答应发送方案，明天。'})
    while asyncio.run(materials.process_one()):
        pass
    detail = materials.detail('alice', updated['id'])
    assert detail['text'] == '更正：我答应发送方案，明天。'
    assert detail['original_version']['text'] == '我答应发送方案。'
    assert materials.get_version('alice', updated['id'], original['original_version']['id'])['text'] == '我答应发送方案。'
    with pytest.raises(KeyError):
        materials.get_version('bob', updated['id'], original['original_version']['id'])
    other = materials.enqueue('alice', {'provider': 'manual', 'title': '另一材料', 'text': '原文不相同'})
    with pytest.raises(KeyError):
        materials.get_version('alice', other['id'], original['original_version']['id'])
