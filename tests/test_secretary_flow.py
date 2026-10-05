"""The real secretary lifecycle, on disposable data and a scripted model only."""
import asyncio
from datetime import datetime
import pytest
from secretary.customer_store import CustomerStore
from secretary.sales_workspace import SalesWorkspace
from secretary.store import SHANGHAI
from secretary.secretary_flow import SecretaryFlow, FlowConflict

NOW = datetime(2026, 10, 4, 12, tzinfo=SHANGHAI).timestamp()
START = datetime(2026, 10, 8, 18, tzinfo=SHANGHAI).timestamp()

class ScriptedInterpreter:
    def __init__(self, answers):
        self.answers = list(answers)
    async def interpret(self, text, now, context):
        return self.answers.pop(0)

def initial():
    return {"intent":"plan","changes":{"title":"与林博士吃饭","person":"林博士","date":"2026-10-08","activity":"meal"},
            "evidence":{"person":"林博士","date":"10月8日","activity":"吃饭"}}

def arranged():
    return {"intent":"update","changes":{"goal":"讨论后量子平台合作","topics":["平台合作","对外title"],
            "start_at":"2026-10-08T18:00:00+08:00","booking":"confirmed","remind_minutes":60},
            "evidence":{"goal":"主要聊后量子平台合作","topics":"主要聊后量子平台合作，顺带问title",
            "start_at":"晚上六点","booking":"已经约好了","remind_minutes":"提前一个小时提醒"}}

@pytest.fixture
def stack(tmp_path):
    crm=CustomerStore(tmp_path/"synthetic.sqlite3")
    flow=SecretaryFlow(crm,SalesWorkspace(crm),asyncio.Lock(),clock=lambda:NOW)
    yield crm,flow
    crm.close()

def submit(flow,text,request,plan=None,**extra):
    data={"text":text,"request_id":request,**extra}
    if plan:
        data.update(plan_id=plan["id"],expected_revision=plan["revision"])
    return flow.submit("owner",data)

def process(flow,turn):
    assert asyncio.run(flow.process_one())
    return flow.turn("owner",turn["id"])

def test_date_only_plan_asks_goal_and_creates_no_notification(stack):
    crm,flow=stack
    flow.interpreter=ScriptedInterpreter([initial()])
    turn=submit(flow,"10月8日约林博士吃饭","first")
    assert turn["status"]=="queued"
    result=process(flow,turn)
    plan=result["plan"]
    assert plan["date"]=="2026-10-08" and plan["start_at"] is None
    assert plan["booking"]=="unknown"
    assert "目标" in result["question"] or "推进" in result["question"] or "想谈" in result["question"]
    assert crm._db.execute("SELECT count(*) FROM notifications").fetchone()[0]==0
    assert crm.get_record("owner",turn["record_id"])["original_content"]=="10月8日约林博士吃饭"

def test_one_answer_fills_same_plan_and_reminds_before_meeting(stack):
    crm,flow=stack
    flow.interpreter=ScriptedInterpreter([initial(),arranged()])
    first=process(flow,submit(flow,"10月8日约林博士吃饭","first"))["plan"]
    second=process(flow,submit(flow,"主要聊后量子平台合作，顺带问title。晚上六点，已经约好了，提前一个小时提醒","answer",first))
    plan=second["plan"]
    assert plan["id"]==first["id"] and plan["start_at"]==START
    assert plan["reminder_at"]==START-3600 and plan["booking"]=="confirmed"
    assert plan["goal"]=="讨论后量子平台合作"
    assert crm._db.execute("SELECT count(*) FROM tasks").fetchone()[0]==1
    reminder=crm._db.execute("SELECT * FROM notifications WHERE status='queued'").fetchone()
    assert reminder["due_at"]==START-3600
    assert len(flow.plan("owner",plan["id"])["turns"])==2
    assert crm._db.execute("SELECT count(*) FROM crm_records WHERE kind='action'").fetchone()[0]==1
    assert second["question"]==""

