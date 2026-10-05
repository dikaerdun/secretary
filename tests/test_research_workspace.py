"""Public research trials use fresh synthetic stores and never real services."""
import asyncio
import json

import httpx
import pytest

from secretary.customer_store import CustomerStore
from secretary.sales_workspace import SalesWorkspace
from secretary.profile_intelligence import ProfileIntelligence
from secretary.public_research import TavilyResearcher
from secretary.research_workspace import ResearchConflict, ResearchWorkspace

NOW = 1_800_000_000.0
NAME = "星河城市银行股份有限公司"


class Search:
    def __init__(self, rows=None):
        self.calls = []
        self.rows = rows

    async def research(self, context):
        self.calls.append(context)
        return self.rows if self.rows is not None else [{
            "url": "https://bank.example/about", "title": NAME + "机构简介",
            "text": NAME + "，行业是金融。总部位于上海。", "entity_name": NAME,
            "published_at": "2021-03-01", "fetched_at": NOW,
        }]


@pytest.fixture
def setup(tmp_path):
    crm = CustomerStore(tmp_path / "fresh-research.sqlite3")
    sales = SalesWorkspace(crm, clock=lambda: NOW)
    customer = crm.create_customer("owner", {"name": NAME, "notes": "合成用户已录入资料，不能覆盖"}, NOW)
    clock = [NOW]
    search = Search()
    profile = ProfileIntelligence(crm, sales, researcher=search, clock=lambda: clock[0])
    profile.configure("owner", {"official_domains": ["bank.example"]}, customer["id"])
    research = ResearchWorkspace(crm, profile, clock=lambda: clock[0])
    yield crm, profile, research, customer, search, clock
    research.close()
    crm.close()


def create(setup, mode="quick", request_id="run-1"):
    return setup[2].create("owner", setup[3]["id"], {"mode": mode, "request_id": request_id})["run"]


def finish(setup, run=None):
    run = run or create(setup)
    asyncio.run(setup[2].process_pending("owner", limit=1))
    return setup[2].get("owner", run["id"])


def decision(item, **changes):
    return {"candidate_id": item["id"], "expected_candidate_revision": item["revision"],
            "expected_fact_id": item["current_fact_id"], "verify_public": True,
            "verify_entity": True, **changes}


def confirm(setup, run, items=None, request_id="confirm-1"):
    return setup[2].confirm("owner", run["id"], {"request_id": request_id,
        "expected_revision": run["revision"], "items": items or [decision(run["items"][0])]})


def test_quick_deep_queries_sources_evidence_and_preserved_user_data(setup):
    crm, _, research, customer, search, _ = setup
    before = crm.get_customer("owner", customer["id"])
    quick = finish(setup)
    assert quick["status"] == "ready" and quick["stage"] == "review"
    assert quick["items"] and all(not x["selected"] and x["basis"] == "observation" for x in quick["items"])
    assert quick["sources"][0]["published_at"] == "2021-03-01"
    assert quick["sources"][0]["identity_verified"]
    assert quick["items"][0]["source"]["quote"] == quick["items"][0]["evidence"]
    deep = finish(setup, create(setup, "deep", "deep-1"))
    assert deep["id"] != quick["id"] and deep["mode"] == "deep"
    assert len(search.calls) == 4
    assert {x["theme"] for x in search.calls[1:]} == {"background", "digitalization", "procurement"}
    assert all(set(x["customer"]) == {"id", "name"} for x in search.calls)
    assert before == crm.get_customer("owner", customer["id"])
    assert crm.profile("owner", customer["id"])["fields"] == []
    for table in ("tasks", "proposals", "notifications"):
        assert crm._db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert len(research.list_runs("owner", customer["id"])["runs"]) == 2


def test_request_and_mode_cache_idempotency_and_explicit_retry(setup):
    crm, _, research, customer, search, _ = setup
    run = finish(setup)
    repeated = research.create("owner", customer["id"], {"mode": "quick", "request_id": "run-1"})
    assert not repeated["created"] and repeated["run"]["id"] == run["id"]
    cached = research.create("owner", customer["id"], {"mode": "quick", "request_id": "cache-1"})
    assert cached["reused"] and cached["run"]["id"] == run["id"]
    with pytest.raises(ResearchConflict):
        research.create("owner", customer["id"], {"mode": "deep", "request_id": "cache-1"})
    retried = research.retry("owner", run["id"], {"expected_revision": run["revision"]})
    assert retried["status"] == "queued"
    finish(setup, retried)
    assert len(search.calls) == 2
    assert crm._db.execute("SELECT count(*) FROM crm_profile_public_sources").fetchone()[0] == 1


