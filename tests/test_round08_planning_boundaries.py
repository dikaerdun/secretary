"""Owner-isolated R08 calendar, evidence and work-queue user journeys.

Only fresh synthetic databases, local TestServer and a capturing offline advisor.
Tokens are fetched explicitly at each reviewed operation, never injected by write.
"""
import asyncio
import copy
import json
import re
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest
from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from secretary.customer_store import CustomerStore
from secretary.overview import OverviewService
from secretary.progress_workspace import _task_overlaps
from secretary.store import SHANGHAI, Store
from secretary.web import create_app, hash_password


OWNER = 'round08-owner'
NOW = datetime(2026, 10, 4, 8, tzinfo=SHANGHAI).timestamp()


def stamp(day, hour=0, minute=0):
    return datetime.fromisoformat(day).replace(hour=hour, minute=minute, tzinfo=SHANGHAI).timestamp()


def run_async(function):
    import functools
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return wrapped


@asynccontextmanager
async def world(tmp_path):
    path = tmp_path / 'isolated-r08.sqlite3'
    crm, store = CustomerStore(path), Store(path)
    clock = [NOW]
    app = create_app(store, crm, asyncio.Lock(), OWNER, hash_password('synthetic-r08-password'), clock=lambda: clock[0])
    web = app.middlewares[0].__self__
    client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
    await client.start_server()
    csrf = (await (await client.post('/api/login', json={'password': 'synthetic-r08-password'})).json())['csrf']
    async def call(method, path, body=None, status=200):
        response = await client.request(method, path, **({'json': body, 'headers': {'X-CSRF-Token': csrf}} if method!='GET' else {}))
        result = await response.json()
        assert response.status == status, result
        return result
    unit = crm.create_customer(OWNER, {'name': '合成计划银行'}, NOW)
    project = web.sales_workspace.create_opportunity(OWNER, unit['id'], {'name': '合成密码项目', 'scope': '接口签名验收'})
    w = SimpleNamespace(crm=crm, store=store, web=web, call=call, clock=clock, unit=unit, project=project, progress=web.progress_workspace)
    try:
        yield w
    finally:
        await client.close()
        crm.close(); store.close()


def action(w, title='核对合成接口', *, terms=None, owner=OWNER, customer=None, content=None):
    record = w.crm.create_record(owner, {'title': title, 'content': content or title, 'kind': 'action', 'status': 'following',
        'customer_id': w.unit['id'] if customer is None else customer}, NOW)
    if terms is not None:
        w.crm.save_action_terms(owner, record['id'], terms, NOW)
    return w.crm.get_record(owner, record['id'])


def internal_schedule(w, record, when, duration=30, *, owner=OWNER, confirmed=True):
    at = min(NOW, when-3600)
    message = w.crm.execute(owner, 'schedule-'+str(record['id']), {'action': 'propose', 'title': record['title'],
        'remind_at': when, 'duration_minutes': duration}, at)
    proposal_id = int(re.search(r'P(\d+)', message)[1])
    w.crm.link_proposal(owner, record['id'], proposal_id, at)
    if confirmed:
        w.crm.execute(owner, 'confirm-'+str(proposal_id), {'action': 'confirm', 'proposal_id': proposal_id}, at)
    return w.crm.record_detail(owner, record['id'])


async def prepare(w, *, kind='plan', customer=True, period='day', start='2026-10-04', text='核对下一步', **scope):
    body = {'request_id': 'prepare-'+str(w.crm._db.execute('SELECT count(*) FROM crm_progress_runs').fetchone()[0]), 'kind': kind, 'text': text, **scope}
    if customer:
        body.setdefault('customer_id', w.unit['id'])
    if kind=='plan':
        body.update(period=period, start_date=start)
    created = w.progress.create(OWNER, body)
    await w.progress.process_pending(OWNER)
    return w.progress.get(OWNER, created['id'])


def choose(w, run, item, **draft):
    return w.progress.edit_draft(OWNER, run['id'], {'expected_revision': run['revision'],
        'items': [{'id': item['id'], 'selected': True, 'draft': draft}]})


def adopt(w, run, request):
    body = {'request_id': request, 'expected_revision': run['revision'], 'items': [
        {'id': item['id'], 'expected_item_revision': item['revision'], 'expected_snapshot': item['versions']['snapshot']}
        for item in run['items'] if item['selected']]}
    return w.progress.confirm(OWNER, run['id'], body), body


