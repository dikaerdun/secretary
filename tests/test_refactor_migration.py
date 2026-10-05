"""Additive refactor migration on a copied synthetic pre-refactor database."""
import asyncio
import hashlib
import sqlite3
from secretary.customer_store import CustomerStore
from secretary.materials import MaterialService
from secretary.visits import VisitService
from secretary.sales_workspace import SalesWorkspace
from secretary.review_queue import ReviewQueue
from secretary.exchange_records import ExchangeRecords


def test_upgrade_copy_preserves_records_task_identity_and_is_restart_safe(tmp_path):
    original=tmp_path/'synthetic-old.sqlite';copy=tmp_path/'synthetic-upgrade-copy.sqlite'
    old=CustomerStore(original);now=1790989200
    c=old.create_customer('me',{'name':'迁移合成客户'},now)
    r=old.create_record('me',{'title':'拜访','content':'原始交流依据','customer_id':c['id'],'kind':'action'},now)
    old.execute('me','p',{'action':'propose','title':'拜访','remind_at':now+3600,'duration_minutes':60},now)
    p=old._db.execute('SELECT id FROM proposals').fetchone()[0]
    old.link_proposal('me',r['id'],p,now);old.execute('me','yes',{'action':'confirm','proposal_id':p},now)
    task=old.record_detail('me',r['id'])['task'];old.close()
    with sqlite3.connect(original) as before:
        before.execute('DROP TABLE crm_action_terms');before.execute('DROP TABLE crm_record_transcripts')
        before.execute('ALTER TABLE proposals DROP COLUMN change_kind')
    original_digest=hashlib.sha256(original.read_bytes()).hexdigest()
    with sqlite3.connect(original) as source,sqlite3.connect(copy) as destination:source.backup(destination)
    for _ in range(2):
        crm=CustomerStore(copy);lock=asyncio.Lock()
        materials=MaterialService(crm,lock,clock=lambda:now)
        visits=VisitService(crm,materials,lock)
        SalesWorkspace(crm,clock=lambda:now);ReviewQueue(crm,materials=materials,visits=visits,clock=lambda:now)
        ExchangeRecords(crm,visits,lambda:now)
        preserved=crm.record_detail('me',r['id'])
        assert preserved['record']['original_content']=='原始交流依据'
        assert preserved['record']['customer_id']==c['id']
        assert preserved['task']['id']==task['id'] and preserved['task']['duration_minutes']==60
        assert preserved['proposal']['change_kind']=='schedule'
        assert preserved['record']['action_terms']=={}
        assert crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0]==1
        assert crm._db.execute('SELECT count(*) FROM crm_records').fetchone()[0]==1
        crm.close()
    assert hashlib.sha256(original.read_bytes()).hexdigest()==original_digest
