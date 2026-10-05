"""Selected evidence and topic recovery on fresh owned data, with no providers.

Most cases exercise real services. The final route case uses a disposable
aiohttp TestServer on an ephemeral port, never an existing app/QA server.
"""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from secretary.customer_store import CustomerStore
from secretary.store import Store
from secretary.web import WebCRM, hash_password


OWNER = "round06-selected-synthetic"
NOW = 1_800_550_000.0
PASSWORD = "round06-isolated-public-test-password"
PASSWORD_HASH = hash_password(PASSWORD)
DEFAULT = "如何更好地跟进这个客户"
REPLY = {"answer": "这是销售建议，需要继续核实。", "next_moves": [],
         "questions": [], "risks": []}


class LocalAdvisor:
    def __init__(self):
        self.calls = []
        self.error = False
        self.started = asyncio.Event()
        self.release = None

    async def reply(self, context, history, text, now):
        self.calls.append(copy.deepcopy((context, history, text, now)))
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.error:
            raise RuntimeError("synthetic failure, no provider")
        return copy.deepcopy(REPLY)


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "round06-discussion-fresh.sqlite3"
    crm, store, clock = CustomerStore(path), Store(path), [NOW]
    web = WebCRM(store, crm, asyncio.Lock(), OWNER, PASSWORD_HASH,
                 clock=lambda: clock[0])
    advisor = LocalAdvisor()
    web.discussions.advisor = advisor
    unit = crm.create_customer(OWNER, {"name": "R06合成单位"}, NOW)
    person = crm.create_contact(OWNER, unit["id"], {"name": "R06林工"}, NOW)
    projects = []
    for label in ("A数据库", "B密钥"):
        project = web.sales_workspace.create_opportunity(OWNER, unit["id"], {"name": label})
        web.sales_workspace.upsert_stakeholder(OWNER, unit["id"], project["id"],
            {"contact_id": person["id"], "expected_revision": project["revision"],
             "roles": ["technical_reviewer"]})
        projects.append(project)
    w = SimpleNamespace(crm=crm, store=store, web=web, clock=clock, model=advisor,
                        unit=unit, person=person, a=projects[0], b=projects[1])
    yield w
    asyncio.run(web.discussions.close())
    crm.close()
    store.close()


def project_current(w, project):
    return next(p for p in w.web.sales_workspace.opportunities(OWNER, w.unit["id"], include_archived=True)["items"]
                if p["id"] == project["id"])


def capture(w, key, text, *, project=None, kind="communication", occurred_at=None, title=None):
    scope = {"contact_id": w.person["id"], "opportunity_id": (project or w.a)["id"]}
    data = {"request_id": key, "text": text, "kind": kind, "occurred_at": occurred_at}
    if title is not None:
        data["title"] = title
    return w.web.timeline.create_record(OWNER, scope, data)


def thread(w, *, chosen=None, project=None, focused=True, **extra):
    data = {"customer_id": w.unit["id"], "opportunity_id": (project or w.a)["id"], **extra}
    if focused:
        data["contact_id"] = w.person["id"]
    if chosen is not None:
        data["timeline_event_keys"] = chosen
    return w.web.discussions.create_thread(OWNER, data)["thread"]