def counts(w):
    return {table: w.crm._db.execute('SELECT count(*) FROM '+table).fetchone()[0]
            for table in ('tasks', 'proposals', 'notifications', 'command_results', 'crm_records')}


@pytest.mark.parametrize('period,start,when,duration', [
    ('day', '2026-10-05', stamp('2026-10-04', 23, 30), 60),
    ('week', '2026-10-05', stamp('2026-10-04', 23, 30), 60),
    ('month', '2026-11-01', stamp('2026-10-31', 23, 30), 60),
])
@run_async
async def test_cross_window_occupation_reaches_plan_and_real_model_payload(tmp_path, period, start, when, duration):
    async with world(tmp_path) as w:
        record = action(w)
        task = internal_schedule(w, record, when, duration)['task']
        class Advisor:
            def __init__(self): self.seen=[]
            async def reply(self, context, history, text, now):
                self.seen.append(copy.deepcopy(context))
                return {'answer': '核对计划，不代表完成', 'questions': [], 'risks': [], 'next_moves': []}
        advisor = Advisor(); w.progress._advisor = advisor
        run = await prepare(w, period=period, start=start)
        assert run['status']=='ready'
        assert {row['current']['task']['id'] for row in run['existing_schedule']} == {task['id']}
        assert {row['id'] for row in advisor.seen[-1]['progress_existing_schedule']} == {task['id']}
        agenda = await w.call('GET', '/api/agenda?period='+period+'&date='+start)
        assert [row['id'] for row in agenda['items']] == [task['id']]
        assert w.crm.get_task(OWNER, task['id']) == task


@run_async
async def test_orphan_calendar_is_in_model_without_becoming_customer_action(tmp_path):
    async with world(tmp_path) as w:
        reply = w.store.execute(OWNER, 'orphan', {'action': 'propose', 'title': '内部合成会议', 'remind_at': NOW+3600, 'duration_minutes': 45}, NOW)
        p = int(re.search(r'P(\d+)', reply)[1]); w.store.execute(OWNER, 'orphan-confirm', {'action': 'confirm', 'proposal_id': p}, NOW)
        class Advisor:
            async def reply(self, context, history, text, now):
                self.seen=copy.deepcopy(context)
                return {'answer': '安排保留', 'questions': [], 'risks': [], 'next_moves': []}
        advisor=Advisor(); w.progress._advisor=advisor
        run=await prepare(w, customer=False)
        assert run['existing_schedule'][0]['record_id'] is None
        assert [x['title'] for x in advisor.seen['progress_existing_schedule']] == ['内部合成会议']
        assert w.crm.list_records(OWNER)['total']==0


@pytest.mark.parametrize('when,duration,expected', [(stamp('2026-10-03',23,30),60,True), (stamp('2026-10-03',23,30),30,False), (stamp('2026-10-05'),30,False)])
@run_async
async def test_overview_today_uses_half_open_occupation_for_linked_and_orphan(tmp_path, when,duration,expected):
    async with world(tmp_path) as w:
        row=action(w); detail=internal_schedule(w,row,when,duration)
        first=OverviewService(w.crm,w.web.sales_workspace,clock=lambda:NOW).get(OWNER)
        assert (detail['task']['id'] in {x['id'] for x in first['today_schedules']}) is expected
        message=w.store.execute(OWNER,'orphan-today',{'action':'propose','title':'内部跨日','remind_at':when+180,'duration_minutes':duration},min(NOW,when-3600))
        p=int(re.search(r'P(\d+)',message)[1]); w.store.execute(OWNER,'orphan-today-confirm',{'action':'confirm','proposal_id':p},min(NOW,when-3600))
        # Actual overlap would prevent the second task: test orphan independently after cancel.
        w.crm.execute(OWNER,'cancel-linked',{'action':'cancel','task_id':detail['task']['id']},NOW)
        w.store.execute(OWNER,'orphan-today-reconfirm',{'action':'confirm','proposal_id':p},min(NOW,when-3600))
        overview=OverviewService(w.crm,w.web.sales_workspace,clock=lambda:NOW).get(OWNER)
        orphan_expected = _task_overlaps({'remind_at':when+180,'duration_minutes':duration},stamp('2026-10-04'),stamp('2026-10-05'))
        assert overview['orphan_task_summary']['today']==int(orphan_expected)
        linked=OverviewService(w.crm,w.web.sales_workspace,clock=lambda:NOW).get(OWNER)
        assert detail['task']['id'] not in {x['id'] for x in linked['today_schedules']}
        assert _task_overlaps({'remind_at':when,'duration_minutes':duration},stamp('2026-10-04'),stamp('2026-10-05')) is expected


