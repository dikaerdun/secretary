"""Uploaded reference documents reach scoped discussions on disposable data."""
import asyncio
import json
from datetime import datetime

import pytest

from secretary.customer_store import CustomerStore
from secretary.customer_timeline import TimelineService
from secretary.document_attachments import DocumentAttachmentService
from secretary.materials import MaterialService
from secretary.sales_discussion import DiscussionService, MAX_CONTEXT_LENGTH
from secretary.sales_workspace import SalesWorkspace
from secretary.secretary_flow import SecretaryFlow
from secretary.store import SHANGHAI
from secretary.visits import VisitService


NOW = datetime(2026, 10, 5, 12, tzinfo=SHANGHAI).timestamp()


class Interpreter:
    async def interpret(self, text, now, context):
        return {'intent': 'plan', 'changes': {'title': '准备技术交流', 'goal': '核对试点条件'},
                'evidence': {'goal': '主要聊试点条件'}}


class Organizer:
    async def organize(self, text, now, context):
        return {'summary': '文件参考摘要', 'key_points': [], 'open_questions': [], 'actions': []}


class Advisor:
    def __init__(self):
        self.contexts = []

    async def reply(self, context, history, text, now):
        self.contexts.append(context)
        return {'answer': '文件是参考方案，建议向客户核实实际条件。', 'next_moves': [], 'questions': [], 'risks': []}


@pytest.fixture
def stack(tmp_path):
    crm = CustomerStore(tmp_path / 'discussion-attachment-synthetic.sqlite3')
    lock = asyncio.Lock()
    workspace = SalesWorkspace(crm, clock=lambda: NOW)
    materials = MaterialService(crm, lock, organizer=Organizer(), clock=lambda: NOW)
    documents = DocumentAttachmentService(crm, materials, clock=lambda: NOW)
    visits = VisitService(crm, materials, lock)
    timeline = TimelineService(crm, workspace, visits=visits, clock=lambda: NOW)
    flow = SecretaryFlow(crm, workspace, lock, visits=visits, timeline=timeline,
                         interpreter=Interpreter(), clock=lambda: NOW)
    advisor = Advisor()
    discussion = DiscussionService(crm, workspace, lock, advisor=advisor, clock=lambda: NOW)
    discussion.timeline = timeline
    customer = crm.create_customer('owner', {'name': '测试银行'}, NOW)
    first_project = workspace.create_opportunity('owner', customer['id'], {'name': '试点A'})
    second_project = workspace.create_opportunity('owner', customer['id'], {'name': '试点B'})
    yield crm, materials, documents, flow, discussion, advisor, customer, first_project, second_project
    crm.close()


def upload(documents, key, text='参考方案：先核对试点范围。', owner='owner'):
    return documents.upload(owner, key + '.txt', text.encode(), key)['material']


def plan(flow, customer, source_ids, *, project=None, contact=None, key='plan'):
    data = {'request_id': key, 'text': '主要聊试点条件', 'customer_id': customer['id'], 'material_ids': source_ids}
    if project:
        data['opportunity_id'] = project['id']
    if contact:
        data['contact_id'] = contact['id']
    turn = flow.submit('owner', data)
    assert asyncio.run(flow.process_one())
    return flow.turn('owner', turn['id'])['plan']


def thread(discussion, customer, *, project=None, source=None, contact=None):
    data = {'customer_id': customer['id']}
    if project:
        data['opportunity_id'] = project['id']
    if source:
        data['source_record_id'] = source
    if contact:
        data['contact_id'] = contact['id']
    public = discussion.create_thread('owner', data)['thread']
    return discussion._require_thread(discussion.crm._db, 'owner', public['id'])


def test_uploaded_flow_plan_reference_reaches_advisor_and_evidence_metadata(stack):
    crm, _, documents, flow, discussion, advisor, customer, project, _ = stack
    source = upload(documents, 'reference', '文件建议：核对设备兼容性。手机号13812345678仅用于联系。')
    saved = plan(flow, customer, [source['id']], project=project)
    selected = thread(discussion, customer, project=project, source=saved['record_id'])
    result = asyncio.run(discussion.send_message('owner', selected['id'], {'request_id': 'question', 'text': '根据附件准备交流问题'}))
    evidence = advisor.contexts[0]['document_attachments'][0]
    assert evidence['material_id'] == source['id'] and evidence['version_id']
    assert evidence['plan_ids'] == [saved['id']] and evidence['filename'] == 'reference.txt'
    assert '核对设备兼容性' in evidence['text'] and '13812345678' not in evidence['text']
    assert evidence['source_type'] == 'document_reference' and evidence['basis'] == 'observation'
    assert evidence['customer_statement_confirmed'] is False
    assistant = next(item for item in result['messages'] if item['role'] == 'assistant')
    attached = next(item for item in assistant['sources'] if item.get('material_id') == source['id'])
    assert attached['version_id'] == evidence['version_id'] and attached['source_type'] == 'document_reference'
    assert crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0


