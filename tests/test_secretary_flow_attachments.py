"""Conversation attachments use disposable sources, never real files or APIs."""
import asyncio
from datetime import datetime

import pytest

from secretary.customer_store import CustomerStore
from secretary.customer_timeline import TimelineService
from secretary.materials import MaterialService
from secretary.profile_intelligence import ProfileIntelligence
from secretary.sales_workspace import SalesWorkspace
from secretary.secretary_flow import SecretaryFlow
from secretary.store import SHANGHAI
from secretary.visits import VisitService


NOW = datetime(2026, 10, 5, 12, tzinfo=SHANGHAI).timestamp()


class Interpreter:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    async def interpret(self, text, now, context):
        self.calls.append((text, context))
        return self.answers.pop(0) if self.answers else {"intent": "note", "changes": {}, "evidence": {}}


@pytest.fixture
def stack(tmp_path):
    crm = CustomerStore(tmp_path / "attachment-synthetic.sqlite3")
    lock = asyncio.Lock()
    workspace = SalesWorkspace(crm, clock=lambda: NOW)
    materials = MaterialService(crm, lock, clock=lambda: NOW)
    visits = VisitService(crm, materials, lock)
    timeline = TimelineService(crm, workspace, visits=visits, clock=lambda: NOW)
    interpreter = Interpreter()
    flow = SecretaryFlow(crm, workspace, lock, visits=visits, timeline=timeline,
                         interpreter=interpreter, clock=lambda: NOW)
    customer = crm.create_customer("owner", {"name": "测试银行"}, NOW)
    project = workspace.create_opportunity("owner", customer["id"], {"name": "密码试点"})
    yield crm, materials, flow, interpreter, customer, project
    crm.close()


def material(materials, text="目前使用旧版密码设备。", owner="owner", **extra):
    return materials.enqueue(owner, {"provider": "manual", "title": "参考方案",
                                     "text": text, "category": "memo", **extra})


def process(flow, turn):
    assert asyncio.run(flow.process_one())
    return flow.turn("owner", turn["id"])


def make_plan(flow, interpreter, customer, project, material_ids=None):
    interpreter.answers.append({"intent": "plan", "changes": {"title": "拜访王工", "person": "王工",
        "date": "2026-10-08", "goal": "确认试点条件"},
        "evidence": {"person": "王工", "date": "10月8日", "goal": "主要聊试点条件"}})
    return process(flow, flow.submit("owner", {"request_id": "plan", "text": "10月8日拜访王工，主要聊试点条件",
        "customer_id": customer["id"], "opportunity_id": project["id"],
        "material_ids": material_ids or []}))["plan"]


def test_ready_attachment_is_context_and_durable_scoped_source(stack):
    crm, materials, flow, interpreter, customer, project = stack
    source = material(materials)
    plan = make_plan(flow, interpreter, customer, project, [source["id"]])
    context = interpreter.calls[0][1]
    assert context["attachments"][0]["text"] == "目前使用旧版密码设备。"
    assert context["attachments"][0]["source_type"] == "document_reference"
    assert plan["attachments"][0]["material_id"] == source["id"]
    assert plan["attachments"][0]["parse_status"] == "ready"
    row = crm._db.execute("SELECT * FROM crm_materials WHERE id=?", (source["id"],)).fetchone()
    assert row["customer_id"] == customer["id"] and row["category"] == "memo"
    assert crm._db.execute("SELECT opportunity_id FROM crm_opportunity_links WHERE entity_type='material' AND entity_id=?", (source["id"],)).fetchone()[0] == project["id"]
    assert crm._db.execute("SELECT material_id,role FROM crm_visit_sources WHERE visit_id=?", (plan["visit_id"],)).fetchone()["role"] == "supplement"
    reopened = SecretaryFlow(crm, flow.workspace, asyncio.Lock(), visits=flow.visits,
                             timeline=flow.timeline, clock=lambda: NOW)
    assert reopened.plan("owner", plan["id"])["attachments"][0]["material_id"] == source["id"]