@pytest.mark.parametrize('duration', [None, 0, True, '30'])
def test_unknown_duration_never_invents_occupation(duration):
    assert not _task_overlaps({'remind_at':NOW,'duration_minutes':duration},NOW-1,NOW+100)


@pytest.mark.parametrize('endpoint,body', [('schedule',{'remind_at':NOW+3600}),('cancel',{}),('schedule',{'remind_at':NOW+3600,'expected_schedule_snapshot':'0'}),('cancel',{'expected_schedule_snapshot':True})])
@run_async
async def test_public_calendar_missing_or_malformed_seen_token_has_no_effect(tmp_path,endpoint,body):
    async with world(tmp_path) as w:
        row=action(w); before=counts(w)
        error=await w.call('POST',f'/api/records/{row["id"]}/{endpoint}',body,400)
        assert '版本' in error['error']; assert counts(w)==before


@run_async
async def test_pending_edit_is_seen_guarded_including_frozen_clock_aba(tmp_path):
    async with world(tmp_path) as w:
        row=action(w); path=f'/api/records/{row["id"]}'
        seen=await w.call('GET',path)
        first=await w.call('POST',path+'/schedule',{'remind_at':NOW+3600,'duration_minutes':30,'expected_schedule_snapshot':seen['schedule_snapshot']})
        second=await w.call('POST',path+'/schedule',{'remind_at':NOW+7200,'duration_minutes':45,'expected_schedule_snapshot':first['schedule_snapshot']})
        before=counts(w)
        await w.call('POST',path+'/schedule',{'remind_at':NOW+10800,'expected_schedule_snapshot':first['schedule_snapshot']},409)
        assert w.crm.get_proposal(OWNER,second['proposal']['id'])['remind_at']==NOW+7200 and counts(w)==before
        third=await w.call('POST',path+'/schedule',{'remind_at':NOW+3600,'duration_minutes':30,'expected_schedule_snapshot':second['schedule_snapshot']})
        assert third['schedule_snapshot']!=first['schedule_snapshot']
        await w.call('POST',path+'/cancel',{'expected_schedule_snapshot':first['schedule_snapshot']},409)


@run_async
async def test_old_cancel_does_not_cancel_replacement_and_fresh_cancel_keeps_business(tmp_path):
    async with world(tmp_path) as w:
        row=action(w); path=f'/api/records/{row["id"]}'
        old=internal_schedule(w,row,NOW+3600)
        seen=await w.call('GET',path)
        w.crm.execute(OWNER,'done-old-task',{'action':'complete','task_id':old['task']['id']},NOW)
        latest=await w.call('GET',path)
        proposal=await w.call('POST',path+'/schedule',{'remind_at':NOW+7200,'duration_minutes':45,'expected_schedule_snapshot':latest['schedule_snapshot']})
        confirmed=await w.call('POST',path+'/confirm',{'proposal_id':proposal['proposal']['id'],'updated_at':proposal['proposal']['updated_at']})
        assert confirmed['task']['id']!=old['task']['id']
        before=counts(w)
        await w.call('POST',path+'/cancel',{'expected_schedule_snapshot':seen['schedule_snapshot']},409)
        assert w.crm.get_task(OWNER,confirmed['task']['id'])['status']=='pending' and counts(w)==before
        current=await w.call('GET',path)
        cancelled=await w.call('POST',path+'/cancel',{'expected_schedule_snapshot':current['schedule_snapshot']})
        assert cancelled['task']['status']=='cancelled' and cancelled['record']['status']=='following'
        assert cancelled['record']['original_content']==row['original_content']


