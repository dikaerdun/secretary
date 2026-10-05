import asyncio
import pytest
from secretary.customer_store import CustomerStore
from secretary.sales_workspace import SalesWorkspace
from secretary.matters import MatterService
from secretary.secretary_flow import SecretaryFlow
from tests.test_secretary_flow import NOW, ScriptedInterpreter, initial, arranged, process, submit

@pytest.fixture
def stack(tmp_path):
    crm=CustomerStore(tmp_path/'synthetic.sqlite3')
    matters=MatterService(crm,clock=lambda:NOW)
    flow=SecretaryFlow(crm,SalesWorkspace(crm),asyncio.Lock(),clock=lambda:NOW)
    crm.secretary_flow=flow;matters.secretary_flow=flow
    yield crm,flow,matters
    crm.close()

class Decisions:
    def __init__(self, values):self.values=iter(values)
    async def route(self,*args,**kwargs):return next(self.values)

def start(stack):
    crm,flow,matters=stack
    flow.interpreter=ScriptedInterpreter([initial(),arranged()])
    flow.matter_router=Decisions([{'kind':'source'},{'kind':'source'}])
    first=process(flow,submit(flow,'10月8日约林博士吃饭','start'))
    ready=process(flow,submit(flow,'主要聊后量子平台合作，顺带问title。晚上六点，已经约好了，提前一个小时提醒','ready',first['plan']))
    return ready

def test_ambiguous_update_does_not_change_existing_plan_or_reminder(stack):
    crm,flow,matters=stack;ready=start(stack)
    old=flow.plan('owner',ready['plan_id'])
    tasks=[tuple(r) for r in crm._db.execute('SELECT * FROM tasks')]
    notifications=[tuple(r) for r in crm._db.execute('SELECT * FROM notifications')]
    flow.interpreter=ScriptedInterpreter([{'intent':'update','changes':{'date':'2026-10-09'},'evidence':{'date':'9号'}}])
    flow.matter_router=Decisions([{'kind':'ambiguous','candidates':[{'id':ready['matter_id'],'title':'原事项'}],'reason':'需要选择目标'}])
    turn=process(flow,submit(flow,'改到9号，还是另一个项目要约','ambiguous',old))
    assert turn['status']=='done' and turn['matter_route']['kind']=='ambiguous'
    assert turn['plan_id'] is None
    assert flow.plan('owner',old['id'])['revision']==old['revision']
    assert tasks==[tuple(r) for r in crm._db.execute('SELECT * FROM tasks')]
    assert notifications==[tuple(r) for r in crm._db.execute('SELECT * FROM notifications')]
    assert crm.get_record('owner',turn['record_id'])['original_content']=='改到9号，还是另一个项目要约'

def test_fresh_goal_does_not_inherit_old_plan(stack):
    crm,flow,matters=stack;ready=start(stack);old=flow.plan('owner',ready['plan_id'])
    matter=matters.get('owner',ready['matter_id'])
    flow.interpreter=ScriptedInterpreter([{'intent':'note','changes':{}}])
    flow.matter_router=Decisions([{'kind':'new','title':'准备渠道合作','objective':'推进渠道合作','actions':[]}])
    turn=process(flow,submit(flow,'另外主要聊渠道合作，改到10号','fresh',matter_id=matter['id'],matter_revision=matter['revision'],matter_mode='fresh'))
    assert turn['status']=='done', turn.get('error')
    assert turn['matter_id']!=matter['id'] and turn['plan_id'] is None
    assert flow.plan('owner',old['id'])['revision']==old['revision']

def test_recording_cannot_cancel_existing_activity(stack):
    crm,flow,matters=stack;ready=start(stack);old=flow.plan('owner',ready['plan_id']);matter=ready['matter']
    tasks=[tuple(r) for r in crm._db.execute('SELECT * FROM tasks')]
    flow.interpreter=ScriptedInterpreter([{'intent':'update','changes':{'booking':'cancelled'},'evidence':{'booking':'取消这次饭局'}}])
    flow.matter_router=Decisions([{'kind':'source'}])
    turn=process(flow,submit(flow,'客户说取消这次饭局','recording',old,source_kind='recording'))
    assert turn['status']=='done'
    assert tasks==[tuple(r) for r in crm._db.execute('SELECT * FROM tasks')]
    assert turn['plan']['status']=='scheduled'

