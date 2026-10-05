"""Actual HTTP visibility flow over disposable fictional records."""
import asyncio
import re
from aiohttp import CookieJar
from aiohttp.test_utils import TestClient,TestServer
from secretary.customer_store import CustomerStore
from secretary.store import Store
from secretary.web import create_app,hash_password

NOW=1800000000.0

def test_http_archive_restore_and_persistent_prompt_archive(tmp_path):
    async def run():
        path=tmp_path/'lifecycle-http-fictional.sqlite3';crm=CustomerStore(path);store=Store(path)
        customer=crm.create_customer('alice',{'name':'测试市场单位（虚构）'},NOW)
        record=crm.create_record('alice',{'title':'临时拜访方向','content':'暂时没有推进价值，稍后再核实','kind':'action','status':'following','customer_id':customer['id']},NOW)
        foreign=crm.create_record('bob',{'title':'另一个人的记录','content':'保持不变'},NOW)
        reply=crm.execute('alice','schedule',{'action':'propose','title':record['title'],'remind_at':NOW+3600},NOW)
        pid=int(re.search(r'P(\d+)',reply)[1]);crm.link_proposal('alice',record['id'],pid,NOW)
        crm.execute('alice','confirm',{'action':'confirm','proposal_id':pid},NOW)
        tid=crm.get_proposal('alice',pid)['task_id']
        app=create_app(store,crm,asyncio.Lock(),'alice',hash_password('fictional-lifecycle-test'),clock=lambda:NOW)
        client=TestClient(TestServer(app),cookie_jar=CookieJar(unsafe=True));await client.start_server()
        try:
            assert (await client.get('/api/record-lifecycle')).status==401
            session=await (await client.post('/api/login',json={'password':'fictional-lifecycle-test'})).json()
            headers={'X-CSRF-Token':session['csrf']};route='/api/records/'+str(record['id'])+'/lifecycle'
            preview=await (await client.get(route)).json()
            assert preview['pending_task_count']==1 and preview['visibility']=='active'
            assert (await client.get('/api/records/'+str(foreign['id'])+'/lifecycle')).status==404
            assert (await client.post(route,json={'action':'archive','snapshot':preview['snapshot']})).status==403
            moved=await (await client.post(route,json={'action':'archive','snapshot':preview['snapshot']},headers=headers)).json()
            assert moved['visibility']=='archived' and moved['effects']['cancelled_tasks']==1
            assert (await client.get('/api/records/'+str(record['id']))).status==404
            assert (await (await client.get('/api/records',params={'q':record['title']})).json())['total']==0
            saved=await (await client.get('/api/record-lifecycle')).json()
            assert saved['items'][0]['record']['original_content']==record['original_content']
            assert store._db.execute('SELECT status FROM tasks WHERE owner=? AND id=?',('alice',tid)).fetchone()['status']=='cancelled'
            assert store.claim_due(NOW+7200) is None
            restored=await (await client.post(route,json={'action':'restore','snapshot':moved['snapshot']},headers=headers)).json()
            assert restored['visibility']=='active'
            assert crm.get_record('alice',record['id'])['status']=='following'
            assert store._db.execute('SELECT status FROM tasks WHERE owner=? AND id=?',('alice',tid)).fetchone()['status']=='cancelled'
            assert (await client.post(route,json={'action':'trash','snapshot':preview['snapshot']},headers=headers)).status==400
            item=(await (await client.get('/api/priorities')).json())['items'][0]
            assert (await client.post('/api/priority-decisions',json={'key':item['key'],'signature':item['signature'],'decision':'archive'},headers=headers)).status==200
            tip=(await (await client.get('/api/priority-archives')).json())['items'][0]
            assert (await client.post('/api/priority-archives/restore',json={'key':tip['key'],'revision':tip['revision']},headers=headers)).status==200
            assert (await (await client.get('/api/priority-archives')).json())['total']==0
            homepage=await client.get('/');assert '/static/record-lifecycle.js?v=' in await homepage.text()
            assert crm.get_record('bob',foreign['id'])['title']=='另一个人的记录'
        finally:await client.close();crm.close();store.close()
    asyncio.run(run())