@run_async
async def test_record_or_terms_changes_invalidate_calendar_seen_and_foreign_owner_is_hidden(tmp_path):
    async with world(tmp_path) as w:
        row=action(w); path=f'/api/records/{row["id"]}'; seen=await w.call('GET',path)
        w.crm.save_action_terms(OWNER,row['id'],{'executor_kind':'customer'},NOW+1)
        await w.call('POST',path+'/schedule',{'remind_at':NOW+3600,'expected_schedule_snapshot':seen['schedule_snapshot']},409)
        foreign=w.crm.create_record('other',{'title':'不可见','kind':'action'},NOW)
        await w.call('POST',f'/api/records/{foreign["id"]}/schedule',{'remind_at':NOW+3600,'expected_schedule_snapshot':seen['schedule_snapshot']},404)
        assert counts(w)['tasks']==0


@run_async
async def test_conflict_web_names_times_verified_projects_and_internal_voice_unchanged(tmp_path):
    async with world(tmp_path) as w:
        first=action(w,'已确认接口访谈'); w.web.sales_workspace.link(OWNER,'record',first['id'],w.project['id'])
        task=internal_schedule(w,first,NOW+3600,45)['task']
        second=action(w,'待确认竞争时段'); pending=internal_schedule(w,second,NOW+4200,30,confirmed=False)['proposal']
        result=await w.call('POST',f'/api/records/{second["id"]}/confirm',{'proposal_id':pending['id'],'updated_at':pending['updated_at']},409)
        conflict=result['conflicts'][0]
        assert conflict['title']==first['title'] and conflict['remind_at']==task['remind_at'] and conflict['end_at']==task['remind_at']+2700
        assert conflict['opportunity_id']==w.project['id'] and conflict['opportunity_scope']=='接口签名验收'
        assert 'P' not in result['correction_hint'] and '发送' not in result['error']
        voice=w.store.execute(OWNER,'original-voice-confirm',{'action':'confirm','proposal_id':pending['id']},NOW)
        assert '#'+str(task['id']) in voice and 'P'+str(pending['id']) in voice
        assert w.crm.get_task(OWNER,task['id'])==task
        w.crm.update_record(OWNER,first['id'],{'content':'后来补充，旧项目待重新核对'},NOW+1)
        fresh=await w.call('GET',f'/api/records/{second["id"]}')
        result=await w.call('POST',f'/api/proposals/{pending["id"]}/confirm',{'updated_at':fresh['proposal']['updated_at']},409)
        assert result['conflicts'][0]['opportunity_id'] is None and result['conflicts'][0]['project_link_stale']


@run_async
async def test_agenda_verified_project_metadata_disappears_on_archive_without_touching_task(tmp_path):
    async with world(tmp_path) as w:
        row=action(w); w.web.sales_workspace.link(OWNER,'record',row['id'],w.project['id'])
        task=internal_schedule(w,row,NOW+3600)['task']
        data=await w.call('GET','/api/agenda?period=day&date=2026-10-04')
        assert data['items'][0]['opportunity_name']==w.project['name']
        w.web.sales_workspace.update_opportunity(OWNER,w.unit['id'],w.project['id'],{'archived':True,'expected_revision':w.project['revision']})
        data=await w.call('GET','/api/agenda?period=day&date=2026-10-04')
        assert data['items'][0]['opportunity_id'] is None and data['items'][0]['project_link_stale']
        assert '归档' in data['items'][0]['project_link_stale_reason'] and w.crm.get_task(OWNER,task['id'])==task


@run_async
async def test_queue_full_counts_filters_and_paging_search_complete_text_not_excerpt(tmp_path):
    async with world(tmp_path) as w:
        ids=[]
        for i in range(47): ids.append(action(w,f'我的合成行动{i}',terms={'executor_kind':'self'},content='正文'*300+('末尾%_独特' if i==0 else '') )['id'])
        other=w.crm.create_customer(OWNER,{'name':'另一单位'},NOW)
        waiting=action(w,'客户等待',terms={'executor_kind':'customer'},customer=other['id'])
        overview=await w.call('GET','/api/overview'); pages=[]
        for page in (1,2,3):
            data=await w.call('GET',f'/api/records?work_queue=my_actions&page={page}&page_size=20')
            assert data['total']==data['base_total']==overview['counts']['my_actions']==47 and data['pages']==3
            pages+= [x['id'] for x in data['items']]
        assert len(set(pages))==47 and set(pages)==set(ids)
        result=await w.call('GET','/api/records?work_queue=my_actions&q=%25_独特')
        assert result['total']==1 and result['items'][0]['id']==ids[0]
        by_customer=await w.call('GET','/api/records?work_queue=my_actions&q=合成计划银行')
        assert by_customer['total']==47
        result=await w.call('GET',f'/api/records?work_queue=waiting_actions&customer_id={w.unit["id"]}')
        assert result['total']==0 and result['base_total']==1
        await w.call('GET','/api/records?work_queue=my_actions&status=following',status=400)
        await w.call('GET','/api/records?work_queue=bad',status=400)
        ordinary=await w.call('GET','/api/records')
        assert ordinary['total']==48 and waiting['id'] not in set(pages)


