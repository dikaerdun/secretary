"""F1: reviewable profile drafts survive unrelated capture metadata writes."""

import asyncio
import hashlib
import json

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer
import pytest

from secretary.customer_service import CustomerService
from secretary.customer_store import CustomerStore
from secretary.store import Store
from secretary.web import create_app, hash_password


NOW = 1_800_000_000.0
TEXT = "拜访复盘：新建客户星河医院，联系人王总，关注密钥管理，希望先微信沟通。"


class ProfileParser:
    async def parse(self, text, now, context=None):
        selected = (context or {}).get("customer")
        return {"intent": "update" if selected else "create", "customer_name": "星河医院",
                "contact_name": "王总", "contact": {"name": "王总"},
                "contact_evidence": {"name": "王总"}, "attributes": [
                    {"target": "account", "key": "crypto_needs", "value": "密钥管理",
                     "basis": "reported", "evidence": "密钥管理"}]}


@pytest.fixture
def stores(tmp_path):
    path = tmp_path / "f1-synthetic.sqlite3"
    crm, store = CustomerStore(path), Store(path)
    yield crm, store, path
    crm.close()
    store.close()


@pytest.mark.parametrize("category", ["auto", "visit_review"])
def test_first_web_confirmation_after_category_save_and_restart(stores, category):
    crm, store, path = stores

    async def scenario():
        lock = asyncio.Lock()
        ticks = iter(range(100))
        clock = lambda: NOW + next(ticks)
        service = CustomerService(crm, ProfileParser(), lock, clock=clock)
        app = create_app(store, crm, lock, "owner", hash_password("f1-synthetic-password"),
                         customer_service=service, clock=clock)
        async with TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True)) as client:
            login = await client.post("/api/login", json={"password": "f1-synthetic-password"})
            csrf = (await login.json())["csrf"]
            headers = {"X-CSRF-Token": csrf}
            reply = await client.post("/api/customer-command", json={"text": TEXT, "category": category}, headers=headers)
            assert reply.status == 200
            result = await reply.json()
            draft, record_id = result["draft"], result["record_id"]
            assert crm.get_record("owner", record_id)["category"] == "visit_review"
            stored = json.loads(crm._db.execute("SELECT payload_json FROM crm_customer_drafts WHERE id=?",
                                (draft["id"],)).fetchone()["payload_json"])
            assert stored["source_snapshot"] == crm.record_snapshot(crm.get_record("owner", record_id))
            assert crm.list_customers("owner")["total"] == 0
            confirm = await client.post(f"/api/customer-drafts/{draft['id']}/confirm", json={}, headers=headers)
            confirmed = await confirm.json()
            assert confirm.status == 200, confirmed
            assert confirmed["draft"]["status"] == "confirmed"
            return draft["id"], confirmed["customer_id"], record_id

    draft_id, customer_id, record_id = asyncio.run(scenario())
    reopened = CustomerStore(path)
    try:
        original = reopened.get_customer_draft("owner", draft_id)
        assert reopened.confirm_customer_draft("owner", draft_id, NOW + 200) == original
        assert reopened.list_customers("owner")["total"] == 1
        profile = reopened.profile("owner", customer_id)
        assert len(profile["contacts"]) == len(profile["history"]) == 1
        assert profile["fields"][0]["source_record_id"] == record_id
        assert reopened.get_record("owner", record_id)["customer_id"] == customer_id
    finally:
        reopened.close()


def make_draft(crm, *, selected=False):
    async def scenario():
        service = CustomerService(crm, ProfileParser(), asyncio.Lock(), clock=lambda: NOW)
        customer = crm.create_customer("owner", {"name": "星河医院"}, NOW) if selected else None
        kwargs = {"selected_customer_id": customer["id"]} if customer else {}
        return await service.handle("owner", "f1-source", TEXT, force=True, **kwargs)

    return asyncio.run(scenario())


@pytest.mark.parametrize("fields", [{"category": "meeting"}, {"status": "done"}, {"kind": "action"}])
def test_unrelated_source_metadata_write_does_not_expire_new_draft(stores, fields):
    crm, _, _ = stores
    result = make_draft(crm)
    crm.update_record("owner", result["record_id"], fields, NOW + 1)
    assert crm.confirm_customer_draft("owner", result["draft"]["id"], NOW + 2)["status"] == "confirmed"


@pytest.mark.parametrize("change", ["content", "original_content", "title", "customer_id", "clear_customer", "target_fact"])
def test_related_source_or_target_conflict_still_blocks(stores, change):
    crm, _, _ = stores
    result = make_draft(crm, selected=change in ("clear_customer", "target_fact"))
    draft, record_id = result["draft"], result["record_id"]
    if change == "target_fact":
        crm.save_fact("owner", draft["customer_id"], {"key": "crypto_needs", "value": "仅需脱敏",
                      "basis": "reported"}, NOW)
    elif change == "original_content":
        with crm._transaction() as db:
            db.execute("UPDATE crm_records SET original_content=? WHERE owner=? AND id=?",
                       ("更正的原话", "owner", record_id))
    else:
        value = (crm.create_customer("owner", {"name": "另一个客户"}, NOW)["id"] if change == "customer_id"
                 else None if change == "clear_customer" else "更正后的内容")
        crm.update_record("owner", record_id, {"customer_id" if change == "clear_customer" else change: value}, NOW)
    with pytest.raises(ValueError, match="过期"):
        crm.confirm_customer_draft("owner", draft["id"], NOW + 1)
    assert crm.get_customer_draft("owner", draft["id"])["status"] == "stale"
    assert crm._db.execute("SELECT COUNT(*) FROM crm_contacts").fetchone()[0] == 0


def test_legacy_source_snapshot_keeps_original_conservative_guard(stores):
    crm, _, _ = stores
    result = make_draft(crm)
    record = crm.get_record("owner", result["record_id"])
    row = crm._db.execute("SELECT payload_json FROM crm_customer_drafts WHERE id=?", (result["draft"]["id"],)).fetchone()
    payload = json.loads(row["payload_json"])
    payload.pop("source_snapshot_version", None)
    # Exact serialization used by releases before F1.
    source = {key: record[key] for key in ("id", "title", "content", "customer_id", "status", "kind", "parent_record_id", "updated_at")}
    payload["source_snapshot"] = hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    with crm._transaction() as db:
        db.execute("UPDATE crm_customer_drafts SET payload_json=? WHERE id=?", (json.dumps(payload), result["draft"]["id"]))
    crm.update_record("owner", record["id"], {"category": "meeting"}, NOW + 1)
    with pytest.raises(ValueError, match="过期"):
        crm.confirm_customer_draft("owner", result["draft"]["id"], NOW + 2)
