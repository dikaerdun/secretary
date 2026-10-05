"""Independent adoption and stale-source regressions for the sales coach."""

import asyncio
import copy
import sqlite3

import pytest

from secretary.coaching_service import CoachingService
from secretary.customer_store import CustomerStore
from tests.test_coaching_service import ADVICE


NOW = 1_800_000_000.0


@pytest.fixture
def crm(tmp_path):
    store = CustomerStore(tmp_path / "coaching-review.sqlite3")
    yield store
    store.close()


class Coach:
    async def advise(self, profile, now):
        return copy.deepcopy(ADVICE)


async def generate(service, customer_id):
    service.schedule("owner", customer_id)
    await asyncio.gather(*list(service.running.values()))
    return service.view("owner", customer_id)["recommendation"]


def test_adoption_failure_is_atomic_and_retry_creates_exactly_one_record(crm):
    async def run():
        customer = crm.create_customer("owner", {"name": "星河医院"}, NOW)
        service = CoachingService(crm, Coach(), asyncio.Lock(), clock=lambda: NOW)
        try:
            advice = await generate(service, customer["id"])
            crm._db.execute("CREATE TRIGGER fail_adoption BEFORE INSERT ON crm_coaching_adoptions "
                            "BEGIN SELECT RAISE(ABORT,'injected write failure'); END")
            with pytest.raises(sqlite3.IntegrityError):
                service.adopt("owner", customer["id"], advice["version"], 1)
            assert crm.list_records("owner")["total"] == 0
            assert service.view("owner", customer["id"])["recommendation"]["stale"] is False
            crm._db.execute("DROP TRIGGER fail_adoption")
            record = service.adopt("owner", customer["id"], advice["version"], 1)
            duplicate = service.adopt("owner", customer["id"], advice["version"], 1)
            assert duplicate["id"] == record["id"]
            assert crm.list_records("owner")["total"] == 1
            assert crm._db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
            assert crm._db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0
        finally:
            await service.close()
    asyncio.run(run())


def test_activity_changes_same_timestamp_stale_advice_and_are_used_by_retry(crm):
    async def run():
        customer = crm.create_customer("owner", {"name": "星河医院"}, NOW)
        note = crm.create_record("owner", {"title": "接口清单", "content": "待发送接口清单", "customer_id": customer["id"]}, NOW)
        calls = []
        class Capture(Coach):
            async def advise(self, profile, now):
                calls.append(profile)
                return await super().advise(profile, now)
        service = CoachingService(crm, Capture(), asyncio.Lock(), clock=lambda: NOW)
        try:
            advice = await generate(service, customer["id"])
            crm.add_activity("owner", note["id"], "已发出接口清单，改为确认测试环境", NOW)
            assert service.view("owner", customer["id"])["recommendation"]["stale"] is True
            with pytest.raises(ValueError, match="变化"):
                service.adopt("owner", customer["id"], advice["version"], 1)
            refreshed = await generate(service, customer["id"])
            assert refreshed["stale"] is False
            assert "确认测试环境" in calls[-1]["brief"]["recent_activities"][0]["content"]
        finally:
            await service.close()
    asyncio.run(run())


def test_manual_change_to_adopted_followup_blocks_remaining_old_moves_without_overwrite(crm):
    async def run():
        customer = crm.create_customer("owner", {"name": "星河医院"}, NOW)
        service = CoachingService(crm, Coach(), asyncio.Lock(), clock=lambda: NOW)
        try:
            advice = await generate(service, customer["id"])
            first = service.adopt("owner", customer["id"], advice["version"], 1)
            crm.update_record("owner", first["id"], {"content": "人工确认：先暂停该项"}, NOW)
            repeat = service.adopt("owner", customer["id"], advice["version"], 1)
            assert repeat["content"] == "人工确认：先暂停该项"
            with pytest.raises(ValueError, match="变化"):
                service.adopt("owner", customer["id"], advice["version"], 2)
            assert crm.list_records("owner")["total"] == 1
        finally:
            await service.close()
    asyncio.run(run())


def test_still_changing_source_not_published_and_provider_can_recover(crm):
    async def run():
        customer = crm.create_customer("owner", {"name": "星河医院"}, NOW)
        service = CoachingService(crm, Coach(), asyncio.Lock(), clock=lambda: NOW)
        try:
            original = await generate(service, customer["id"])
            class Changing(Coach):
                calls = 0
                async def advise(self, profile, now):
                    self.calls += 1
                    crm.update_customer("owner", customer["id"], {"notes": "变化" + str(self.calls)}, NOW)
                    return await super().advise(profile, now)
            changing = Changing()
            service.coach = changing
            after = await generate(service, customer["id"])
            assert changing.calls == 2
            assert after["version"] == original["version"] and after["stale"] is True
            assert service.view("owner", customer["id"])["generating"] is False
            assert "仍在更新" in service.view("owner", customer["id"])["error"]
            service.coach = Coach()
            recovered = await generate(service, customer["id"])
            assert recovered["version"] == original["version"] + 1
            assert recovered["stale"] is False
            assert service.view("owner", customer["id"])["error"] is None
        finally:
            await service.close()
    asyncio.run(run())


def test_close_cancels_inflight_without_publishing_or_leaking_error(crm):
    async def run():
        customer = crm.create_customer("owner", {"name": "星河医院"}, NOW)
        started = asyncio.Event()
        class Slow(Coach):
            async def advise(self, profile, now):
                started.set()
                await asyncio.Event().wait()
                return await super().advise(profile, now)
        service = CoachingService(crm, Slow(), asyncio.Lock(), clock=lambda: NOW)
        service.schedule("owner", customer["id"])
        await asyncio.wait_for(started.wait(), 2)
        await service.close()
        assert service.running == {}
        assert service.view("owner", customer["id"])["recommendation"] is None
        service.schedule("owner", customer["id"])
        assert service.running == {}
    asyncio.run(run())
