"""Independent counterexamples for late ordinary scan versus managed exchange."""
import asyncio
import pytest

from secretary.customer_store import CustomerStore
from secretary.sales_workspace import SalesWorkspace
from secretary.profile_intelligence import ProfileIntelligence
from secretary.exchange_workspace import ExchangeWorkspace
from secretary.sales_discussion import DiscussionService


@pytest.mark.parametrize("late_failure", [False, True])
def test_late_scan_reply_or_failure_cannot_overwrite_managed_exchange(tmp_path, late_failure):
    async def run():
        crm = CustomerStore(tmp_path / "fresh-scan-exchange-race.sqlite3")
        now = 1_800_000_000.0
        sales = SalesWorkspace(crm, clock=lambda: now)
        unit = crm.create_customer("owner", {"name": "合成扫描银行"}, now)
        row = crm.create_record("owner", {"customer_id": unit["id"], "kind": "note", "title": "行业原话", "content": "合成扫描银行行业是金融。"}, now)
        started, release = asyncio.Event(), asyncio.Event()
        class Analyzer:
            calls = 0
            async def extract(self, source, context):
                self.calls += 1
                if self.calls == 1:
                    started.set()
                    await release.wait()
                    if late_failure:
                        raise RuntimeError("old scanner response failed")
                    value = "不该写入的迟到扫描摘要"
                else:
                    value = "统一准备稿已分析的金融行业"
                return [{"key": "industry", "value": value, "basis": "reported", "evidence": "合成扫描银行行业是金融"}]
        analyzer = Analyzer()
        profile = ProfileIntelligence(crm, sales, analyzer=analyzer, clock=lambda: now)
        exchange = ExchangeWorkspace(crm, sales, profile, clock=lambda: now)
        try:
            old_worker = asyncio.create_task(profile.scan("owner", force=True))
            await asyncio.wait_for(started.wait(), 2)
            assert crm._db.execute("SELECT status FROM crm_profile_source_jobs").fetchone()[0] == "scan_processing"
            view = await exchange.prepare("owner", "record", row["id"])
            assert view["status"] == "ready"
            current_job = dict(crm._db.execute("SELECT * FROM crm_profile_source_jobs").fetchone())
            assert current_job["status"] == "complete"
            release.set()
            await asyncio.wait_for(old_worker, 2)
            final_job = dict(crm._db.execute("SELECT * FROM crm_profile_source_jobs").fetchone())
            assert final_job == current_job
            candidates = profile.list_candidates("owner")["items"]
            assert len(candidates) == 1 and candidates[0]["value"] == "统一准备稿已分析的金融行业"
            assert analyzer.calls == 2
            assert crm._db.execute("SELECT count(*) FROM crm_customer_facts").fetchone()[0] == 0
        finally:
            release.set()
            profile.close()
            crm.close()
    asyncio.run(run())


@pytest.mark.parametrize("text,expected", [("行业是金融。", "observation"), ("客户说行业是金融。", "reported"),
                                           ("我想到先按金融行业准备方案，尚需客户核实。", None)])
def test_human_discussion_does_not_upgrade_own_thought_or_recycle_ai_reply(tmp_path, text, expected):
    async def run():
        crm = CustomerStore(tmp_path / "fresh-human-discussion-nature.sqlite3")
        now = 1_800_000_000.0
        sales = SalesWorkspace(crm, clock=lambda: now)
        unit = crm.create_customer("owner", {"name": "合成讨论银行"}, now)
        class Advisor:
            async def reply(self, context, history, message, timestamp):
                return {"answer": "AI自己生成：行业是医疗，不是客户原话。", "questions": [], "risks": [], "next_moves": []}
        class Analyzer:
            seen = []
            async def extract(self, source, context):
                self.seen.append(source)
                return [{"key": "industry", "value": source["text"], "basis": "reported", "evidence": source["text"]}]
        analyzer = Analyzer()
        discussion = DiscussionService(crm, sales, asyncio.Lock(), advisor=Advisor(), clock=lambda: now)
        profile = ProfileIntelligence(crm, sales, analyzer=analyzer, clock=lambda: now)
        try:
            thread = discussion.create_thread("owner", {"customer_id": unit["id"], "title": "合成讨论"})["thread"]
            await discussion.send_message("owner", thread["id"], {"request_id": "human-own-thought", "text": text})
            await profile.scan("owner", force=True)
            candidates = profile.list_candidates("owner")["items"]
            assert len(candidates) == (1 if expected else 0)
            if expected:
                assert candidates[0]["basis"] == expected
                assert candidates[0]["evidence"] == text
            assert len(analyzer.seen) == 1 and analyzer.seen[0]["type"] == "discussion_user"
            assert "医疗" not in analyzer.seen[0]["text"]
            assert crm._db.execute("SELECT count(*) FROM crm_customer_facts").fetchone()[0] == 0
        finally:
            profile.close()
            crm.close()
    asyncio.run(run())