def test_selected_plan_excludes_other_plan_same_customer_project(stack):
    _, _, documents, flow, discussion, _, customer, project, _ = stack
    first = upload(documents, 'first', '当前计划资料。')
    second = upload(documents, 'second', '另一计划资料。')
    chosen = plan(flow, customer, [first['id']], project=project, key='chosen')
    plan(flow, customer, [second['id']], project=project, key='other')
    selected = thread(discussion, customer, project=project, source=chosen['record_id'])
    raw = discussion._raw_context('owner', selected)
    assert [item['material_id'] for item in raw['document_attachments']] == [first['id']]


def test_wrong_project_customer_owner_and_contact_are_excluded(stack):
    crm, _, documents, flow, discussion, _, customer, project, another = stack
    one = upload(documents, 'scope', '只属于试点A的参考。')
    saved = plan(flow, customer, [one['id']], project=project)
    assert discussion._raw_context('owner', thread(discussion, customer, project=another))['document_attachments'] == []
    assert discussion._raw_context('owner', thread(discussion, customer))['document_attachments'] == []
    wrong_customer = crm.create_customer('owner', {'name': '其他客户'}, NOW)
    assert discussion._raw_context('owner', thread(discussion, wrong_customer))['document_attachments'] == []
    foreign = upload(documents, 'private', '另一owner的秘密参考', owner='other')
    scoped = thread(discussion, customer, project=project)
    with crm._transaction() as db:
        db.execute('PRAGMA defer_foreign_keys=ON')
        # Simulate a corrupted legacy reference without moving the foreign material.
        db.execute("INSERT INTO crm_secretary_plan_attachments VALUES (?,?,?,?)", ('owner', saved['id'], foreign['id'], NOW))
        raw = discussion._raw_context('owner', scoped)
        assert '另一owner的秘密' not in json.dumps(raw, ensure_ascii=False)
        db.execute('DELETE FROM crm_secretary_plan_attachments WHERE owner=? AND material_id=?', ('owner', foreign['id']))
    person = crm.create_contact('owner', customer['id'], {'name': '李工'}, NOW)
    focus = thread(discussion, customer, contact=person)
    assert discussion._raw_context('owner', focus)['document_attachments'] == []


def test_unreadable_document_never_sends_placeholder_to_discussion(stack):
    _, _, documents, flow, discussion, _, customer, _, _ = stack
    source = documents.upload('owner', '扫描.png', b'\x89PNG\r\n\x1a\nimage', 'image')['material']
    saved = plan(flow, customer, [source['id']])
    raw = discussion._raw_context('owner', thread(discussion, customer, source=saved['record_id']))
    assert raw['document_attachments'] == []


def test_document_text_and_total_context_are_bounded(stack):
    _, _, documents, flow, discussion, _, customer, _, _ = stack
    sources = [upload(documents, 'long' + str(i), ('第%d份材料。' % i) + '范围需要核对。' * 3000) for i in range(6)]
    saved = plan(flow, customer, [item['id'] for item in sources])
    safe, _, evidence, _ = discussion._context_snapshot('owner', thread(discussion, customer, source=saved['record_id']))
    assert safe['document_attachments']
    assert all(len(item['text']) <= 6000 and item['truncated'] for item in safe['document_attachments'])
    assert sum(len(item['text']) for item in safe['document_attachments']) <= 18000
    assert len(json.dumps(safe, ensure_ascii=False, separators=(',', ':'))) <= MAX_CONTEXT_LENGTH


def test_changed_document_invalidates_existing_advice_snapshot(stack):
    crm, materials, documents, flow, discussion, _, customer, _, _ = stack
    source = upload(documents, 'changing', '旧版文件建议核对范围。')
    saved = plan(flow, customer, [source['id']])
    selected = thread(discussion, customer, source=saved['record_id'])
    result = asyncio.run(discussion.send_message('owner', selected['id'], {'request_id': 'question', 'text': '如何推进？'}))
    assistant_id = next(item['id'] for item in result['messages'] if item['role'] == 'assistant')
    current = materials.detail('owner', source['id'])['material']
    materials.update('owner', source['id'], {'revision': current['revision'], 'text': '新版文件：需要重新核对验证标准。'})
    assert next(item for item in discussion.get_thread('owner', selected['id'])['messages'] if item['id'] == assistant_id)['stale'] is True