def test_replay_and_reopen_do_not_duplicate_source_or_plan(stack):
    crm,flow=stack
    flow.interpreter=ScriptedInterpreter([initial()])
    turn=submit(flow,"10月8日约林博士吃饭","same")
    result=process(flow,turn)
    replay=submit(flow,"10月8日约林博士吃饭","same")
    assert replay["id"]==turn["id"]
    assert crm._db.execute("SELECT count(*) FROM crm_records").fetchone()[0]==1
    reopened=SecretaryFlow(crm,SalesWorkspace(crm),asyncio.Lock(),clock=lambda:NOW)
    assert reopened.plan("owner",result["plan"]["id"])["date"]=="2026-10-08"
    with pytest.raises(ValueError):
        submit(flow,"修改了输入","same")

def test_foreign_scope_and_stale_plan_are_rejected_without_new_source(stack):
    crm,flow=stack
    flow.interpreter=ScriptedInterpreter([initial(),arranged()])
    customer=crm.create_customer("other",{"name":"private"},NOW)
    with pytest.raises((KeyError,ValueError)):
        submit(flow,"新记录","foreign",customer_id=customer["id"])
    first=process(flow,submit(flow,"10月8日约林博士吃饭","first"))["plan"]
    process(flow,submit(flow,"主要聊后量子平台合作，顺带问title。晚上六点，已经约好了，提前一个小时提醒","answer",first))
    count=crm._db.execute("SELECT count(*) FROM crm_records").fetchone()[0]
    with pytest.raises(FlowConflict):
        submit(flow,"改到9号","stale",first)
    assert crm._db.execute("SELECT count(*) FROM crm_records").fetchone()[0]==count
    with pytest.raises(KeyError):
        flow.plan("other",first["id"])

def test_model_without_literal_time_evidence_cannot_enable_reminder(stack):
    crm,flow=stack
    wrong=arranged()
    wrong["evidence"]["start_at"]="晚上六点"
    flow.interpreter=ScriptedInterpreter([initial(),wrong])
    first=process(flow,submit(flow,"10月8日约林博士吃饭","first"))["plan"]
    result=process(flow,submit(flow,"主要聊后量子平台合作，顺带问title。已经约好了，提前一个小时提醒","answer",first))
    assert result["plan"]["start_at"] is None
    assert crm._db.execute("SELECT count(*) FROM tasks").fetchone()[0]==0

def planned(flow):
    flow.interpreter=ScriptedInterpreter([initial(),arranged()])
    first=process(flow,submit(flow,"10月8日约林博士吃饭","first"))["plan"]
    return process(flow,submit(flow,"主要聊后量子平台合作，顺带问title。晚上六点，已经约好了，提前一个小时提醒","answer",first))["plan"]

def test_reschedule_same_task_cancel_and_keep_original_sources(stack):
    crm,flow=stack
    plan=planned(flow)
    flow.interpreter=ScriptedInterpreter([
        {"intent":"update","changes":{"date":"2026-10-09","start_at":"ignored","topics":["平台合作"]},
         "evidence":{"date":"9号","start_at":"晚上六点半","topics":"title先不谈"}},
        {"intent":"update","changes":{"booking":"cancelled"},"evidence":{"booking":"取消这次饭局"}}])
    # New coordination contract: the old agreement cannot authorize a changed
    # candidate. This test reports agreement on the changed time explicitly.
    newer=process(flow,submit(flow,"改到9号晚上六点半，已经约好了，title先不谈","move",plan))["plan"]
    assert newer["task_id"]==plan["task_id"]
    assert newer["start_at"]==datetime(2026,10,9,18,30,tzinfo=SHANGHAI).timestamp()
    assert newer["reminder_at"]==newer["start_at"]-3600
    assert newer["booking"]=="confirmed" and newer["topics"]==["平台合作"]
    assert crm._db.execute("SELECT count(*) FROM tasks").fetchone()[0]==1
    assert crm._db.execute("SELECT count(*) FROM notifications WHERE status='queued'").fetchone()[0]==1
    cancelled=process(flow,submit(flow,"取消这次饭局","cancel",newer))["plan"]
    assert cancelled["status"]=="cancelled"
    assert crm._db.execute("SELECT count(*) FROM notifications WHERE status='queued'").fetchone()[0]==0
    assert crm.get_record("owner",plan["record_id"])["original_content"]=="10月8日约林博士吃饭"

