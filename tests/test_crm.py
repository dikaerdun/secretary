from datetime import datetime
import sqlite3

import pytest

from secretary.crm import CRMStore, MAX_AMOUNT_CENTS, analysis_fingerprint
from secretary.store import SHANGHAI


NOW = 1_800_000_000.0


@pytest.fixture
def crm(tmp_path):
    result = CRMStore(tmp_path / "crm.sqlite3")
    yield result
    result.close()


def customer(crm, owner="alice", **fields):
    return crm.create_customer(owner, {"name": "客户甲", "amount_cents": 10001, **fields}, NOW)


def record(crm, owner="alice", **fields):
    return crm.create_record(owner, {"title": "项目进展", "content": "现场记录", **fields}, NOW)


def test_customer_record_activity_owner_isolation(crm):
    c = customer(crm)
    r = record(crm, customer_id=c["id"])
    activity = crm.add_activity("alice", r["id"], "客户要求补充方案", NOW + 1)
    assert activity["record_id"] == r["id"]
    assert crm.get_customer("bob", c["id"]) is None
    assert crm.get_record("bob", r["id"]) is None
    assert crm.record_detail("bob", r["id"]) is None
    assert crm.list_customers("bob")["total"] == 0
    assert crm.list_records("bob", customer_id=c["id"])["total"] == 0
    for call in (
        lambda: crm.update_customer("bob", c["id"], {"name": "偷改"}, NOW),
        lambda: crm.update_record("bob", r["id"], {"status": "done"}, NOW),
        lambda: crm.add_activity("bob", r["id"], "偷改", NOW),
        lambda: record(crm, "bob", customer_id=c["id"]),
    ):
        with pytest.raises(KeyError):
            call()
    assert crm.record_detail("alice", r["id"])["activities"][0]["content"] == "客户要求补充方案"
    assert "owner" not in crm.get_customer("alice", c["id"])
    assert "source_id" not in crm.get_record("alice", r["id"])


def test_capture_is_durable_idempotent_and_edit_preserves_original(tmp_path):
    path = tmp_path / "saved.sqlite3"
    crm = CRMStore(path)
    r = crm.capture_message("alice", "voice-1", "  客户需要蓝色方案\n预算两万  ", "voice", NOW)
    duplicate = crm.capture_message("alice", "voice-1", "重新识别的文本", "text", NOW + 1)
    assert duplicate["id"] == r["id"]
    assert duplicate["original_content"] == r["original_content"]
    crm.update_record("alice", r["id"], {"title": "补充蓝色方案", "content": "整理后的内容", "status": "following"}, NOW + 2)
    crm.close()
    crm = CRMStore(path)
    try:
        saved = crm.get_record("alice", r["id"])
        assert saved["source"] == "voice"
        assert saved["original_content"] == "  客户需要蓝色方案\n预算两万  "
        assert saved["content"] == "整理后的内容"
        assert crm.list_records("alice")["total"] == 1
        assert crm.claim_due(NOW + 1_000_000) is None
        assert crm._db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert crm._db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0
    finally:
        crm.close()


def test_failed_parse_keeps_note_commands_hide_only_own_capture(crm):
    first = crm.capture_message("alice", "same", "客户谈了三件事", "voice", NOW)
    crm.capture_message("bob", "same", "另一位的内容", "text", NOW)
    assert crm.apply_command("alice", "same", None, None, NOW)["id"] == first["id"]
    crm.capture_message("alice", "help", "帮助", "text", NOW)
    assert crm.apply_command("alice", "help", {"action": "help"}, "用法", NOW) is None
    assert crm.list_records("alice")["total"] == 1
    assert crm.list_records("bob")["total"] == 1
    assert crm.get_record("alice", first["id"])["title"] == "客户谈了三件事"


