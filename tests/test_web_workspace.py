"""HTTP boundaries of the integrated local workspace, using synthetic data."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer
from secretary.customer_store import CustomerStore
from secretary.materials import MaterialService
from secretary.store import Store
from secretary.web import create_app,hash_password


class WorkspaceWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder=tempfile.TemporaryDirectory();path=Path(self.folder.name)/'synthetic.sqlite'
        self.crm,self.store=CustomerStore(path),Store(path)
        self.now=1790989200.0;lock=asyncio.Lock()
        class Organizer:
            async def organize(inner,text,now,context):
                return {'summary':'记录约定','actions':[{'title':'发送方案','kind':'commitment',
                    'reason':'明确原话','owner_hint':'我','evidence':'我答应发送方案','remind_at':None}]}
        self.materials=MaterialService(self.crm,lock,organizer=Organizer(),clock=lambda:self.now)
        app=create_app(self.store,self.crm,lock,'me',hash_password('synthetic-test-password'),
            materials=self.materials,clock=lambda:self.now)
        self.controller=app.middlewares[0].__self__
        self.client=TestClient(TestServer(app),cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        result=await self.client.post('/api/login',json={'password':'synthetic-test-password'})
        self.csrf=(await result.json())['csrf']
        self.customer=self.crm.create_customer('me',{'name':'合成客户','aliases':['合成公司']},self.now)

    async def asyncTearDown(self):
        await self.client.close();await self.materials.close()
        self.crm.close();self.store.close();self.folder.cleanup()

    async def write(self,method,path,data,status=200):
        result=await self.client.request(method,path,json=data,headers={'X-CSRF-Token':self.csrf})
        value=await result.json()
        self.assertEqual(result.status,status,value)
        return value

    async def test_dashboard_project_total_does_not_mix_legacy_or_foreign_money(self):
        self.crm.update_customer('me', self.customer['id'], {'amount_cents': 1000000}, self.now)
        cid=self.customer['id']
        await self.write('POST', f'/api/customers/{cid}/opportunities',
            {'name':'合成试点','amount_cents':18000000,'amount_type':'estimate'},201)
        await self.write('POST', f'/api/customers/{cid}/opportunities',
            {'name':'待核项目'},201)
        response=await self.client.get('/api/dashboard')
        self.assertEqual(response.status,200)
        stats=(await response.json())['stats']
        self.assertEqual(stats['pipeline_cents'],1000000)
        self.assertEqual(stats['project_pipeline_cents'],18000000)
        self.assertEqual(stats['active_projects'],2)
        self.assertEqual(stats['project_unknown_amounts'],1)

    async def test_project_candidate_and_owner_boundaries(self):
        cid=self.customer['id'];base=f'/api/customers/{cid}/opportunities'
        created=await self.write('POST',base,{'name':'数据库加密'},201)
        project=created['opportunity']
        self.assertIsNone(project['amount_cents'])
        await self.write('PATCH',base+f"/{project['id']}",{'expected_revision':1,'amount_cents':500000,'amount_type':'quote'})
        await self.write('PATCH',base+f"/{project['id']}",{'expected_revision':1,'name':'旧页面修改'},400)
        private=self.crm.create_customer('other',{'name':'外部客户'},self.now)
        self.assertEqual((await self.client.get(f"/api/customers/{private['id']}/workbench")).status,404)
        candidate=await self.write('POST','/api/customer-candidates',{'text':'合成公司今天讨论加密'})
        self.assertEqual(candidate['status'],'single');self.assertIsNone(candidate['selected_customer_id'])
        await self.write('POST','/api/opportunity-links',{},400)
        r=self.crm.create_record('me',{'title':'准备资料','customer_id':cid,'kind':'action'},self.now)
        await self.write('POST','/api/opportunity-links',{'entity_type':'record','entity_id':r['id'],'opportunity_id':project['id']})
        bench=await (await self.client.get(f'/api/customers/{cid}/workbench')).json()
        self.assertEqual(bench['opportunity_links'][0]['opportunity_id'],project['id'])
        self.assertEqual(self.crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0],0)

    async def test_queue_decision_is_visible_in_every_source_alias(self):
        created=await self.write('POST','/api/visits',{'title':'合成交流','customer_id':self.customer['id']},201)
        vid=created['visit']['id']
        added=await self.write('POST',f'/api/visits/{vid}/materials',{'role':'recording','provider':'manual',
            'title':'合成录音','text':'我答应发送方案'},202)
        await self.materials.process_one()
        queue=await (await self.client.get('/api/review-inbox')).json()
        action=next(i for i in queue['items'] if i['review_type']=='action')
        await self.write('POST','/api/review-decisions',{'key':action['key'],'signature':action['signature'],'decision':'dismiss'})
        material=await (await self.client.get(f"/api/materials/{added['material']['id']}")).json()
        alias=material['analysis']['actions'][0]
        self.assertEqual(alias['decision_state'],'dismissed');self.assertEqual(alias['review_key'],action['key'])
        visit=await (await self.client.get(f'/api/visits/{vid}')).json()
        self.assertEqual(visit['actions'][0]['decision_state'],'dismissed')
        await self.write('POST','/api/review-decisions',{'key':action['key'],'signature':action['signature'],'decision':'reset'})
        self.assertEqual((await (await self.client.get('/api/review-inbox')).json())['counts']['pending'],1)

    async def test_terms_and_completion_keep_schedule_confirmation_and_snapshot(self):
        r=self.crm.create_record('me',{'title':'发送方案','kind':'action','customer_id':self.customer['id']},self.now)
        terms=await self.write('PATCH',f"/api/records/{r['id']}/terms",{'executor_kind':'self',
            'deadline_date':'2026-10-05','expected_updated_at':None})
        await self.write('PATCH',f"/api/records/{r['id']}/terms",{'executor_kind':'customer','expected_updated_at':None},400)
        agenda=await (await self.client.get('/api/agenda?period=month&date=2026-10-03')).json()
        self.assertTrue(agenda['planning_nodes'][0]['date_only'])
        detail=await (await self.client.get(f"/api/records/{r['id']}")).json()
        self.crm.update_record('me',r['id'],{'content':'后来澄清客户要求'},self.now+1)
        body={'request_id':'finish','result':'已发送','next_step':'检查反馈','remind_at':self.now+3600,
            'expected_snapshot':detail['completion_snapshot']}
        await self.write('POST',f"/api/records/{r['id']}/complete-outcome",body,400)
        updated=await (await self.client.get(f"/api/records/{r['id']}")).json()
        body['expected_snapshot']=updated['completion_snapshot']
        done=await self.write('POST',f"/api/records/{r['id']}/complete-outcome",body)
        self.assertEqual(done['proposal']['status'],'pending')
        self.assertEqual(self.crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0],0)
        self.now+=3601
        replay=await self.write('POST',f"/api/records/{r['id']}/complete-outcome",body)
        self.assertEqual(replay['outcome']['id'],done['outcome']['id'])
        self.assertEqual(replay['next_record']['id'],done['next_record']['id'])

    def completion_proposal(self, record, delta, *, confirm=True):
        owner = 'me'
        key = 'dd28-proposal-' + str(record['id']) + '-' + str(delta)
        self.crm.execute(owner, key, {'action':'propose','title':record['title'],
            'remind_at':self.now+delta,'duration_minutes':30}, self.now)
        identifier = self.crm._db.execute('SELECT max(id) FROM proposals WHERE owner=?', (owner,)).fetchone()[0]
        self.crm.link_proposal(owner, record['id'], identifier, self.now)
        if confirm:
            self.crm.execute(owner, key+'-confirm', {'action':'confirm','proposal_id':identifier}, self.now)
        return self.crm.get_proposal(owner, identifier)

    def completion_business_state(self):
        names = ('crm_records','tasks','proposals','notifications','crm_record_proposals',
                 'crm_activities','crm_action_outcomes','crm_action_terms',
                 'crm_secretary_plans','crm_arrangement_notifications')
        return {name:[tuple(row) for row in self.crm._db.execute('SELECT * FROM '+name+' ORDER BY rowid')]
                for name in names}

    async def test_dd28_get_completion_effects_exactly_match_write_and_exclude_child(self):
        record = self.crm.create_record('me', {'title':'核对会后结果','kind':'action',
            'status':'following','customer_id':self.customer['id']}, self.now)
        first = self.completion_proposal(record, 3600)
        second = self.completion_proposal(record, 7200)
        pending = self.completion_proposal(record, 10800, confirm=False)
        child = self.crm.create_record('me', {'title':'另一次活动','kind':'action',
            'status':'following','customer_id':self.customer['id'],'parent_record_id':record['id']}, self.now)
        child_proposal = self.completion_proposal(child, 14400)
        before = self.completion_business_state()
        changes = self.crm._db.total_changes

        response = await self.client.get(f"/api/records/{record['id']}")
        self.assertEqual(response.status, 200)
        detail = await response.json()
        effects = detail['completion_effects']
        self.assertEqual(effects['scope'], 'record_completion')
        self.assertEqual(effects['record_id'], record['id'])
        self.assertEqual(effects['snapshot'], detail['completion_snapshot'])
        self.assertEqual(effects['pending_task_count'], 2)
        self.assertEqual(effects['pending_proposal_count'], 1)
        expected_task_ids = {first['task_id'], second['task_id']}
        self.assertEqual({item['id'] for item in effects['pending_tasks']}, expected_task_ids)
        self.assertEqual(effects['pending_proposals'], [{key:pending[key]
            for key in ('id','title','remind_at','change_kind','status')}])
        for item in effects['pending_tasks']:
            task = self.crm.get_task('me', item['id'])
            self.assertEqual(item, {key:task[key]
                for key in ('id','title','remind_at','duration_minutes','status','revision')})
        # The old summary only sees the current proposal and child record;
        # it is deliberately different from the full record completion scope.
        self.assertEqual({item['id'] for item in detail['active_reminders']}, {child_proposal['task_id']})
        self.assertEqual(self.crm._db.total_changes, changes)
        self.assertEqual(self.completion_business_state(), before)

        done = await self.write('POST', f"/api/records/{record['id']}/complete-outcome", {
            'request_id':'dd28-explicit-complete','result':'实际结果已核对','expected_snapshot':effects['snapshot']})
        self.assertEqual(done['record']['status'], 'done')
        self.assertEqual({row['id'] for row in self.crm._db.execute("SELECT id FROM tasks WHERE status='completed'")},
                         expected_task_ids)
        self.assertEqual({row['id'] for row in self.crm._db.execute("SELECT id FROM proposals WHERE status='rejected'")},
                         {pending['id']})
        self.assertEqual(self.crm.get_task('me', child_proposal['task_id'])['status'], 'pending')
        self.assertEqual(self.crm.get_record('me', child['id'])['status'], 'following')

    async def test_dd28_get_snapshot_rejects_changed_historical_task_atomically_with_original_400(self):
        record = self.crm.create_record('me', {'title':'核对历史活动','kind':'action','status':'following'}, self.now)
        first = self.completion_proposal(record, 3600)
        self.completion_proposal(record, 7200)
        seen = await (await self.client.get(f"/api/records/{record['id']}")).json()
        old = seen['completion_effects']
        self.crm.execute('me', 'dd28-change-history', {'action':'snooze','task_id':first['task_id'],
            'remind_at':self.now+18000}, self.now+1)
        before = self.completion_business_state()
        error = await self.write('POST', f"/api/records/{record['id']}/complete-outcome", {
            'request_id':'dd28-stale-complete','result':'保留原稿','expected_snapshot':old['snapshot']}, 400)
        self.assertIn('变化', error['error'])
        self.assertEqual(self.completion_business_state(), before)
        latest = await (await self.client.get(f"/api/records/{record['id']}")).json()
        self.assertNotEqual(latest['completion_snapshot'], old['snapshot'])
        self.assertEqual(latest['completion_effects']['snapshot'], latest['completion_snapshot'])
        history = next(item for item in latest['completion_effects']['pending_tasks'] if item['id']==first['task_id'])
        self.assertEqual(history['remind_at'], self.now+18000)
        self.assertEqual(history['revision'], old['pending_tasks'][0]['revision']+1)

    async def test_dd28_completion_get_preserves_owner_visibility_and_session_boundaries(self):
        foreign = self.crm.create_record('other', {'title':'私有待办','kind':'action'}, self.now)
        hidden = self.crm.create_record('me', {'title':'不可见待办','kind':'action'}, self.now)
        visible = self.crm.create_record('me', {'title':'无关联任务待办','kind':'action'}, self.now)
        with self.crm._transaction() as db:
            db.execute('UPDATE crm_records SET hidden=1 WHERE id=?', (hidden['id'],))
        before = self.completion_business_state()
        self.assertEqual((await self.client.get(f"/api/records/{foreign['id']}")).status, 404)
        self.assertEqual((await self.client.get(f"/api/records/{hidden['id']}")).status, 404)
        detail = await (await self.client.get(f"/api/records/{visible['id']}")).json()
        self.assertEqual(detail['completion_effects']['pending_tasks'], [])
        self.assertEqual(detail['completion_effects']['pending_proposals'], [])
        self.assertEqual(detail['completion_effects']['pending_task_count'], 0)
        self.assertEqual(detail['completion_effects']['pending_proposal_count'], 0)
        self.assertEqual(self.completion_business_state(), before)
        await self.write('POST', '/api/logout', {})
        self.assertEqual((await self.client.get(f"/api/records/{visible['id']}")).status, 401)

    async def test_new_endpoints_require_session_and_csrf(self):
        forbidden=await self.client.post('/api/review-decisions',json={})
        self.assertEqual(forbidden.status,403)
        await self.write('POST','/api/logout',{})
        for path in ('/api/review-inbox','/api/priorities',f"/api/customers/{self.customer['id']}/workbench"):
            self.assertEqual((await self.client.get(path)).status,401)

    async def test_followup_communication_archives_original_record_and_changes_do_not(self):
        r=self.crm.create_record('me',{'title':'拜访','kind':'action','customer_id':self.customer['id']},self.now)
        fresh=await self.write('POST',f"/api/records/{r['id']}/activities/organize",{'content':'今天和客户讨论了方案边界'},201)
        self.assertIsNotNone(fresh['visit_ref'])
        original=await (await self.client.get(f"/api/records/{fresh['record']['id']}")).json()
        self.assertEqual(original['visit_ref']['record_id'],fresh['record']['id'])
        count=self.controller.visits.list('me')['total']
        # No active task: save the original spoken change for clarification.
        await self.write('POST',f"/api/records/{r['id']}/activities/organize",{'content':'改到明天下午三点','request_id':'change'})
        self.assertEqual(self.controller.visits.list('me')['total'],count)
