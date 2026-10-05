"""Explicit live connection checks; never creates or confirms a task.

Run from the deployed application directory:
    .venv/bin/python deploy/live_verify.py --env .env
    .venv/bin/python deploy/live_verify.py --env .env --send-test

Output is JSON Lines containing fixed status codes, booleans, exception class
names, and (only on model mismatch) sanitized provider model identifiers.
No secrets, user identifiers, model responses, or exception messages are output.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any


# Allow execution by absolute filename without requiring an editable install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TEST_TEXT = "明天下午三点提醒我测试私人秘书"
TEST_MESSAGE = "私人秘书连接测试成功。你可以发一条语音事项，我会先整理给你确认。"


def report(code: str, **fields: Any) -> None:
    print(json.dumps({"code": code, **fields}, ensure_ascii=False), flush=True)


def failure(stage: str, error: BaseException) -> None:
    # Never emit exception text: HTTP and SDK errors may contain credentials.
    # The installed official SDK embeds the platform's integer code in this
    # fixed prefix. Extract only that integer and discard the complete message.
    fields: dict[str, Any] = {}
    if type(error) is RuntimeError and error.args and isinstance(error.args[0], str):
        match = re.match(r"^Reply ack error: errcode=(-?\d{1,9}), errmsg=", error.args[0])
        if match:
            fields["platform_errcode"] = int(match.group(1))
    report("CHECK_FAILED", stage=stage, exception_type=type(error).__name__, **fields)


async def verify_models(config: dict[str, Any]) -> bool:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.get(
                config["base_url"] + "/models",
                headers={"Authorization": "Bearer " + config["api_key"]},
            )
            response.raise_for_status()
            data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            report("MODELS_RESPONSE_INVALID")
            return False
        models = [item.get("id") for item in data["data"] if isinstance(item, dict)]
        available = config["model"] in models
        if available:
            report("MODELS_OK", configured_model_available=True)
            return True
        secrets = (config["api_key"], config["bot_secret"], config["bot_id"],
                   *config["allowed_user_ids"])
        safe_names = sorted({
            name for name in models
            if isinstance(name, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", name)
            and not any(secret and secret in name for secret in secrets)
        })[:100]
        report("MODEL_UNAVAILABLE", configured_model_available=False,
               available_models=safe_names)
        return False
    except Exception as error:
        failure("models", error)
        return False


async def verify_parser(config: dict[str, Any]) -> bool:
    from secretary.parser import DeepSeekParser

    try:
        parser = DeepSeekParser(config["api_key"], config["model"], config["base_url"])
        now = time.time()
        command = await asyncio.wait_for(parser.parse(TEST_TEXT, now), timeout=50)
        action_present = isinstance(command, dict) and bool(command.get("action"))
        remind_at = command.get("remind_at") if isinstance(command, dict) else None
        reminder_present = remind_at is not None
        passed = (action_present and command["action"] == "propose"
                  and type(remind_at) in (int, float) and remind_at > now)
        report("PARSER_OK" if passed else "PARSER_FAILED",
               action_present=action_present, remind_at_present=reminder_present)
        return passed
    except Exception as error:
        failure("parser", error)
        return False


async def verify_wecom(config: dict[str, Any], send_test: bool) -> bool:
    from aibot import WSClient, WSClientOptions
    from secretary.gateway import SafeSDKLogger

    if send_test and len(config["allowed_user_ids"]) != 1:
        report("SEND_REFUSED", reason="exactly_one_allowed_user_required")
        return False

    authenticated = asyncio.Event()
    failed = asyncio.Event()
    closing = False
    client = None
    tasks: list[asyncio.Task[Any]] = []
    passed = False
    loop = asyncio.get_running_loop()

    def on_failure(*_args: Any) -> None:
        if not closing:
            failed.set()

    def task_finished(task: asyncio.Task[Any]) -> None:
        if not task.cancelled() and task.exception() is not None:
            on_failure()

    def safe_background_error(_loop: Any, _context: Any) -> None:
        # Keep this handler through asyncio.run shutdown, including SDK-owned
        # task cancellation. Never forward contexts or exception tracebacks.
        on_failure()

    loop.set_exception_handler(safe_background_error)
    try:
        client = WSClient(WSClientOptions(
            bot_id=config["bot_id"], secret=config["bot_secret"],
            max_reconnect_attempts=0, logger=SafeSDKLogger(),
        ))
        client.on("authenticated", lambda *_: authenticated.set())
        client.on("error", on_failure)
        client.on("disconnected", on_failure)
        connect = asyncio.create_task(client.connect())
        connect.add_done_callback(task_finished)
        auth_wait = asyncio.create_task(authenticated.wait())
        fail_wait = asyncio.create_task(failed.wait())
        tasks.extend((connect, auth_wait, fail_wait))
        await asyncio.wait_for(
            asyncio.wait((auth_wait, fail_wait), return_when=asyncio.FIRST_COMPLETED),
            timeout=30,
        )
        if failed.is_set() or not authenticated.is_set() or not client.is_connected:
            report("WECOM_AUTH_FAILED")
        else:
            report("WECOM_AUTH_OK")
            passed = True
            if send_test:
                receipt = await asyncio.wait_for(client.send_message(
                    config["allowed_user_ids"][0],
                    {"msgtype": "markdown", "markdown": {"content": TEST_MESSAGE}},
                ), timeout=10)
                passed = (isinstance(receipt, dict)
                          and type(receipt.get("errcode")) is int
                          and receipt["errcode"] == 0 and not failed.is_set())
                report("SEND_ACK_OK" if passed else "SEND_ACK_FAILED")
    except TimeoutError:
        passed = False
        report("WECOM_TIMEOUT")
    except Exception as error:
        passed = False
        failure("wecom", error)
    finally:
        closing = True
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if client is not None:
            try:
                before_disconnect = asyncio.all_tasks()
                client.disconnect()
                # SDK disconnect schedules its own async close task. Await those
                # new tasks without private SDK attributes or a long fixed sleep.
                cleanup = asyncio.all_tasks() - before_disconnect
                if cleanup:
                    results = await asyncio.wait_for(
                        asyncio.gather(*cleanup, return_exceptions=True), timeout=6,
                    )
                    if any(isinstance(result, BaseException) for result in results):
                        passed = False
                        report("WECOM_DISCONNECT_FAILED")
                report("WECOM_DISCONNECTED")
            except Exception as error:
                passed = False
                failure("disconnect", error)
    return passed


async def run(config: dict[str, Any], send_test: bool = False) -> bool:
    # This handler remains active throughout event-loop shutdown, not just the
    # WebSocket phase, so no background exception can print a raw traceback.
    asyncio.get_running_loop().set_exception_handler(
        lambda _loop, _context: report("BACKGROUND_ERROR")
    )
    if not await verify_models(config):
        return False
    if not await verify_parser(config):
        return False
    return await verify_wecom(config, send_test)


def main() -> int:
    arguments = argparse.ArgumentParser(description="Secret-safe live connection checks")
    arguments.add_argument("--env", default=".env")
    arguments.add_argument("--send-test", action="store_true",
                           help="Send one fixed message to the sole allowed user")
    arguments.add_argument("--wecom-only", action="store_true",
                           help="Retry only WeCom checks after model checks have passed")
    args = arguments.parse_args()
    logging.disable(logging.CRITICAL)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        from secretary.__main__ import read_config

        config = read_config(args.env)
        if args.send_test and len(config["allowed_user_ids"]) != 1:
            report("SEND_REFUSED", reason="exactly_one_allowed_user_required")
            return 2
        passed = asyncio.run(verify_wecom(config, args.send_test) if args.wecom_only
                             else run(config, args.send_test))
        report("LIVE_VERIFY_OK" if passed else "LIVE_VERIFY_FAILED")
        return 0 if passed else 1
    except KeyboardInterrupt:
        report("CHECK_INTERRUPTED")
        return 130
    except Exception as error:
        failure("startup", error)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
