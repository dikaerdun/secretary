from datetime import datetime
import pytest
from secretary.crm import CRMStore
from secretary.planning import planning_nodes
from secretary.store import SHANGHAI


NOW=datetime(2026,10,3,9,tzinfo=SHANGHAI).timestamp()


def test_date_only_deadline_and_check_do_not_allocate_time_or_reminders(tmp_path):
    crm=CRMStore(tmp_path/'plan.sqlite')
    r=crm.create_record('me',{'title':'等客户清单','kind':'action','content':'客户说10月5日前提供清单'},NOW)
    crm.save_action_terms('me',r['id'],{'executor_kind':'customer','deadline_date':'2026-10-05',
        'deadline_evidence':'10月5日前提供清单','check_date':'2026-10-06','check_evidence':'6日检查进度'},NOW)
    nodes=planning_nodes(crm,'me',period='month',now=NOW)
    assert {i['kind'] for i in nodes}=={'deadline','check'}
    assert all(i['date_only'] and i['at'] is None for i in nodes)
    assert planning_nodes(crm,'me',period='day',now=NOW)==[]
    assert len(planning_nodes(crm,'me',period='day',date='2026-10-05',now=NOW))==1
    assert planning_nodes(crm,'other',period='month',now=NOW)==[]
    assert crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0]==0
    assert crm._db.execute('SELECT count(*) FROM notifications').fetchone()[0]==0
    crm.close()


def test_explicit_metadata_edit_guard_and_automatic_replay_does_not_overwrite(tmp_path):
    crm=CRMStore(tmp_path/'plan.sqlite')
    r=crm.create_record('me',{'title':'方案','kind':'action'},NOW)
    first=crm.save_action_terms('me',r['id'],{'executor_kind':'unknown'},NOW)
    updated=crm.save_action_terms('me',r['id'],{'executor_kind':'self','deadline_date':'2026-10-12'},NOW,
        explicit=True,expected_updated_at=first['terms_updated_at'])
    with pytest.raises(ValueError,match='已有变化'):
        crm.save_action_terms('me',r['id'],{'executor_kind':'customer'},NOW,explicit=True,
            expected_updated_at=first['terms_updated_at'])
    replay=crm.save_action_terms('me',r['id'],{'executor_kind':'unknown'},NOW+1)
    assert replay['action_terms']==updated['action_terms']
    with pytest.raises(KeyError):crm.save_action_terms('other',r['id'],{},NOW)
    assert crm.list_records('me')['items'][0]['action_terms']['deadline_date']=='2026-10-12'
    crm.close()


def test_original_transcript_immutable_and_restart_safe(tmp_path):
    path=tmp_path/'plan.sqlite';crm=CRMStore(path)
    r=crm.create_record('me',{'title':'复盘','content':'修正后的客户名'},NOW)
    crm.save_transcript('me',r['id'],'原来转写错了','修正后的客户名',NOW)
    crm.update_record('me',r['id'],{'content':'再次校对客户名'},NOW+1)
    assert crm.record_detail('me',r['id'])['transcript']['corrected_text']=='再次校对客户名'
    with pytest.raises(ValueError,match='不能覆盖'):
        crm.save_transcript('me',r['id'],'新的原文','别的修正',NOW)
    with pytest.raises(KeyError):crm.save_transcript('other',r['id'],'a','b',NOW)
    crm.close();crm=CRMStore(path)
    assert crm.record_detail('me',r['id'])['transcript']['original_text']=='原来转写错了'
    assert crm.record_detail('other',r['id']) is None
    crm.close()


def test_completed_or_cancelled_appointment_keeps_requirements_until_follow_up_complete(tmp_path):
    from secretary.sales_workspace import SalesWorkspace
    crm=CRMStore(tmp_path/'completed.sqlite'); sales=SalesWorkspace(crm,clock=lambda:NOW)
    c=crm.create_customer('me',{'name':'测试客户'},NOW)
    r=crm.create_record('me',{'title':'发送方案','kind':'action','customer_id':c['id']},NOW)
    crm.save_action_terms('me',r['id'],{'deadline_date':'2026-10-05'},NOW)
    crm.execute('me','new',{'action':'propose','title':r['title'],'remind_at':NOW+3600},NOW)
    p=crm._db.execute('SELECT id FROM proposals').fetchone()[0]
    crm.link_proposal('me',r['id'],p,NOW);crm.execute('me','confirm',{'action':'confirm','proposal_id':p},NOW)
    task=crm.record_detail('me',r['id'])['task']
    crm.execute('me','cancel',{'action':'cancel','task_id':task['id']},NOW)
    assert len(sales.workbench('me',c['id'])['open_actions'])==1
    assert len(planning_nodes(crm,'me',period='month',now=NOW))==1
    # The time slot ends; the follow-up remains open until its result is recorded.
    crm.execute('me','new2',{'action':'propose','title':r['title'],'remind_at':NOW+7200},NOW)
    p2=crm._db.execute('SELECT max(id) FROM proposals').fetchone()[0]
    crm.link_proposal('me',r['id'],p2,NOW);crm.execute('me','confirm2',{'action':'confirm','proposal_id':p2},NOW)
    current=crm.record_detail('me',r['id'])['task']
    crm.execute('me','done',{'action':'complete','task_id':current['id']},NOW)
    assert len(sales.workbench('me',c['id'])['open_actions'])==1
    assert len(planning_nodes(crm,'me',period='month',now=NOW))==1
    sales.complete_record('me',r['id'],{'request_id':'record-result','result':'方案已发送并落实'})
    assert sales.workbench('me',c['id'])['open_actions']==[]
    assert planning_nodes(crm,'me',period='month',now=NOW)==[]
    crm.close()
