"""Persistent discussion exercises on synthetic data only; no provider requests."""
import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from secretary.customer_store import CustomerStore
from secretary.sales_workspace import SalesWorkspace
from secretary.sales_discussion import DiscussionAdvisor, DiscussionService, validate_reply


NOW = 1_800_000_000.0
REPLY = {
    "answer": "先核实测试评价标准，再讨论采购路径。预算目前尚未核实。",
    "next_moves": [
        {"title": "核实试点验收指标", "reason": "试点需要客户认可的评价标准",
         "contact_hint": "王工", "preparation": "准备性能与兼容性指标示例", "success_signal": "得到双方认可的测试指标"},
        {"title": "确认试点需求负责人", "reason": "采购角色尚未确认",
         "contact_hint": "需求负责人（姓名与职责待确认）", "preparation": "整理需求问题清单", "success_signal": "明确需求确认角色"}],
    "questions": ["由谁确认测试范围？"], "risks": ["预算状态待核实"]}


class Advisor:
    def __init__(self):
        self.calls = []
        self.started = asyncio.Event()
        self.release = None
        self.error = False

    async def reply(self, context, history, text, now):
        self.calls.append(copy.deepcopy({"context": context, "history": history, "text": text, "now": now}))
        self.started.set()
        if self.release:
            await self.release.wait()
        if self.error:
            raise RuntimeError("private API KEY and original payload")
        return copy.deepcopy(REPLY)


class DiscussionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "discussion-synthetic.sqlite3"
        self.crm = CustomerStore(self.path)
        self.workspace = SalesWorkspace(self.crm, clock=lambda: NOW)
        self.customer = self.crm.create_customer("owner", {"name": "星河医院", "phone": "400-818-0001"}, NOW)
        self.person = self.crm.create_contact("owner", self.customer["id"], {"name": "王工", "role": "技术", "phone": "13812345678"}, NOW)
        self.project = self.workspace.create_opportunity("owner", self.customer["id"], {"name": "数据库加密试点", "amount_cents": 18000000, "amount_type": "estimate", "approval": "unconfirmed"})
        self.model, self.lock = Advisor(), asyncio.Lock()
        self.service = DiscussionService(self.crm, self.workspace, self.lock, advisor=self.model, clock=lambda: NOW)
        self.thread = self.service.create_thread("owner", {"customer_id": self.customer["id"], "opportunity_id": self.project["id"], "title": "讨论试点推进"})["thread"]

    async def asyncTearDown(self):
        await self.service.close()
        self.crm.close()
        self.folder.cleanup()

    async def send(self, text="这个试点该怎么推进？", request_id="request-1"):
        return await self.service.send_message("owner", self.thread["id"], {"text": text, "request_id": request_id})

    def assistant(self, response):
        return next(item for item in reversed(response["messages"]) if item["role"] == "assistant")

    async def test_reply_persists_history_and_no_action_until_explicit_adoption(self):
        response = await self.send()
        self.assertEqual([m["role"] for m in response["messages"]], ["user", "assistant"])
        self.assertEqual(self.assistant(response)["data"]["answer"], REPLY["answer"])
        self.assertEqual(self.crm.list_records("owner")["total"], 0)
        second = await self.send("接口兼容性还没确认，先处理什么？", "request-2")
        self.assertEqual(len(second["messages"]), 4)
        self.assertEqual(len(self.model.calls[1]["history"]), 2)
        self.assertEqual(self.model.calls[1]["history"][0]["content"], "这个试点该怎么推进？")
        self.assertEqual(self.crm._db.execute("SELECT count(*) FROM tasks").fetchone()[0], 0)
        self.assertEqual(self.crm._db.execute("SELECT count(*) FROM notifications").fetchone()[0], 0)
        restored = DiscussionService(self.crm, self.workspace, self.lock, advisor=self.model, clock=lambda: NOW)
        self.assertEqual(restored.get_thread("owner", self.thread["id"])["messages"], second["messages"])
        self.assertEqual(restored.list_threads("owner")["total"], 1)
        await restored.close()

    async def test_same_request_is_idempotent_and_changed_text_is_rejected(self):
        first = await self.send()
        again = await self.send()
        self.assertEqual(first["messages"], again["messages"])
        self.assertEqual(len(self.model.calls), 1)
        with self.assertRaises(ValueError):
            await self.send("changed", "request-1")

    async def test_user_edits_before_adoption_keep_original_advice_and_replay(self):
        message = self.assistant(await self.send())
        edits = {'title': '先约王工核实测试口径', 'preparation': '只带一页指标表', 'executor_kind': 'self'}
        body = {'request_id': 'edited-move', 'expected_snapshot': message['snapshot'], 'draft': edits}
        record = self.service.adopt('owner', self.thread['id'], message['id'], 1, body)
        self.assertEqual(record['title'], edits['title'])
        self.assertIn('只带一页指标表', record['content'])
        self.assertIn(REPLY['next_moves'][0]['title'], record['original_content'])
        self.assertNotIn('只带一页指标表', record['original_content'])
        self.assertEqual(record['action_terms']['executor_kind'], 'self')
        self.assertIsNone(record['proposal_id'])
        self.assertEqual(self.service.adopt('owner', self.thread['id'], message['id'], 1, body)['id'], record['id'])
        with self.assertRaises(ValueError):
            self.service.adopt('owner', self.thread['id'], message['id'], 1, {**body, 'draft': {**edits, 'title': '改了再重复'}})
        self.assertEqual(self.crm._db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 0)

    async def test_old_draft_cannot_adopt_after_other_material_or_discussion_changed(self):
        message = self.assistant(await self.send())
        body = {'draft': {'title': '核对后标题'}, 'expected_snapshot': message['snapshot']}
        await self.send('项目范围又改了', 'next-user-message')
        with self.assertRaises(ValueError):
            self.service.adopt('owner', self.thread['id'], message['id'], 1, body)
        self.assertEqual(self.crm.list_records('owner')['total'], 0)

    async def test_failed_request_keeps_original_and_can_retry_without_duplicate_user(self):
        self.model.error = True
        response = await self.send("请保留这条重要原话")
        self.assertEqual(len(response["messages"]), 1)
        self.assertEqual(response["messages"][0]["text"], "请保留这条重要原话")
        self.assertEqual(response["messages"][0]["status"], "failed")
        self.assertNotIn("private", json.dumps(response))
        self.model.error = False
        response = await self.send("请保留这条重要原话")
        self.assertEqual([m["role"] for m in response["messages"]], ["user", "assistant"])
        self.assertEqual(len(self.model.calls), 2)

    async def test_missing_provider_saves_failed_turn_without_faking_ai(self):
        self.service.advisor = None
        response = await self.send()
        self.assertFalse(response["configured"])
        self.assertEqual(response["messages"][0]["status"], "failed")
        self.assertEqual(self.model.calls, [])
        self.assertIn("配置", response["messages"][0]["error"])

    async def test_global_lock_is_free_while_model_waits_and_one_thread_serializes(self):
        self.model.release = asyncio.Event()
        task = asyncio.create_task(self.send())
        await self.model.started.wait()
        same = asyncio.create_task(self.send())
        second = asyncio.create_task(self.send("还有什么风险？", "request-2"))
        async with asyncio.timeout(1):
            async with self.lock:
                self.assertTrue(self.service.get_thread("owner", self.thread["id"])["generating"])
        self.assertEqual(len(self.model.calls), 1)
        self.model.release.set()
        await asyncio.gather(task, same, second)
        self.assertEqual(len(self.model.calls), 2)
        self.assertEqual(len(self.model.calls[1]["history"]), 2)

    async def test_owner_scope_in_list_get_send_adopt_and_source(self):
        response = await self.send()
        message_id = self.assistant(response)["id"]
        self.assertEqual(self.service.list_threads("other")["items"], [])
        for call in (lambda: self.service.get_thread("other", self.thread["id"]),
                     lambda: self.service.create_thread("other", {"customer_id": self.customer["id"]}),
                     lambda: self.service.adopt("other", self.thread["id"], message_id, 1, {})):
            with self.assertRaises(KeyError):
                call()
        with self.assertRaises(KeyError):
            await self.service.send_message("other", self.thread["id"], {"text": "hello", "request_id": "x"})

    async def test_source_record_is_explicit_context_without_changing_unassigned_record(self):
        note = self.crm.create_record("owner", {"title": "现场想法", "content": "需要核实密钥轮换接口"}, NOW)
        response = self.service.create_thread("owner", {"customer_id": self.customer["id"], "opportunity_id": self.project["id"], "source_record_id": note["id"]})
        self.thread = response["thread"]
        response = await self.send()
        self.assertIsNone(self.crm.get_record("owner", note["id"])["customer_id"])
        self.assertIn("密钥轮换", self.model.calls[0]["context"]["source_record"]["content"])
        self.assertIn(note["id"], [source["record_id"] for source in self.assistant(response)["sources"]])

    async def test_customer_or_project_mismatch_cannot_create_discussion(self):
        other = self.crm.create_customer("owner", {"name": "异客户"}, NOW)
        note = self.crm.create_record("owner", {"title": "异客户记录", "content": "测试", "customer_id": other["id"]}, NOW)
        for data in ({"customer_id": other["id"], "opportunity_id": self.project["id"]},
                     {"customer_id": self.customer["id"], "source_record_id": note["id"]}):
            with self.assertRaises((KeyError, ValueError)):
                self.service.create_thread("owner", data)
        same_note = self.crm.create_record("owner", {"title": "项目来源", "content": "测试", "customer_id": self.customer["id"]}, NOW)
        self.workspace.link("owner", "record", same_note["id"], self.project["id"])
        second = self.workspace.create_opportunity("owner", self.customer["id"], {"name": "另一个项目"})
        with self.assertRaises(ValueError):
            self.service.create_thread("owner", {"customer_id": self.customer["id"], "opportunity_id": second["id"], "source_record_id": same_note["id"]})

    async def test_adoption_is_idempotent_action_project_link_without_schedule(self):
        response = await self.send()
        message = self.assistant(response)
        record = self.service.adopt("owner", self.thread["id"], message["id"], 1, {})
        again = self.service.adopt("owner", self.thread["id"], message["id"], 1, {})
        second = self.service.adopt("owner", self.thread["id"], message["id"], 2, {})
        self.assertEqual(record["id"], again["id"])
        self.assertNotEqual(record["id"], second["id"])
        self.assertEqual((record["kind"], record["status"], record["customer_id"]), ("action", "following", self.customer["id"]))
        self.assertIsNone(record["proposal_id"])
        self.assertIn("AI讨论建议", record["content"])
        links = self.workspace.workbench("owner", self.customer["id"])["opportunity_links"]
        self.assertEqual({link["opportunity_id"] for link in links}, {self.project["id"]})
        self.assertEqual(self.crm._db.execute("SELECT count(*) FROM tasks").fetchone()[0], 0)
        self.assertEqual(self.crm._db.execute("SELECT count(*) FROM proposals").fetchone()[0], 0)
        self.assertEqual(self.crm._db.execute("SELECT count(*) FROM notifications").fetchone()[0], 0)
        self.assertEqual(self.assistant(self.service.get_thread("owner", self.thread["id"]))["data"]["next_moves"][0]["adopted_record_id"], record["id"])

    async def test_changed_facts_or_new_discussion_marks_old_reply_stale(self):
        response = await self.send()
        message = self.assistant(response)
        self.crm.save_fact("owner", self.customer["id"], {"key": "requirements", "value": "新边界", "basis": "reported"}, NOW)
        self.assertTrue(self.assistant(self.service.get_thread("owner", self.thread["id"]))["stale"])
        with self.assertRaisesRegex(ValueError, "变化"):
            self.service.adopt("owner", self.thread["id"], message["id"], 1, {})
        response = await self.send("新的事实怎么影响下一步？", "request-2")
        newest = self.assistant(response)
        self.assertFalse(newest["stale"])
        self.assertTrue(response["messages"][1]["stale"])

    async def test_changes_during_inference_are_visible_and_not_adoptable(self):
        self.model.release = asyncio.Event()
        task = asyncio.create_task(self.send())
        await self.model.started.wait()
        self.workspace.update_opportunity("owner", self.customer["id"], self.project["id"], {"expected_revision": 1, "amount_cents": 46000000, "amount_type": "budget"})
        self.model.release.set()
        response = await task
        message = self.assistant(response)
        self.assertTrue(message["stale"])
        self.assertIn("变化", message["warning"])
        with self.assertRaises(ValueError):
            self.service.adopt("owner", self.thread["id"], message["id"], 1, {})

    async def test_model_context_redacts_contact_data_and_keeps_fact_basis_amount_provenance(self):
        self.crm.save_fact("owner", self.customer["id"], {"key": "requirements", "value": "数据库加密", "basis": "reported"}, NOW)
        self.crm.save_fact("owner", self.customer["id"], {"key": "budget_notes", "value": "个人推测可能有预算", "basis": "observation"}, NOW)
        note = self.crm.create_record("owner", {"title": "技术记录", "content": "电话400-818-0001或13812345678，邮箱private@example.com", "customer_id": self.customer["id"]}, NOW)
        self.thread = self.service.create_thread("owner", {"customer_id": self.customer["id"], "opportunity_id": self.project["id"], "source_record_id": note["id"]})["thread"]
        await self.send("请联系13812345678和private@example.com问接口，不要丢这句话")
        payload = json.dumps(self.model.calls[0], ensure_ascii=False)
        for value in ("400-818-0001", "13812345678", "private@example.com"):
            self.assertNotIn(value, payload)
        self.assertEqual(self.model.calls[0]["context"]["project"]["amount_type"], "estimate")
        self.assertEqual(self.model.calls[0]["context"]["project"]["approval"], "unconfirmed")
        profile = self.model.calls[0]["context"]["profile"]
        self.assertEqual(profile["reported_facts"][0]["field"], "requirements")
        self.assertEqual(profile["observations"][0]["field"], "budget_notes")
        self.assertIn("13812345678", self.service.get_thread("owner", self.thread["id"])["messages"][0]["text"])

    async def test_archived_project_preserves_history_but_blocks_new_send_and_adoption(self):
        response = await self.send()
        self.workspace.update_opportunity("owner", self.customer["id"], self.project["id"], {"expected_revision": 1, "archived": True})
        self.assertEqual(len(self.service.get_thread("owner", self.thread["id"])["messages"]), 2)
        with self.assertRaises(ValueError):
            await self.send("请继续", "request-2")
        with self.assertRaises(ValueError):
            self.service.adopt("owner", self.thread["id"], self.assistant(response)["id"], 1, {})

    async def test_clarification_only_is_a_valid_discussion_and_cannot_be_adopted(self):
        async def clarify(*args):
            return {"answer": "需要先确认具体系统范围。", "next_moves": [], "questions": ["哪个系统？"], "risks": []}
        self.model.reply = clarify
        response = await self.send()
        self.assertEqual(self.assistant(response)["data"]["next_moves"], [])
        with self.assertRaises(ValueError):
            self.service.adopt("owner", self.thread["id"], self.assistant(response)["id"], 1, {})

    async def test_invalid_executable_reply_fails_without_adoptable_draft(self):
        async def invalid(*args):
            return {**REPLY, "action": "propose", "remind_at": NOW + 3600}
        self.model.reply = invalid
        response = await self.send()
        self.assertEqual(len(response["messages"]), 1)
        self.assertEqual(response["messages"][0]["status"], "failed")

    async def test_cancelled_inference_preserves_retryable_original(self):
        self.model.release = asyncio.Event()
        task = asyncio.create_task(self.send("需要保留的原话"))
        await self.model.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        view = self.service.get_thread("owner", self.thread["id"])
        self.assertFalse(view["generating"])
        self.assertEqual(view["messages"][0]["status"], "failed")
        self.assertEqual(view["messages"][0]["text"], "需要保留的原话")

    async def test_restart_recovers_pending_request_and_actual_database_reopen_keeps_history(self):
        self.model.error = True
        response = await self.send("断电前的想法")
        with self.crm._transaction() as db:
            db.execute("UPDATE crm_sales_discussion_messages SET status='pending',error=NULL WHERE id=?", (response["messages"][0]["id"],))
        await self.service.close()
        self.crm.close()
        self.crm = CustomerStore(self.path)
        self.workspace = SalesWorkspace(self.crm, clock=lambda: NOW)
        self.service = DiscussionService(self.crm, self.workspace, self.lock, advisor=self.model, clock=lambda: NOW)
        recovered = self.service.get_thread("owner", self.thread["id"])
        self.assertEqual(recovered["messages"][0]["status"], "failed")
        self.assertEqual(recovered["messages"][0]["text"], "断电前的想法")
        self.model.error = False
        response = await self.send("断电前的想法")
        self.assertEqual(len(response["messages"]), 2)
        self.assertEqual(response["messages"][0]["id"], recovered["messages"][0]["id"])

    async def test_source_reassignment_during_inference_keeps_reply_but_blocks_adoption(self):
        source = self.crm.create_record("owner", {"title": "接口问题", "content": "需要核实测试环境", "customer_id": self.customer["id"]}, NOW)
        other = self.crm.create_customer("owner", {"name": "另一家公司"}, NOW)
        self.thread = self.service.create_thread("owner", {"customer_id": self.customer["id"], "source_record_id": source["id"]})["thread"]
        self.model.release = asyncio.Event()
        task = asyncio.create_task(self.send())
        await self.model.started.wait()
        self.crm.update_record("owner", source["id"], {"customer_id": other["id"]}, NOW)
        self.model.release.set()
        response = await task
        self.assertTrue(response["context_invalid"])
        self.assertTrue(self.assistant(response)["stale"])
        with self.assertRaises(ValueError):
            self.service.adopt("owner", self.thread["id"], self.assistant(response)["id"], 1, {})

    async def test_context_excludes_other_project_money_and_actions_and_reads_real_outcomes(self):
        chosen = self.crm.create_record("owner", {"title": "提交测试报告", "content": "提交测试报告", "kind": "action", "status": "following", "customer_id": self.customer["id"]}, NOW)
        self.workspace.link("owner", "record", chosen["id"], self.project["id"])
        self.workspace.complete_record("owner", chosen["id"], {"request_id": "result-1", "result": "接口兼容仍有问题", "next_step": "核实驱动版本"})
        other = self.workspace.create_opportunity("owner", self.customer["id"], {"name": "密码机采购", "amount_cents": 99000000, "amount_type": "budget"})
        alien = self.crm.create_record("owner", {"title": "异项目报价", "content": "与试点无关", "kind": "action", "status": "following", "customer_id": self.customer["id"]}, NOW)
        self.workspace.link("owner", "record", alien["id"], other["id"])
        await self.send()
        context = self.model.calls[0]["context"]
        self.assertEqual(context["project"]["amount_cents"], 18000000)
        self.assertNotIn("异项目报价", json.dumps(context, ensure_ascii=False))
        self.assertNotIn("recorded_opportunity_amount_cents", context["profile"]["customer"])
        self.assertEqual(context["outcomes"][0]["result"], "接口兼容仍有问题")
        self.assertIn("核实驱动版本", [action["title"] for action in context["open_actions"]])

    async def test_history_is_bounded_before_provider_and_create_request_is_idempotent(self):
        original = {"customer_id": self.customer["id"], "title": "限长讨论", "request_id": "create-1"}
        first = self.service.create_thread("owner", original)
        repeated = self.service.create_thread("owner", original)
        self.assertEqual(first["thread"]["id"], repeated["thread"]["id"])
        with self.assertRaises(ValueError):
            self.service.create_thread("owner", {**original, "title": "different"})
        # Stored long-term conversation is intact; only the prompt gets a
        # bounded recent excerpt. Simulate prior complete user turns.
        with self.crm._transaction() as db:
            for index in range(30):
                db.execute("INSERT INTO crm_sales_discussion_messages(owner,thread_id,role,text,status,request_id,created_at,updated_at) VALUES (?,?,'user',?,'complete',?,?,?)", ("owner", self.thread["id"], "背景" * 1900, "prior-" + str(index), NOW, NOW))
        response = await self.send()
        history = self.model.calls[0]["history"]
        self.assertLessEqual(len(history), 20)
        self.assertLessEqual(len(json.dumps(history, ensure_ascii=False, separators=(",", ":"))), 24_000)
        self.assertEqual(response["message_total"], 32)

    async def test_base_crm_without_customer_profile_is_supported(self):
        from secretary.crm import CRMStore
        base = CRMStore(Path(self.folder.name) / "base-synthetic.sqlite3")
        try:
            workspace = SalesWorkspace(base, clock=lambda: NOW)
            customer = base.create_customer("owner", {"name": "仅基础客户"}, NOW)
            service = DiscussionService(base, workspace, self.lock, advisor=self.model, clock=lambda: NOW)
            thread = service.create_thread("owner", {"customer_id": customer["id"]})
            response = await service.send_message("owner", thread["thread"]["id"], {"request_id": "base-1", "text": "该如何开始？"})
            self.assertEqual(response["messages"][0]["status"], "complete")
            await service.close()
        finally:
            base.close()

    async def test_stale_project_source_is_excluded_and_link_reconfirmation_changes_context(self):
        note = self.crm.create_record("owner", {"title": "原试点来源", "content": "数据库加密试点需要核实测试性能", "customer_id": self.customer["id"]}, NOW)
        self.workspace.link("owner", "record", note["id"], self.project["id"])
        self.thread = self.service.create_thread("owner", {"customer_id": self.customer["id"], "opportunity_id": self.project["id"], "source_record_id": note["id"]})["thread"]
        first = await self.send()
        old_message = self.assistant(first)
        self.crm.update_record("owner", note["id"], {"content": "更正：讨论的是电子病历签名接口，无关数据库加密。"}, NOW + 1)
        with self.assertRaises(ValueError):
            self.service.adopt("owner", self.thread["id"], old_message["id"], 1, {})
        await self.send("先核对当前项目的来源", "request-2")
        context = self.model.calls[-1]["context"]
        self.assertIsNone(context["source_record"])
        self.assertTrue(context["needs_reconfirmation"])
        self.assertNotIn("更正：讨论的是电子病历签名接口", json.dumps(context, ensure_ascii=False))
        self.assertNotIn(note["id"], [source["record_id"] for source in self.assistant(self.service.get_thread("owner", self.thread["id"]))["sources"]])
        # An explicit new confirmation restores the corrected source as project
        # evidence, and invalidates the prior recommendation's snapshot.
        self.workspace.link("owner", "record", note["id"], self.project["id"])
        self.assertTrue(self.assistant(self.service.get_thread("owner", self.thread["id"]))["stale"])
        await self.send("已重新核对项目归属，怎么继续？", "request-3")
        self.assertEqual(self.model.calls[-1]["context"]["needs_reconfirmation"], [])
        self.assertIn("签名接口", self.model.calls[-1]["context"]["source_record"]["content"])

    async def test_explicit_source_outside_latest_eighty_project_links_is_validated_separately(self):
        oldest = self.crm.create_record("owner", {"title": "更早的显式来源", "content": "原试点问题", "customer_id": self.customer["id"]}, NOW - 100)
        self.workspace.link("owner", "record", oldest["id"], self.project["id"])
        self.thread = self.service.create_thread("owner", {"customer_id": self.customer["id"], "opportunity_id": self.project["id"], "source_record_id": oldest["id"]})["thread"]
        for index in range(81):
            newer = self.crm.create_record("owner", {"title": f"近期来源{index}", "content": "其他已经核对的试点背景", "customer_id": self.customer["id"]}, NOW + index)
            self.workspace.link("owner", "record", newer["id"], self.project["id"])
        # Keep the corrected explicit source older than the bounded catalogue.
        self.crm.update_record("owner", oldest["id"], {"content": "更正：旧录音其实讨论的是另一个签名项目。"}, NOW - 50)
        await self.send()
        context = self.model.calls[-1]["context"]
        self.assertIsNone(context["source_record"])
        self.assertNotIn("旧录音其实讨论的是另一个签名项目", json.dumps(context, ensure_ascii=False))
        view = self.service.get_thread("owner", self.thread["id"])
        self.assertEqual(view["source_warnings"][0]["record_id"], oldest["id"])
        self.assertNotIn(oldest["id"], [source["record_id"] for source in self.assistant(view)["sources"]])
        self.workspace.link("owner", "record", oldest["id"], self.project["id"])
        self.assertTrue(self.assistant(self.service.get_thread("owner", self.thread["id"]))["stale"])
        await self.send("已重新核对更早的来源", "request-2")
        self.assertIn("旧录音其实讨论", self.model.calls[-1]["context"]["source_record"]["content"])

    async def test_explicit_source_moved_to_another_customer_is_excluded_and_warning_survives_invalid_context(self):
        note = self.crm.create_record("owner", {"title": "带入的来源", "content": "原项目测试背景", "customer_id": self.customer["id"]}, NOW)
        self.workspace.link("owner", "record", note["id"], self.project["id"])
        self.thread = self.service.create_thread("owner", {"customer_id": self.customer["id"], "opportunity_id": self.project["id"], "source_record_id": note["id"]})["thread"]
        response = await self.send()
        other = self.crm.create_customer("owner", {"name": "另一个客户"}, NOW)
        self.crm.update_record("owner", note["id"], {"customer_id": other["id"], "content": "这个内容是另一个客户的采购讨论。"}, NOW + 1)
        with self.crm._lock:
            thread = self.service._require_thread(self.crm._db, "owner", self.thread["id"])
            safe, _, sources, _ = self.service._context_snapshot("owner", thread)
        self.assertIsNone(safe["source_record"])
        self.assertNotIn("另一个客户的采购讨论", json.dumps(safe, ensure_ascii=False))
        self.assertNotIn(note["id"], [source["record_id"] for source in sources])
        view = self.service.get_thread("owner", self.thread["id"])
        self.assertTrue(view["context_invalid"])
        self.assertEqual(view["source_warnings"][0]["record_id"], note["id"])
        self.assertTrue(self.assistant(view)["stale"])
        with self.assertRaises(ValueError):
            self.service.adopt("owner", self.thread["id"], self.assistant(response)["id"], 1, {})


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_uses_bounded_json_schema_context_and_conversation(self):
        requests = []
        def handle(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(REPLY, ensure_ascii=False)}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            advisor = DiscussionAdvisor("synthetic-key", base_url="https://provider.invalid/v1", model="test-model", client=client)
            reply = await advisor.reply({"profile": {"contacts": [{"name": "王工"}]}}, [{"role": "user", "content": "曾讨论测试范围"}], "如何继续？", NOW)
        self.assertEqual(reply, REPLY)
        self.assertEqual(requests[0]["model"], "test-model")
        self.assertEqual(requests[0]["response_format"], {"type": "json_object"})
        self.assertIn("曾讨论测试范围", requests[0]["messages"][1]["content"])
        self.assertIn("不能", requests[0]["messages"][0]["content"])

    async def test_provider_rejects_truncated_or_duplicate_json_and_hides_errors(self):
        for content, finish in ((json.dumps(REPLY), "length"), ('{"answer":"a","answer":"b"}', "stop")):
            def handle(request):
                return httpx.Response(200, json={"choices": [{"finish_reason": finish, "message": {"content": content}}]})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                advisor = DiscussionAdvisor("synthetic-key", client=client)
                with self.assertRaises(ValueError) as error:
                    await advisor.reply({}, [], "测试", NOW)
            self.assertNotIn("synthetic-key", str(error.exception))

    def test_validation_rejects_execution_fields_and_non_advice_payloads(self):
        for bad in ({**REPLY, "status": "confirmed"}, {**REPLY, "answer": "我已经替你联系客户"},
                    {**REPLY, "next_moves": [{**REPLY["next_moves"][0], "task_id": 123}]},
                    {**REPLY, "next_moves": [REPLY["next_moves"][0]] * 4}):
            with self.assertRaises(ValueError):
                validate_reply(bad)