def test_proposal_link_requires_owner_and_title_and_replay_preserves_edits(crm):
    crm.capture_message("alice", "source", "客户要方案", "text", NOW)
    command = {"action": "propose", "title": "提交客户方案", "remind_at": NOW + 100}
    reply = crm.execute("alice", "source", command, NOW)
    result = crm.apply_command("alice", "source", command, reply, NOW)
    assert result["proposal_id"] == 1
    assert crm.record_detail("alice", result["id"])["proposal"]["title"] == command["title"]
    assert crm.claim_due(NOW + 100) is None
    crm.update_record("alice", result["id"], {"title": "整理后标题"}, NOW + 1)
    assert crm.apply_command("alice", "source", command, reply, NOW + 2)["title"] == "整理后标题"
    assert crm.get_proposal("bob", 1) is None
    other = record(crm, "bob", title=command["title"])
    with pytest.raises(KeyError):
        crm.link_proposal("bob", other["id"], 1, NOW)
    crm.capture_message("bob", "source", "外部输入", "voice", NOW)
    assert crm.apply_command("bob", "source", command, reply, NOW)["proposal_id"] is None
    crm.capture_message("alice", "wrong-title", "不同事项", "voice", NOW)
    wrong = crm.apply_command("alice", "wrong-title", {"action": "propose", "title": "不同事项"}, reply, NOW)
    assert wrong["proposal_id"] is None


def test_import_legacy_once_preserves_confirmation_and_handles_recovered_callback(crm):
    crm.capture_message("alice", "pending", "这是客户原话", "voice", NOW)
    command = {"action": "propose", "title": "整理后事项", "remind_at": NOW + 100}
    reply = crm.execute("alice", "pending", command, NOW)
    crm.execute("alice", "confirm", {"action": "confirm", "proposal_id": 1}, NOW)
    crm.execute("alice", "discard", {"action": "propose", "title": "不需要"}, NOW)
    crm.execute("alice", "reject", {"action": "reject", "proposal_id": 2}, NOW)
    assert crm.import_legacy("alice", NOW + 1) == 1
    assert crm.import_legacy("alice", NOW + 1) == 0
    legacy = crm.list_records("alice")["items"][0]
    assert legacy["source"] == "legacy"
    crm.update_record("alice", legacy["id"], {"content": "人工补充的信息"}, NOW + 2)
    linked = crm.apply_command("alice", "pending", command, reply, NOW + 3)
    assert linked["id"] == legacy["id"]
    assert linked["source"] == "voice"
    assert linked["original_content"] == "这是客户原话"
    assert linked["content"] == "人工补充的信息"
    assert crm.list_records("alice")["total"] == 1
    assert crm.import_legacy("alice", NOW + 4) == 0
    detail = crm.record_detail("alice", linked["id"])
    assert detail["proposal"]["status"] == "confirmed"
    assert detail["task"]["status"] == "pending"
    assert "owner" not in detail["task"]
    assert crm.get_task("bob", detail["task"]["id"]) is None


@pytest.mark.parametrize("amount", [-1, True, 1.25, "123", MAX_AMOUNT_CENTS + 1])
def test_invalid_amount_rejected(crm, amount):
    with pytest.raises(ValueError):
        customer(crm, amount_cents=amount)


@pytest.mark.parametrize("data", [{"name": " "}, {"stage": "guess"}, {"name": 7}, {"owner": "bob"}])
def test_invalid_customer_fields_rejected(crm, data):
    with pytest.raises(ValueError):
        crm.create_customer("alice", {"name": "客户甲", **data}, NOW)


def test_customer_amount_stage_and_pagination(crm):
    first = customer(crm, name="100% 客户", amount_cents=MAX_AMOUNT_CENTS)
    customer(crm, name="1000 客户", amount_cents=99, stage="won")
    third = customer(crm, amount_cents=123, stage="lost")
    assert crm.list_customers("alice", q="%")['total'] == 1
    assert crm.list_customers("alice", q="%")['items'][0]['id'] == first['id']
    assert crm.list_customers("alice", stage="won")['total'] == 1
    assert crm.list_customers("alice", page_size=2)['pages'] == 2
    assert len(crm.list_customers("alice", page=2, page_size=2)['items']) == 1
    assert crm.dashboard("alice", NOW)['stats']['pipeline_cents'] == MAX_AMOUNT_CENTS
    changed = crm.update_customer("alice", third["id"], {"stage": "negotiation", "amount_cents": 12001}, NOW + 1)
    assert changed["amount_cents"] == 12001
    assert crm.dashboard("alice", NOW)['stats']['pipeline_cents'] == MAX_AMOUNT_CENTS + 12001
    for page, page_size in ((0, 50), (True, 50), (1, 201), (1, False)):
        with pytest.raises(ValueError):
            crm.list_customers("alice", page=page, page_size=page_size)