def test_recording_and_conditional_do_not_cancel_or_complete_active_plan(stack):
    crm,flow=stack
    plan=planned(flow)
    flow.interpreter=ScriptedInterpreter([
        {"intent":"recap","changes":{"booking":"cancelled","date":"2026-10-09"},"evidence":{"booking":"取消这次饭局","date":"10月9日"}},
        {"intent":"update","changes":{"booking":"cancelled"},"evidence":{"booking":"取消这次饭局"}}])
    note=process(flow,submit(flow,"复盘时他说10月9日取消这次饭局","audio",plan,source_kind="recording"))["plan"]
    assert note["status"]=="scheduled"
    assert note['start_at']==plan['start_at'] and note['date']==plan['date']
    note=process(flow,submit(flow,"要不要取消这次饭局","conditional",note))["plan"]
    assert note["status"]=="scheduled" and note["booking"]=="confirmed"
    assert crm._db.execute("SELECT status FROM tasks").fetchone()[0]=="pending"

def test_failing_apply_is_atomic_and_retry_keeps_one_source(stack):
    crm,flow=stack
    invalid={"intent":"plan","changes":{"unexpected":1},"evidence":{}}
    flow.interpreter=ScriptedInterpreter([invalid,initial()])
    turn=submit(flow,"10月8日约林博士吃饭","failure")
    failed=process(flow,turn)
    assert failed["status"]=="failed"
    assert crm._db.execute("SELECT count(*) FROM crm_secretary_plans").fetchone()[0]==0
    retried=process(flow,flow.retry("owner",turn["id"]))
    assert retried["status"]=="done"
    assert crm._db.execute("SELECT count(*) FROM crm_records").fetchone()[0]==1

def test_explicit_preferences_and_suggestions_not_action_debt(stack):
    crm,flow=stack
    flow.update_settings("owner",{"expected_revision":0,"business_context":"擅长密钥管理","remind_minutes":60})
    raw=initial()
    raw["suggestions"]=[{"title":"准备试点案例","reason":"可选建议","evidence":""}]
    flow.interpreter=ScriptedInterpreter([raw])
    result=process(flow,submit(flow,"10月8日约林博士吃饭","first"))
    assert crm._db.execute("SELECT count(*) FROM crm_records").fetchone()[0]==1
    action=flow.adopt("owner",result["id"],1)
    assert flow.adopt("owner",result["id"],1)["id"]==action["id"]
    assert crm._db.execute("SELECT count(*) FROM tasks").fetchone()[0]==0

def test_recap_finishes_task_and_same_conversation_keeps_history(stack):
    crm,flow=stack
    plan=planned(flow)
    flow.clock=lambda:START+7200
    flow.interpreter=ScriptedInterpreter([{"intent":"recap","changes":{},"evidence":{},"summary":"讨论完成，待技术方案。"}])
    result=process(flow,submit(flow,"饭吃完了，复盘：下次再看技术方案","recap",plan))
    assert result["plan"]["id"]==plan["id"] and result["plan"]["status"]=="recapped"
    assert crm._db.execute("SELECT status FROM tasks").fetchone()[0]=="completed"
    assert len(flow.plan("owner",plan["id"])["turns"])==3

def test_negative_booking_not_confirmed_and_reminder_clock_separate(stack):
    crm,flow=stack
    raw=arranged();raw["evidence"]["booking"]="还没约好"
    flow.interpreter=ScriptedInterpreter([initial(),raw])
    plan=process(flow,submit(flow,"10月8日约林博士吃饭","first"))["plan"]
    result=process(flow,submit(flow,"主要聊后量子平台合作，顺带问title。晚上六点，还没约好，提前一个小时提醒","answer",plan))
    assert result["plan"]["booking"]=="unknown"
    assert crm._db.execute("SELECT count(*) FROM tasks").fetchone()[0]==0

def test_nothing_but_topics_still_fills_goal_without_asking_for_phone(stack):
    crm,flow=stack
    one=initial();one['identity_question']='林博士单位和电话是什么？'
    two=arranged();two['changes'].pop('goal');two['evidence'].pop('goal')
    flow.interpreter=ScriptedInterpreter([one,two])
    plan=process(flow,submit(flow,'10月8日约林博士吃饭','first'))['plan']
    assert '电话' not in plan['question'] and '单位' not in plan['question']
    result=process(flow,submit(flow,'主要聊后量子平台合作，顺带问title。晚上六点，已经约好了，提前一个小时提醒','answer',plan))
    assert result['plan']['goal']=='后量子平台合作' and result['plan']['status']=='scheduled'