@run_async
async def test_latest_adopted_waiting_feedback_controls_queue_and_unadopted_draft_does_not(tmp_path):
    async with world(tmp_path) as w:
        row=action(w,terms={'executor_kind':'self'})
        run=await prepare(w,kind='followup_result',record_id=row['id'])
        run=choose(w,run,run['items'][0],decision='waiting',result='接口还未返回')
        assert (await w.call('GET','/api/records?work_queue=my_actions'))['total']==1
        first,_=adopt(w,run,'waiting-adopt')
        assert first['status']=='complete'
        assert (await w.call('GET','/api/records?work_queue=waiting_actions'))['items'][0]['id']==row['id']
        w.clock[0]+=1
        run=await prepare(w,kind='followup_result',record_id=row['id'],text='继续我来核对')
        run=choose(w,run,run['items'][0],decision='continue',result='材料已返回，继续本人核对')
        adopt(w,run,'continue-adopt')
        assert (await w.call('GET','/api/records?work_queue=my_actions'))['total']==1
        assert (await w.call('GET','/api/records?work_queue=waiting_actions'))['total']==0
        assert w.crm.get_record(OWNER,row['id'])['status']=='following'


@run_async
async def test_overdue_queue_matches_overview_without_inventing_today_clock_or_orphan_record(tmp_path):
    async with world(tmp_path) as w:
        today=action(w,'今日日期检查',terms={'executor_kind':'self','check_date':'2026-10-04'})
        old=action(w,'昨日截止',terms={'executor_kind':'customer','deadline_date':'2026-10-03'})
        completed=action(w,'只完成日程'); task=internal_schedule(w,completed,NOW-3600)['task']
        w.crm.execute(OWNER,'complete-only-calendar',{'action':'complete','task_id':task['id']},NOW)
        result=await w.call('GET','/api/records?work_queue=overdue')
        overview=await w.call('GET','/api/overview')
        assert result['total']==overview['counts']['overdue']==1 and result['items'][0]['id']==old['id']
        assert today['id'] in {x['id'] for x in overview['my_actions']}
        assert completed['id'] in {x['id'] for x in overview['my_actions']}


def rich_terms():
    return {'executor_kind':'self','executor_evidence':'原话：我负责接口核验',
        'duration_minutes':60,'duration_evidence':'原话：需要60分钟',
        'execution_at':stamp('2026-10-04',10),'execution_evidence':'原话：10点进行核验',
        'deadline_at':stamp('2026-10-06',18),'deadline_date':None,'deadline_evidence':'原话：6日18点之前',
        'check_at':stamp('2026-10-05',12),'check_date':None,'check_evidence':'原话：5日12点回访'}


@run_async
async def test_plan_changed_terms_replace_only_their_evidence_and_replay_preserves_edits(tmp_path):
    async with world(tmp_path) as w:
        terms=rich_terms(); row=action(w,terms=terms); internal_schedule(w,row,terms['execution_at'],60)
        run=await prepare(w); item=next(x for x in run['items'] if x['current']['record_id']==row['id'])
        run=choose(w,run,item,remind_at=stamp('2026-10-04',11),duration_minutes=30,check_at=stamp('2026-10-05',13),deadline_at=stamp('2026-10-06',19))
        result,body=adopt(w,run,'terms-adopt'); assert result['status']=='complete'
        current=w.crm.get_record(OWNER,row['id'])['action_terms']
        assert current['executor_evidence']==terms['executor_evidence']
        for name in ('duration','execution','deadline','check'): assert current[name+'_evidence']!=terms[name+'_evidence'] and '用户' in current[name+'_evidence']
        edited=w.crm.get_record(OWNER,row['id']); new={**current,'duration_minutes':45,'duration_evidence':'用户后来核对45分钟'}
        w.crm.save_action_terms(OWNER,row['id'],new,NOW+1,explicit=True,expected_updated_at=edited['terms_updated_at'])
        replay=w.progress.confirm(OWNER,run['id'],body)
        assert replay['replayed'] and w.crm.get_record(OWNER,row['id'])['action_terms']['duration_minutes']==45


