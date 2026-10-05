import time
import pytest

from secretary.crm import CRMStore, analysis_fingerprint
from secretary.review_queue import ReviewQueue


NOW = 1791000000.0


def note(crm, owner='me', title='发送方案'):
    record = crm.create_record(owner, {'title':'交流','content':'我答应'+title}, NOW)
    crm.save_analysis(owner, record['id'], {'summary':'明确承诺','actions':[
        {'title':title,'kind':'commitment','reason':'原话','owner_hint':'我','remind_at':None,
         'evidence':'我答应'+title}], 'input_fingerprint':analysis_fingerprint(record)}, NOW)
    return record


def test_large_queue_paginates_all_and_isolates_owner(tmp_path):
    crm = CRMStore(tmp_path/'queue.sqlite')
    try:
        queue = ReviewQueue(crm,clock=lambda:NOW)
        for n in range(205): note(crm,title=f'发送方案{n}')
        note(crm,'other')
        first = queue.list('me',page_size=200)
        second = queue.list('me',page=2,page_size=200)
        assert first['total']==second['total']==205
        assert first['counts']['pending']==205
        keys=[i['key'] for i in first['items']+second['items']]
        assert len(keys)==len(set(keys))==205
        assert queue.list('other')['total']==1
    finally: crm.close()


def test_decisions_survive_restart_and_deferred_reappear(tmp_path):
    path=tmp_path/'queue.sqlite'
    crm=CRMStore(path); current=[NOW]
    note(crm)
    q=ReviewQueue(crm,clock=lambda:current[0]); item=q.list('me')['items'][0]
    q.decide('me',{'key':item['key'],'signature':item['signature'],'decision':'defer','until_at':NOW+60})
    assert q.list('me')['total']==0
    assert q.list('me',state='deferred')['total']==1
    crm.close(); crm=CRMStore(path); q=ReviewQueue(crm,clock=lambda:current[0])
    assert q.list('me',state='deferred')['total']==1
    current[0]+=61
    assert q.list('me')['total']==1
    item=q.list('me')['items'][0]
    q.decide('me',{'key':item['key'],'signature':item['signature'],'decision':'dismiss'})
    assert q.list('me')['total']==0
    assert q.list('me',state='dismissed')['total']==1
    crm.close()


def test_source_change_preserves_candidate_and_requires_refresh(tmp_path):
    crm=CRMStore(tmp_path/'queue.sqlite'); record=note(crm)
    q=ReviewQueue(crm,clock=lambda:NOW); item=q.list('me')['items'][0]
    crm.update_record('me',record['id'],{'content':'我暂不发方案'},NOW+1)
    with pytest.raises(ValueError,match='来源已有变化'):
        q.decide('me',{'key':item['key'],'signature':item['signature'],'decision':'dismiss'})
    assert q.list('me')['items'][0]['blocked']
    with pytest.raises(KeyError):
        q.decide('other',{'key':item['key'],'signature':item['signature'],'decision':'dismiss'})
    crm.close()


def test_adoption_removes_candidate_without_duplicate_task(tmp_path):
    crm=CRMStore(tmp_path/'queue.sqlite'); r=note(crm)
    q=ReviewQueue(crm,clock=lambda:NOW)
    child=crm.adopt_action('me',r['id'],1,NOW)
    assert q.list('me')['total']==0
    assert crm.adopt_action('me',r['id'],1,NOW)['id']==child['id']
    assert child['proposal_id'] is None
    assert crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0]==0
    crm.close()


def test_dismissal_survives_reorganizing_unchanged_evidence(tmp_path):
    crm=CRMStore(tmp_path/'stable.sqlite'); r=note(crm)
    q=ReviewQueue(crm,clock=lambda:NOW)
    item=q.list('me')['items'][0]
    q.decide('me',{'key':item['key'],'signature':item['signature'],'decision':'dismiss'})
    old=crm.get_analysis('me',r['id'])
    payload={k:v for k,v in old.items() if k in ('summary','key_points','open_questions','actions','input_fingerprint')}
    payload['actions']=[{k:v for k,v in old['actions'][0].items() if k not in ('id','adopted_record_id','record_id','stale')}]
    payload['actions'][0]['reason']='同一依据换一种措辞'
    crm.save_analysis('me',r['id'],payload,NOW+1)
    assert q.list('me')['total']==0
    assert q.list('me',state='dismissed')['items'][0]['key']==item['key']
    crm.close()


def test_exchange_material_adapter_appears_once_in_global_queue(tmp_path):
    crm=CRMStore(tmp_path/'queue.sqlite')
    action={'id':1,'title':'提供清单','kind':'commitment','adopted_record_id':None}
    class Materials:
        def list(self,owner,**kwargs):
            return {'items':[{'id':7,'title':'现场录音','status':'review','revision':1,'record_id':None}], 'pages':1}
        def detail(self,owner,identifier):return {'analysis':{'actions':[action]}}
    class Visits:
        def list(self,owner,**kwargs):return {'items':[{'id':3,'title':'拜访','customer_id':None}], 'total':1}
        def detail(self,owner,identifier):
            return {'visit':{'revision':'v1'},'sources':[{'material_id':7,'material':{'record_id':None}}],
                    'actions':[{**action,'key':'stable','needs_review':False}]}
    q=ReviewQueue(crm,materials=Materials(),visits=Visits(),clock=lambda:NOW)
    result=q.list('me')
    assert result['total']==1
    assert result['items'][0]['source_type']=='visit'
    assert result['items'][0]['action_key']=='stable'
    crm.close()
