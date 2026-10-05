"""Run with python -m secretary --demo, --check, or --run."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path
import signal
import sys
import tempfile
import time

from dotenv import load_dotenv

from .gateway import BotGateway, GatewayError, run_bot
from .parser import DeepSeekParser
from .store import Store


class InstanceLock:
    """Hold an OS file lock for the lifetime of the one sending process."""

    def __init__(self, path: Path):
        self.path = path
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(self.path, 'a+b')
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                if self.path.stat().st_size == 0:
                    self.file.write(b'0')
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise GatewayError('这个任务数据库已有一个秘书进程在运行，请先停止旧进程。') from None
        return self

    def __exit__(self, *_):
        if self.file:
            self.file.close()


def read_config(env_path: str) -> dict:
    load_dotenv(env_path, override=False)
    required = ['WECOM_BOT_ID', 'WECOM_BOT_SECRET', 'WECOM_ALLOWED_USER_IDS', 'DEEPSEEK_API_KEY']
    missing = [name for name in required if not os.environ.get(name, '').strip()]
    if missing:
        raise GatewayError('配置尚未填写：' + '、'.join(missing))
    allowed = [item.strip() for item in os.environ['WECOM_ALLOWED_USER_IDS'].split(',') if item.strip()]
    if not allowed:
        raise GatewayError('WECOM_ALLOWED_USER_IDS 需要至少一个成员账号。')
    base_url = os.environ.get('DEEPSEEK_BASE_URL', 'https://api.deepseek.com').strip().rstrip('/')
    if not base_url.startswith('https://'):
        raise GatewayError('DEEPSEEK_BASE_URL 必须使用 HTTPS 地址。')
    model = os.environ.get('DEEPSEEK_MODEL', 'deepseek-flash').strip()
    if not model:
        raise GatewayError('请填写 DEEPSEEK_MODEL。')
    listen_note_url = os.environ.get('LISTEN_NOTE_MCP_URL', 'https://collab.ctcdn.cn/mcp').strip()
    from urllib.parse import urlsplit
    listen_note_address = urlsplit(listen_note_url)
    if (listen_note_address.scheme != 'https' or not listen_note_address.hostname
            or listen_note_address.username or listen_note_address.password
            or listen_note_address.query or listen_note_address.fragment):
        raise GatewayError('聆记 MCP 地址需要为不含凭据和查询参数的 HTTPS 地址。')
    web_enabled = os.environ.get('SECRETARY_WEB_ENABLED', '').strip() == '1'
    web_password_hash = os.environ.get('SECRETARY_WEB_PASSWORD_HASH', '').strip()
    if web_enabled:
        from .web import valid_password_hash
        if len(allowed) != 1:
            raise GatewayError('当前私人后台需要且仅允许配置一个成员账号。')
        if not valid_password_hash(web_password_hash):
            raise GatewayError('请先配置有效的后台登录密码哈希。')
    try:
        web_port = int(os.environ.get('SECRETARY_WEB_PORT', '8765'))
        if not 1024 <= web_port <= 65535:
            raise ValueError()
    except ValueError:
        raise GatewayError('后台端口需要是 1024 至 65535。') from None
    web_origin = os.environ.get('SECRETARY_WEB_ORIGIN', '').strip().rstrip('/') or None
    if web_origin:
        from urllib.parse import urlsplit
        parsed_origin = urlsplit(web_origin)
        if (parsed_origin.scheme not in ('http', 'https') or not parsed_origin.netloc
                or parsed_origin.username or parsed_origin.password or parsed_origin.path
                or parsed_origin.query or parsed_origin.fragment):
            raise GatewayError('后台外部地址必须是完整的 http/https 域名与可选端口，不包含路径。')
    return {
        'bot_id': os.environ['WECOM_BOT_ID'].strip(),
        'bot_secret': os.environ['WECOM_BOT_SECRET'].strip(),
        'allowed_user_ids': allowed,
        'api_key': os.environ['DEEPSEEK_API_KEY'].strip(),
        'model': model,
        'base_url': base_url,
        'db_path': Path(os.environ.get('SECRETARY_DB_PATH', 'data/secretary.sqlite3')).resolve(),
        'web_enabled': web_enabled,
        'web_password_hash': web_password_hash,
        'web_host': os.environ.get('SECRETARY_WEB_HOST', '127.0.0.1').strip(),
        'web_port': web_port,
        'web_origin': web_origin,
        'web_secure_cookie': os.environ.get('SECRETARY_WEB_SECURE_COOKIE', '').strip() == '1',
        'audio_api_key': os.environ.get('DASHSCOPE_API_KEY', '').strip(),
        'audio_base_url': os.environ.get('SECRETARY_ASR_BASE_URL', 'https://dashscope.aliyuncs.com/compatible-mode/v1').strip(),
        'audio_model': os.environ.get('SECRETARY_ASR_MODEL', 'qwen3-asr-flash').strip(),
        'listen_note_api_key': os.environ.get('LISTEN_NOTE_API_KEY', '').strip(),
        'listen_note_url': listen_note_url,
        'profile_search_api_key': os.environ.get('PROFILE_WEB_SEARCH_API_KEY', '').strip(),
    }


async def serve(config: dict) -> None:
    parser = DeepSeekParser(config['api_key'], config['model'], config['base_url'])
    with InstanceLock(Path(str(config['db_path']) + '.lock')):
        store = Store(config['db_path'])
        task = asyncio.create_task(run_bot(store, parser, config))
        loop = asyncio.get_running_loop()
        installed = []
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, task.cancel)
                installed.append(sig)
            except (NotImplementedError, RuntimeError):
                pass
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            for sig in installed:
                loop.remove_signal_handler(sig)
            store.close()


async def demo() -> None:
    """An explicit offline simulation; no credentials, network, or real reminders."""
    class DemoClient:
        is_connected = True

        def on(self, *_):
            pass

        async def reply_stream(self, frame, stream, content, finish):
            if finish:
                print('秘书：' + content)
            return {'errcode': 0}

        async def send_message(self, owner, body):
            print('模拟企业微信主动提醒：' + body['markdown']['content'])
            return {'errcode': 0}

    now = [time.time()]

    class DemoParser:
        async def parse(self, text, timestamp):
            if text == '研究一下新的供应商':
                return {'action': 'propose', 'title': '研究新的供应商', 'remind_at': None,
                        'duration_minutes': 30, 'schedule_note': ''}
            if text == '把提案P1安排到一分钟后':
                return {'action': 'reschedule_proposal', 'proposal_id': 1, 'remind_at': timestamp + 60}
            return await DeepSeekParser('').parse(text, timestamp)

    def frame(identifier, text, kind='text'):
        return {'headers': {'req_id': identifier}, 'body': {
            'msgid': identifier, 'chattype': 'single', 'from': {'userid': 'demo-user'},
            'msgtype': kind, kind: {'content': text},
        }}

    print('离线演示：模拟语音转写和模型结果，时间加速；不会连接企业微信或 DeepSeek。')
    with tempfile.TemporaryDirectory(prefix='secretary-demo-') as folder:
        path = Path(folder) / 'demo.sqlite3'
        store = Store(path)
        gateway = BotGateway(DemoClient(), store, DemoParser(), ['demo-user'], clock=lambda: now[0])
        gateway.on_authenticated()
        try:
            print('你（模拟语音）：研究一下新的供应商')
            message = frame('voice-1', '研究一下新的供应商', 'voice')
            await gateway.handle_message(message)
            print('模拟重复回调，同一条语音再次到达：')
            await gateway.handle_message(message)
            print('你（模拟语音）：把提案P1安排到一分钟后')
            await gateway.handle_message(frame('reschedule-1', '把提案P1安排到一分钟后', 'voice'))
            if store.claim_due(now[0] + 120) is not None:
                raise RuntimeError('unconfirmed proposal scheduled a reminder')
            print('你：确认 P1')
            await gateway.handle_message(frame('confirm-1', '确认 P1'))
            for index, period in enumerate(('今天安排', '本周安排', '本月安排')):
                print('你：' + period)
                await gateway.handle_message(frame(f'agenda-{index}', period))
            store.close()
            store = Store(path)
            gateway.store = store
            now[0] += 61
            print('重新打开数据库，并将模拟时间推进 61 秒：')
            if not await gateway.deliver_due_once():
                raise RuntimeError('offline demo did not deliver')
            print('你：完成 1')
            await gateway.handle_message(frame('complete-1', '完成 1'))
            print('你：待办')
            await gateway.handle_message(frame('list-1', '待办'))
        finally:
            store.close()


def main() -> int:
    args_parser = argparse.ArgumentParser(description='企业微信私人秘书')
    modes = args_parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--demo', action='store_true', help='运行完全离线演示')
    modes.add_argument('--check', action='store_true', help='检查本地配置，不调用外部服务')
    modes.add_argument('--run', action='store_true', help='连接企业微信并启动提醒服务')
    args_parser.add_argument('--env', default='.env', help='配置文件路径，默认当前目录 .env')
    args = args_parser.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    try:
        if args.demo:
            asyncio.run(demo())
        else:
            config = read_config(args.env)
            if args.check:
                from aibot import WSClient, WSClientOptions  # noqa: F401
                print('配置字段与依赖检查通过；尚未验证企业微信权限、DeepSeek 凭据或手机送达。')
            else:
                asyncio.run(serve(config))
        return 0
    except GatewayError as error:
        print(str(error), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    except Exception:
        # Avoid printing request objects, credentials, private task content.
        logging.error('secretary_startup_or_runtime_failed')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
