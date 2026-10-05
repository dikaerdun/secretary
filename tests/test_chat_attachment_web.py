import asyncio
from aiohttp import CookieJar, FormData
from aiohttp.test_utils import TestClient, TestServer

from secretary.customer_store import CustomerStore
from secretary.materials import MaterialService
from secretary.store import Store
from secretary.web import create_app, hash_password


def test_file_upload_auth_csrf_original_download_and_replay(tmp_path):
    async def run():
        path = tmp_path/'uploads.sqlite3'
        crm, store = CustomerStore(path), Store(path)
        lock = asyncio.Lock()
        service = MaterialService(crm, lock)
        app = create_app(store, crm, lock, 'alice', hash_password('attachment-test-password'), materials=service)
        client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
        await client.start_server()
        try:
            def form(key='upload-1', name='材料.txt', raw=None):
                data = FormData(); data.add_field('request_id', key)
                data.add_field('file', raw or '项目汇报：预算尚未审批，建议先试点。'.encode(), filename=name, content_type='application/octet-stream')
                return data
            assert (await client.post('/api/materials/upload', data=form())).status == 401
            session = await (await client.post('/api/login', json={'password':'attachment-test-password'})).json()
            assert (await client.post('/api/materials/upload', data=form())).status == 403
            headers = {'X-CSRF-Token':session['csrf']}
            async def parsed(mid):
                for _ in range(60):
                    value=await (await client.get('/api/materials/'+str(mid))).json()
                    if value['document']['parse_status']!='parsing':return value
                    await asyncio.sleep(.02)
                assert False,'background file parser did not finish'
            response = await client.post('/api/materials/upload', data=form(), headers=headers)
            assert response.status == 202
            value = await response.json(); mid = value['material']['id']
            assert value['attachment']['parse_status'] == 'parsing'
            assert (await parsed(mid))['document']['parse_status']=='ready'
            listed = await (await client.get('/api/materials', params={'q':value['attachment']['filename'],'page_size':'1'})).json()
            assert listed['total'] == 1 and listed['page'] == 1 and listed['pages'] == 1
            assert len(listed['items']) == 1 and listed['items'][0]['id'] == mid
            listed_document = listed['items'][0]['document']
            assert listed_document['parse_status'] == 'ready' and listed_document['filename'] == value['attachment']['filename']
            assert listed_document['download_url'] == value['attachment']['download_url']
            assert set(listed_document) == {'material_id','filename','size','parse_status','parse_error','truncated','download_url'}
            repeat = await (await client.post('/api/materials/upload', data=form(), headers=headers)).json()
            assert repeat['material']['id'] == mid
            download = await client.get(value['attachment']['download_url'])
            assert download.status == 200 and await download.read() == '项目汇报：预算尚未审批，建议先试点。'.encode()
            assert 'attachment;' in download.headers['Content-Disposition']
            assert (await client.post('/api/materials/upload', data=form('unsafe','run.exe',b'content'),headers=headers)).status == 400
            image = await (await client.post('/api/materials/upload', data=form('image','扫描.png',b'\x89PNG\r\n\x1a\nimage'), headers=headers)).json()
            image_detail=await parsed(image['material']['id'])
            assert image_detail['document']['parse_status'] == 'needs_text'
            image_list = await (await client.get('/api/materials', params={'q':image['attachment']['filename']})).json()
            assert image_list['items'][0]['document'] == image_detail['document']
            rejected=await client.patch('/api/materials/'+str(image['material']['id']),json={'revision':image_detail['material']['revision'],'text':image_detail['text']},headers=headers)
            assert rejected.status==400
            corrected = await client.patch('/api/materials/'+str(image['material']['id']), json={'revision':image_detail['material']['revision'],'text':'用户补充：项目范围待核实。'}, headers=headers)
            assert corrected.status == 200
            detail = await (await client.get('/api/materials/'+str(image['material']['id']))).json()
            assert detail['document']['parse_status'] == 'ready' and detail['text'] == '用户补充：项目范围待核实。'
            legacy = service.enqueue('alice', {'provider':'manual','title':'legacy-only-text','text':'旧文本材料。'})
            service.enqueue('bob', {'provider':'manual','title':'legacy-only-text','text':'其他用户文本。'})
            legacy_list = await (await client.get('/api/materials', params={'q':'legacy-only-text','page_size':'1'})).json()
            assert legacy_list['total'] == 1 and legacy_list['items'][0]['id'] == legacy['id']
            assert 'document' not in legacy_list['items'][0]
            assert crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
        finally:
            await client.close(); crm.close(); store.close()
    asyncio.run(run())


def test_original_is_committed_before_slow_background_extraction(tmp_path,monkeypatch):
    import threading
    from secretary import document_attachments as docs
    started,released=threading.Event(),threading.Event()
    original=docs.extract_document
    def slow(name,raw):
        started.set()
        released.wait(5)
        return original(name,raw)
    monkeypatch.setattr(docs,'extract_document',slow)
    async def run():
        path=tmp_path/'durable.sqlite3';crm,store=CustomerStore(path),Store(path)
        lock=asyncio.Lock();materials=MaterialService(crm,lock)
        app=create_app(store,crm,lock,'alice',hash_password('attachment-test-password'),materials=materials)
        client=TestClient(TestServer(app),cookie_jar=CookieJar(unsafe=True));await client.start_server()
        try:
            session=await (await client.post('/api/login',json={'password':'attachment-test-password'})).json()
            def form():
                data=FormData();data.add_field('request_id','stable-upload');data.add_field('file',b'real scope pending approval',filename='pending.txt');return data
            response=await client.post('/api/materials/upload',data=form(),headers={'X-CSRF-Token':session['csrf']})
            assert response.status==202
            uploaded=await response.json();mid=uploaded['material']['id']
            assert not released.is_set() and uploaded['attachment']['parse_status']=='parsing'
            assert crm._db.execute('SELECT content FROM crm_document_files WHERE material_id=?',(mid,)).fetchone()[0]==b'real scope pending approval'
            listed = await (await client.get('/api/materials',params={'q':'pending.txt'})).json()
            assert listed['items'][0]['document']['parse_status'] == 'parsing'
            assert listed['items'][0]['document']['filename'] == 'pending.txt'
            assert listed['items'][0]['document']['download_url'] == uploaded['attachment']['download_url']
            replay=await (await client.post('/api/materials/upload',data=form(),headers={'X-CSRF-Token':session['csrf']})).json()
            assert replay['material']['id']==mid
            released.set()
            for _ in range(60):
                detail=await (await client.get('/api/materials/'+str(mid))).json()
                if detail['document']['parse_status']=='ready':break
                await asyncio.sleep(.02)
            assert started.is_set() and detail['text']=='real scope pending approval'
            assert detail['source']=='DOCUMENT' and detail['original_version']['text']==detail['text']
        finally:released.set();await client.close();crm.close();store.close()
    asyncio.run(run())
