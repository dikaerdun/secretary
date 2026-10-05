"""Integration regressions for long model waits and mutable CRM records."""

import asyncio

import pytest

from secretary.crm import analysis_fingerprint
from secretary.customer_service import CustomerService
from secretary.customer_store import CustomerStore
from secretary.gateway import BotGateway
from secretary.store import Store
from tests.test_gateway import FakeClient, FakeParser, message


NOW = 1_800_000_000.0


@pytest.fixture
def stores(tmp_path):
    path = tmp_path / "races.sqlite3"
    crm, store = CustomerStore(path), Store(path)
    yield crm, store
    crm.close()
    store.close()


def accounts(crm):
    return (crm.create_customer("owner", {"name": "星河医院"}, NOW),
            crm.create_customer("owner", {"name": "另一家公司"}, NOW))


def analysis():
    return {"summary": "准备下一次技术交流", "key_points": [], "open_questions": [], "actions": [
        {"title": "准备技术材料", "kind": "suggestion", "reason": "待用户核实", "owner_hint": "待确认", "remind_at": None}]}


class NoteParser:
    async def parse(self, text, now):
        return {"intent": "note", "customer_name": "星河医院", "contact_name": None}


def test_user_reassignment_during_note_parsing_is_not_overwritten(stores):
    crm, _ = stores
    _, manual_customer = accounts(crm)

    async def scenario():
        started, resume = asyncio.Event(), asyncio.Event()

        class DelayedParser(NoteParser):
            async def parse(self, text, now):
                started.set()
                await resume.wait()
                return await super().parse(text, now)

        service = CustomerService(crm, DelayedParser(), asyncio.Lock(), clock=lambda: NOW)
        pending = asyncio.create_task(service.handle("owner", "note-race", "记录客户星河医院交流，准备技术材料"))
        await asyncio.wait_for(started.wait(), 2)
        record = crm.list_records("owner")["items"][0]
        crm.update_record("owner", record["id"], {"customer_id": manual_customer["id"]}, NOW)
        resume.set()
        result = await asyncio.wait_for(pending, 2)
        assert "已保留你的修改" in result["message"]
        assert crm.get_record("owner", record["id"])["customer_id"] == manual_customer["id"]
        assert crm.get_analysis("owner", record["id"]) is None

    asyncio.run(scenario())


def test_user_content_correction_during_note_parsing_is_not_organized_from_old_text(stores):
    crm, _ = stores
    accounts(crm)

    async def scenario():
        started, resume = asyncio.Event(), asyncio.Event()

        class DelayedParser(NoteParser):
            async def parse(self, text, now):
                started.set()
                await resume.wait()
                return await super().parse(text, now)

        class MustNotOrganize:
            async def organize(self, *_args):
                raise AssertionError("Corrected raw note must wait for manual reorganization")

        service = CustomerService(crm, DelayedParser(), asyncio.Lock(), clock=lambda: NOW, organizer=MustNotOrganize())
        pending = asyncio.create_task(service.handle("owner", "content-race", "记录客户星河医院交流，准备技术材料"))
        await asyncio.wait_for(started.wait(), 2)
        record = crm.list_records("owner")["items"][0]
        crm.update_record("owner", record["id"], {"content": "更正：对方暂不需要材料"}, NOW)
        resume.set()
        result = await asyncio.wait_for(pending, 2)
        assert "已保留你的修改" in result["message"]
        assert crm.get_record("owner", record["id"])["content"] == "更正：对方暂不需要材料"
        assert crm.get_analysis("owner", record["id"]) is None

    asyncio.run(scenario())


def test_user_reassignment_during_organizing_rejects_old_customer_context(stores):
    crm, store = stores
    _, manual_customer = accounts(crm)

    async def scenario():
        started, resume = asyncio.Event(), asyncio.Event()

        class DelayedOrganizer:
            async def organize(self, text, now, context):
                assert context["customer"]["name"] == "星河医院"
                started.set()
                await resume.wait()
                return analysis()

        service = CustomerService(crm, NoteParser(), asyncio.Lock(), clock=lambda: NOW, organizer=DelayedOrganizer())
        pending = asyncio.create_task(service.handle("owner", "analysis-race", "记录客户星河医院交流，准备技术材料"))
        await asyncio.wait_for(started.wait(), 2)
        record = crm.list_records("owner")["items"][0]
        crm.update_record("owner", record["id"], {"customer_id": manual_customer["id"]}, NOW)
        resume.set()
        result = await asyncio.wait_for(pending, 2)
        assert "自动整理暂未完成" in result["message"]
        assert crm.get_record("owner", record["id"])["customer_id"] == manual_customer["id"]
        assert crm.get_analysis("owner", record["id"]) is None
        assert store._db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert store._db.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0

    asyncio.run(scenario())


def test_old_analysis_is_stale_and_cannot_be_adopted_after_customer_reassignment(stores):
    crm, _ = stores
    original, manual_customer = accounts(crm)
    record = crm.create_record("owner", {"title": "现场讨论", "content": "准备技术材料", "customer_id": original["id"]}, NOW)
    old_fingerprint = analysis_fingerprint(record)
    crm.save_analysis("owner", record["id"], {**analysis(), "input_fingerprint": old_fingerprint}, NOW)
    crm.update_record("owner", record["id"], {"customer_id": manual_customer["id"]}, NOW)
    assert crm.get_analysis("owner", record["id"])["stale"] is True
    with pytest.raises(ValueError, match="过期"):
        crm.adopt_action("owner", record["id"], 1, NOW)
    with pytest.raises(ValueError, match="修改"):
        crm.save_analysis("owner", record["id"], {**analysis(), "input_fingerprint": old_fingerprint}, NOW)
    assert crm.list_records("owner")["total"] == 1


def test_gateway_whitespace_does_not_change_source_evidence(stores):
    crm, store = stores

    async def scenario():
        class NewCustomerParser:
            async def parse(self, text, now):
                return {"intent": "create", "customer_name": "星河医院", "contact_name": None,
                        "basic": {}, "contact": {}, "attributes": []}

        lock = asyncio.Lock()
        service = CustomerService(crm, NewCustomerParser(), lock, clock=lambda: NOW)
        client, task_parser = FakeClient(), FakeParser()
        gateway = BotGateway(client, store, task_parser, ["owner"], crm=crm, customer_service=service, clock=lambda: NOW)
        gateway.lock = lock
        frame = message()
        frame["body"]["voice"]["content"] = "  新建客户星河医院 \n"
        await gateway.handle_message(frame)
        draft = crm.list_customer_drafts("owner")["items"][0]
        assert draft["source_text"] == frame["body"]["voice"]["content"]
        assert draft["status"] == "pending"
        assert "客户资料待确认" in client.replies[-1][0]
        assert task_parser.calls == []
        assert crm.list_customers("owner")["total"] == 0

    asyncio.run(scenario())