def test_edit_sidecar_preserves_ai_original_and_quick_draft_on_deep(setup):
    _, profile, research, _, _, _ = setup
    quick = finish(setup)
    item = quick["items"][0]
    edited = research.edit_draft("owner", quick["id"], {"expected_revision": quick["revision"],
        "items": [{"candidate_id": item["id"], "value": "用户修正后的单位背景", "selected": True}]})
    assert edited["items"][0]["draft_value"] == "用户修正后的单位背景"
    assert profile.get_candidate("owner", item["id"])["value"] == item["value"]
    with pytest.raises(ResearchConflict):
        research.edit_draft("owner", quick["id"], {"expected_revision": quick["revision"], "items": [{"candidate_id": item["id"], "selected": False}]})
    deep = finish(setup, create(setup, "deep", "deep-1"))
    assert deep["items"][0]["draft_value"] != "用户修正后的单位背景"
    assert research.get("owner", quick["id"])["items"][0]["draft_value"] == "用户修正后的单位背景"


def test_batch_partial_fact_cas_keeps_drafts_and_refresh_can_resume(setup):
    crm, _, research, customer, _, _ = setup
    run = finish(setup)
    industry = next(x for x in run["items"] if x["key"] == "industry")
    region = next(x for x in run["items"] if x["key"] == "region")
    new = crm.save_fact("owner", customer["id"], {"key": "industry", "value": "保险", "basis": "reported"}, NOW)
    result = confirm(setup, run, [decision(industry), decision(region)])
    assert result["status"] == "partial"
    assert [x["status"] for x in result["results"]] == ["conflict", "confirmed"]
    assert research.get("owner", run["id"])["items"]
    replay = confirm(setup, run, [decision(industry), decision(region)])
    assert replay["replayed"] and replay["results"] == result["results"]
    current = research.get("owner", run["id"])
    item = next(x for x in current["items"] if x["key"] == "industry")
    assert item["current_fact_id"] == new["id"] and item["captured_current_fact_id"] is None
    assert item["changed_since_capture"]
    done = confirm(setup, current, [decision(item)], "confirm-2")
    assert done["status"] == "complete"
    assert crm.profile("owner", customer["id"])["history_total"] == 3


def test_public_verification_and_nonofficial_identity_are_not_automatic(setup):
    _, profile, _, customer, search, _ = setup
    profile.configure("owner", {"official_domains": []}, customer["id"])
    run = finish(setup)
    item = run["items"][0]
    assert item["requires_entity_confirmation"]
    blocked = confirm(setup, run, [decision(item, verify_public=False, verify_entity=False)])
    assert blocked["status"] == "blocked" and blocked["results"][0]["code"] == 400
    current = setup[2].get("owner", run["id"])
    blocked = confirm(setup, current, [decision(current["items"][0], verify_entity=False)], "c2")
    assert blocked["results"][0]["status"] == "blocked"
    assert not search.calls[0]["official_domains"]


def test_run_owner_candidate_binding_and_invalid_request_are_nonmutating(setup):
    crm, _, research, _, _, _ = setup
    run = finish(setup)
    other = crm.create_customer("owner", {"name": "另一家合成公司"}, NOW)
    foreign = research.create("owner", other["id"], {"mode": "quick", "request_id": "other"})["run"]
    assert research.get("intruder", run["id"]) is None
    with pytest.raises(KeyError):
        research.edit_draft("intruder", run["id"], {"expected_revision": run["revision"], "items": [{"candidate_id": run["items"][0]["id"], "selected": True}]})
    with pytest.raises((KeyError, ValueError)):
        research.edit_draft("owner", foreign["id"], {"expected_revision": foreign["revision"], "items": [{"candidate_id": run["items"][0]["id"], "value": "误写"}]})
    with pytest.raises(ValueError):
        research.confirm("owner", run["id"], {"request_id": "bad", "expected_revision": run["revision"], "items": [decision(run["items"][0]), decision(run["items"][0])]})
    assert crm.profile("owner", setup[3]["id"])["fields"] == []


