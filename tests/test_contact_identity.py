import pytest

from secretary.customer_store import CustomerStore


@pytest.fixture
def identity_crm(tmp_path):
    crm = CustomerStore(tmp_path / 'identity.sqlite3')
    yield crm
    crm.close()


def test_same_name_in_distinct_departments_is_not_merged(identity_crm):
    crm = identity_crm
    unit = crm.create_customer('owner', {'name': '同名测试银行'}, 1000)
    a = crm.create_contact('owner', unit['id'], {'name': '张工', 'department': '信息中心'}, 1001)
    b = crm.create_contact('owner', unit['id'], {'name': '张工', 'department': '采购部'}, 1002)
    assert a['id'] != b['id']
    assert len(crm.find_contacts_exact('owner', '张工', customer_id=unit['id'])) == 2
    crm.save_fact('owner', unit['id'], {'key': 'concerns', 'value': '技术稳定性', 'basis': 'reported', 'contact_id': a['id']}, 1003)
    profile = crm.profile('owner', unit['id'])
    assert next(person for person in profile['contacts'] if person['id'] == b['id'])['fields'] == []
    with pytest.raises(ValueError):
        crm.update_contact('owner', unit['id'], b['id'], {'department': '信息中心'}, 1004)
    assert crm.profile('owner', unit['id'])['contacts'][1]['department'] == '采购部'


def test_distinct_explicit_phones_disambiguate_same_name(identity_crm):
    crm = identity_crm
    unit = crm.create_customer('owner', {'name': '电话身份测试'}, 1000)
    a = crm.create_contact('owner', unit['id'], {'name': '李经理', 'phone': '13000001001'}, 1001)
    b = crm.create_contact('owner', unit['id'], {'name': '李经理', 'phone': '13000001002'}, 1002)
    assert a['id'] != b['id']
    with pytest.raises(ValueError):
        crm.create_contact('owner', unit['id'], {'name': '李经理', 'phone': '130 0000 1001', 'department': '其他部'}, 1003)


def test_unknown_identity_and_restoring_collision_require_review(identity_crm):
    crm = identity_crm
    unit = crm.create_customer('owner', {'name': '未知身份测试'}, 1000)
    a = crm.create_contact('owner', unit['id'], {'name': '林工'}, 1001)
    with pytest.raises(ValueError):
        crm.create_contact('owner', unit['id'], {'name': '林工', 'department': '信息部'}, 1002)
    crm.update_contact('owner', unit['id'], a['id'], {'archived': True}, 1003)
    crm.create_contact('owner', unit['id'], {'name': '林工'}, 1004)
    with pytest.raises(ValueError):
        crm.update_contact('owner', unit['id'], a['id'], {'archived': False}, 1005)


def test_contact_department_noop_and_restart_preserve_identity(tmp_path):
    path = tmp_path / 'persist.sqlite3'
    crm = CustomerStore(path)
    unit = crm.create_customer('owner', {'name': '重启身份测试'}, 1000)
    contact = crm.create_contact('owner', unit['id'], {'name': '陈工', 'department': '密码应用部'}, 1001)
    noop = crm.update_contact('owner', unit['id'], contact['id'], {'department': '密码应用部'}, 1005)
    assert noop['updated_at'] == contact['updated_at']
    crm.close()
    crm = CustomerStore(path)
    assert crm.profile('owner', unit['id'])['contacts'][0]['department'] == '密码应用部'
    assert crm.find_contacts_exact('other', '陈工') == []
    crm.close()