@run_async
async def test_plan_unmodified_evidence_preserved_and_date_only_replaces_its_clock_not_calendar(tmp_path):
    async with world(tmp_path) as w:
        terms=rich_terms(); row=action(w,terms=terms); task=internal_schedule(w,row,terms['execution_at'],60)['task']
        run=await prepare(w); item=next(x for x in run['items'] if x['current']['record_id']==row['id'])
        run=choose(w,run,item,deadline_date='2026-10-08',check_date='2026-10-07')
        result,_=adopt(w,run,'dates-only'); assert result['status']=='complete'
        current=w.crm.get_record(OWNER,row['id'])['action_terms']
        assert current['deadline_at'] is None and current['check_at'] is None
        assert current['deadline_date']=='2026-10-08' and current['check_date']=='2026-10-07'
        for name in ('executor','duration','execution'): assert current[name+'_evidence']==terms[name+'_evidence']
        assert w.crm.get_task(OWNER,task['id'])==task
        run=await prepare(w,text='仅改个人执行安排')
        item=next(x for x in run['items'] if x['current']['record_id']==row['id'])
        run=choose(w,run,item,remind_at=stamp('2026-10-04',11),duration_minutes=60)
        adopt(w,run,'only-execution')
        current=w.crm.get_record(OWNER,row['id'])['action_terms']
        assert current['duration_evidence']==terms['duration_evidence'] and current['executor_evidence']==terms['executor_evidence']


@run_async
async def test_conflicted_plan_receipt_reopens_as_pending_and_repair_replay_never_duplicates(tmp_path):
    async with world(tmp_path) as w:
        row=action(w,'原安排',terms={'executor_kind':'self'}); original=internal_schedule(w,row,NOW+3600,30)['task']
        fixed=action(w,'固定会议'); fixed_task=internal_schedule(w,fixed,NOW+3*3600,60)['task']
        run=await prepare(w); item=next(x for x in run['items'] if x['current']['record_id']==row['id'])
        run=choose(w,run,item,remind_at=NOW+3*3600+900,duration_minutes=30)
        result,body=adopt(w,run,'conflicting-plan-adoption')
        assert result['results'][0]['schedule_pending'] and w.crm.get_task(OWNER,original['id'])==original
        restored=w.progress.get(OWNER,run['id']); persisted=next(x for x in restored['items'] if x['id']==item['id'])
        assert persisted['status']=='adopted' and persisted['receipt']['schedule_pending']
        path=f'/api/records/{row["id"]}'; seen=await w.call('GET',path)
        proposal=await w.call('POST',path+'/schedule',{'remind_at':NOW+5*3600,'duration_minutes':30,'expected_schedule_snapshot':seen['schedule_snapshot']})
        confirmed=await w.call('POST',path+'/confirm',{'proposal_id':proposal['proposal']['id'],'updated_at':proposal['proposal']['updated_at']})
        assert confirmed['task']['id']==original['id'] and confirmed['task']['remind_at']==NOW+5*3600
        after=counts(w); replay=w.progress.confirm(OWNER,run['id'],body)
        assert replay['replayed'] and counts(w)==after
        assert w.crm.get_task(OWNER,original['id'])['remind_at']==NOW+5*3600 and w.crm.get_task(OWNER,fixed_task['id'])==fixed_task