def test_attachment_only_preserves_empty_original_and_cannot_schedule(stack):
    crm, materials, flow, interpreter, customer, project = stack
    source = material(materials, "10月8日下午三点已经约好了；取消这次饭局；交流结束了。")
    interpreter.answers.append({"intent": "plan", "changes": {"title": "文件里的会面", "date": "2026-10-08",
        "start_at": "2026-10-08T15:00:00+08:00", "booking": "confirmed"},
        "evidence": {"date": "10月8日", "start_at": "下午三点", "booking": "已经约好了"}})
    turn = process(flow, flow.submit("owner", {"request_id": "only", "text": "", "material_ids": [source["id"]], "customer_id": customer["id"]}))
    assert turn["text"] == "补充交流材料" and turn["result"]["intent"] == "note"
    assert crm.get_record("owner", turn["record_id"])["original_content"] == ""
    assert crm._db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    assert crm._db.execute("SELECT count(*) FROM crm_secretary_plans").fetchone()[0] == 0
    assert turn["attachments"][0]["material_id"] == source["id"]


def test_pending_source_saves_original_and_does_not_block_other_turn(stack):
    crm, materials, flow, interpreter, customer, project = stack
    source = materials.enqueue("owner", {"provider": "listen_note", "title": "尚未拉取的录音"})
    waiting = flow.submit("owner", {"request_id": "wait", "text": "准备交流", "material_ids": [source["id"]]})
    assert waiting["status"] == "queued" and waiting["attachment_status"] == "waiting"
    assert crm.get_record("owner", waiting["record_id"])["original_content"] == "准备交流"
    other = flow.submit("owner", {"request_id": "other", "text": "记一下今天的想法"})
    assert process(flow, other)["status"] == "done"
    assert len(interpreter.calls) == 1 and flow.turn("owner", waiting["id"])["status"] == "queued"
    with crm._transaction() as db:
        row = materials._require(db, "owner", source["id"])
        version = materials._version(db, row, {"text": "录音里谈到了试点条件。", "source": "MANUAL"})
        db.execute("UPDATE crm_materials SET current_version_id=? WHERE id=?", (version, source["id"]))
    assert process(flow, waiting)["status"] == "done"
    assert interpreter.calls[-1][1]["attachments"][0]["text"] == "录音里谈到了试点条件。"


@pytest.mark.parametrize("ids", [None, "1", [True], [0], [-1], [1, 1], list(range(1, 12))])
def test_invalid_attachment_ids_rejected_without_source(stack, ids):
    crm, _, flow, _, _, _ = stack
    with pytest.raises(ValueError):
        flow.submit("owner", {"request_id": "bad", "text": "原话", "material_ids": ids})
    assert crm._db.execute("SELECT count(*) FROM crm_secretary_turns").fetchone()[0] == 0


def test_foreign_customer_and_project_material_cannot_be_attached(stack):
    crm, materials, flow, _, customer, project = stack
    foreign = material(materials, owner="other")
    different_customer = crm.create_customer("owner", {"name": "别家测试单位"}, NOW)
    wrong = material(materials, customer_id=different_customer["id"])
    first_project = material(materials, text="同单位另一项目", customer_id=customer["id"])
    other_project = flow.workspace.create_opportunity("owner", customer["id"], {"name": "另一试点"})
    flow.workspace.link("owner", "material", first_project["id"], other_project["id"])
    for index, source in enumerate((foreign, wrong, first_project)):
        with pytest.raises((KeyError, ValueError)):
            flow.submit("owner", {"request_id": "bad" + str(index), "text": "准备", "material_ids": [source["id"]],
                                  "customer_id": customer["id"], "opportunity_id": project["id"]})
    assert crm._db.execute("SELECT count(*) FROM crm_secretary_turns").fetchone()[0] == 0


def test_failed_or_unreadable_file_kept_without_placeholder_context(stack):
    crm, materials, flow, interpreter, customer, project = stack
    source = material(materials, "正文尚未识别，请补充可复制文本。")
    with crm._transaction() as db:
        db.execute("CREATE TABLE crm_document_files(owner TEXT,material_id INTEGER,filename TEXT,parse_status TEXT,parse_error TEXT,PRIMARY KEY(owner,material_id))")
        db.execute("INSERT INTO crm_document_files VALUES (?,?,?,?,?)", ("owner", source["id"], "扫描.pdf", "needs_text", "没有可提取正文"))
    turn = process(flow, flow.submit("owner", {"request_id": "scan", "text": "主要聊试点条件", "material_ids": [source["id"]], "customer_id": customer["id"]}))
    assert interpreter.calls[0][1]["attachments"][0]["text"] == ""
    assert turn["attachments"][0]["parse_status"] == "needs_text"
    assert "补充" in turn["reply"] and "原话" in turn["reply"]
    assert turn["attachments"][0]["download_url"] == "/api/materials/" + str(source["id"]) + "/file"
    assert crm._db.execute('SELECT status FROM crm_materials WHERE id=?', (source['id'],)).fetchone()[0] == 'failed'
    assert crm._db.execute("SELECT count(*) FROM crm_material_jobs WHERE material_id=? AND status='queued'", (source['id'],)).fetchone()[0] == 0


