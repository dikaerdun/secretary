from datetime import datetime
import pytest

from secretary.crm import CRMStore
from secretary.spoken_changes import SpokenChangeService, parse_change
from secretary.store import SHANGHAI


NOW=datetime(2026,10,3,9,tzinfo=SHANGHAI).timestamp()


@pytest.mark.parametrize('text',[
    '不要取消原提醒，客户还没确认','暂不改到明天下午三点','是否取消原安排',
    '改到明天下午三点或后天下午四点，待客户确认',
    '改到明天后天上午三点','改到明天下午三点和四点',
])
def test_negative_or_ambiguous_change_is_saved_without_proposal(tmp_path,text):
    crm=CRMStore(tmp_path/'negative.sqlite'); old=active(crm)
    result=SpokenChangeService(crm,lambda:NOW).propose('me',old['record']['id'],text,'neg')
    assert result['task']['remind_at']==old['task']['remind_at']
    assert result['proposal']['status']=='confirmed'
    assert crm._db.execute("SELECT count(*) FROM proposals WHERE status='pending'").fetchone()[0]==0
    assert crm._db.execute('SELECT content FROM crm_activities').fetchone()[0]==text
    crm.close()


def test_noon_one_oclock_is_1300():
    result=parse_change('改到明天中午一点',NOW,{'id':1,'title':'拜访','status':'pending','duration_minutes':60})
    assert result['remind_at']==datetime(2026,10,4,13,tzinfo=SHANGHAI).timestamp()


def active(crm):
    r=crm.create_record('me',{'title':'再次拜访','kind':'action','content':'拜访客户'},NOW)
    crm.execute('me','new',{'action':'propose','title':r['title'],'remind_at':NOW+86400,'duration_minutes':60},NOW)
    p=crm._db.execute('SELECT * FROM proposals ORDER BY id DESC').fetchone()
    crm.link_proposal('me',r['id'],p['id'],NOW)
    crm.execute('me','yes',{'action':'confirm','proposal_id':p['id']},NOW)
    return crm.record_detail('me',r['id'])


def test_spoken_reschedule_preserves_identity_before_and_after_confirmation(tmp_path):
    crm=CRMStore(tmp_path/'spoke.sqlite'); old=active(crm)
    svc=SpokenChangeService(crm,lambda:NOW)
    result=svc.propose('me',old['record']['id'],'取消原时间，改到10月12日上午10点，仍60分钟，不要新增','change-1')
    assert result['task']['id']==old['task']['id']
    assert result['task']['remind_at']==old['task']['remind_at']
    p=result['proposal']
    assert p['target_task_id']==old['task']['id']
    assert p['remind_at']==datetime(2026,10,12,10,tzinfo=SHANGHAI).timestamp()
    assert p['duration_minutes']==60
    assert svc.propose('me',old['record']['id'],'取消原时间，改到10月12日上午10点，仍60分钟，不要新增','change-1')['proposal']['id']==p['id']
    crm.execute('me','confirm-change',{'action':'confirm','proposal_id':p['id']},NOW)
    detail=crm.record_detail('me',old['record']['id'])
    assert detail['task']['id']==old['task']['id']
    assert detail['task']['remind_at']==p['remind_at']
    assert len(detail['activities'])==1
    assert crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0]==1
    assert crm._db.execute("SELECT count(*) FROM notifications WHERE task_id=? AND task_revision=? AND status='obsolete'",
        (old['task']['id'],old['task']['revision'])).fetchone()[0]==1
    crm.close()


def test_cancel_is_pending_until_confirm_and_repeat_is_idempotent(tmp_path):
    crm=CRMStore(tmp_path/'spoke.sqlite'); old=active(crm)
    result=SpokenChangeService(crm,lambda:NOW).propose('me',old['record']['id'],'取消这次拜访安排','cancel-1')
    p=result['proposal']; assert p['change_kind']=='cancel'
    assert crm.get_task('me',old['task']['id'])['status']=='pending'
    crm.execute('me','cancel-confirm',{'action':'confirm','proposal_id':p['id']},NOW)
    assert crm.get_task('me',old['task']['id'])['status']=='cancelled'
    assert crm._db.execute("SELECT count(*) FROM notifications WHERE status IN ('queued','leased')").fetchone()[0]==0
    crm.execute('me','cancel-again',{'action':'confirm','proposal_id':p['id']},NOW)
    assert crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0]==1
    crm.close()


def test_ambiguous_change_saved_without_changing_schedule(tmp_path):
    crm=CRMStore(tmp_path/'spoke.sqlite'); old=active(crm)
    result=SpokenChangeService(crm,lambda:NOW).propose('me',old['record']['id'],'改到下周一三点','vague')
    assert result['needs_clarification']
    assert result['task']['remind_at']==old['task']['remind_at']
    assert result['activities'][0]['content']=='改到下周一三点'
    assert crm._db.execute('SELECT count(*) FROM proposals').fetchone()[0]==1
    crm.close()


def test_change_rejects_foreign_and_real_task_revision_conflict(tmp_path):
    crm=CRMStore(tmp_path/'spoke.sqlite'); old=active(crm); svc=SpokenChangeService(crm,lambda:NOW)
    with pytest.raises(KeyError):svc.propose('other',old['record']['id'],'改到明天下午三点','foreign')
    result=svc.propose('me',old['record']['id'],'改到明天下午三点','valid')
    crm.execute('me','complete-original',{'action':'complete','task_id':old['task']['id']},NOW)
    reply=crm.execute('me','stale-confirm',{'action':'confirm','proposal_id':result['proposal']['id']},NOW)
    assert crm.get_proposal('me',result['proposal']['id'])['status']=='rejected'
    assert crm.get_task('me',old['task']['id'])['status']=='completed'
    with pytest.raises(ValueError,match='提交标识'):
        svc.propose('me',old['record']['id'],'改到后天下午三点','valid')
    crm.close()


@pytest.mark.parametrize('utterance,expected',[
    ('改到明天下午三点',datetime(2026,10,4,15,tzinfo=SHANGHAI)),
    ('改到后天上午十点半',datetime(2026,10,5,10,30,tzinfo=SHANGHAI)),
    ('改到下周一下午三点',datetime(2026,10,5,15,tzinfo=SHANGHAI)),
    ('改到2026-10-12 10:00',datetime(2026,10,12,10,tzinfo=SHANGHAI)),
])
def test_supported_explicit_times(utterance,expected):
    task={'id':1,'title':'拜访','status':'pending','duration_minutes':60}
    result=parse_change(utterance,NOW,task)
    assert result['remind_at']==expected.timestamp()
    assert result['duration_minutes']==60
