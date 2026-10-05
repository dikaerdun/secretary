"""Persistent suggestion archives use isolated fictional data, never formal CRM."""
import pytest
from secretary.customer_store import CustomerStore
from secretary.sales_workspace import SalesWorkspace

NOW=1800000000.0

@pytest.fixture
def context(tmp_path):
    crm=CustomerStore(tmp_path/'priority-archive-fictional.sqlite3')
    sales=SalesWorkspace(crm,clock=lambda:NOW)
    customer=crm.create_customer('alice',{'name':'演练单位（虚构）'},NOW)
    record=crm.create_record('alice',{'title':'临时讨论方向','content':'先核实交流范围','kind':'action','status':'following','customer_id':customer['id']},NOW)
    yield crm,sales,record
    crm.close()

def test_archived_suggestion_stays_hidden_when_source_changes_and_can_restore(context):
    crm,sales,record=context
    item=sales.priorities('alice')['items'][0]
    sales.decide_priority('alice',{'key':item['key'],'signature':item['signature'],'decision':'archive'})
    assert sales.priorities('alice')['total']==0
    crm.update_record('alice',record['id'],{'title':'修正后的临时方向'},NOW+1)
    assert sales.priorities('alice')['total']==0
    archive=sales.priority_archives('alice')['items'][0]
    assert archive['item']['talk']=='临时讨论方向' and archive['revision']==1
    assert crm.get_record('alice',record['id'])['status']=='following'
    with pytest.raises(KeyError): sales.restore_priority_archive('bob',{'key':item['key'],'revision':archive['revision']})
    with pytest.raises(ValueError): sales.restore_priority_archive('alice',{'key':item['key'],'revision':0})
    sales.restore_priority_archive('alice',{'key':item['key'],'revision':archive['revision']})
    assert sales.priority_archives('alice')['total']==0
    assert sales.priorities('alice')['items'][0]['talk']=='修正后的临时方向'

def test_archive_rejects_stale_signature_and_persists_on_reopen(context):
    crm,sales,record=context
    item=sales.priorities('alice')['items'][0]
    with pytest.raises(ValueError): sales.decide_priority('alice',{'key':item['key'],'signature':'stale','decision':'archive'})
    assert sales.priority_archives('alice')['total']==0
    sales.decide_priority('alice',{'key':item['key'],'signature':item['signature'],'decision':'archive'})
    reopened=SalesWorkspace(crm,clock=lambda:NOW+864000)
    assert reopened.priorities('alice')['total']==0
    assert reopened.priority_archives('alice')['total']==1
    assert reopened.priority_archives('bob')['total']==0