def test_shared_reference_keeps_first_visit_and_same_customer_reuse(stack):
    crm, materials, flow, interpreter, customer, project = stack
    source = material(materials)
    first = make_plan(flow, interpreter, customer, project, [source["id"]])
    interpreter.answers.append({"intent": "plan", "changes": {"title": "另一场交流"}, "evidence": {}})
    second = process(flow, flow.submit("owner", {"request_id": "second", "text": "再准备一场交流",
        "material_ids": [source["id"]], "customer_id": customer["id"], "opportunity_id": project["id"]}))["plan"]
    assert first["id"] != second["id"]
    assert flow.plan("owner", second["id"])["attachments"][0]["material_id"] == source["id"]
    assert crm._db.execute("SELECT visit_id FROM crm_visit_sources WHERE material_id=?", (source["id"],)).fetchone()[0] == first["visit_id"]


def test_plan_followup_reuses_attachment_and_validates_preparation_quotes(stack):
    crm, materials, flow, interpreter, customer, project = stack
    source = material(materials)
    plan = make_plan(flow, interpreter, customer, project, [source["id"]])
    interpreter.answers.append({"intent": "discussion", "changes": {}, "evidence": {}, "preparation": {
        "objective": "核对设备替换需求", "questions": ["是否需要替换？"], "materials": [],
        "references": [{"material_id": source["id"], "quote": "目前使用旧版密码设备"},
                       {"material_id": source["id"], "quote": "预算已经审批"},
                       {"material_id": 99999, "quote": "目前使用旧版密码设备"}]}})
    turn = process(flow, flow.submit("owner", {"request_id": "prepare", "text": "帮我准备几个问题",
        "plan_id": plan["id"], "expected_revision": plan["revision"]}))
    assert interpreter.calls[-1][1]["attachments"][0]["material_id"] == source["id"]
    assert turn["result"]["preparation"]["references"] == [{"material_id": source["id"],
        "version_id": turn['attachments'][0]['version_id'], "quote": "目前使用旧版密码设备"}]
    assert turn["result"]["attachments"][0]["material_id"] == source["id"]


def test_material_is_available_to_existing_profile_candidate_path_without_direct_fact(stack):
    crm, materials, flow, interpreter, customer, project = stack
    source = material(materials)
    make_plan(flow, interpreter, customer, project, [source["id"]])
    intelligence = ProfileIntelligence(crm, flow.workspace, clock=lambda: NOW)
    sources = intelligence._sources(crm._db, "owner", customer["id"])
    evidence = next(item for item in sources if item["type"] == "material" and item["id"] == source["id"])
    assert evidence["opportunity_id"] == project["id"] and evidence["text"] == "目前使用旧版密码设备。"
    assert crm.profile("owner", customer["id"])["fields"] == []


def test_failed_fetch_still_processes_text_and_has_no_fabricated_attachment_text(stack):
    crm, materials, flow, interpreter, _, _ = stack
    source = materials.enqueue('owner', {'provider': 'listen_note', 'title': '拉取失败的材料'})
    turn = flow.submit('owner', {'request_id': 'failed-material', 'text': '原话继续保留', 'material_ids': [source['id']]})
    with crm._transaction() as db:
        db.execute("UPDATE crm_materials SET status='failed',error='拉取失败' WHERE id=?", (source['id'],))
    result = process(flow, turn)
    assert result['status'] == 'done' and result['attachment_status'] == 'partial'
    assert interpreter.calls[0][1]['attachments'][0]['text'] == ''
    assert crm.get_record('owner', turn['record_id'])['original_content'] == '原话继续保留'