@run_async
async def test_action_terms_form_new_duration_and_clears_preserve_other_original_evidence(tmp_path):
    async with world(tmp_path) as w:
        old=rich_terms(); row=action(w,terms=old); source=row['original_content']
        body={**old,'duration_minutes':30,**{k:'原话：我负责接口核验' for k in old if k.endswith('_evidence')},'expected_updated_at':row['terms_updated_at']}
        saved=(await w.call('PATCH',f'/api/records/{row["id"]}/terms',body))['record']
        terms=saved['action_terms']; assert '用户' in terms['duration_evidence']
        for key in ('executor_evidence','deadline_evidence','execution_evidence','check_evidence'): assert terms[key]==old[key]
        cleared=(await w.call('PATCH',f'/api/records/{row["id"]}/terms',{'duration_minutes':None,'duration_evidence':'新的备注','expected_updated_at':saved['terms_updated_at']}))['record']
        assert cleared['action_terms']['duration_evidence']=='' and cleared['action_terms']['duration_minutes'] is None
        assert cleared['original_content']==source and counts(w)['tasks']==0


@run_async
async def test_new_completion_child_explicit_clock_and_date_preserve_existing_contract(tmp_path):
    async with world(tmp_path) as w:
        row=action(w,terms={'executor_kind':'self'})
        run=await prepare(w,kind='followup_result',record_id=row['id'],text='旧交付已兑现，留下一项检查')
        run=choose(w,run,run['items'][0],decision='complete',result='已实际交付接口清单',next_title='核对后续接收情况',
            next_step='核对接收并在另一个日期继续回访',executor_kind='customer',remind_at=NOW+7200,
            check_at=NOW+7200,check_date='2026-10-06',duration_minutes=30)
        result,_=adopt(w,run,'new-child-explicit-two-times'); assert result['status']=='complete'
        child=w.crm.get_record(OWNER,result['results'][0]['next_record_id'])
        assert child['action_terms']['check_at']==NOW+7200 and child['action_terms']['check_date']=='2026-10-06'
        assert child['action_terms']['execution_at'] is None


@pytest.mark.parametrize('executor,check,execution', [('self',NOW+3600,None),('customer',NOW+3600,None),('team',NOW+3600,None),('self',NOW+7200,NOW+3600),('self',None,NOW+3600)])
@run_async
async def test_explicit_same_time_check_does_not_invent_execution(tmp_path,executor,check,execution):
    async with world(tmp_path) as w:
        row=action(w,terms={'executor_kind':executor})
        run=await prepare(w); item=next(x for x in run['items'] if x['current']['record_id']==row['id'])
        run=choose(w,run,item,executor_kind=executor,remind_at=NOW+3600,duration_minutes=30,check_at=check)
        result,_=adopt(w,run,'explicit-check'); assert result['status']=='complete'
        terms=w.crm.get_record(OWNER,row['id'])['action_terms']
        assert terms['executor_kind']==executor and terms['execution_at']==execution and terms['check_at']==check


@run_async
async def test_history_exact_scope_filters_before_paging_and_reads_no_business(tmp_path):
    async with world(tmp_path) as w:
        old=w.progress.create(OWNER,{'request_id':'old-project-plan','kind':'plan','customer_id':w.unit['id'],'opportunity_id':w.project['id'],
            'period':'week','start_date':'2026-10-05','text':'未来一周的保存稿'})
        other=w.crm.create_customer(OWNER,{'name':'其他范围'},NOW)
        for i in range(103):
            w.progress.create(OWNER,{'request_id':'unrelated-'+str(i),'kind':'plan','customer_id':other['id'],'period':'day','start_date':'2026-10-04','text':'无关范围计划'})
        before=counts(w); raw=w.crm._db.execute('SELECT status,revision FROM crm_progress_runs WHERE id=?',(old['id'],)).fetchone()
        data=await w.call('GET',f'/api/progress-workspaces?kind=plan&customer_id={w.unit["id"]}&opportunity_id={w.project["id"]}&contact_id=&record_id=&page_size=20')
        assert data['total']==1 and data['runs'][0]['id']==old['id'] and data['runs'][0]['start_date']=='2026-10-05'
        assert data['runs'][0]['period']=='week' and counts(w)==before
        assert tuple(w.crm._db.execute('SELECT status,revision FROM crm_progress_runs WHERE id=?',(old['id'],)).fetchone())==tuple(raw)
        empty=await w.call('GET',f'/api/progress-workspaces?kind=plan&customer_id={w.unit["id"]}&opportunity_id=&contact_id=&record_id=')
        assert empty['total']==0
        foreign=w.crm.create_customer('other',{'name':'隐藏范围'},NOW)
        await w.call('GET',f'/api/progress-workspaces?customer_id={foreign["id"]}',status=404)