def test_explicit_material_summary_source_without_plan_reads_document(stack):
    _, materials, documents, _, discussion, _, customer, _, _ = stack
    source = upload(documents, 'standalone', '只有文件资料：先核实采购口径。')
    current = materials.detail('owner', source['id'])['material']
    materials.update('owner', source['id'], {'revision': current['revision'], 'customer_id': customer['id']})
    assert asyncio.run(materials.process_one())
    detail = materials.detail('owner', source['id'])
    assert detail['material']['record_id']
    selected = thread(discussion, customer, source=detail['material']['record_id'])
    raw = discussion._raw_context('owner', selected)
    assert raw['document_attachments'][0]['material_id'] == source['id']
    assert raw['document_attachments'][0]['plan_ids'] == []


def test_updated_project_link_is_excluded_until_reconfirmed(stack):
    _, materials, documents, flow, discussion, _, customer, project, _ = stack
    source = upload(documents, 'stale-project')
    saved = plan(flow, customer, [source['id']], project=project)
    selected = thread(discussion, customer, project=project, source=saved['record_id'])
    current = materials.detail('owner', source['id'])['material']
    materials.update('owner', source['id'], {'revision': current['revision'], 'text': '来源已修订，项目需重新核对。'})
    assert discussion._raw_context('owner', selected)['document_attachments'] == []


def test_direct_discussion_upload_only_replays_and_keeps_thread_reference(stack):
    crm, _, documents, _, discussion, advisor, customer, project, _ = stack
    source = upload(documents, 'direct', '技术方案参考：先检查兼容性。')
    selected = thread(discussion, customer, project=project)
    payload = {'request_id': 'file-only', 'material_ids': [source['id']]}
    result = asyncio.run(discussion.send_message('owner', selected['id'], payload))
    user = next(item for item in result['messages'] if item['role'] == 'user')
    assert user['text'] == '补充讨论材料' and user['original_text'] == ''
    assert user['attachments'][0]['material_id'] == source['id']
    assert advisor.contexts[0]['document_attachments'][0]['material_id'] == source['id']
    assert asyncio.run(discussion.send_message('owner', selected['id'], payload))['messages'] == result['messages']
    assert len(advisor.contexts) == 1
    with pytest.raises(ValueError):
        asyncio.run(discussion.send_message('owner', selected['id'], {**payload, 'text': '改了文字'}))
    asyncio.run(discussion.send_message('owner', selected['id'], {'request_id': 'next', 'text': '继续讨论验证条件'}))
    assert advisor.contexts[-1]['document_attachments'][0]['material_id'] == source['id']
    assert crm._db.execute('SELECT count(*) FROM crm_secretary_plan_attachments').fetchone()[0] == 0
    assert crm._db.execute('SELECT count(*) FROM crm_visit_sources').fetchone()[0] == 0


def test_direct_discussion_rejects_foreign_and_different_project_before_message(stack):
    crm, _, documents, flow, discussion, _, customer, project, other = stack
    foreign = upload(documents, 'other-owner', '私有内容', owner='other')
    wrong_project = upload(documents, 'wrong-project')
    plan(flow, customer, [wrong_project['id']], project=other)
    selected = thread(discussion, customer, project=project)
    for index, source in enumerate((foreign, wrong_project)):
        with pytest.raises((ValueError, KeyError)):
            asyncio.run(discussion.send_message('owner', selected['id'], {'request_id': 'invalid' + str(index),
                'text': '结合文件继续', 'material_ids': [source['id']]}))
    assert crm._db.execute('SELECT count(*) FROM crm_sales_discussion_messages').fetchone()[0] == 0


def test_direct_needs_text_metadata_is_preserved_without_placeholder(stack):
    _, _, documents, _, discussion, advisor, customer, _, _ = stack
    source = documents.upload('owner', '扫描.png', b'\x89PNG\r\n\x1a\nimage', 'direct-image')['material']
    selected = thread(discussion, customer)
    result = asyncio.run(discussion.send_message('owner', selected['id'], {'request_id': 'image',
        'text': '先讨论我的交流目标', 'material_ids': [source['id']]}))
    user = next(item for item in result['messages'] if item['role'] == 'user')
    assert user['attachments'][0]['parse_status'] == 'needs_text'
    assert user['attachment_status'] == 'partial'
    assert advisor.contexts[0]['document_attachments'] == []
    assert '正文尚未识别' not in json.dumps(advisor.contexts[0], ensure_ascii=False)


