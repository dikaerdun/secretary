"""Gateway behavior tests; no SDK import, credentials or network required."""

import asyncio
import unittest

from secretary.gateway import BotGateway, GatewayError, SafeSDKLogger


class FakeClient:
    def __init__(self):
        self.handlers = {}
        self.is_connected = True
        self.replies = []
        self.sent = []
        self.receipt = {"errcode": 0}
        self.send_error = None
        self.send_started = asyncio.Event()
        self.send_release = None
        self.disconnected = False

    def on(self, name, handler):
        self.handlers[name] = handler

    async def connect(self):
        self.handlers["authenticated"]()

    def disconnect(self):
        self.disconnected = True
        self.is_connected = False

    async def reply_stream(self, frame, stream, text, finish):
        self.replies.append((text, finish))
        return {"errcode": 0}

    async def send_message(self, owner, body):
        self.sent.append((owner, body))
        self.send_started.set()
        if self.send_release is not None:
            await self.send_release.wait()
        if self.send_error:
            raise self.send_error
        return self.receipt


class FakeParser:
    def __init__(self):
        self.calls = []
        self.error = None

    async def parse(self, text, now):
        self.calls.append((text, now))
        if self.error:
            raise self.error
        return {"action": "create", "title": text}


class FakeStore:
    def __init__(self):
        self.results = {}
        self.executed = []
        self.due = []
        self.claims = 0
        self.acks = []
        self.retries = []

    def get_result(self, owner, source_id):
        return self.results.get((owner, source_id))

    def execute(self, owner, source_id, command, now):
        self.executed.append((owner, source_id, command, now))
        result = "已记录，将在指定时间提醒你。"
        self.results[(owner, source_id)] = result
        return result

    def claim_due(self, now):
        self.claims += 1
        return self.due.pop(0) if self.due else None

    def ack(self, reminder_id, token, now):
        self.acks.append((reminder_id, token, now))

    def retry(self, reminder_id, token, now):
        self.retries.append((reminder_id, token, now))


def message(kind="voice", user="owner", chat="single", msgid="m1"):
    return {"headers": {"req_id": "request"}, "body": {
        "msgid": msgid, "chattype": chat, "from": {"userid": user},
        "msgtype": kind, kind: {"content": "明天下午三点提醒我寄合同"},
    }}


