import asyncio
import pytest
from secretary.crm import CRMStore,analysis_fingerprint
from secretary.materials import MaterialService
from secretary.visits import VisitService
from secretary.exchange_records import ExchangeRecords
from secretary.review_queue import ReviewQueue


def setup(path):
    crm=CRMStore(path);lock=asyncio.Lock()
    materials=MaterialService(crm,lock,clock=lambda:1791000000.0)
    visits=VisitService(crm,materials,lock)
    return crm,materials,visits,ExchangeRecords(crm,visits,lambda:1791000000.0)


def test_quick_recap_archives_original_identity_and_actions_once(tmp_path):
    crm,m,v,e=setup(tmp_path/'exchange.sqlite')
    c=crm.create_customer('me',{'name':'测试客户'},1791000000)
    r=crm.create_record('me',{'title':'现场复盘','content':'我答应提供方案','customer_id':c['id'],
        'category':'visit_review'},1791000000)
    crm.save_analysis('me',r['id'],{'summary':'拜访后行动','input_fingerprint':analysis_fingerprint(r),
        'actions':[{'title':'提供方案','kind':'commitment','reason':'明确约定','owner_hint':'我',
                    'evidence':'我答应提供方案','remind_at':None}]},1791000000)
    linked=e.auto_archive('me',r['id'])
    assert e.auto_archive('me',r['id'])['visit_id']==linked['visit_id']
    detail=v.detail('me',linked['visit_id'])
    assert detail['visit']['occurred_at'] is None
    assert detail['visit']['source_count']==1
    assert detail['record_sources'][0]['record']['id']==r['id']
    q=ReviewQueue(crm,materials=m,visits=v,clock=lambda:1791000000)
    assert q.list('me')['total']==1
    child=crm.adopt_action('me',r['id'],1,1791000000)
    assert q.list('me')['total']==0
    assert crm._db.execute('SELECT count(*) FROM crm_records').fetchone()[0]==2
    assert crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0]==0
    assert crm.get_record('me',r['id'])['original_content']=='我答应提供方案'
    crm.close()


def test_explicit_archive_guards_owner_customer_and_visit_revision(tmp_path):
    crm,m,v,e=setup(tmp_path/'exchange.sqlite')
    c=crm.create_customer('me',{'name':'客户一'},1791000000)
    other=crm.create_customer('me',{'name':'客户二'},1791000000)
    r=crm.create_record('me',{'title':'复盘','customer_id':c['id']},1791000000)
    visit=v.create('me',{'title':'交流','customer_id':c['id']})
    with pytest.raises(KeyError):e.archive('other',r['id'],{'visit_id':visit['id'],'revision':visit['revision']})
    with pytest.raises(ValueError,match='变化'):e.archive('me',r['id'],{'visit_id':visit['id'],'revision':'old'})
    bad=v.create('me',{'title':'别的客户交流','customer_id':other['id']})
    with pytest.raises(ValueError,match='客户不同'):e.archive('me',r['id'],{'visit_id':bad['id'],'revision':bad['revision']})
    linked=e.archive('me',r['id'],{'visit_id':visit['id'],'revision':visit['revision']})
    assert e.archive('me',r['id'],{'visit_id':visit['id'],'revision':visit['revision']})==linked
    assert v.detail('me',visit['id'])['visit']['revision']!=visit['revision']
    assert crm._db.execute('SELECT count(*) FROM crm_visit_record_history').fetchone()[0]==1
    crm.close()


def test_linked_record_adoption_obeys_complete_exchange_sources(tmp_path):
    crm,m,v,e=setup(tmp_path/'guard.sqlite')
    c=crm.create_customer('me',{'name':'测试客户'},1791000000)
    r=crm.create_record('me',{'title':'复盘','content':'我答应提供方案','customer_id':c['id'],
        'category':'visit_review'},1791000000)
    crm.save_analysis('me',r['id'],{'summary':'约定','input_fingerprint':analysis_fingerprint(r),
        'actions':[{'title':'提供方案','kind':'commitment','reason':'原话','owner_hint':'我',
        'evidence':'我答应提供方案','remind_at':None}]},1791000000)
    link=e.auto_archive('me',r['id']); visit=v.detail('me',link['visit_id'])['visit']
    added=v.add_material('me',visit['id'],{'role':'recording',
        'provider':'manual','title':'尚未整理的现场录音','text':'现场讨论'},source_id='pending')
    q=ReviewQueue(crm,materials=m,visits=v,clock=lambda:1791000000)
    candidate=next(i for i in q.list('me')['items'] if i.get('record_id')==r['id'])
    assert candidate['blocked']
    with pytest.raises(ValueError,match='来源'):
        crm.adopt_action('me',r['id'],1,1791000000)
    detail=v.detail('me',visit['id'])
    source=detail['sources'][0]
    v.decide_source('me',visit['id'],source['linked_material_id'],detail['visit']['revision'],'excluded','录音无关')
    current=v.detail('me',visit['id'])['visit']['revision']
    with pytest.raises(ValueError,match='变化'):
        crm.adopt_action('me',r['id'],1,1791000000,expected_visit_revision=visit['revision'])
    child=crm.adopt_action('me',r['id'],1,1791000000,expected_visit_revision=current)
    assert crm.adopt_action('me',r['id'],1,1791000000,expected_visit_revision='old')['id']==child['id']
    crm.close()


def test_invalid_archive_or_failed_insert_leaves_no_empty_exchange(tmp_path,monkeypatch):
    crm,m,v,e=setup(tmp_path/'atomic.sqlite')
    r=crm.create_record('me',{'title':'复盘'},1791000000)
    with pytest.raises(ValueError):e.archive('me',r['id'],{'note':''})
    assert v.list('me')['total']==0
    crm._db.execute("CREATE TRIGGER fail_link BEFORE INSERT ON crm_visit_records BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):e.archive('me',r['id'])
    assert v.list('me')['total']==0
    assert e.reference('me',r['id']) is None
    crm.close()