def test_search_failure_and_analysis_failure_have_distinct_recoverable_states(setup):
    crm, profile, research, _, search, _ = setup
    class BadSearch:
        async def research(self, context):
            raise RuntimeError("sensitive_provider_text_should_not_escape")
    profile.researcher = BadSearch()
    failed = finish(setup)
    assert failed["status"] == "failed" and failed["errors"][0]["stage"] == "searching"
    assert "sensitive_provider_text" not in json.dumps(failed)
    profile.researcher = search
    class BadAnalyzer:
        async def extract(self, source, context):
            raise ValueError("sensitive_model_payload")
    profile.analyzer = BadAnalyzer()
    queued = research.retry("owner", failed["id"], {"expected_revision": failed["revision"]})
    partial = finish(setup, queued)
    assert partial["status"] == "partial" and partial["sources"][0]["status"] == "failed"
    assert partial["summary"]["source_count"] == 1 and not partial["items"]
    assert "sensitive_model_payload" not in json.dumps(partial)
    profile.analyzer = None
    queued = research.retry("owner", partial["id"], {"expected_revision": partial["revision"]})
    complete = finish(setup, queued)
    assert complete["status"] == "ready" and complete["items"]
    assert crm._db.execute("SELECT count(*) FROM crm_profile_public_sources").fetchone()[0] == 1


def test_not_configured_is_honest_and_retry_uses_later_configuration(setup):
    _, profile, research, _, search, _ = setup
    profile.researcher = None
    run = finish(setup)
    assert run["status"] == "failed" and not run["capabilities"]["research_configured"]
    profile.researcher = search
    ready = finish(setup, research.retry("owner", run["id"], {"expected_revision": run["revision"]}))
    assert ready["status"] == "ready"


def test_cancel_and_late_search_cannot_add_sources_or_candidates(setup):
    crm, profile, research, _, _, _ = setup
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        class Slow(Search):
            async def research(self, context):
                entered.set()
                await release.wait()
                return await super().research(context)
        profile.researcher = Slow()
        run = create(setup)
        task = asyncio.create_task(research.process_pending("owner"))
        await entered.wait()
        live = research.get("owner", run["id"])
        cancelled = research.cancel("owner", run["id"], {"expected_revision": live["revision"]})
        release.set()
        await task
        assert cancelled["status"] == research.get("owner", run["id"])["status"] == "cancelled"
    asyncio.run(scenario())
    assert crm._db.execute("SELECT count(*) FROM crm_profile_public_sources").fetchone()[0] == 0


