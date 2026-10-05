"""Everyday spoken commitments should not require a special command phrase."""
from secretary.organizer import validate_organization


def result(evidence):
    return {'summary': '我的后续计划', 'key_points': [], 'open_questions': [], 'actions': [
        {'title': '去拜访王总', 'kind': 'commitment', 'owner_hint': '我',
         'reason': '我的明确计划', 'evidence': evidence, 'time_evidence': '', 'remind_at': None}]}


def test_direct_spoken_own_plan_keeps_commitment_but_does_not_invent_time():
    evidence = '我明天去拜访王总。'
    organized = validate_organization(result(evidence), evidence, 1790841600)
    assert organized['actions'][0]['kind'] == 'commitment'
    assert organized['actions'][0]['remind_at'] is None


def test_cancelled_spoken_own_plan_is_not_resurrected():
    evidence = '我明天去拜访王总，这个计划取消了。'
    organized = validate_organization(result(evidence), evidence, 1790841600)
    assert not any(a['kind'] == 'commitment' for a in organized['actions'])
