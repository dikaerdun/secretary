"""Save-first guarantees and races, using an isolated synthetic database only."""
import asyncio
from functools import wraps

import pytest

from secretary.capture_inbox import CaptureService
from secretary.customer_resolution import CustomerResolutionService
from secretary.customer_store import CustomerStore
from secretary.sales_workspace import SalesWorkspace


NOW=1800000000.


def run_async(function):
    @wraps(function)
    def run(*args,**kwargs):return asyncio.run(function(*args,**kwargs))
    return run


@pytest.fixture
def services(tmp_path):
    crm=CustomerStore(tmp_path/'capture-synthetic.sqlite3')
    lock=asyncio.Lock()
    workspace=SalesWorkspace(crm,clock=lambda:NOW)
    service=CaptureService(crm,workspace,lock,resolution=CustomerResolutionService(crm,workspace,lock),clock=lambda:NOW)
    yield crm,workspace,service
    crm.close()


def submit(service, text='昨天医院陈工说签名方案的接口需要再核对', request='save-1', owner='owner'):
    return service.capture(owner,{'text':text,'request_id':request})


def classify(service,identifier,data):
    return service.classify('owner',identifier,{'expected_updated_at':service.get('owner',identifier)['record']['updated_at'],**data})


def test_save_is_atomic_durable_idempotent_and_owner_scoped(services):
    crm,_,service=services
    item=submit(service)
    assert item['status']=='queued' and item['record']['status']=='unfiled'
    assert item['record']['original_content']==item['record']['content']
    assert submit(service)['id']==item['id']
    with pytest.raises(ValueError):submit(service,'改过的话')
    other=submit(service,owner='other')
    assert other['record_id']!=item['record_id']
    with pytest.raises(KeyError):service.get('other',item['id'])
    assert service.list('owner')['total']==1
    assert crm._db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]==0


@run_async
async def test_unconfigured_model_keeps_original_and_permits_manual_use(services):
    crm,workspace,service=services
    item=submit(service)
    await service.process_one()
    item=service.get('owner',item['id'])
    assert item['status']=='needs_details' and '原话已保存' in item['error']
    result=classify(service,item['id'],{'purpose':'note'})
    assert result['record']['status']=='following' and result['record']['customer_id'] is None
    assert result['capture']['purpose']=='note'


class Organizer:
    async def organize(self,content,now,context):
        return {'summary':'核对签名接口','key_points':[],'open_questions':[],
                'actions':[{'title':'核对签名接口','kind':'suggestion','reason':'讨论建议','owner_hint':'我','remind_at':None}]}


@run_async
async def test_analysis_only_proposes_attribution_and_actions(services):
    crm,workspace,service=services
    customer=crm.create_customer('owner',{'name':'星河医院'},NOW)
    item=submit(service,'星河医院想先看看签名接口')
    service.organizer=Organizer()
    await service.process_one()
    ready=service.get('owner',item['id'])
    assert ready['status']=='review' and ready['record']['customer_id'] is None
    assert ready['resolution']['items'][0]['customer_id']==customer['id']
    assert ready['resolution']['selected_customer_id'] is None
    assert len(ready['analysis']['actions'])==1
    assert crm._db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]==0
    assert crm._db.execute('SELECT COUNT(*) FROM crm_records').fetchone()[0]==1


@run_async
async def test_error_does_not_drop_text_or_echo_provider_secrets(services):
    crm,_,service=services
    class Failed:
        async def organize(self,*args):raise RuntimeError('sk-private-provider-message')
    service.organizer=Failed()
    item=submit(service)
    await service.process_one()
    failed=service.get('owner',item['id'])
    assert failed['status']=='failed' and failed['record']['content']==item['record']['content']
    assert 'sk-private' not in failed['error']
    assert service.retry('owner',item['id'])['status']=='queued'


@run_async
async def test_human_edit_and_classification_win_over_running_model(services):
    crm,_,service=services
    started,release=asyncio.Event(),asyncio.Event()
    class Slow(Organizer):
        async def organize(self,*args):
            started.set();await release.wait()
            return await super().organize(*args)
    service.organizer=Slow()
    item=submit(service)
    task=asyncio.create_task(service.process_one())
    await started.wait()
    classify(service,item['id'],{'purpose':'note'})
    release.set();await task
    result=service.get('owner',item['id'])
    assert result['status']=='filed' and result['analysis'] is None
    assert result['record']['status']=='following'


@run_async
async def test_shutdown_requeues_and_edit_cannot_be_overwritten(services):
    crm,_,service=services
    started=asyncio.Event()
    class Slow:
        async def organize(self,*args):started.set();await asyncio.Event().wait()
    service.organizer=Slow()
    item=submit(service)
    task=asyncio.create_task(service.process_one());await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert service.get('owner',item['id'])['status']=='queued'
    service.organizer=Organizer()
    await service.process_one()
    assert service.get('owner',item['id'])['status']=='review'


def test_project_reference_and_schedule_classification_never_create_reminder(services):
    crm,workspace,service=services
    customer=crm.create_customer('owner',{'name':'星河医院'},NOW)
    other=crm.create_customer('owner',{'name':'工业客户'},NOW)
    project=workspace.create_opportunity('owner',customer['id'],{'name':'病历签名'})
    item=submit(service)
    with pytest.raises(ValueError):classify(service,item['id'],{'purpose':'project_reference'})
    with pytest.raises(KeyError):classify(service,item['id'],{'purpose':'project_reference','customer_id':other['id'],'opportunity_id':project['id']})
    result=classify(service,item['id'],{'purpose':'project_reference','customer_id':customer['id'],'opportunity_id':project['id']})
    assert result['record']['kind']=='note' and result['record']['original_content']==item['record']['content']
    links=workspace.workbench('owner',customer['id'])['opportunity_links']
    assert len(links)==1 and not links[0]['stale']
    schedule=submit(service,'下次再拜访陈工',request='save-2')
    result=classify(service,schedule['id'],{'purpose':'schedule','customer_id':customer['id']})
    assert result['record']['kind']=='note' and result['record']['proposal_id'] is None
    assert not crm._db.execute('SELECT 1 FROM crm_analysis_actions').fetchone()
    assert crm._db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]==0


def test_stale_confirmation_rolls_back_and_long_input_limit(services):
    crm,_,service=services
    item=submit(service)
    crm.update_record('owner',item['record_id'],{'content':'补充了新的现场原话'},NOW+1)
    with pytest.raises(ValueError):service.classify('owner',item['id'],{'purpose':'action','expected_updated_at':NOW})
    assert service.get('owner',item['id'])['record']['kind']=='note'
    with pytest.raises(ValueError):submit(service,'字'*20001,request='too-long')