def test_worker_cancellation_and_new_service_resume_saved_sources_without_duplicate(setup):
    crm, profile, research, _, search, _ = setup
    async def scenario():
        entered = asyncio.Event()
        class SlowAnalyzer:
            async def extract(self, source, context):
                entered.set()
                await asyncio.Future()
        profile.analyzer = SlowAnalyzer()
        run = create(setup)
        task = asyncio.create_task(research.process_pending("owner"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert research.get("owner", run["id"])["status"] == "queued"
        return run
    run = asyncio.run(scenario())
    profile.analyzer = None
    resumed = ResearchWorkspace(crm, profile, clock=lambda: NOW)
    asyncio.run(resumed.process_pending("owner"))
    assert resumed.get("owner", run["id"])["status"] == "ready"
    assert len(search.calls) == 1
    assert crm._db.execute("SELECT count(*) FROM crm_profile_public_sources").fetchone()[0] == 1


def test_identity_change_during_search_and_before_confirm_needs_review(setup):
    crm, profile, research, customer, _, _ = setup
    class Rename(Search):
        async def research(self, context):
            crm.update_customer("owner", customer["id"], {"name": "更名后的单位"}, NOW)
            return await super().research(context)
    profile.researcher = Rename()
    run = finish(setup)
    assert run["status"] == "needs_review"
    assert not run["sources"]
    crm.update_customer("owner", customer["id"], {"name": NAME}, NOW)
    profile.researcher = Search()
    run = finish(setup, create(setup, request_id="run-2"))
    profile.configure("owner", {"official_domains": ["new.example"]}, customer["id"])
    with pytest.raises(ResearchConflict):
        confirm(setup, run)
    assert crm.profile("owner", customer["id"])["fields"] == []


def test_confirmation_crash_after_decide_replays_audit_without_duplicate_fact(setup, monkeypatch):
    crm, profile, research, customer, _, _ = setup
    run = finish(setup)
    real = profile.decide
    def crash(owner, candidate_id, body):
        result = real(owner, candidate_id, body)
        raise KeyboardInterrupt("synthetic abrupt process death after committed decide")
    monkeypatch.setattr(profile, "decide", crash)
    with pytest.raises(KeyboardInterrupt):
        confirm(setup, run)
    assert crm.profile("owner", customer["id"])["history_total"] == 1
    monkeypatch.setattr(profile, "decide", real)
    recreated = ResearchWorkspace(crm, profile, clock=lambda: NOW)
    setup2 = (*setup[:2], recreated, *setup[3:])
    result = confirm(setup2, run)
    assert result["results"][0]["status"] == "already_confirmed"
    assert crm.profile("owner", customer["id"])["history_total"] == 1


def test_batch_same_request_changed_payload_conflicts(setup):
    run = finish(setup)
    confirm(setup, run)
    with pytest.raises(ResearchConflict):
        confirm(setup, run, [decision(run["items"][0], value="其他值")])


def test_confirmed_candidate_changed_draft_is_not_falsely_reported_as_saved(setup):
    crm, _, research, customer, _, _ = setup
    run = finish(setup)
    result = confirm(setup, run)
    current = result["run"]
    item = current["items"][0]
    changed = confirm(setup, current, [decision(item, value="不同的新内容，不能假成功")], "new-change")
    assert changed["status"] == "blocked" and changed["results"][0]["status"] == "conflict"
    assert crm.profile("owner", customer["id"])["history_total"] == 1
    assert "不同的新内容" not in json.dumps(crm.profile("owner", customer["id"]), ensure_ascii=False)


def test_cleared_latest_field_does_not_resurrect_historical_gap_state(setup):
    crm, _, _, customer, _, _ = setup
    crm.save_fact("owner", customer["id"], {"key": "legal_name", "value": NAME, "basis": "reported"}, NOW)
    crm.save_fact("owner", customer["id"], {"key": "legal_name", "value": "", "basis": "reported"}, NOW)
    run = finish(setup)
    assert "legal_name" in {x["key"] for x in run["gaps"]}


def test_project_clue_is_visible_but_not_batch_saved_as_unit_fact(setup):
    _, _, _, _, search, _ = setup
    search.rows = [{"url":"https://bank.example/tender", "title":NAME+"采购公告",
        "text":NAME+"，预算30万元尚未审批。", "entity_name":NAME}]
    run = finish(setup)
    assert run["items"][0]["scope"] == "project"
    assert any(x["scope"] == "project" for x in run["recommendations"])
    with pytest.raises(ValueError):
        confirm(setup, run)


def test_recovered_batch_only_accepts_matching_committed_decision(setup, monkeypatch):
    crm, profile, research, customer, _, _ = setup
    run = finish(setup)
    real = profile.decide
    def different(owner, candidate_id, body):
        result = real(owner, candidate_id, {**body, "value":"另外一次明确核对的内容"})
        raise KeyboardInterrupt("synthetic abrupt death after a different decision")
    monkeypatch.setattr(profile, "decide", different)
    with pytest.raises(KeyboardInterrupt):
        confirm(setup, run)
    monkeypatch.setattr(profile, "decide", real)
    restored = ResearchWorkspace(crm, profile, clock=lambda: NOW)
    setup2 = (*setup[:2], restored, *setup[3:])
    result = confirm(setup2, run)
    assert result["status"] == "blocked" and result["results"][0]["status"] == "conflict"
    assert crm.profile("owner", customer["id"])["history_total"] == 1


def test_recovery_from_expired_lease_and_second_worker_waits_for_live_lease(setup):
    crm, profile, research, _, search, clock = setup
    run = create(setup)
    claimed = research._claim("owner")
    other = ResearchWorkspace(crm, profile, clock=lambda: clock[0])
    assert asyncio.run(other.process_pending("owner"))["processed"] == 0
    assert not search.calls
    clock[0] += 181
    assert asyncio.run(other.process_pending("owner"))["processed"] == 1
    assert other.get("owner", run["id"])["status"] == "ready"
    assert claimed["lease_token"] not in json.dumps(other.get("owner", run["id"]))
    assert len(search.calls) == 1


def test_user_edits_while_later_source_analyzes_are_retained(setup):
    _, profile, research, _, search, _ = setup
    search.rows = [{"url":"https://bank.example/a", "title":NAME, "text":NAME+"，行业是金融。"},
                   {"url":"https://bank.example/b", "title":NAME, "text":NAME+"，总部位于上海。"}]
    run = create(setup)
    class ConcurrentAnalyzer:
        def __init__(self): self.calls = 0
        async def extract(self, source, context):
            self.calls += 1
            if self.calls == 2:
                live = research.get("owner", run["id"])
                research.edit_draft("owner", run["id"], {"expected_revision":live["revision"],
                    "items":[{"candidate_id":live["items"][0]["id"],"value":"并行修改保留","selected":True}]})
            from secretary.profile_intelligence import _rule_extract
            return _rule_extract(source, context)
    profile.analyzer = ConcurrentAnalyzer()
    ready = finish(setup, run)
    assert ready["status"] == "ready" and len(ready["items"]) == 2
    assert ready["items"][0]["draft_value"] == "并行修改保留" and ready["items"][0]["selected"]


def test_bad_model_evidence_rolls_back_entire_source_candidates(setup):
    crm, profile, _, _, _, _ = setup
    class Fabricated:
        async def extract(self, source, context):
            return [{"key":"industry","value":"金融","evidence":"行业是金融","basis":"observation"},
                    {"key":"region","value":"北京","evidence":"不存在的原话","basis":"observation"}]
    profile.analyzer = Fabricated()
    run = finish(setup)
    assert run["status"] == "partial" and not run["items"]
    assert crm._db.execute("SELECT count(*) FROM crm_profile_candidates").fetchone()[0] == 0
    assert len(run["sources"]) == 1


def test_inline_confirmation_edits_are_saved_to_run_on_success_and_conflict(setup):
    crm, _, research, customer, _, _ = setup
    run = finish(setup)
    industry = next(x for x in run["items"] if x["key"] == "industry")
    region = next(x for x in run["items"] if x["key"] == "region")
    crm.save_fact("owner", customer["id"], {"key":"industry","value":"已确认的新行业","basis":"reported"}, NOW)
    result = confirm(setup, run, [decision(industry, value="用户此次改写的行业"),
        decision(region, value="用户此次改写的地域")])
    assert result["status"] == "partial"
    saved = {x["key"]:x for x in result["run"]["items"]}
    assert saved["industry"]["draft_value"] == "用户此次改写的行业" and saved["industry"]["selected"]
    assert saved["region"]["draft_value"] == "用户此次改写的地域" and saved["region"]["selected"]
    assert saved["region"]["status"] == "confirmed"
    assert saved["region"]["value"] == region["value"]  # AI's source proposal is preserved.
    assert research.get("owner", run["id"])["items"] == result["run"]["items"]


def test_entire_batch_validation_precedes_inline_draft_writes(setup):
    _, _, research, _, _, _ = setup
    run = finish(setup)
    with pytest.raises((ValueError, KeyError)):
        confirm(setup, run, [decision(run["items"][0], value="这不能部分改草稿"),
            {"candidate_id":2**60,"expected_candidate_revision":1,"expected_fact_id":None}])
    assert research.get("owner", run["id"])["items"][0]["draft_value"] == run["items"][0]["draft_value"]
    with pytest.raises(ValueError):
        confirm(setup, run, request_id="   ")


def test_retry_keeps_source_budget_bounded(setup):
    _, _, research, _, search, _ = setup
    search.rows = [{"url":f"https://bank.example/a-{i}","title":NAME,"text":NAME+"，行业是金融。"} for i in range(5)]
    run = finish(setup, create(setup, "deep", "deep-budget"))
    for n in range(1, 5):
        search.rows = [{"url":f"https://bank.example/{n}-{i}","title":NAME,"text":NAME+"，总部位于上海。"} for i in range(5)]
        run = finish(setup, research.retry("owner", run["id"], {"expected_revision":run["revision"]}))
    assert run["summary"]["source_count"] <= 15


def test_regular_error_after_committed_decide_returns_recovered_receipt(setup, monkeypatch):
    crm, profile, _, customer, _, _ = setup
    run = finish(setup)
    real = profile.decide
    def after_commit(owner, candidate_id, body):
        real(owner, candidate_id, body)
        raise RuntimeError("synthetic response handling failure")
    monkeypatch.setattr(profile, "decide", after_commit)
    result = confirm(setup, run)
    assert result["status"] == "complete" and result["results"][0]["status"] == "already_confirmed"
    assert confirm(setup, run)["results"] == result["results"]
    assert crm.profile("owner", customer["id"])["history_total"] == 1


def test_automatic_research_reuses_active_deep_and_syncs_shared_schedule(setup):
    _, profile, research, customer, search, _ = setup
    profile.configure("owner", {"auto_research":True}, customer["id"])
    deep = create(setup, "deep", "active-deep")
    queued = research.queue_automatic("owner", customer["id"], "automatic-1")
    assert queued["reused"] and queued["run"]["id"] == deep["id"]
    finished = finish(setup, deep)
    assert finished["status"] == "ready" and len(search.calls) == 3
    settings = profile.status("owner", customer["id"])["customer_settings"]
    assert settings["last_research_at"] == NOW and settings["research_retry_at"] == 0


def test_automatic_queue_rechecks_current_settings_without_cancelling_manual_deep(setup):
    _, profile, research, customer, _, _ = setup
    deep = create(setup, "deep", "manual-deep")
    blocked = research.queue_automatic("owner", customer["id"], "old-snapshot-auto")
    assert blocked["blocked"] and blocked["run"] is None
    assert research.get("owner", deep["id"])["status"] == "queued"
    profile.configure("owner", {"auto_research":True,"enabled":False}, customer["id"])
    assert research.queue_automatic("owner", customer["id"], "disabled-auto")["blocked"]
    profile.configure("owner", {"enabled":True,"official_domains":[]}, customer["id"])
    assert research.queue_automatic("owner", customer["id"], "no-domains-auto")["blocked"]
    assert len(research.list_runs("owner", customer["id"])["runs"]) == 1


def test_empty_search_result_honestly_describes_zero_sources(setup):
    _, _, _, _, search, _ = setup
    search.rows = []
    run = finish(setup)
    assert run["status"] == "ready" and not run["sources"] and not run["items"]
    assert "没有找到" in run["message"] and "查看来源" not in run["message"]


@pytest.mark.parametrize("handling", ["rejected", "stale_source_correction"])
def test_unusable_public_candidate_reopens_background_gap_without_fact_or_schedule(setup, handling):
    crm, profile, research, customer, _, _ = setup
    before = crm.get_customer("owner", customer["id"])
    run = finish(setup)
    candidate = next(item for item in run["items"] if item["key"] == "industry")
    assert candidate["status"] == "pending" and candidate["basis"] == "observation"
    assert "industry" not in {item["key"] for item in run["gaps"]}
    if handling == "rejected":
        profile.decide("owner", candidate["id"], {"decision": "reject", "expected_revision": candidate["revision"],
                        "reason": "原公开线索不可靠，不能采用。"})
        expected = "rejected"
    else:
        # Correct this synthetic saved source. The canonical reader, rather
        # than the fixture, decides whether the candidate becomes stale.
        with crm._transaction() as db:
            db.execute("UPDATE crm_profile_public_sources SET text=? WHERE owner=? AND id=?",
                       ("公开描述已纠正，目标行业尚未明确。", "owner", run["sources"][0]["id"]))
        expected = "stale"
    current = research.get("owner", run["id"])
    saved = next(item for item in current["items"] if item["id"] == candidate["id"])
    assert saved["status"] == expected
    assert (saved["value"], saved["evidence"], saved["draft_value"]) == (candidate["value"], candidate["evidence"], candidate["draft_value"])
    assert "industry" in {item["key"] for item in current["gaps"]}
    assert crm.get_customer("owner", customer["id"]) == before
    assert crm.profile("owner", customer["id"])["fields"] == []
    for table in ("crm_records", "tasks", "proposals", "notifications"):
        assert crm._db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_rejected_candidate_does_not_hide_another_current_same_field_clue(setup):
    _, profile, research, _, search, _ = setup
    search.rows = [{"url": "https://bank.example/about", "title": NAME + "行业简介", "text": NAME + "，行业是金融。", "entity_name": NAME},
                   {"url": "https://bank.example/about-new", "title": NAME + "另一份介绍", "text": NAME + "，行业是银行业。", "entity_name": NAME}]
    run = finish(setup)
    industry = [item for item in run["items"] if item["key"] == "industry"]
    assert len(industry) == 2
    profile.decide("owner", industry[0]["id"], {"decision": "reject", "expected_revision": industry[0]["revision"]})
    current = research.get("owner", run["id"])
    assert {item["status"] for item in current["items"] if item["key"] == "industry"} == {"pending", "rejected"}
    assert "industry" not in {item["key"] for item in current["gaps"]}


def test_confirmed_public_background_retains_observation_and_still_satisfies_gap(setup):
    crm, _, research, customer, _, _ = setup
    run = finish(setup)
    candidate = next(item for item in run["items"] if item["key"] == "industry")
    result = confirm(setup, run, [decision(candidate)])
    assert result["status"] == "complete"
    current = research.get("owner", run["id"])
    assert "industry" not in {item["key"] for item in current["gaps"]}
    field = next(item for item in crm.profile("owner", customer["id"])["fields"] if item["key"] == "industry")
    assert field["basis"] == "observation" and field["value"] == candidate["value"]
    assert crm._db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_profile_worker_does_not_double_analyze_managed_research_source_on_cancel(setup):
    crm, profile, research, _, _, _ = setup
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        class SlowAnalyzer:
            def __init__(self): self.calls = 0
            async def extract(self, source, context):
                self.calls += 1
                entered.set()
                await release.wait()
                return [{"key":"industry","value":"金融","evidence":"行业是金融","basis":"observation"}]
        analyzer = SlowAnalyzer()
        profile.analyzer = analyzer
        run = create(setup)
        task = asyncio.create_task(research.process_pending("owner"))
        await entered.wait()
        scan = await asyncio.wait_for(profile.scan("owner", force=True), 1)
        assert scan["processed"] == 0 and analyzer.calls == 1
        live = research.get("owner", run["id"])
        research.cancel("owner", run["id"], {"expected_revision":live["revision"]})
        again = await asyncio.wait_for(profile.scan("owner", force=True), 1)
        assert again["processed"] == 0 and analyzer.calls == 1
        release.set()
        await task
    asyncio.run(scenario())
    assert crm._db.execute("SELECT count(*) FROM crm_profile_candidates").fetchone()[0] == 0
    assert crm._db.execute("SELECT status FROM crm_profile_source_jobs").fetchone()[0] == "research_managed"


@pytest.mark.parametrize("data", [None, {}, {"mode":"invalid","request_id":"a"}, {"mode":"quick","request_id":""},
    {"mode":"quick","request_id":"a", "query":"任意内部查询"}, {"mode":"quick","request_id":123}])
def test_create_rejects_invalid_bounded_payload(setup, data):
    with pytest.raises(ValueError):
        setup[2].create("owner", setup[3]["id"], data)


def test_deep_adapter_procurement_can_find_external_public_sources_without_trusting_identity():
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"results":[{"url":"https://public.example/tender", "title":NAME+"采购公告",
            "content":NAME+"公开采购数据库加密设备", "published_date":"2020-01-01"},
            {"url":"https://other.example/tender", "title":"另一家银行", "content":"其他单位采购"}]})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = TavilyResearcher("synthetic-key", client=client, clock=lambda: NOW)
            return await adapter.research({"customer":{"name":NAME},"official_domains":["bank.example"],
                "research_domains":["bank.example"],"mode":"deep","theme":"procurement"})
    rows = asyncio.run(scenario())
    assert len(rows) == 1 and rows[0]["url"] == "https://public.example/tender"
    assert "同名" in rows[0]["identity_reason"] and "include_domains" not in calls[0]
    assert "采购" in calls[0]["query"] and calls[0]["search_depth"] == "basic"