def test_explicit_goal_change_and_reopen_step_preserve_originals(stack):
    crm,flow,matters=stack
    source=crm.create_record('owner',{'title':'原始准备说明','content':'原始准备说明','kind':'action'},NOW)
    matter=matters.create('owner',{'title':'旧目标','action_record_ids':[source['id']],'request_id':'create'})['matter']
    crm._db.execute("UPDATE crm_records SET status='done' WHERE id=?",(source['id'],));crm._db.commit()
    text='目标改为准备汇报，原始准备说明还没完成，需要重做'
    flow.interpreter=ScriptedInterpreter([{'intent':'note','changes':{}}])
    flow.matter_router=Decisions([{'kind':'existing','matter_id':matter['id'],'base_revision':matter['revision'],
        'updates':{'objective':'准备汇报'},'actions':[{'title':source['title'],'existing_record_id':source['id'],'status':'following','evidence':'原始准备说明还没完成，需要重做'}]}])
    turn=process(flow,submit(flow,text,'goal',matter_id=matter['id'],matter_revision=matter['revision']))
    assert turn['status']=='done'
    assert turn['matter']['objective']=='准备汇报'
    assert crm.get_record('owner',source['id'])['status']=='following'
    assert crm.get_record('owner',source['id'])['original_content']==source['original_content']


def test_same_customer_different_project_cannot_reuse_old_appointment(stack):
    from secretary.secretary_flow import FlowConflict
    crm,flow,matters=stack
    customer=crm.create_customer('owner',{'name':'虚构交叉项目单位'},NOW)
    first=flow.workspace.create_opportunity('owner',customer['id'],{'name':'密码汇报'})
    second=flow.workspace.create_opportunity('owner',customer['id'],{'name':'数据加密试点'})
    flow.interpreter=ScriptedInterpreter([initial()])
    flow.matter_router=Decisions([{'kind':'source'}])
    original=process(flow,submit(flow,'10月8日约林博士吃饭','cross-plan',customer_id=customer['id'],opportunity_id=first['id']))
    plan=original['plan']
    matter=matters.create('owner',{'title':'另一项目','customer_id':customer['id'],
        'opportunity_id':second['id'],'request_id':'cross-matter'})['matter']
    with pytest.raises(FlowConflict,match='另一个项目'):
        submit(flow,'改到10号','cross-rejected',plan,matter_id=matter['id'],matter_revision=matter['revision'])
    assert flow.plan('owner',plan['id'])['revision']==plan['revision']
    assert not crm._db.execute("SELECT 1 FROM crm_secretary_turns WHERE request_id='cross-rejected'").fetchone()


def test_factual_supplement_fills_unknown_customer_and_keeps_goal(stack):
    crm,flow,matters=stack
    original=crm.create_record('owner',{'title':'原始准备说明','content':'准备说明'},NOW)
    matter=matters.create('owner',{'title':'准备材料','source_record_ids':[original['id']],'request_id':'unknown-goal'})['matter']
    customer=crm.create_customer('owner',{'name':'虚构后补归属'},NOW)
    flow.interpreter=ScriptedInterpreter([{'intent':'note','changes':{}}])
    flow.matter_router=Decisions([{'kind':'source','matter_id':matter['id'],'base_revision':matter['revision']}])
    turn=process(flow,submit(flow,'客户认为验收标准还需要内部讨论','fact',matter_id=matter['id'],
        matter_revision=matter['revision'],customer_id=customer['id']))
    assert turn['status']=='done' and turn['matter_id']==matter['id']
    assert turn['matter']['customer_id']==customer['id']
    assert turn['matter']['action_count']==0
    assert original['id'] in [source['id'] for source in turn['matter']['sources']]
    assert crm.get_record('owner',original['id'])['original_content']==original['original_content']