def test_record_filter_status_and_foreign_customer_update(crm):
    c = customer(crm, name="跟进客户")
    foreign = customer(crm, "bob")
    r = record(crm, customer_id=c["id"], status="following")
    record(crm, title="另一个事项", status="done")
    assert crm.list_records("alice", q="跟进客户", status="following")["total"] == 1
    assert crm.get_customer("alice", c["id"])["record_count"] == 1
    with pytest.raises(KeyError):
        crm.update_record("alice", r["id"], {"customer_id": foreign["id"]}, NOW)
    with pytest.raises(ValueError):
        crm.update_record("alice", r["id"], {"original_content": "覆盖原话"}, NOW)
    with pytest.raises(ValueError):
        crm.update_record("alice", r["id"], {"status": "pending"}, NOW)
    assert crm.update_record("alice", r["id"], {"customer_id": None, "status": "done"}, NOW)["customer_name"] is None
    assert crm.claim_due(NOW + 1000) is None


def test_database_enforces_cross_owner_customer_and_activity_links(crm):
    c = customer(crm)
    r = record(crm)
    with pytest.raises(sqlite3.IntegrityError):
        crm._db.execute("INSERT INTO crm_records(owner,title,content,original_content,source,customer_id,created_at,updated_at) "
                        "VALUES ('bob','x','','','web',?,0,0)", (c["id"],))
    with pytest.raises(sqlite3.IntegrityError):
        crm._db.execute("INSERT INTO crm_activities(owner,record_id,content,created_at) VALUES ('bob',?,'x',0)", (r["id"],))


def test_agenda_shanghai_windows_overlap_history_and_owners(crm):
    def at(value):
        return datetime.fromisoformat(value).replace(tzinfo=SHANGHAI).timestamp()

    def task(owner, source, title, when, duration=30):
        crm.execute(owner, source, {"action": "propose", "title": title, "remind_at": when,
                                   "duration_minutes": duration}, at("2026-09-01T00:00"))
        pid = crm._db.execute("SELECT MAX(id) FROM proposals").fetchone()[0]
        crm.execute(owner, source + "-confirm", {"action": "confirm", "proposal_id": pid}, at("2026-09-01T00:00"))
        return crm.get_proposal(owner, pid)["task_id"]

    task("alice", "cross", "跨日事项", at("2026-09-29T23:45"), 30)
    task("alice", "boundary", "零点结束", at("2026-09-29T22:00"), 120)
    completed = task("alice", "done", "已完成", at("2026-09-30T10:00"))
    cancelled = task("alice", "cancelled", "已取消", at("2026-09-30T12:00"))
    task("bob", "private", "另一个人", at("2026-09-30T15:00"))
    task("alice", "nextmonth", "下月事项", at("2026-10-01T15:00"))
    crm.execute("alice", "complete", {"action": "complete", "task_id": completed}, at("2026-09-30T11:00"))
    crm.execute("alice", "cancel", {"action": "cancel", "task_id": cancelled}, at("2026-09-30T11:00"))
    day = crm.agenda("alice", "day", "2026-09-30")
    assert day["start"] == at("2026-09-30T00:00")
    assert [t["title"] for t in day["items"]] == ["跨日事项", "已完成"]
    assert day["items"][1]["status"] == "completed"
    assert crm.agenda("alice", "week", "2026-09-30")["end"] == at("2026-10-05T00:00")
    assert crm.agenda("alice", "month", "2026-09-30")["end"] == at("2026-10-01T00:00")
    assert crm.agenda("alice", "day", "2026-09-30", page_size=1)["pages"] == 2
    assert len(crm.agenda("alice", "day", "2026-09-30", page_size=1, page=2)["items"]) == 1
    for bad in ("2026-9-30", "2026-02-30", "9999-12-31"):
        with pytest.raises(ValueError):
            crm.agenda("alice", "day", bad)


def test_edit_title_updates_pending_proposal_before_confirmation_only(crm):
    r = record(crm, title="原始标题")
    crm.execute("alice", "schedule", {"action": "propose", "title": "原始标题", "remind_at": NOW + 100}, NOW)
    crm.link_proposal("alice", r["id"], 1, NOW)
    crm.update_record("alice", r["id"], {"title": "确认前人工整理的标题"}, NOW + 1)
    assert crm.get_proposal("alice", 1)["title"] == "确认前人工整理的标题"
    assert crm.claim_due(NOW + 100) is None
    crm.execute("alice", "confirm", {"action": "confirm", "proposal_id": 1}, NOW + 2)
    detail = crm.record_detail("alice", r["id"])
    assert detail["task"]["title"] == "确认前人工整理的标题"
    crm.update_record("alice", r["id"], {"title": "确认后新的客户记录标题"}, NOW + 3)
    assert crm.get_proposal("alice", 1)["title"] == "确认前人工整理的标题"
    assert crm.get_task("alice", detail["task"]["id"])["title"] == "确认前人工整理的标题"
    assert crm.get_record("alice", r["id"])["title"] == "确认后新的客户记录标题"