def test_unknown_unit_person_saved_then_linked_without_losing_source(stack):
    crm,flow=stack
    flow.interpreter=ScriptedInterpreter([{'intent':'contact','changes':{'person':'李工','phone':'13800001111','wechat':'li_test'},
        'evidence':{'person':'李工','phone':'13800001111','wechat':'li_test'}}])
    turn=process(flow,submit(flow,'新认识李工，电话13800001111，微信li_test，单位还不知道','person'))
    prospect=flow.prospects('owner')['items'][0]
    customer=crm.create_customer('owner',{'name':'虚构测试单位'},NOW)
    linked=flow.link_prospect('owner',prospect['id'],{'customer_id':customer['id'],'expected_updated_at':prospect['updated_at']})
    assert linked['contact_id'] and linked['status']=='linked'
    assert crm.get_record('owner',turn['record_id'])['original_content']==turn['text']
    assert crm.get_record('owner',turn['record_id'])['customer_id']==customer['id']
    facts=crm.profile('owner',customer['id'])['contacts'][0]['fields']
    assert any(f['value']=='微信：li_test' and f['evidence']=='li_test' for f in facts)

def test_clear_unit_and_new_person_go_directly_into_contact_archive(stack):
    crm,flow=stack
    customer=crm.create_customer('owner',{'name':'虚构数据公司'},NOW)
    flow.interpreter=ScriptedInterpreter([{'intent':'contact','changes':{'person':'周工','customer_id':customer['id']},
        'evidence':{'person':'周工','customer_id':'虚构数据公司'}}])
    turn=process(flow,submit(flow,'今天新认识虚构数据公司的周工','known-unit'))
    assert turn['result']['scope']['customer_id']==customer['id']
    assert turn['result']['scope']['contact_id']
    assert flow.prospects('owner')['items'][0]['status']=='linked'

def test_undetermined_reschedule_preserves_effective_task_and_reminder(stack):
    crm,flow=stack
    plan=planned(flow)
    flow.interpreter=ScriptedInterpreter([{'intent':'update','changes':{'start_at':None},'evidence':{'start_at':'时间还没定'}}])
    result=process(flow,submit(flow,'改期，时间还没定','tbd',plan))
    assert result['plan']['start_at'] is None and result['plan']['task_id'] == plan['task_id']
    assert result['plan']['active_schedule']['id'] == plan['task_id']
    assert result['plan']['arrangement']['settling_state'] == 'pending'
    assert crm._db.execute("SELECT count(*) FROM notifications WHERE status='queued'").fetchone()[0]==1


def test_explicit_withdrawal_cancels_old_execution_but_preserves_history(stack):
    crm,flow=stack
    plan=planned(flow)
    flow.interpreter=ScriptedInterpreter([{'intent':'update','changes':{'start_at':None},'evidence':{'start_at':'时间还没定'}}])
    result=process(flow,submit(flow,'原时间不去了，新时间还没定','withdraw',plan))['plan']
    assert result['active_schedule'] is None and result['task_id']==plan['task_id']
    assert crm._db.execute('SELECT status FROM tasks WHERE id=?',(plan['task_id'],)).fetchone()[0]=='cancelled'
    assert crm._db.execute("SELECT count(*) FROM notifications WHERE status='queued'").fetchone()[0]==0
    assert result['arrangement']['settling_state']=='pending'

def test_failed_change_keeps_old_appointment_and_reports_it(stack):
    crm,flow=stack
    plan=planned(flow)
    occupied=datetime(2026,10,9,18,30,tzinfo=SHANGHAI).timestamp()
    with crm._transaction() as db:
        crm._execute(db,'owner',{'action':'propose','title':'已有其他安排','remind_at':occupied},NOW)
        proposal=db.execute("SELECT max(id) FROM proposals").fetchone()[0]
        crm._execute(db,'owner',{'action':'confirm','proposal_id':proposal},NOW)
    flow.interpreter=ScriptedInterpreter([{'intent':'update','changes':{'date':'2026-10-09','start_at':'ignored'},
        'evidence':{'date':'9号','start_at':'晚上六点半'}}])
    changed=process(flow,submit(flow,'改到9号晚上六点半','conflict',plan))['plan']
    assert changed['status']=='needs_attention'
    assert changed['active_schedule']['remind_at']==START
    assert crm._db.execute("SELECT due_at FROM notifications WHERE task_id=? AND status='queued'",(plan['task_id'],)).fetchone()[0]==START-3600