@pytest.mark.parametrize('ids', [None, '1', [True], [0], [1, 1], list(range(1, 12))])
def test_direct_invalid_attachment_list_is_rejected(stack, ids):
    crm, _, _, _, discussion, _, customer, _, _ = stack
    selected = thread(discussion, customer)
    with pytest.raises(ValueError):
        asyncio.run(discussion.send_message('owner', selected['id'], {'request_id': 'invalid', 'text': '原话', 'material_ids': ids}))
    assert crm._db.execute('SELECT count(*) FROM crm_sales_discussion_messages').fetchone()[0] == 0


def test_background_parser_pending_keeps_flow_queued_until_real_text(stack):
    _, _, documents, flow, _, _, customer, project, _ = stack
    source=documents.upload('owner','pending.txt',b'real original content','background',parsed={
        'text':'','parse_status':'parsing','parse_error':'','truncated':False})['material']
    turn=flow.submit('owner',{'request_id':'pending-flow','text':'主要聊试点条件',
        'customer_id':customer['id'],'opportunity_id':project['id'],'material_ids':[source['id']]})
    assert turn['attachment_status']=='waiting' and turn['attachments'][0]['file_parse_status']=='parsing'
    assert asyncio.run(flow.process_one()) is False
    documents.finish_parse('owner',source['id'],{'text':'解析后的正文：试点范围待核对。',
        'parse_status':'ready','parse_error':'','truncated':False})
    assert asyncio.run(flow.process_one())
    assert flow.turn('owner',turn['id'])['attachments'][0]['parse_status']=='ready'


def test_contact_scope_reads_only_explicitly_matching_plan_reference(stack):
    crm, _, documents, flow, discussion, _, customer, _, _ = stack
    first=crm.create_contact('owner',customer['id'],{'name':'王工'},NOW)
    second=crm.create_contact('owner',customer['id'],{'name':'李工'},NOW)
    source=upload(documents,'wang','为王工准备的参考。')
    saved=plan(flow,customer,[source['id']],contact=first)
    focused=thread(discussion,customer,contact=first,source=saved['record_id'])
    assert discussion._raw_context('owner',focused)['document_attachments'][0]['material_id']==source['id']
    other=thread(discussion,customer,contact=second)
    assert discussion._raw_context('owner',other)['document_attachments']==[]


def test_initial_background_parse_preserves_explicit_discussion_project_link(stack):
    _, _, documents, _, discussion, advisor, customer, project, _ = stack
    source=documents.upload('owner','pending-project.txt',b'original bytes','background-project',parsed={
        'text':'','parse_status':'parsing','parse_error':'','truncated':False})['material']
    selected=thread(discussion,customer,project=project)
    first=asyncio.run(discussion.send_message('owner',selected['id'],{
        'request_id':'pending-message','text':'先保存参考文件，继续讨论目标','material_ids':[source['id']]}))
    user=next(item for item in first['messages'] if item['role']=='user')
    assert user['attachment_status']=='waiting' and user['attachments'][0]['file_parse_status']=='parsing'
    assert advisor.contexts[-1]['document_attachments']==[]
    documents.finish_parse('owner',source['id'],{'text':'后台解析完成：需要核对兼容性要求。',
        'parse_status':'ready','parse_error':'','truncated':False})
    asyncio.run(discussion.send_message('owner',selected['id'],{'request_id':'parsed-message','text':'现在结合参考文件完善提纲'}))
    assert advisor.contexts[-1]['document_attachments'][0]['material_id']==source['id']
    assert '需要核对兼容性要求' in advisor.contexts[-1]['document_attachments'][0]['text']


def test_context_reads_document_metadata_without_original_blob(stack):
    crm, _, documents, flow, discussion, _, customer, _, _ = stack
    source=upload(documents,'no-binary')
    saved=plan(flow,customer,[source['id']])
    selected=thread(discussion,customer,source=saved['record_id'])
    queries=[]
    crm._db.set_trace_callback(queries.append)
    try:
        safe,_,_,_=discussion._context_snapshot('owner',selected)
    finally:
        crm._db.set_trace_callback(None)
    assert safe['document_attachments']
    file_queries=[query.lower() for query in queries if 'from crm_document_files' in query.lower()]
    assert file_queries and all('select *' not in query and 'select content' not in query for query in file_queries)
