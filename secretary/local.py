"""Persistent loopback-only CRM, with no WeCom connection or outbound notices.

Run ``python -m secretary.local`` from any working directory. Model calls happen
only when a saved material or a user-requested analysis is processed.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import io
import hashlib
import json
import logging
import os
import re
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
from urllib.parse import urlsplit

import httpx
from aiohttp import web
from dotenv import dotenv_values

from .__main__ import InstanceLock
from .audio import AudioService
from .coaching_service import CoachingService
from .customer_parser import CustomerVoiceParser
from .customer_service import CustomerService
from .customer_store import CustomerStore
from .listen_note import ListenNoteClient
from .materials import MaterialService
from .organizer import InteractionOrganizer
from .sales_coach import SalesCoach
from .store import Store
from .web import create_app, hash_password, valid_password_hash, verify_password

APP_ROOT = Path(__file__).resolve().parent.parent
LOCAL_OWNER = 'local-user'
LOG = logging.getLogger(__name__)


class LocalError(ValueError):
    """An error whose fixed message is safe to display."""


def _path(root: Path, value: str | Path) -> Path:
    result = Path(value)
    return (result if result.is_absolute() else root / result).resolve()


def _https_url(value: str, label: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or
            parsed.password or parsed.query or parsed.fragment):
        raise LocalError(label + '需要使用不含凭据、查询参数的 HTTPS 地址。')
    return value.rstrip('/')


def read_local_config(env_path='.env', local_env_path='.env.local', *, root=APP_ROOT,
                      environ=None, port=None, db_path=None, host='127.0.0.1') -> dict:
    """Read private files without modifying process environment or bot config."""
    root = Path(root).resolve()
    values = {**dotenv_values(_path(root, env_path)),
              **dotenv_values(_path(root, local_env_path)),
              **(os.environ if environ is None else environ)}
    get = lambda key, default='': str(values.get(key) or default).strip()
    if host != '127.0.0.1':
        raise LocalError('本机模式只监听 127.0.0.1。')
    try:
        selected_port = int(port if port is not None else get('LOCAL_SECRETARY_PORT', '8765'))
        if not 1024 <= selected_port <= 65535:
            raise ValueError()
    except (ValueError, TypeError):
        raise LocalError('本机端口需要是 1024 至 65535。') from None
    selected_db = _path(root, db_path or get('LOCAL_SECRETARY_DB_PATH', 'data/local-secretary.sqlite3'))
    remote_db = _path(root, get('SECRETARY_DB_PATH', 'data/secretary.sqlite3'))
    if selected_db == remote_db:
        raise LocalError('本机模式需要使用独立数据库，请勿指定企业微信服务的数据库。')
    model = get('DEEPSEEK_MODEL', 'deepseek-flash')
    config = {
        'root': root, 'owner': LOCAL_OWNER, 'host': host, 'port': selected_port,
        'url': f'http://{host}:{selected_port}', 'db_path': selected_db,
        'api_key': get('DEEPSEEK_API_KEY'), 'model': model,
        'base_url': _https_url(get('DEEPSEEK_BASE_URL', 'https://api.deepseek.com'), '模型服务地址'),
        'listen_note_api_key': get('LISTEN_NOTE_API_KEY'),
        'listen_note_url': _https_url(get('LISTEN_NOTE_MCP_URL', 'https://collab.ctcdn.cn/mcp'), '聆记服务地址'),
        'audio_api_key': get('DASHSCOPE_API_KEY'),
        'audio_base_url': _https_url(get('SECRETARY_ASR_BASE_URL', 'https://dashscope.aliyuncs.com/compatible-mode/v1'), '语音服务地址'),
        'audio_model': get('SECRETARY_ASR_MODEL', 'qwen3-asr-flash'),
        'profile_search_api_key': get('PROFILE_WEB_SEARCH_API_KEY'),
    }
    return config


def private_file(path: Path) -> None:
    """Restrict generated secrets to the current Windows account / POSIX user."""
    if os.name != 'nt':
        path.chmod(0o600)
        return
    try:
        flags = subprocess.CREATE_NO_WINDOW
        identity = subprocess.run(['whoami', '/user', '/fo', 'csv', '/nh'],
            capture_output=True, check=True, text=True, creationflags=flags)
        sid = next(csv.reader(io.StringIO(identity.stdout)))[1]
        if not sid.startswith('S-1-') or any(c not in 'S-0123456789' for c in sid):
            raise ValueError()
        # Files created by this module have inherited permissions only. Reset
        # also removes explicit grants if a previous copy was less restricted.
        subprocess.run(['icacls', str(path), '/reset'], capture_output=True, check=True, creationflags=flags)
        subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r', f'*{sid}:(F)'],
            capture_output=True, check=True, creationflags=flags)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        raise LocalError('无法保护本机私有文件，请检查当前账号的文件权限。') from None


def _write_private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + secrets.token_hex(8) + '.local')
    try:
        with temporary.open('x', encoding='utf-8') as output:
            # Windows creates the file with its directory's inherited ACL.
            # Restrict the empty file before any credential bytes exist, keeping
            # this same handle through the write to avoid reopening by name.
            private_file(temporary)
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write('\n')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def ensure_access_file(path: Path, config: dict) -> dict:
    """Preserve the first generated login; never print or log its password."""
    if path.exists():
        private_file(path)
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            password, encoded = value['password'], value['password_hash']
            if (not isinstance(password, str) or not 12 <= len(password) <= 256 or
                    not valid_password_hash(encoded) or not verify_password(password, encoded)):
                raise ValueError()
        except (OSError, ValueError, KeyError, TypeError):
            raise LocalError('本机登录文件无效，请保留文件后检查密码及哈希；不会自动覆盖现有口令。') from None
    else:
        password = secrets.token_urlsafe(24)
        encoded = hash_password(password)
    result = {'version': 1, 'owner': LOCAL_OWNER, 'url': config['url'],
              'password': password, 'password_hash': encoded}
    _write_private_json(path, result)
    return result


def runtime_status(config: dict) -> dict:
    from .public_research import public_research_configuration
    configured = bool(config['api_key'])
    return {
        'mode': 'local', 'label': '本机模式', 'wecom_connected': False,
        'reminders': 'web', 'reminder_status': '提醒在后台可见，未发送企业微信通知',
        'arrangement_reminders': 'persistent_web',
        'arrangement_reminder_status': '待落实提示持久保留；本机服务运行时处理，重开网页可查看',
        'model_configured': configured,
        'model_status': '已配置 DeepSeek；调用时验证凭据与可用性' if configured else '尚未配置 DeepSeek；原文可保存，配置后再整理',
        'listen_note_configured': bool(config['listen_note_api_key']),
        'audio_configured': bool(config['audio_api_key']), 'database': 'local',
        **public_research_configuration(config),
    }


async def build_local_app(config: dict, password_hash: str, *, stopped=None):
    """Wire real services and one durable material worker; no BotGateway exists."""
    store = crm = coaching = materials = None
    # A stale workstation proxy must not route local model requests. This
    # preference applies to this app-owned client; process/global proxy settings
    # and the remote bot's HTTP clients remain unchanged.
    model_client = httpx.AsyncClient(timeout=httpx.Timeout(70, connect=15),
        follow_redirects=False, trust_env=False)
    try:
        store = Store(config['db_path'])
        crm = CustomerStore(config['db_path'])
        lock = asyncio.Lock()
        organizer = InteractionOrganizer(config['api_key'], config['model'], config['base_url'], client=model_client)
        parser = CustomerVoiceParser(config['api_key'], config['model'], config['base_url'], client=model_client)
        coaching = CoachingService(crm,
            SalesCoach(config['api_key'], config['model'], config['base_url'], client=model_client), lock)
        materials = MaterialService(crm, lock, organizer=organizer, customer_parser=parser,
            connector=ListenNoteClient(config['listen_note_api_key'], config['listen_note_url']), coaching=coaching)
        from .sales_workspace import SalesWorkspace
        from .customer_resolution import CustomerResolver, CustomerResolutionService
        from .sales_discussion import DiscussionAdvisor, DiscussionService
        from .profile_intelligence import ProfileAnalyzer
        from .public_research import make_public_researcher
        workspace=SalesWorkspace(crm)
        resolution=CustomerResolutionService(crm,workspace,lock,
            resolver=CustomerResolver(config['api_key'],config['base_url'],config['model'],client=model_client)
            if config['api_key'] else None)
        discussions=DiscussionService(crm,workspace,lock,
            advisor=DiscussionAdvisor(config['api_key'],config['base_url'],config['model'],client=model_client)
            if config['api_key'] else None)
        app = create_app(store, crm, lock, LOCAL_OWNER, password_hash, connected=lambda: False,
            public_origin=config['url'], runtime=runtime_status(config), organizer=organizer,
            resolution=resolution,discussions=discussions,
            profile_analyzer=ProfileAnalyzer(config['api_key'],config['model'],config['base_url'],client=model_client)
                if config['api_key'] else None,
            public_researcher=make_public_researcher(config,client=model_client),
            coaching=coaching, materials=materials,
            customer_service=CustomerService(crm, parser, lock, organizer=organizer, coach=coaching),
            audio=AudioService(crm, config['audio_api_key'], config['audio_base_url'], config['audio_model']))
    except BaseException:
        try:
            if materials is not None:
                await materials.close()
            if coaching is not None:
                await coaching.close()
        finally:
            await model_client.aclose()
            if crm is not None:
                crm.close()
            if store is not None:
                store.close()
        raise

    async def material_worker():
        try:
            while True:
                if not await materials.process_one():
                    await asyncio.sleep(.5)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.error('local_material_worker_failed')
            if stopped is not None:
                stopped.set()

    async def services(_app):
        worker = asyncio.create_task(material_worker())
        try:
            yield
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            try:
                await materials.close()
                await coaching.close()
            finally:
                await model_client.aclose()
                crm.close()
                store.close()

    async def local_guide(request):
        guide=APP_ROOT/'deploy'/'使用指南与案例.html'
        if not guide.is_file():raise web.HTTPNotFound()
        return web.FileResponse(guide)
    app.router.add_get('/guide',local_guide)
    @web.middleware
    async def guide_policy(request,handler):
        result=await handler(request)
        if request.path=='/guide' and result.status==200:
            source=(APP_ROOT/'deploy'/'使用指南与案例.html').read_text(encoding='utf-8-sig')
            def hashes(tag):
                return ' '.join("'sha256-"+base64.b64encode(hashlib.sha256(block.encode('utf-8')).digest()).decode('ascii')+"'"
                    for block in re.findall(r'<'+tag+r'\b[^>]*>(.*?)</'+tag+'>',source,re.S|re.I))
            result.headers['Content-Security-Policy']=("default-src 'self'; script-src 'self' "+hashes('script')+
                "; style-src 'self' "+hashes('style')+"; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        return result
    app.middlewares.insert(0,guide_policy)
    app.cleanup_ctx.append(services)
    return app


def check_port(host: str, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            if os.name == 'nt':
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind((host, port))
        except OSError:
            raise LocalError('本机后台端口已占用或无法监听，请停止旧实例或选择其他端口。') from None


async def serve_local(config: dict, *, access_file: Path, state_file: Path,
                      stop_file: Path, instance_id: str) -> None:
    with InstanceLock(Path(str(config['db_path']) + '.lock')):
        check_port(config['host'], config['port'])
        access = ensure_access_file(access_file, config)
        stopped = asyncio.Event()
        app = await build_local_app(config, access['password_hash'], stopped=stopped)
        runner = web.AppRunner(app, access_log=None)
        loop = asyncio.get_running_loop()
        previous = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stopped.set))
        watcher = None
        try:
            await runner.setup()
            await web.TCPSite(runner, config['host'], config['port']).start()
            _write_private_json(state_file, {
                'pid': os.getpid(), 'executable': str(Path(sys.executable).resolve()),
                'process_executable': str(Path(getattr(sys, '_base_executable', sys.executable)).resolve()),
                'app_root': str(config['root']), 'module': 'secretary.local', 'instance_id': instance_id,
                'url': config['url'], 'port': config['port'], 'database': str(config['db_path']),
                'stop_file': str(stop_file), 'status': 'ready',
            })
            LOG.info('local_dashboard_started')

            async def watch_stop_file():
                while not stopped.is_set():
                    try:
                        if stop_file.exists() and stop_file.stat().st_size < 256:
                            request = json.loads(stop_file.read_text(encoding='utf-8-sig'))
                            if request.get('instance_id') == instance_id:
                                stopped.set()
                                return
                    except (OSError, ValueError, AttributeError):
                        pass
                    await asyncio.sleep(.25)

            watcher = asyncio.create_task(watch_stop_file())
            await stopped.wait()
        finally:
            if watcher:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            await runner.cleanup()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            try:
                state = json.loads(state_file.read_text(encoding='utf-8'))
                if state.get('instance_id') == instance_id:
                    state_file.unlink(missing_ok=True)
            except (OSError, ValueError, AttributeError):
                pass
            stop_file.unlink(missing_ok=True)


def main(argv=None) -> int:
    args = argparse.ArgumentParser(description='本机私人秘书后台（不连接企业微信）')
    args.add_argument('--env', default='.env')
    args.add_argument('--local-env', default='.env.local')
    args.add_argument('--host', default='127.0.0.1')
    args.add_argument('--port', type=int)
    args.add_argument('--db')
    args.add_argument('--check', action='store_true', help='只检查配置；不调用外部服务')
    args.add_argument('--access-file', default='deploy/local-access.local.json')
    args.add_argument('--state-file', default='deploy/local-process.local.json')
    args.add_argument('--stop-file')
    args.add_argument('--instance-id', default=None)
    options = args.parse_args(argv)
    os.umask(0o077)
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    try:
        config = read_local_config(options.env, options.local_env, port=options.port,
                                   db_path=options.db, host=options.host)
        if options.check:
            print('本机配置检查通过；企微未连接；提醒为后台可见；尚未验证外部服务凭据。')
            print(runtime_status(config)['model_status'])
            return 0
        instance_id = options.instance_id or secrets.token_hex(16)
        if not instance_id.isascii() or not instance_id.isalnum() or len(instance_id) > 64:
            raise LocalError('本机实例标识无效。')
        asyncio.run(serve_local(config,
            access_file=_path(APP_ROOT, options.access_file), state_file=_path(APP_ROOT, options.state_file),
            stop_file=_path(APP_ROOT, options.stop_file or f'deploy/local-stop-{instance_id}.local'),
            instance_id=instance_id))
        return 0
    except (LocalError, RuntimeError) as error:
        if isinstance(error, LocalError) or type(error).__name__ == 'GatewayError':
            print(str(error), file=sys.stderr)
        else:
            LOG.error('local_startup_or_runtime_failed')
        return 2
    except KeyboardInterrupt:
        return 0
    except Exception:
        LOG.error('local_startup_or_runtime_failed')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