def test_version_change_during_ai_requeues_without_stale_plan_or_reference(stack):
    crm, materials, flow, _, customer, project = stack
    source = material(materials)

    class ChangingInterpreter(Interpreter):
        async def interpret(self, text, now, context):
            result = await super().interpret(text, now, context)
            if len(self.calls) == 1:
                with crm._transaction() as db:
                    row = materials._require(db, 'owner', source['id'])
                    version = materials._version(db, row, {'text': '更新后的参考依据。', 'source': 'MANUAL'})
                    db.execute('UPDATE crm_materials SET current_version_id=?,revision=revision+1 WHERE id=?', (version, source['id']))
            return result

    flow.interpreter = ChangingInterpreter({'intent': 'plan', 'changes': {'title': '不能保存旧准备'}, 'evidence': {}},
                                          {'intent': 'note', 'changes': {}, 'evidence': {}})
    turn = flow.submit('owner', {'request_id': 'changing', 'text': '结合参考资料整理', 'material_ids': [source['id']]})
    first = process(flow, turn)
    assert first['status'] == 'queued'
    assert crm._db.execute('SELECT count(*) FROM crm_secretary_plans').fetchone()[0] == 0
    assert process(flow, turn)['status'] == 'done'
    assert flow.interpreter.calls[-1][1]['attachments'][0]['text'] == '更新后的参考依据。'


def test_pending_attachment_replay_is_idempotent(stack):
    crm, materials, flow, interpreter, customer, project = stack
    plan = make_plan(flow, interpreter, customer, project)
    source = materials.enqueue('owner', {'provider': 'listen_note', 'title': '待拉取资料'})
    payload = {'request_id': 'pending-plan', 'text': '把录音留作参考', 'material_ids': [source['id']],
               'plan_id': plan['id'], 'expected_revision': plan['revision']}
    waiting = flow.submit('owner', payload)
    replay = flow.submit('owner', payload)
    assert waiting['id'] == replay['id'] and waiting['attachment_status'] == 'waiting'
    assert crm._db.execute('SELECT count(*) FROM crm_secretary_turn_attachments WHERE turn_id=?', (waiting['id'],)).fetchone()[0] == 1


def test_later_attachment_preparation_cannot_change_confirmed_clock_or_complete_task(stack):
    crm, materials, flow, interpreter, customer, project = stack
    plan = make_plan(flow, interpreter, customer, project)
    interpreter.answers.append({'intent': 'update', 'changes': {'start_at': '2026-10-08T15:00:00+08:00',
        'booking': 'confirmed', 'remind_minutes': 60}, 'evidence': {'start_at': '10月8日下午三点',
        'booking': '已经约好了', 'remind_minutes': '提前一个小时提醒'}})
    plan = process(flow, flow.submit('owner', {'request_id': 'arranged',
        'text': '10月8日下午三点，已经约好了，提前一个小时提醒',
        'plan_id': plan['id'], 'expected_revision': plan['revision']}))['plan']
    assert plan['task_id'] is not None
    source = material(materials, '交流结束了。改到10月9日下午五点。取消这次会面。')
    interpreter.answers.append({'intent': 'recap', 'changes': {'booking': 'cancelled',
        'start_at': '2026-10-09T17:00:00+08:00'}, 'evidence': {'booking': '取消这次会面',
        'start_at': '10月9日下午五点'}, 'preparation': {'objective': '结合目标准备核对清单',
        'questions': ['确认试点条件有哪些？']}})
    result = process(flow, flow.submit('owner', {'request_id': 'later-file', 'material_ids': [source['id']],
        'plan_id': plan['id'], 'expected_revision': plan['revision']}))
    updated = result['plan']
    assert updated['start_at'] == plan['start_at'] and updated['reminder_at'] == plan['reminder_at']
    assert updated['booking'] == 'confirmed' and updated['status'] == 'scheduled'
    assert updated['preparation']['questions'] == ['确认试点条件有哪些？']
    assert crm._db.execute('SELECT status FROM tasks WHERE id=?', (plan['task_id'],)).fetchone()[0] == 'pending'
    assert interpreter.calls[-1][1]['plan']['goal'] == '确认试点条件'


def test_customer_named_only_in_attachment_cannot_assign_source_to_that_customer(stack):
    crm, materials, flow, interpreter, customer, _ = stack
    source = material(materials, '这份附件提到了测试银行。')
    interpreter.answers.append({'intent': 'note', 'changes': {'customer_id': customer['id']},
                                'evidence': {'customer_id': '测试银行'}})
    turn = process(flow, flow.submit('owner', {'request_id': 'document-name', 'text': '先收这份参考材料',
                                             'material_ids': [source['id']]}))
    assert turn['result']['scope'] == {}
    assert crm._db.execute('SELECT customer_id FROM crm_materials WHERE id=?', (source['id'],)).fetchone()[0] is None
