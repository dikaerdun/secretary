"""Authenticated full HTTP lifecycle; isolated synthetic records only."""
import asyncio
from aiohttp import CookieJar
from aiohttp.test_utils import TestClient,TestServer
from deploy.training import build_training_app,training_config,PASSWORD,OWNER
from tests.test_secretary_flow import ScriptedInterpreter,initial,arranged,NOW,START


def test_real_http_date_only_schedule_reschedule_recap_and_mobile_assets(tmp_path):
    async def run():
        app=await build_training_app(training_config(root=tmp_path),clock=lambda:NOW)
        webcrm=next(route.handler.__self__ for route in app.router.routes() if getattr(route.handler,'__name__','')=='dashboard')
        flow=webcrm.secretary_flow
        flow.interpreter=ScriptedInterpreter([initial(),arranged(),{'intent':'recap','changes':{},'summary':'饭后结论保留'}])
        client=TestClient(TestServer(app),cookie_jar=CookieJar(unsafe=True));await client.start_server()
        try:
            assert (await client.post('/api/secretary/turns',json={'text':'不能未登录写入','request_id':'anon'})).status==401
            session=await (await client.post('/api/login',json={'password':PASSWORD})).json()
            headers={'X-CSRF-Token':session['csrf']}
            assert (await client.post('/api/secretary/turns',json={'text':'不能绕过csrf','request_id':'csrf'})).status==403
            async def say(text,key,plan=None):
                body={'text':text,'request_id':key}
                if plan:body.update(plan_id=plan['id'],expected_revision=plan['revision'])
                response=await client.post('/api/secretary/turns',json=body,headers=headers);assert response.status==202
                turn=(await response.json())['turn']
                for _ in range(50):
                    await asyncio.sleep(.03)
                    turn=(await (await client.get(f"/api/secretary/turns/{turn['id']}")).json())['turn']
                    if turn['status'] not in ('queued','processing'):break
                assert turn['status']=='done',turn.get('error')
                return turn
            first=await say('10月8日约林博士吃饭','first')
            agenda=await (await client.get('/api/agenda?date=2026-10-08')).json()
            assert first['plan']['id'] in [p['id'] for p in agenda['secretary_plans']]
            assert first['plan']['start_at'] is None
            second=await say('主要聊后量子平台合作，顺带问title。晚上六点，已经约好了，提前一个小时提醒','answer',first['plan'])
            assert second['plan']['start_at']==START and second['plan']['reminder_at']==START-3600
            assert second['plan']['visit_id']
            stale=await client.post('/api/secretary/turns',json={'text':'旧页改期','request_id':'stale','plan_id':first['plan']['id'],'expected_revision':1},headers=headers)
            assert stale.status==409
            agenda=await (await client.get('/api/agenda?date=2026-10-08')).json()
            assert not agenda['secretary_plans']
            assert any(p['id']==second['plan']['task_id'] for p in agenda['items'])
            webcrm.secretary_flow.clock=lambda:START+7200
            recap=await say('饭吃完了，复盘：结论是下次看方案','recap',second['plan'])
            assert recap['plan']['status']=='recapped'
            details=(await (await client.get(f"/api/secretary/plans/{first['plan']['id']}")).json())['plan']
            assert len(details['turns'])==3
            source=webcrm.crm.get_record(OWNER,first['record_id']);assert source['original_content']=='10月8日约林博士吃饭'
            html=await (await client.get('/')).text()
            assert '/static/secretary-flow.js?v=' in html
            assert (await client.get('/static/secretary-flow.css')).status==200
        finally:await client.close()
    asyncio.run(run())

def test_customer_contact_project_and_every_turn_share_one_exchange(tmp_path):
    async def run():
        app=await build_training_app(training_config(root=tmp_path),clock=lambda:NOW)
        controller=next(route.handler.__self__ for route in app.router.routes() if getattr(route.handler,'__name__','')=='dashboard')
        crm,flow=controller.crm,controller.secretary_flow
        customer=crm.create_customer(OWNER,{'name':'虚构后量子研究单位'},NOW)
        contact=crm.create_contact(OWNER,customer['id'],{'name':'查飞','role':'研究负责人'},NOW)
        project=controller.sales_workspace.create_opportunity(OWNER,customer['id'],{'name':'虚构合作项目'})
        flow.interpreter=ScriptedInterpreter([initial(),arranged(),{'intent':'recap','changes':{},'summary':'原始复盘和计划统一归档'}])
        client=TestClient(TestServer(app),cookie_jar=CookieJar(unsafe=True));await client.start_server()
        try:
            session=await (await client.post('/api/login',json={'password':PASSWORD})).json();headers={'X-CSRF-Token':session['csrf']}
            first=flow.submit(OWNER,{'text':'10月8日约林博士吃饭','request_id':'first','customer_id':customer['id'],'contact_id':contact['id'],'opportunity_id':project['id']})
            await flow.process_one();one=flow.turn(OWNER,first['id']);assert one['status']=='done',one['error']
            plan=one['plan'];assert plan['person']=='查飞'
            for key,text in [('next','主要聊后量子平台合作，顺带问title。晚上六点，已经约好了，提前一个小时提醒'),('recap','饭吃完了，复盘：下次关注技术方案')]:
                if key=='recap':flow.clock=lambda:START+7200
                turn=flow.submit(OWNER,{'text':text,'request_id':key,'plan_id':plan['id'],'expected_revision':plan['revision']})
                await flow.process_one();turn=flow.turn(OWNER,turn['id']);assert turn['status']=='done',turn['error'];plan=turn['plan']
            all_turns=flow.plan(OWNER,plan['id'])['turns']
            assert len(all_turns)==3 and plan['status']=='recapped'
            rows=crm._db.execute('SELECT record_id,role FROM crm_visit_records WHERE owner=? AND visit_id=?',(OWNER,plan['visit_id'])).fetchall()
            assert {r['record_id'] for r in rows}=={t['record_id'] for t in all_turns}
            assert any(r['role']=='recap' for r in rows)
            link=crm._db.execute("SELECT * FROM crm_opportunity_links WHERE owner=? AND entity_type='visit' AND entity_id=?",(OWNER,plan['visit_id'])).fetchone()
            visit=controller.sales_workspace._entity(crm._db,OWNER,'visit',plan['visit_id'])
            assert link['opportunity_id']==project['id']
            assert link['source_snapshot']==controller.sales_workspace._link_snapshot(crm._db,OWNER,'visit',visit)
            response=await client.get(f"/api/timeline?contact_id={contact['id']}")
            assert response.status==200
            timeline=await response.json()
            assert '下次关注技术方案' in str(timeline)
            original=crm.get_record(OWNER,all_turns[0]['record_id'])
            assert original['original_content']=='10月8日约林博士吃饭'
        finally:await client.close()
    asyncio.run(run())