def rows(w):
    tables = [r[0] for r in w.crm._db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    return {table: [tuple(row) for row in w.crm._db.execute('SELECT * FROM "' + table + '"')]
            for table in tables}


def read(w, t):
    before, writes, calls = rows(w), w.crm._db.total_changes, len(w.model.calls)
    result = w.web.discussions.get_thread(OWNER, t["id"])
    assert rows(w) == before
    assert w.crm._db.total_changes == writes
    assert len(w.model.calls) == calls
    return result


def send(w, t, text, request_id):
    return asyncio.run(w.web.discussions.send_message(OWNER, t["id"],
                                                     {"text": text, "request_id": request_id}))


def test_preview_selected_old_original_not_latest_background(world):
    w = world
    old = capture(w, "older", "原来选中的沟通", occurred_at=NOW - 86400)
    t = thread(w, chosen=[old["event"]["key"]])
    latest = capture(w, "latest", "后来新发生的实际沟通", occurred_at=NOW - 300)
    value = read(w, t)
    assert [p["key"] for p in value["evidence_preview"]] == [old["event"]["key"]]
    assert value["evidence_preview"][0]["excerpt"] == old["record"]["content"]
    assert value["focus_summary"]["latest_communication"]["key"] == latest["event"]["key"]
    assert read(w, t)["evidence_preview"] == value["evidence_preview"]


def test_six_selected_in_explicit_order_do_not_add_newest_or_scope_metadata(world):
    w = world
    originals = [capture(w, "selected-" + str(i), "第" + str(i) + "条原文",
                         kind="reflection" if i % 2 else "communication") for i in range(6)]
    order = [4, 0, 5, 1, 3, 2]
    chosen = [originals[i]["event"]["key"] for i in order]
    t = thread(w, chosen=chosen)
    capture(w, "new-background", "这不是明确勾选的证据")
    preview = read(w, t)["evidence_preview"]
    assert [p["key"] for p in preview] == chosen
    assert [p["excerpt"] for p in preview] == [originals[i]["record"]["content"] for i in order]
    assert all(set(p) == {"key", "kind", "title", "excerpt", "occurred_at", "recorded_at", "text_truncated"}
               for p in preview)
    with pytest.raises(ValueError):
        thread(w, chosen=chosen + ["record:999999"])
    with pytest.raises(ValueError):
        thread(w, chosen=[chosen[0], chosen[0]])


def test_reflection_time_unknown_is_distinct_from_recorded_time(world):
    w = world
    original = capture(w, "reflection", "我认为可能有采购需求，这不是客户承诺。", kind="reflection")
    t = thread(w, chosen=[original["event"]["key"]])
    preview = read(w, t)["evidence_preview"][0]
    assert preview["kind"] == "reflection"
    assert preview["occurred_at"] is None
    assert preview["recorded_at"] == NOW
    assert preview["excerpt"] == original["record"]["content"]
    assert preview["text_truncated"] is False
    assert read(w, t)["focus_summary"]["latest_communication"] is None


def test_long_excerpt_keeps_final_correction_and_never_changes_original(world):
    w = world
    text = "开始说可能采购。" + "尚待核实的信息。" * 250 + "最后修正：目前没有预算，不能承诺采购。"
    original = capture(w, "long", text, title="长标题" * 40)
    t = thread(w, chosen=[original["event"]["key"]])
    preview = read(w, t)["evidence_preview"][0]
    assert len(preview["title"]) <= 120
    assert len(preview["excerpt"]) <= 600
    assert "中间内容省略" in preview["excerpt"]
    assert preview["excerpt"].startswith("开始说可能采购。")
    assert preview["excerpt"].endswith("最后修正：目前没有预算，不能承诺采购。")
    assert preview["text_truncated"] is True
    current = w.crm.get_record(OWNER, original["record"]["id"])
    assert current["content"] == text
    assert current["original_content"] == text


def test_preview_does_not_publish_unselected_other_project_person_or_unit(world):
    w = world
    chosen = capture(w, "chosen-A", "A的明确勾选内容")
    capture(w, "project-B-private", "B项目私有CANARY", project=w.b)
    another = w.crm.create_contact(OWNER, w.unit["id"], {"name": "另一联系人"}, NOW)
    w.web.timeline.create_record(OWNER, {"contact_id": another["id"]},
        {"request_id": "other-person", "text": "另一人私有CANARY", "kind": "communication"})
    other_unit = w.crm.create_customer(OWNER, {"name": "其他单位"}, NOW)
    w.web.timeline.create_record(OWNER, {"customer_id": other_unit["id"]},
        {"request_id": "other-unit", "text": "其他单位私有CANARY", "kind": "communication"})
    t = thread(w, chosen=[chosen["event"]["key"]])
    preview = read(w, t)["evidence_preview"]
    assert len(preview) == 1
    assert "CANARY" not in json.dumps(preview, ensure_ascii=False)
    for key in ("source_refs", "contact_relations", "profile", "customer_name"):
        assert key not in preview[0]


def test_foreign_owner_selection_and_read_do_not_disclose_preview(world):
    w = world
    chosen = capture(w, "private", "只有本人能看到CANARY")
    t = thread(w, chosen=[chosen["event"]["key"]])
    before, writes = rows(w), w.crm._db.total_changes
    with pytest.raises(KeyError):
        w.web.discussions.get_thread("other-owner", t["id"])
    alien = w.crm.create_customer("other-owner", {"name": "别人的单位"}, NOW)
    before, writes = rows(w), w.crm._db.total_changes
    with pytest.raises(KeyError):
        w.web.discussions.create_thread("other-owner", {"customer_id": alien["id"],
            "timeline_event_keys": [chosen["event"]["key"]]})
    assert rows(w) == before
    assert w.crm._db.total_changes == writes


@pytest.mark.parametrize("change", ["content", "project", "membership", "person", "hidden"])
def test_invalid_selected_original_has_no_preview_but_keeps_saved_questions(world, change):
    w = world
    if change == "hidden":
        record = w.crm.capture_message(OWNER, "hidden-original", "待隐藏的原文CANARY", "voice", NOW)
        record = w.crm.update_record(OWNER, record["id"], {"customer_id": w.unit["id"]}, NOW)
        w.web.sales_workspace.link(OWNER, "record", record["id"], w.a["id"])
        event = w.web.timeline.get_event(OWNER, "record:" + str(record["id"]))
        event = w.web.timeline.save_context(OWNER, event["key"], {"expected_revision": event["revision"],
            "kind": "communication", "contact_relations": [{"contact_id": w.person["id"], "relation": "direct"}]})
    else:
        original = capture(w, "will-change", "已选择的原文CANARY")
        record, event = original["record"], original["event"]
    t = thread(w, chosen=[event["key"]])
    question = "请保留这个问题和完整回复，不要因来源失效而删除。"
    saved = send(w, t, question, "question-before-change")
    if change == "content":
        w.crm.update_record(OWNER, record["id"], {"content": "改了原文，还没有重新核对"}, NOW + 1)
    elif change == "project":
        w.web.sales_workspace.link(OWNER, "record", record["id"], w.b["id"])
    elif change == "membership":
        p = project_current(w, w.a)
        w.web.sales_workspace.archive_stakeholder(OWNER, w.unit["id"], w.a["id"], w.person["id"],
                                                 {"expected_revision": p["revision"], "archived": True})
    elif change == "person":
        w.crm.update_contact(OWNER, w.unit["id"], w.person["id"], {"archived": True}, NOW + 1)
    else:
        w.crm.apply_command(OWNER, "hidden-original", {"action": "cancel"}, "", NOW + 1)
    value = read(w, t)
    assert value["context_invalid"] is True
    assert value["evidence_preview"] is None
    assert [m["text"] for m in value["messages"]] == [m["text"] for m in saved["messages"]]
    assert value["messages"][0]["text"] == question
    assert value["messages"][1]["stale"] is True


def test_archived_project_selected_history_remains_read_only_and_send_blocked(world):
    w = world
    original = capture(w, "archived-history", "归档前的有效所选历史")
    t = thread(w, chosen=[original["event"]["key"]])
    p = project_current(w, w.a)
    w.web.sales_workspace.update_opportunity(OWNER, w.unit["id"], w.a["id"],
                                            {"expected_revision": p["revision"], "archived": True})
    value = read(w, t)
    assert value["context_invalid"] is False
    assert value["thread"]["opportunity_archived"] is True
    assert value["evidence_preview"][0]["excerpt"] == original["record"]["content"]
    assert value["focus_summary"] is None
    before = rows(w)
    with pytest.raises(ValueError):
        send(w, t, "不能发送到归档项目", "blocked-new-question")
    assert rows(w) == before


@pytest.mark.parametrize("timeline", [False, True])
def test_no_explicit_selection_does_not_preview_background_or_source_record(world, timeline):
    w = world
    original = capture(w, "source-only", "单独来源不是本次所选历程")
    t = thread(w, focused=False, chosen=[] if timeline else None,
               source_record_id=original["record"]["id"])
    value = read(w, t)
    assert value["thread"]["timeline_enabled"] is timeline
    assert value["evidence_preview"] == []


def test_preview_text_is_plain_and_redacted_without_mutating_owned_original(world):
    w = world
    text = '<img src=x onerror="attack()">原话：请联系13812345678或private@example.com。'
    original = capture(w, "markup", text, title="<script>attack()</script>")
    t = thread(w, chosen=[original["event"]["key"]])
    preview = read(w, t)["evidence_preview"][0]
    assert preview["title"] == "<script>attack()</script>"
    assert '<img src=x onerror="attack()">' in preview["excerpt"]
    assert "13812345678" not in preview["excerpt"]
    assert "private@example.com" not in preview["excerpt"]
    assert preview["text_truncated"] is False
    assert w.crm.get_record(OWNER, original["record"]["id"])["content"] == text


def test_selected_historical_ai_discussion_remains_discussion_not_customer_fact(world):
    w = world
    prior = thread(w)
    send(w, prior, "以前的AI建议还需要验证", "old-question")
    key = "discussion:" + str(prior["id"])
    current = thread(w, chosen=[key])
    preview = read(w, current)["evidence_preview"][0]
    assert preview["key"] == key
    assert preview["kind"] == "discussion"
    assert "我的问题：以前的AI建议还需要验证" in preview["excerpt"]
    assert "历史AI建议：" in preview["excerpt"]
    assert read(w, current)["focus_summary"]["latest_communication"] is None


def test_selected_empty_historical_discussion_does_not_invent_excerpt(world):
    w = world
    empty = thread(w)
    current = thread(w, chosen=["discussion:" + str(empty["id"])])
    item = read(w, current)["evidence_preview"][0]
    assert item["kind"] == "discussion"
    assert item["excerpt"] == ""
    assert item["text_truncated"] is False
    assert len(w.model.calls) == 0


def test_first_question_short_topic_retains_complete_original_and_replay(world):
    w = world
    t = thread(w, request_id="create-default")
    question = "如何推进数据库试点？\n先了解技术边界，再核对采购路径。" + "还要核对密码合规要求。" * 25
    value = send(w, t, question, "topic-first")
    expected = " ".join(question.split())[:48]
    assert value["thread"]["title"] == expected
    assert len(value["thread"]["title"]) <= 48
    assert value["messages"][0]["text"] == question
    calls = len(w.model.calls)
    assert send(w, t, question, "topic-first") == value
    assert len(w.model.calls) == calls
    repeated_create = w.web.discussions.create_thread(OWNER, {"customer_id": w.unit["id"],
        "opportunity_id": w.a["id"], "contact_id": w.person["id"], "request_id": "create-default"})
    assert repeated_create["thread"]["title"] == expected
    assert repeated_create["thread"]["id"] == t["id"]


@pytest.mark.parametrize("custom", ["我自己的讨论主题", "如何更好地跟进这个客户？"])
def test_custom_topic_not_replaced_by_first_question(world, custom):
    w = world
    t = thread(w, title=custom)
    value = send(w, t, "这个问题不应覆盖我的自定义标题。", "custom-first")
    assert value["thread"]["title"] == custom


def test_failed_first_turn_gets_topic_once_and_later_default_rename_is_respected(world):
    w = world
    t = thread(w)
    w.model.error = True
    first = send(w, t, "失败也应该能认出原先讨论的话题", "failed-first")
    assert first["thread"]["title"] == "失败也应该能认出原先讨论的话题"
    assert first["messages"][0]["status"] == "failed"
    renamed = w.web.discussions.rename_thread(OWNER, t["id"], DEFAULT, first["thread"]["updated_at"])
    w.model.error = False
    retried = send(w, t, "失败也应该能认出原先讨论的话题", "failed-first")
    assert retried["thread"]["title"] == DEFAULT
    assert len(retried["messages"]) == 2
    later = send(w, t, "第二条问题也不重新生成标题", "second-question")
    assert later["thread"]["title"] == DEFAULT
    assert renamed["thread"]["updated_at"] < later["thread"]["updated_at"]


def test_rename_only_thread_metadata_preserves_messages_originals_and_versions(world):
    w = world
    original = capture(w, "original", "不因改讨论标题改变任何原文")
    t = thread(w, chosen=[original["event"]["key"]])
    saved = send(w, t, "我的原问题保持完整", "before-rename")
    before, calls = rows(w), len(w.model.calls)
    result = w.web.discussions.rename_thread(OWNER, t["id"], "  新的推进主题  ", saved["thread"]["updated_at"])
    after = rows(w)
    assert result["thread"]["title"] == "新的推进主题"
    assert result["thread"]["updated_at"] > saved["thread"]["updated_at"]
    assert result["messages"] == saved["messages"]
    assert result["evidence_preview"] == saved["evidence_preview"]
    assert len(w.model.calls) == calls
    assert {k: v for k, v in after.items() if k != "crm_sales_discussions"} == {
        k: v for k, v in before.items() if k != "crm_sales_discussions"}
    prior = w.crm._db.execute("SELECT * FROM crm_sales_discussions WHERE id=?", (t["id"],)).fetchone()
    assert prior["timeline_event_keys_json"] == json.dumps([original["event"]["key"]], separators=(",", ":"))


def test_rename_stale_and_foreign_reject_atomically_same_title_is_noop(world):
    w = world
    t = thread(w)
    changed = w.web.discussions.rename_thread(OWNER, t["id"], "first", t["updated_at"])
    before, writes = rows(w), w.crm._db.total_changes
    with pytest.raises(ValueError):
        w.web.discussions.rename_thread(OWNER, t["id"], "lost update", t["updated_at"])
    with pytest.raises(ValueError):
        w.web.discussions.rename_thread(OWNER, t["id"], "first", t["updated_at"])
    with pytest.raises(KeyError):
        w.web.discussions.rename_thread("other-owner", t["id"], "foreign", changed["thread"]["updated_at"])
    same = w.web.discussions.rename_thread(OWNER, t["id"], "first", changed["thread"]["updated_at"])
    assert same == changed
    assert rows(w) == before
    assert w.crm._db.total_changes == writes


def test_renaming_topic_keeps_already_adopted_action_receipt_and_original_advice(world):
    w = world

    async def local_move(context, history, text, now):
        w.model.calls.append((context, history, text, now))
        return {**REPLY, "next_moves": [{"title": "核对试点边界", "reason": "边界尚需明确",
                "contact_hint": "技术负责人待确认", "preparation": "准备一页问题清单", "success_signal": "明确测试范围"}]}

    w.web.discussions.advisor.reply = local_move
    t = thread(w)
    value = send(w, t, "先讨论怎么核对试点边界", "adopted-topic")
    assistant = value["messages"][1]
    record = w.web.discussions.adopt(OWNER, t["id"], assistant["id"], 1,
        {"request_id": "topic-action", "expected_snapshot": assistant["snapshot"],
         "draft": {"title": "用户修改后的核对动作"}})
    fresh = read(w, t)
    before = rows(w)
    changed = w.web.discussions.rename_thread(OWNER, t["id"], "后续推进主题", fresh["thread"]["updated_at"])
    assert changed["messages"] == fresh["messages"]
    assert changed["messages"][1]["data"]["next_moves"][0]["adopted_record_id"] == record["id"]
    assert w.crm.get_record(OWNER, record["id"])["original_content"] == record["original_content"]
    assert "核对试点边界" in record["original_content"]
    assert {k: v for k, v in rows(w).items() if k != "crm_sales_discussions"} == {
        k: v for k, v in before.items() if k != "crm_sales_discussions"}


@pytest.mark.parametrize("invalid", [None, False, 7, "", " \n ", "x" * 121, "bad\x00title"])
def test_rename_invalid_title_has_no_writes(world, invalid):
    w = world
    t = thread(w)
    before, writes = rows(w), w.crm._db.total_changes
    with pytest.raises(ValueError):
        w.web.discussions.rename_thread(OWNER, t["id"], invalid, t["updated_at"])
    assert rows(w) == before
    assert w.crm._db.total_changes == writes


@pytest.mark.parametrize("invalid", [None, False, "1800550000", float("nan"), float("inf")])
def test_rename_invalid_expected_version_has_no_writes(world, invalid):
    w = world
    t = thread(w)
    before, writes = rows(w), w.crm._db.total_changes
    with pytest.raises(ValueError):
        w.web.discussions.rename_thread(OWNER, t["id"], "valid title", invalid)
    assert rows(w) == before
    assert w.crm._db.total_changes == writes


def test_archive_history_can_rename_without_reactivating_project_or_messages(world):
    w = world
    t = thread(w)
    saved = send(w, t, "归档前的完整问题", "archive-question")
    p = project_current(w, w.a)
    w.web.sales_workspace.update_opportunity(OWNER, w.unit["id"], w.a["id"],
                                            {"expected_revision": p["revision"], "archived": True})
    result = w.web.discussions.rename_thread(OWNER, t["id"], "已归档：采购复盘", saved["thread"]["updated_at"])
    assert result["thread"]["opportunity_archived"] is True
    assert result["thread"]["title"] == "已归档：采购复盘"
    assert [m["text"] for m in result["messages"]] == [m["text"] for m in saved["messages"]]
    assert project_current(w, w.a)["archived"] is True
    with pytest.raises(ValueError):
        send(w, t, "不能因为改标题启用项目", "blocked-after-rename")


def test_pending_provider_completion_preserves_rename_and_monotonic_cas(world):
    w = world

    async def run():
        t = thread(w)
        w.model.release = asyncio.Event()
        task = asyncio.create_task(w.web.discussions.send_message(OWNER, t["id"],
            {"text": "慢回复期间我主动改题目", "request_id": "pending-topic"}))
        await w.model.started.wait()
        pending = read(w, t)
        w.clock[0] = NOW - 60
        renamed = w.web.discussions.rename_thread(OWNER, t["id"], "我手动确认的主题", pending["thread"]["updated_at"])
        w.model.release.set()
        completed = await task
        assert completed["thread"]["title"] == "我手动确认的主题"
        assert completed["thread"]["updated_at"] > renamed["thread"]["updated_at"] > pending["thread"]["updated_at"]
        assert completed["messages"][0]["text"] == "慢回复期间我主动改题目"
        before = rows(w)
        with pytest.raises(ValueError):
            w.web.discussions.rename_thread(OWNER, t["id"], "旧窗口不能覆盖", renamed["thread"]["updated_at"])
        assert rows(w) == before

    asyncio.run(run())


def test_title_route_security_schema_cas_and_fresh_preview_on_ephemeral_http(world):
    w = world

    async def run():
        original = capture(w, "http-source", "HTTP所选原文")
        t = thread(w, chosen=[original["event"]["key"]])
        client = TestClient(TestServer(w.web.application()), cookie_jar=CookieJar(unsafe=True))
        await client.start_server()
        try:
            url = f'/api/sales-discussions/{t["id"]}/title'
            body = {"title": "HTTP新主题", "expected_updated_at": t["updated_at"]}
            assert (await client.post(url, json=body)).status == 401
            login = await client.post("/api/login", json={"password": PASSWORD})
            assert login.status == 200
            csrf = (await login.json())["csrf"]
            assert (await client.post(url, json=body)).status == 403
            headers = {"X-CSRF-Token": csrf}
            assert (await client.post(url, json=body, headers={**headers, "Origin": "https://foreign.invalid"})).status == 403
            for invalid in ({"title": "missing CAS"}, {**body, "customer_id": w.unit["id"]}):
                assert (await client.post(url, json=invalid, headers=headers)).status == 400
            result = await client.post(url, json=body, headers=headers)
            assert result.status == 200
            value = await result.json()
            assert value["thread"]["title"] == "HTTP新主题"
            assert value["thread"]["updated_at"] > t["updated_at"]
            assert value["evidence_preview"][0]["key"] == original["event"]["key"]
            assert value["evidence_preview"][0]["excerpt"] == "HTTP所选原文"
            fetched = await client.get(f'/api/sales-discussions/{t["id"]}')
            assert await fetched.json() == value
            assert (await client.post(url, json={**body, "title": "stale overwrite"}, headers=headers)).status == 400
            assert (await client.post("/api/sales-discussions/999999/title", json=body, headers=headers)).status == 404
            before, writes = rows(w), w.crm._db.total_changes
            same = await client.post(url, json={"title": "HTTP新主题", "expected_updated_at": value["thread"]["updated_at"]}, headers=headers)
            assert same.status == 200
            assert await same.json() == value
            assert rows(w) == before
            assert w.crm._db.total_changes == writes
            assert len(w.model.calls) == 0
        finally:
            await client.close()

    asyncio.run(run())