def test_successive_followups_stay_one_record_after_restart(tmp_path):
    path = tmp_path / "followups.sqlite3"
    crm = CRMStore(path)
    r = record(crm, title="首次联系")
    crm.execute("alice", "first", {"action": "propose", "title": "首次联系", "remind_at": NOW + 100}, NOW)
    crm.link_proposal("alice", r["id"], 1, NOW)
    crm.execute("alice", "first-confirm", {"action": "confirm", "proposal_id": 1}, NOW + 1)
    task_id = crm.get_proposal("alice", 1)["task_id"]
    crm.execute("alice", "first-complete", {"action": "complete", "task_id": task_id}, NOW + 2)
    crm.update_record("alice", r["id"], {"title": "再次跟进"}, NOW + 3)
    crm.execute("alice", "second", {"action": "propose", "title": "再次跟进", "remind_at": NOW + 4000}, NOW + 4)
    crm.link_proposal("alice", r["id"], 2, NOW + 4)
    crm.link_proposal("alice", r["id"], 2, NOW + 4)
    assert crm.import_legacy("alice", NOW + 5) == 0
    crm.close()
    crm = CRMStore(path)
    try:
        assert crm.import_legacy("alice", NOW + 6) == 0
        assert crm.list_records("alice")["total"] == 1
        assert crm.get_record("alice", r["id"])["proposal_id"] == 2
        assert crm._db.execute("SELECT COUNT(*) FROM crm_record_proposals WHERE owner='alice'").fetchone()[0] == 2
        assert crm.get_task("alice", task_id)["title"] == "首次联系"
        other = record(crm, title="首次联系")
        with pytest.raises(ValueError, match="已关联"):
            crm.link_proposal("alice", other["id"], 1, NOW + 7)
    finally:
        crm.close()


def test_existing_current_links_are_backfilled_on_schema_upgrade(tmp_path):
    path = tmp_path / "upgrade.sqlite3"
    crm = CRMStore(path)
    r = record(crm, title="旧版记录")
    crm.execute("alice", "first", {"action": "propose", "title": "旧版记录"}, NOW)
    crm.link_proposal("alice", r["id"], 1, NOW)
    crm._db.execute("DROP TABLE crm_record_proposals")
    crm.close()
    crm = CRMStore(path)
    try:
        assert crm.import_legacy("alice", NOW + 1) == 0
        assert crm._db.execute("SELECT record_id FROM crm_record_proposals WHERE proposal_id=1").fetchone()[0] == r["id"]
    finally:
        crm.close()


def test_recover_cached_creation_after_crash_without_reexecuting(tmp_path):
    path = tmp_path / "crashed.sqlite3"
    crm = CRMStore(path)
    crm.capture_message("alice", "voice-1", "客户周五前要收到方案", "voice", NOW)
    command = {"action": "propose", "title": "准备客户方案"}
    reply = crm.execute("alice", "voice-1", command, NOW)
    crm.close()  # Store succeeded, apply_command was never reached.
    crm = CRMStore(path)
    try:
        assert crm.import_legacy("alice", NOW + 1) == 1
        recovered = crm.recover_message("alice", "voice-1", reply, NOW + 2)
        assert recovered["original_content"] == "客户周五前要收到方案"
        assert recovered["source"] == "voice"
        assert recovered["proposal_id"] == 1
        assert crm.list_records("alice")["total"] == 1
        assert crm._db.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 1
        assert crm._db.execute("SELECT COUNT(*) FROM command_results").fetchone()[0] == 1
        crm.recover_message("alice", "voice-1", reply, NOW + 3)
        assert crm.import_legacy("alice", NOW + 4) == 0
        assert crm.claim_due(NOW + 1_000_000) is None
    finally:
        crm.close()