def reminder(owner="owner", reminder_id=1):
    return {"id": reminder_id, "owner": owner, "text": "该寄合同了", "token": "lease"}


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = FakeClient()
        self.parser = FakeParser()
        self.store = FakeStore()
        self.gateway = BotGateway(self.client, self.store, self.parser, ["owner"], clock=lambda: 1000)

    async def test_voice_text_is_parsed_saved_and_acknowledged(self):
        await self.gateway.handle_message(message())
        self.assertEqual(self.parser.calls, [("明天下午三点提醒我寄合同", 1000)])
        self.assertEqual(self.store.executed[0][:2], ("owner", "m1"))
        self.assertFalse(self.client.replies[0][1])
        self.assertEqual(self.client.replies[-1], ("已记录，将在指定时间提醒你。", True))

    async def test_non_allowlisted_and_group_messages_have_no_effect(self):
        await self.gateway.handle_message(message(user="outsider"))
        await self.gateway.handle_message(message(chat="group"))
        self.assertEqual(self.parser.calls, [])
        self.assertEqual(self.store.executed, [])
        self.assertEqual(self.client.replies, [])

    async def test_replayed_message_uses_durable_result_without_parsing(self):
        await self.gateway.handle_message(message(kind="text"))
        await self.gateway.handle_message(message(kind="text"))
        self.assertEqual(len(self.parser.calls), 1)
        self.assertEqual(len(self.store.executed), 1)

    async def test_parse_failure_has_no_write_or_success_claim(self):
        self.parser.error = RuntimeError("sensitive provider data")
        with self.assertLogs("secretary.gateway", level="WARNING") as logs:
            await self.gateway.handle_message(message())
        self.assertEqual(self.store.executed, [])
        self.assertIn("还没有保存", self.client.replies[-1][0])
        self.assertNotIn("sensitive", " ".join(logs.output))

    async def test_parser_clarification_is_returned_without_write(self):
        self.parser.error = ValueError("请补充具体提醒时间。")
        await self.gateway.handle_message(message())
        self.assertEqual(self.client.replies[-1], ("请补充具体提醒时间。", True))
        self.assertEqual(self.store.executed, [])

    async def test_disconnected_or_unauthenticated_never_claims(self):
        self.store.due.append(reminder())
        await self.gateway.deliver_due_once()
        self.gateway.on_authenticated()
        self.client.is_connected = False
        await self.gateway.deliver_due_once()
        self.assertEqual(self.store.claims, 0)

    async def test_send_exception_keeps_reminder_for_retry(self):
        self.gateway.on_authenticated()
        self.store.due.append(reminder())
        self.client.send_error = RuntimeError("secret token")
        with self.assertLogs("secretary.gateway", level="WARNING") as logs:
            self.assertFalse(await self.gateway.deliver_due_once())
        self.assertEqual(self.store.acks, [])
        self.assertEqual(self.store.retries, [(1, "lease", 1000)])
        self.assertNotIn("secret token", " ".join(logs.output))

    async def test_nonzero_or_missing_receipt_cannot_ack(self):
        self.gateway.on_authenticated()
        for receipt in ({"errcode": 400}, {}, None):
            self.store.due.append(reminder())
            self.client.receipt = receipt
            self.gateway._last_send.clear()
            with self.assertLogs("secretary.gateway", level="WARNING"):
                await self.gateway.deliver_due_once()
        self.assertEqual(self.store.acks, [])
        self.assertEqual(len(self.store.retries), 3)

    async def test_success_after_reconnection_is_acknowledged(self):
        self.store.due.append(reminder())
        self.gateway.on_authenticated()
        self.gateway.on_disconnected("sensitive reason")
        await self.gateway.deliver_due_once()
        self.gateway.on_authenticated()
        self.assertTrue(await self.gateway.deliver_due_once())
        self.assertEqual(self.store.acks, [(1, "lease", 1000)])
        self.assertEqual(self.client.sent[0][0], "owner")

    async def test_due_reminder_owner_must_still_be_allowed(self):
        self.store.due.append(reminder("outsider"))
        self.gateway.on_authenticated()
        with self.assertLogs("secretary.gateway", level="WARNING"):
            await self.gateway.deliver_due_once()
        self.assertEqual(self.client.sent, [])
        self.assertEqual(len(self.store.retries), 1)

    async def test_command_execute_waits_for_inflight_reminder(self):
        self.gateway.on_authenticated()
        self.store.due.append(reminder())
        self.client.send_release = asyncio.Event()
        sending = asyncio.create_task(self.gateway.deliver_due_once())
        await self.client.send_started.wait()
        handling = asyncio.create_task(self.gateway.handle_message(message()))
        await asyncio.sleep(0)
        self.assertEqual(self.store.executed, [])
        self.client.send_release.set()
        await asyncio.gather(sending, handling)
        self.assertEqual(len(self.store.executed), 1)

    async def test_rate_limit_waits_one_second_between_same_owner_sends(self):
        elapsed = [10.0]
        delays = []

        async def fake_sleep(delay):
            delays.append(delay)
            elapsed[0] += delay

        self.gateway.monotonic = lambda: elapsed[0]
        self.gateway.sleep = fake_sleep
        self.gateway.on_authenticated()
        self.store.due.extend([reminder(), reminder(reminder_id=2)])
        await self.gateway.deliver_due_once()
        await self.gateway.deliver_due_once()
        self.assertEqual(delays, [1.0])

    async def test_cancellation_shuts_down_client_and_owned_tasks(self):
        task = asyncio.create_task(self.gateway.run())
        await self.gateway.ready.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.client.disconnected)
        self.assertTrue(all(task.done() for task in self.gateway._tasks))

    async def test_sdk_error_exits_with_safe_message(self):
        task = asyncio.create_task(self.gateway.run())
        await self.gateway.ready.wait()
        with self.assertLogs("secretary.gateway", level="ERROR") as logs:
            self.gateway.on_error(RuntimeError("sensitive error"))
            with self.assertRaises(GatewayError):
                await task
        self.assertNotIn("sensitive", " ".join(logs.output))

    async def test_normal_close_without_event_exits_instead_of_hanging(self):
        # Exercise run(), including supervision and cleanup, while advancing the
        # injected sleep rapidly rather than waiting a real five-second interval.
        async def fast_sleep(_delay):
            await asyncio.sleep(0)

        self.gateway.sleep = fast_sleep
        task = asyncio.create_task(self.gateway.run())
        await self.gateway.ready.wait()
        # Reproduce SDK 1.0.1: the socket closes but no disconnected event arrives.
        self.client.is_connected = False
        with self.assertLogs("secretary.gateway", level="ERROR") as logs:
            with self.assertRaises(GatewayError):
                await asyncio.wait_for(task, timeout=1)
        self.assertTrue(self.client.disconnected)
        self.assertFalse(self.gateway.ready.is_set())
        self.assertIn("wecom_connection_closed_without_event", " ".join(logs.output))

    async def test_watchdog_allows_initial_connect_and_sdk_reconnection(self):
        async def fast_sleep(_delay):
            await asyncio.sleep(0)

        self.gateway.sleep = fast_sleep
        self.client.is_connected = False
        watchdog = asyncio.create_task(self.gateway._watch_connection())
        try:
            # No first authentication yet: a closed socket is expected.
            for _ in range(3):
                await asyncio.sleep(0)
            self.assertFalse(self.gateway.failed.is_set())
            self.gateway.on_authenticated()
            with self.assertLogs("secretary.gateway", level="WARNING"):
                self.gateway.on_disconnected()
            # A normal SDK disconnect event clears readiness while reconnecting.
            for _ in range(3):
                await asyncio.sleep(0)
            self.assertFalse(self.gateway.failed.is_set())
            self.assertFalse(watchdog.done())
        finally:
            watchdog.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await watchdog

    def test_sdk_logger_never_echoes_payload(self):
        logger = SafeSDKLogger()
        with self.assertLogs("secretary.gateway", level="WARNING") as logs:
            logger.warn("secret", {"message": "private"})
            logger.error("token")
        self.assertNotIn("secret", " ".join(logs.output).replace("secretary", ""))
        self.assertNotIn("private", " ".join(logs.output))
        self.assertNotIn("token", " ".join(logs.output))


if __name__ == "__main__":
    unittest.main()
