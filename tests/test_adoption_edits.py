import pytest

from secretary.adoption_edits import edit_unconfirmed_action
from secretary.customer_store import CustomerStore

NOW = 1791082800


def test_user_draft_edit_is_atomic_preserves_original_and_rejects_pending(tmp_path):
    crm = CustomerStore(tmp_path / 'synthetic-edits.sqlite3')
    record = crm.create_record('me', {'title': '原行动', 'content': '原录音依据', 'kind': 'action'}, NOW)
    original = record['original_content']
    crm.save_action_terms('me', record['id'], {'executor_kind': 'self'}, NOW)
    reply = crm.execute('me', 'pending-original', {'action': 'propose', 'title': record['title'], 'remind_at': NOW+3600}, NOW)
    import re
    identifier = int(re.search(r'P(\d+)', reply)[1])
    record = crm.link_proposal('me', record['id'], identifier, NOW)
    result = edit_unconfirmed_action(crm, 'me', record['id'], {'title': '用户改为先核实预算', 'execution_at': None}, NOW+1,
                                    expected_updated_at=record['updated_at'], expected_terms_updated_at=record['terms_updated_at'])
    assert result['title'] == '用户改为先核实预算' and result['original_content'] == original
    assert result['proposal_id'] is None and crm.get_proposal('me', identifier)['status'] == 'rejected'
    again = edit_unconfirmed_action(crm, 'me', record['id'], {'title': result['title'], 'execution_at': None}, NOW+2,
                                   expected_updated_at=result['updated_at'], expected_terms_updated_at=result['terms_updated_at'])
    assert again['updated_at'] == result['updated_at']
    with pytest.raises(ValueError):
        edit_unconfirmed_action(crm, 'me', record['id'], {'title': '旧页面覆盖'}, NOW+3, expected_updated_at=record['updated_at'])
    assert crm.get_record('me', record['id'])['title'] == result['title']
    with pytest.raises(KeyError):
        edit_unconfirmed_action(crm, 'other', record['id'], {'title': '不属于我'}, NOW+3, expected_updated_at=result['updated_at'])
    crm.close()


def test_confirmed_action_cannot_be_changed_through_draft_seam(tmp_path):
    crm = CustomerStore(tmp_path / 'synthetic-confirmed.sqlite3')
    record = crm.create_record('me', {'title': '已安排拜访', 'content': '双方约定', 'kind': 'action'}, NOW)
    import re
    identifier = int(re.search(r'P(\d+)', crm.execute('me', 'propose', {'action': 'propose', 'title': record['title'], 'remind_at': NOW+3600}, NOW))[1])
    crm.link_proposal('me', record['id'], identifier, NOW)
    crm.execute('me', 'confirm', {'action': 'confirm', 'proposal_id': identifier}, NOW)
    record = crm.get_record('me', record['id'])
    with pytest.raises(ValueError, match='正式日程'):
        edit_unconfirmed_action(crm, 'me', record['id'], {'title': '悄悄改期'}, NOW+1, expected_updated_at=record['updated_at'])
    assert crm.get_record('me', record['id'])['title'] == record['title']
    crm.close()


def test_absent_terms_snapshot_cannot_overwrite_newly_added_terms(tmp_path):
    crm = CustomerStore(tmp_path / 'synthetic-new-terms.sqlite3')
    record = crm.create_record('me', {'title': '核实要求', 'content': '原话', 'kind': 'action'}, NOW)
    assert record['terms_updated_at'] is None
    crm.save_action_terms('me', record['id'], {'executor_kind': 'customer'}, NOW+1)
    with pytest.raises(ValueError, match='责任与时间'):
        edit_unconfirmed_action(crm, 'me', record['id'], {'executor_kind': 'self'}, NOW+2,
            expected_updated_at=record['updated_at'], expected_terms_updated_at=None)
    assert crm.get_record('me', record['id'])['action_terms']['executor_kind'] == 'customer'
    crm.close()