def test_recovery_hides_only_verified_known_commands_and_preserves_unknown(crm):
    raw = crm.capture_message("alice", "help", "帮助", "text", NOW)
    reply = crm.execute("alice", "help", {"action": "help"}, NOW)
    assert crm.recover_message("alice", "help", reply + "伪造内容", NOW)["id"] == raw["id"]
    assert crm.recover_message("alice", "help", reply, NOW) is None
    crm.capture_message("bob", "help", "客户情况", "text", NOW)
    assert crm.recover_message("bob", "help", reply, NOW) is not None
    unknown = crm.capture_message("alice", "unknown", "客户情况无法识别", "voice", NOW)
    unknown_reply = crm.execute("alice", "unknown", {"action": "unknown"}, NOW)
    assert crm.recover_message("alice", "unknown", unknown_reply, NOW)["id"] == unknown["id"]
    assert crm.list_records("alice")["total"] == 1
    crm.capture_message("bob", "foreign-proposal", "另一位客户", "voice", NOW)
    crm.execute("alice", "own-proposal", {"action": "propose", "title": "自己的提案"}, NOW)
    assert crm.recover_message("bob", "foreign-proposal", "已整理，待你确认：P1 自己的提案", NOW)["proposal_id"] is None


def test_recovered_reschedule_does_not_fabricate_legacy_original(crm):
    crm.execute("alice", "old-create", {"action": "propose", "title": "历史提案"}, NOW)
    assert crm.import_legacy("alice", NOW + 1) == 1
    legacy = crm.list_records("alice")["items"][0]
    crm.capture_message("alice", "reschedule", "把提案P1改到明天下午", "voice", NOW + 2)
    reply = crm.execute("alice", "reschedule", {"action": "reschedule_proposal", "proposal_id": 1,
                                              "remind_at": NOW + 1000}, NOW + 2)
    assert crm.recover_message("alice", "reschedule", reply, NOW + 3) is None
    original = crm.get_record("alice", legacy["id"])
    assert original["source"] == "legacy"
    assert original["original_content"] == "历史提案"
    assert original["remind_at"] == NOW + 1000
    assert crm.list_records("alice")["total"] == 1
    assert crm.claim_due(NOW + 1000) is None


def analysis_data(r, **fields):
    return {
        "summary": "客户需要补充报价，并评估实施排期。",
        "key_points": ["客户认可方案方向", "预算仍需确认"],
        "open_questions": ["最终预算由谁批准？"],
        "actions": [
            {"title": "发送补充报价", "kind": "commitment", "reason": "客户明确要求补充报价",
             "owner_hint": "我", "remind_at": NOW + 200},
            {"title": "确认预算审批人", "kind": "suggestion", "reason": "明确后续决策流程",
             "owner_hint": "我", "remind_at": None},
        ],
        "input_fingerprint": analysis_fingerprint(r),
        **fields,
    }


def test_analysis_snapshot_and_adoption_are_persistent_and_never_schedule(tmp_path):
    path = tmp_path / "analysis.sqlite3"
    crm = CRMStore(path)
    c = customer(crm)
    r = record(crm, customer_id=c["id"])
    saved = crm.save_analysis("alice", r["id"], analysis_data(r), NOW)
    assert saved["version"] == 1
    assert saved["stale"] is False
    assert [a["id"] for a in saved["actions"]] == [1, 2]
    assert saved["actions"][0]["record_id"] is None
    assert crm.list_records("alice")["total"] == 1
    adopted = crm.adopt_action("alice", r["id"], 1, NOW + 1)
    assert adopted["customer_id"] == c["id"]
    assert adopted["parent_record_id"] == r["id"]
    assert adopted["status"] == "following"
    assert adopted["source"] == "web"
    assert adopted["proposal_id"] is None
    assert adopted["remind_at"] is None
    assert "客户明确要求补充报价" in adopted["original_content"]
    assert "负责人提示：我" in adopted["original_content"]
    crm.close()
    crm = CRMStore(path)
    try:
        saved = crm.record_detail("alice", r["id"])["analysis"]
        assert saved["actions"][0]["record_id"] == adopted["id"]
        assert saved["actions"][0]["adopted_record_id"] == adopted["id"]
        assert crm.adopt_action("alice", r["id"], 1, NOW + 2)["id"] == adopted["id"]
        assert crm.list_records("alice")["total"] == 2
        assert crm._db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert crm._db.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0
        assert crm._db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0
        assert crm.claim_due(NOW + 200) is None
    finally:
        crm.close()


