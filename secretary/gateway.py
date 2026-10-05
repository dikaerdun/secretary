"""Private, allowlisted WeCom gateway using the official asynchronous SDK."""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections.abc import Callable
from typing import Any


LOG = logging.getLogger("secretary.gateway")


class GatewayError(RuntimeError):
    """Safe error text suitable for an operator log."""


class SafeSDKLogger:
    """Never forward SDK messages: some contain complete user message bodies."""

    def debug(self, *_: Any) -> None:
        pass

    def info(self, *_: Any) -> None:
        pass

    def warn(self, *_: Any) -> None:
        LOG.warning("wecom_sdk_warning")

    def error(self, *_: Any) -> None:
        LOG.error("wecom_sdk_error")


def _check_receipt(receipt: Any) -> None:
    # The SDK already raises for a nonzero errcode. Check defensively so an
    # incompatible SDK or malformed acknowledgement can never mark a reminder sent.
    if not isinstance(receipt, dict) or receipt.get("errcode") != 0:
        raise GatewayError("企业微信未确认消息送达。")


class BotGateway:
    """Small injectable adapter; store methods are synchronous SQLite operations."""

    def __init__(
        self,
        client: Any,
        store: Any,
        parser: Any,
        allowed_user_ids: list[str],
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[..., Any] = asyncio.sleep,
        crm: Any = None,
        customer_service: Any = None,
        materials: Any = None,
        visits: Any = None,
    ) -> None:
        self.client = client
        self.store = store
        self.parser = parser
        self.allowed = frozenset(allowed_user_ids)
        if not self.allowed or any(not isinstance(x, str) or not x for x in self.allowed):
            raise GatewayError("请先配置允许使用秘书的企业微信成员 ID。")
        self.clock = clock
        self.monotonic = monotonic
        self.sleep = sleep
        self.crm = crm
        self.customer_service = customer_service
        self.materials = materials
        self.ready = asyncio.Event()
        self.failed = asyncio.Event()
        self.lock = asyncio.Lock()
        self.visits = visits or getattr(materials, 'visit_service', None)
        if self.visits is None and materials is not None and crm is not None:
            from .visits import VisitService
            self.visits = VisitService(crm, materials, self.lock)
        self._inflight: set[tuple[str, str]] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._last_send: dict[str, float] = {}
        self._closing = False

        client.on("authenticated", self.on_authenticated)
        client.on("disconnected", self.on_disconnected)
        client.on("reconnecting", self.on_disconnected)
        client.on("error", self.on_error)
        client.on("message.text", self._on_message)
        client.on("message.voice", self._on_message)

    def on_authenticated(self, *_: Any) -> None:
        if not self._closing:
            self.ready.set()
            LOG.info("wecom_authenticated")

    def on_disconnected(self, *_: Any) -> None:
        self.ready.clear()
        LOG.warning("wecom_disconnected")

    def on_error(self, *_: Any) -> None:
        # The SDK may include secrets or original text in exception messages.
        # Fail safely; the service manager can restart the process. In particular,
        # authentication errors otherwise leave an open but unusable connection.
        self.ready.clear()
        LOG.error("wecom_connection_error")
        self.failed.set()

    def _start(self, coroutine: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            LOG.error("gateway_background_task_failed")
            self.failed.set()

    def _on_message(self, frame: Any) -> None:
        if not self._closing:
            self._start(self.handle_message(frame))

    def _connected(self) -> bool:
        return self.ready.is_set() and bool(self.client.is_connected)

    async def _reply(self, frame: dict[str, Any], stream: str, text: str, finish: bool) -> None:
        receipt = await asyncio.wait_for(
            self.client.reply_stream(frame, stream, text, finish), timeout=10
        )
        _check_receipt(receipt)

    async def handle_message(self, frame: Any) -> None:
        """Accept only explicitly allowed single-chat senders and valid envelopes."""
        if not isinstance(frame, dict):
            return
        body = frame.get("body")
        if not isinstance(body, dict) or body.get("chattype") != "single":
            return
        sender = body.get("from")
        owner = sender.get("userid") if isinstance(sender, dict) else None
        if not isinstance(owner, str) or owner not in self.allowed:
            return
        message_type = body.get("msgtype")
        if message_type not in ("text", "voice"):
            return
        payload = body.get(message_type)
        text = payload.get("content") if isinstance(payload, dict) else None
        source_id = body.get("msgid")
        headers = frame.get("headers")
        if (
            not isinstance(source_id, str) or not source_id
            or not isinstance(headers, dict) or not headers.get("req_id")
            or not isinstance(text, str) or not text.strip()
        ):
            return
        key = (owner, source_id)
        if key in self._inflight:
            return
        self._inflight.add(key)
        stream = uuid.uuid4().hex
        try:
            if self.crm is not None:
                try:
                    async with self.lock:
                        captured = self.crm.capture_message(owner, source_id, text, message_type, self.clock())
                    if captured['original_content'] != text:
                        await self._reply(frame, stream, '这条消息的内容与已保存原文不同，本次未执行。请核对后台原文；新增内容请作为一条新消息发送。', True)
                        return
                except Exception:
                    LOG.error("message_inbox_save_failed")
                    await self._reply(frame, stream, "这条记录未能保存，请稍后重发。", True)
                    return
            if self.visits is not None:
                visit_reply = await self._visit_command(owner, source_id, text)
                if visit_reply is not None:
                    await self._reply(frame, stream, visit_reply, True)
                    return
            material_match = re.fullmatch(r'\s*导入聆记\s*[:：]\s*(.*?)\s*', text, re.DOTALL)
            if material_match and self.materials is not None:
                try:
                    async with self.lock:
                        material = self.materials.enqueue(owner, {'provider': 'listen_note', 'title': material_match[1],
                            'category': 'auto'}, source_id='wecom:' + source_id)
                        if self.crm is not None:
                            self.crm.apply_command(owner, source_id, {'action': 'help'}, '', self.clock())
                    if material['status'] in ('queued', 'reading', 'organizing'):
                        result = (f"聆记材料 #{material['id']} 已加入整理队列。\n"
                                  "整理完成会通知你；请在后台‘录音与材料’核对客户、日期和待办。确认前不会启用提醒。")
                    else:
                        result = (f"聆记材料 #{material['id']} 已在收件箱。\n"
                                  "请打开后台‘录音与材料’核对；若聆记转写已更新，可在那里重新读取。重复发送不会新建待办或提醒。")
                except ValueError as error:
                    result = str(error) or '请复制聆记中的完整标题，再发送“导入聆记：完整标题”。'
                except Exception:
                    LOG.warning('material_enqueue_failed')
                    result = '这次导入未能保存，请在后台录音与材料入口重试。'
                await self._reply(frame, stream, result, True)
                return
            try:
                initial = ('已收到语音转写：' + text[:700] + ('…' if len(text) > 700 else '') + '\n正在理解；转写有误可在后台原始记录中纠正。'
                           if message_type == 'voice' else '正在整理这件事……')
                await self._reply(frame, stream, initial, False)
            except Exception:
                LOG.warning("message_initial_reply_failed")
                return

            async with self.lock:
                result = self.store.get_result(owner, source_id)
                if result is not None and self.crm is not None:
                    self.crm.recover_message(owner, source_id, result, self.clock())
            if result is None and self.customer_service is not None:
                try:
                    customer_result = await self.customer_service.handle(owner, source_id, text, source=message_type)
                    if customer_result is not None:
                        result = customer_result['message']
                except ValueError as error:
                    result = (str(error) or '请补充客户公司全名和要记录的内容。') + '\n原话已保存在后台待整理。'
                except Exception:
                    LOG.warning('customer_voice_failed')
                    result = '客户资料整理暂未完成，原话已保存在后台待整理，可稍后重试。'
            if result is None:
                try:
                    command = await asyncio.wait_for(
                        self.parser.parse(text, self.clock()), timeout=60
                    )
                except ValueError as error:
                    # Parser's public ValueError contract contains fixed, safe
                    # Chinese clarification text; no provider exception is echoed.
                    result = str(error) or "我还没弄清这件事，请补充具体事项和提醒时间。"
                    if self.crm is not None:
                        result += "\n原话已保存在后台待整理箱，可以回来继续整理。"
                except Exception:
                    LOG.warning("message_parse_failed")
                    result = ("原话已保存在后台待整理箱，自动整理暂时失败，可回来继续整理。"
                              if self.crm is not None else "这次没能整理成功，事情还没有保存，请稍后重发。")
                else:
                    try:
                        async with self.lock:
                            # The store remains the durable idempotency boundary.
                            result = self.store.execute(owner, source_id, command, self.clock())
                            if self.crm is not None:
                                self.crm.apply_command(owner, source_id, command, result, self.clock())
                    except Exception:
                        LOG.error("message_store_failed")
                        result = "这次操作未能确认成功，请发送“查看待办”核对后再试。"
            try:
                await self._reply(frame, stream, result, True)
            except Exception:
                # A saved task stays saved. Replay of the same msgid returns the
                # store's original acknowledgement without executing it again.
                LOG.warning("message_final_reply_failed")
        finally:
            self._inflight.discard(key)

    async def _visit_command(self, owner, source_id, text):
        """Only explicit user commands select an exchange; source text stays data."""
        create = re.fullmatch(r'\s*(?:新建交流|新建拜访)\s*[:：]\s*(.+?)\s*', text, re.S)
        recap = re.fullmatch(r'\s*(?:口述复盘|记录复盘)\s*[:：]\s*(.+?)\s*', text, re.S)
        append = re.fullmatch(r'\s*(复盘|补充)交流\s*[JjＪ]?(\d+)\s*[:：]\s*(.+?)\s*', text, re.S)
        recording = re.fullmatch(r'\s*(?:给)?交流\s*[JjＪ]?(\d+)\s*导入聆记\s*[:：]\s*(.+?)\s*', text, re.S)
        show = re.fullmatch(r'\s*查看交流\s*[JjＪ]?(\d+)\s*', text)
        listing = text.strip() in ('我的交流', '查看客户交流')
        if not any((create, recap, append, recording, show, listing)):
            return None
        try:
            async with self.lock:
                if create or recap:
                    content = (create or recap)[1]
                    title = content if create else '口述复盘：' + content[:45]
                    visit = self.visits.create(owner, {'title': title}, source_id='wecom-visit-create:' + source_id)
                    if recap:
                        self.visits.add_material(owner, visit['id'], {'role': 'recap', 'provider': 'manual',
                            'title': '我的口述复盘', 'text': content}, source_id='wecom-visit-material:' + source_id)
                    result = (f"客户交流 J{visit['id']} 已保存。" + ('复盘已进入整理队列。' if recap else '')
                        + f"\n可继续说‘复盘交流 J{visit['id']}：内容’，或发送‘交流 J{visit['id']} 导入聆记：完整标题’。"
                        + '\n请在后台客户交流中集中核对；客户和日期未明确时不会猜测，确认前不启用提醒。')
                elif append or recording:
                    if append:
                        identifier, role, content = int(append[2]), 'recap' if append[1] == '复盘' else 'supplement', append[3]
                        data = {'role': role, 'provider': 'manual', 'title': '我的口述复盘' if role == 'recap' else '后续补充', 'text': content}
                    else:
                        identifier = int(recording[1])
                        data = {'role': 'recording', 'provider': 'listen_note', 'title': recording[2]}
                    saved = self.visits.add_material(owner, identifier, data, source_id='wecom-visit-material:' + source_id)
                    result = (f"已归入交流 J{saved['visit']['id']}：{saved['material']['title']}。"
                        + '\n已保留独立来源，处理状态请在后台核对；重复发送不会重复安排提醒。')
                elif show:
                    detail = self.visits.detail(owner, int(show[1]))
                    result = f"交流 J{detail['visit']['id']}：{detail['visit']['title']}\n来源：{len(detail['sources'])} 份"
                    result += '\n' + '\n'.join(item['title'] + ('（需核对）' if item['needs_review'] else '（待采纳）') for item in detail['actions'][:8])
                    result += '\n请在后台客户交流中查看原话和采纳状态。'
                else:
                    data = self.visits.list(owner, limit=8)
                    result = '客户交流：\n' + ('\n'.join(f"J{item['id']}：{item['title']}" for item in data['items']) or '暂无交流，可以说“口述复盘：内容”。')
                if self.crm is not None:
                    self.crm.apply_command(owner, source_id, {'action': 'help'}, '', self.clock())
                return result
        except (ValueError, KeyError) as error:
            return '未找到你的交流，请核对交流编号。' if isinstance(error, KeyError) else str(error)
        except Exception:
            LOG.warning('visit_command_failed')
            return '本次交流操作尚未完成，请在后台核对已保存内容后再试。'

    async def deliver_due_once(self) -> bool:
        """Send one claimed reminder; never claim while disconnected."""
        if not self._connected():
            return False
        async with self.lock:
            if not self._connected():
                return False
            item = self.store.claim_due(self.clock())
            if item is None:
                return False
            reminder_id, token = item["id"], item["token"]
            owner = item["owner"]
            if owner not in self.allowed:
                LOG.warning("reminder_owner_not_allowed")
                self.store.retry(reminder_id, token, self.clock())
                return False
            try:
                delay = 1.0 - (self.monotonic() - self._last_send.get(owner, float("-inf")))
                if delay > 0:
                    await self.sleep(delay)
                if not self._connected():
                    self.store.retry(reminder_id, token, self.clock())
                    return False
                self._last_send[owner] = self.monotonic()
                receipt = await asyncio.wait_for(
                    self.client.send_message(owner, {
                        "msgtype": "markdown",
                        "markdown": {"content": item["text"]},
                    }), timeout=10,
                )
                _check_receipt(receipt)
            except asyncio.CancelledError:
                self.store.retry(reminder_id, token, self.clock())
                raise
            except Exception:
                LOG.warning("reminder_send_failed")
                self.store.retry(reminder_id, token, self.clock())
                return False
            self.store.ack(reminder_id, token, self.clock())
            LOG.info("reminder_sent")
            return True

    async def _reminder_loop(self) -> None:
        while True:
            # Bound each pass so a growing queue cannot monopolize the service.
            for _ in range(20):
                if not await self.deliver_due_once():
                    break
            await self.sleep(5)

    async def _material_loop(self) -> None:
        while True:
            progressed = await self.materials.process_one()
            if not progressed:
                await self.sleep(2)

    async def _material_notice_loop(self) -> None:
        while True:
            if self._connected():
                async with self.lock:
                    notice = self.materials.claim_notice(self.allowed, self.clock())
                if notice:
                    try:
                        if not self._connected():
                            async with self.lock:
                                self.materials.retry_notice(notice['id'], notice['token'], self.clock())
                            await self.sleep(5)
                            continue
                        # Titles can contain Markdown; keep the notification plain.
                        title = re.sub(r'[\r\n`*_<>]', ' ', notice['title'])[:100]
                        status = '已整理，待你核对' if notice['status'] == 'review' else '整理未完成，可在后台重试'
                        content = f"录音材料 #{notice['id']} {status}\n{title}\n请打开后台‘录音与材料’。未确认的提醒不会生效。"
                        receipt = await asyncio.wait_for(self.client.send_message(notice['owner'],
                            {'msgtype': 'markdown', 'markdown': {'content': content}}), timeout=10)
                        _check_receipt(receipt)
                    except asyncio.CancelledError:
                        async with self.lock:
                            self.materials.retry_notice(notice['id'], notice['token'], self.clock())
                        raise
                    except Exception:
                        LOG.warning('material_notice_failed')
                        async with self.lock:
                            self.materials.retry_notice(notice['id'], notice['token'], self.clock())
                    else:
                        async with self.lock:
                            self.materials.ack_notice(notice['id'], notice['token'], self.clock())
            await self.sleep(5)

    async def _watch_connection(self) -> None:
        while True:
            await self.sleep(5)
            # SDK 1.0.1 can end its receive loop on a normal WebSocket close
            # without emitting disconnected or scheduling a reconnect. Detect
            # that stale authenticated state independently of the reminder loop.
            # Before first authentication, or after a real disconnected event,
            # ready is clear and the SDK remains responsible for connecting.
            if self.ready.is_set() and not self.client.is_connected:
                self.ready.clear()
                LOG.error("wecom_connection_closed_without_event")
                self.failed.set()
                return

    async def run(self) -> None:
        """Run until cancelled or an observable fatal background failure occurs."""
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()

        def sdk_task_error(_loop: Any, _context: Any) -> None:
            # Official SDK creates internal tasks. Do not let its default asyncio
            # traceback logger expose original payloads, tokens or server replies.
            LOG.error("wecom_unhandled_background_error")
            self.failed.set()

        loop.set_exception_handler(sdk_task_error)
        try:
            self._start(self.client.connect())
            self._start(self._reminder_loop())
            self._start(self._watch_connection())
            if self.materials is not None:
                self._start(self._material_loop())
                self._start(self._material_notice_loop())
            await self.failed.wait()
            raise GatewayError("企业微信连接异常，服务将退出以便自动重启。")
        finally:
            self._closing = True
            self.ready.clear()
            tasks = list(self._tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                self.client.disconnect()
                # The SDK's disconnect is sync and schedules its async cleanup.
                await asyncio.sleep(0)
            except Exception:
                LOG.warning("wecom_disconnect_failed")
            loop.set_exception_handler(previous_handler)


async def run_bot(store: Any, parser: Any, config: dict[str, Any]) -> None:
    """Public application entry point. No actual connection occurs on import."""
    from aibot import WSClient, WSClientOptions

    client = WSClient(WSClientOptions(
        bot_id=config["bot_id"],
        secret=config["bot_secret"],
        max_reconnect_attempts=-1,
        logger=SafeSDKLogger(),
    ))
    from .customer_store import CustomerStore
    from .customer_parser import CustomerVoiceParser
    from .customer_service import CustomerService
    from .organizer import InteractionOrganizer
    from .sales_coach import SalesCoach
    from .coaching_service import CoachingService

    crm = CustomerStore(config['db_path'])
    runner = None
    coaching = None
    materials = None
    try:
        for owner in config['allowed_user_ids']:
            crm.import_legacy(owner, time.time())
        gateway = BotGateway(client, store, parser, config["allowed_user_ids"], crm=crm)
        organizer = InteractionOrganizer(config['api_key'], config['model'], config['base_url'])
        coaching = CoachingService(crm, SalesCoach(config['api_key'], config['model'], config['base_url']), gateway.lock)
        gateway.customer_service = CustomerService(crm,
            CustomerVoiceParser(config['api_key'], config['model'], config['base_url']), gateway.lock,
            organizer=organizer, coach=coaching)
        from .listen_note import ListenNoteClient
        from .materials import MaterialService
        materials = MaterialService(crm, gateway.lock,
            connector=ListenNoteClient(config.get('listen_note_api_key', ''), config.get('listen_note_url', 'https://collab.ctcdn.cn/mcp')),
            organizer=organizer, customer_parser=gateway.customer_service.parser, coaching=coaching)
        gateway.materials = materials
        from .visits import VisitService
        gateway.visits = VisitService(crm, materials, gateway.lock)
        if config.get('web_enabled'):
            from aiohttp import web
            from .web import create_app
            from .organizer import InteractionOrganizer
            from .audio import AudioService
            from .profile_intelligence import ProfileAnalyzer
            from .public_research import make_public_researcher

            app = create_app(store, crm, gateway.lock, config['allowed_user_ids'][0],
                             config['web_password_hash'], connected=gateway._connected,
                             secure_cookie=config.get('web_secure_cookie', False),
                             public_origin=config.get('web_origin'),
                             organizer=organizer, customer_service=gateway.customer_service, coaching=coaching, materials=materials,
                             profile_analyzer=ProfileAnalyzer(config['api_key'],config['model'],config['base_url']),
                             public_researcher=make_public_researcher(config),
                             audio=AudioService(crm, config.get('audio_api_key', ''),
                                                config.get('audio_base_url', 'https://dashscope.aliyuncs.com/compatible-mode/v1'),
                                                config.get('audio_model', 'qwen3-asr-flash')))
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            await web.TCPSite(runner, config['web_host'], config['web_port']).start()
            LOG.info('web_dashboard_started')
        await gateway.run()
    finally:
        if runner is not None:
            await runner.cleanup()
        if coaching is not None:
            await coaching.close()
        if materials is not None:
            await materials.close()
        crm.close()