def test_analysis_owner_isolation_and_missing_action(crm):
    r = record(crm)
    crm.save_analysis("alice", r["id"], analysis_data(r), NOW)
    assert crm.get_analysis("bob", r["id"]) is None
    with pytest.raises(KeyError):
        crm.save_analysis("bob", r["id"], analysis_data(r), NOW)
    with pytest.raises(KeyError):
        crm.adopt_action("bob", r["id"], 1, NOW)
    with pytest.raises(KeyError):
        crm.adopt_action("alice", r["id"], 3, NOW)
    with pytest.raises(ValueError):
        crm.adopt_action("alice", r["id"], True, NOW)
    assert "owner" not in crm.get_analysis("alice", r["id"])


def test_analysis_cannot_be_replaced_after_adoption_and_duplicate_preserves_edits(crm):
    r = record(crm)
    crm.save_analysis("alice", r["id"], analysis_data(r), NOW)
    second = crm.save_analysis("alice", r["id"], analysis_data(r, summary="重新整理交流内容"), NOW + 1)
    assert second["version"] == 2
    action_id = second["actions"][1]["id"]
    adopted = crm.adopt_action("alice", r["id"], action_id, NOW + 2)
    crm.update_record("alice", adopted["id"], {"title": "用户补充的下一步", "content": "已有补充信息"}, NOW + 3)
    same = crm.adopt_action("alice", r["id"], action_id, NOW + 4)
    assert same["id"] == adopted["id"]
    assert same["content"] == "已有补充信息"
    with pytest.raises(ValueError, match="后续交流"):
        crm.save_analysis("alice", r["id"], analysis_data(r), NOW + 5)
    assert crm.list_records("alice")["total"] == 2
    assert crm.get_analysis("alice", r["id"])["version"] == 2


def test_analysis_stale_input_rejected_and_old_snapshot_marked(crm):
    r = record(crm)
    old_data = analysis_data(r)
    crm.save_analysis("alice", r["id"], old_data, NOW)
    edited = crm.update_record("alice", r["id"], {"content": "客户已明确预算和负责人"}, NOW + 1)
    assert crm.get_analysis("alice", r["id"])["stale"] is True
    with pytest.raises(ValueError, match="记录已修改"):
        crm.save_analysis("alice", r["id"], old_data, NOW + 2)
    with pytest.raises(ValueError, match="已过期"):
        crm.adopt_action("alice", r["id"], 1, NOW + 2)
    updated = crm.save_analysis("alice", r["id"], analysis_data(edited), NOW + 3)
    assert updated["stale"] is False
    action_id = updated["actions"][0]["id"]
    adopted = crm.adopt_action("alice", r["id"], action_id, NOW + 4)
    crm.update_record("alice", r["id"], {"title": "后来更新的交流标题"}, NOW + 5)
    assert crm.get_analysis("alice", r["id"])["stale"] is True
    assert crm.adopt_action("alice", r["id"], action_id, NOW + 6)["id"] == adopted["id"]
    with pytest.raises(ValueError, match="已过期"):
        crm.adopt_action("alice", r["id"], 2, NOW + 6)


@pytest.mark.parametrize("fields", [
    {"summary": " "},
    {"input_fingerprint": "invalid"},
    {"key_points": "不是列表"},
    {"open_questions": ["问题"] * 13},
    {"actions": [{"title": "事项", "kind": "suggestion"}] * 7},
    {"actions": [{"title": "事项", "kind": "confirmed"}]},
    {"actions": [{"title": "事项", "kind": "suggestion", "remind_at": float("nan")}]},
    {"actions": [{"title": "事项", "kind": "suggestion", "id": 5}]},
])
def test_analysis_field_validation(crm, fields):
    r = record(crm)
    with pytest.raises(ValueError):
        crm.save_analysis("alice", r["id"], analysis_data(r, **fields), NOW)
    assert crm.get_analysis("alice", r["id"]) is None


def test_concurrent_adoption_creates_only_one_child(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "concurrent.sqlite3"
    first = CRMStore(path)
    second = CRMStore(path)
    try:
        r = record(first)
        first.save_analysis("alice", r["id"], analysis_data(r), NOW)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(store.adopt_action, "alice", r["id"], 1, NOW + 1)
                       for store in (first, second)]
            children = [future.result() for future in futures]
        assert children[0]["id"] == children[1]["id"]
        assert first.list_records("alice")["total"] == 2
    finally:
        first.close()
        second.close()
