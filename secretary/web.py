"""Small authenticated CRM web application sharing the bot's mutation lock."""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from .store import _timestamp, SHANGHAI
from .record_categories import CATEGORIES, resolve_category
from .profile_intelligence import ProfileConflict
from .customer_timeline import TimelineService, TimelineConflict
from .progress_workspace import ProgressConflict
from .exchange_workspace import ExchangeConflict
from .secretary_flow import SecretaryFlow, FlowConflict
from .secretary_interpreter import SecretaryInterpreter
from .arrangement_queue import ArrangementConflict
from .matters import MatterService, MatterConflict

LOG = logging.getLogger(__name__)
STATIC = Path(__file__).with_name('static')
COOKIE = 'secretary_session'
SESSION_SECONDS = 12 * 3600
CAPTURE_WORKER = web.AppKey('capture_worker',asyncio.Task)
PROFILE_WORKER = web.AppKey('profile_worker',asyncio.Task)
RESEARCH_WORKER = web.AppKey('research_worker', asyncio.Task)
PROGRESS_WORKER = web.AppKey('progress_worker', asyncio.Task)
SECRETARY_WORKER = web.AppKey('secretary_worker', asyncio.Task)
DOCUMENT_WORKER = web.AppKey('document_worker', asyncio.Task)
ARRANGEMENT_WORKER = web.AppKey('arrangement_worker', asyncio.Task)


def hash_password(password: str) -> str:
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise ValueError('后台密码需要 12 至 256 个字符。')
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return 'scrypt$' + salt.hex() + '$' + key.hex()


def valid_password_hash(encoded: str) -> bool:
    return isinstance(encoded, str) and bool(re.fullmatch(r'scrypt\$[a-f0-9]{32}\$[a-f0-9]{64}', encoded))


def verify_password(password: Any, encoded: str) -> bool:
    if not isinstance(password, str) or len(password) > 256 or not valid_password_hash(encoded):
        return False
    _, salt, expected = encoded.split('$')
    key = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32)
    return hmac.compare_digest(key.hex(), expected)


def response(data: dict, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda value: json.dumps(value, ensure_ascii=False, allow_nan=False))


class WebCRM:
    def __init__(self, store, crm, lock, owner, password_hash, *, connected=None, secure_cookie=False,
                 public_origin=None, demo=False, clock=time.time, organizer=None, customer_service=None, coaching=None,
                 audio=None, materials=None, visits=None, runtime=None, resolution=None, discussions=None,
                 captures=None, profile_intelligence=None, profile_analyzer=None, public_researcher=None,
                 research_workspace=None, secretary_flow=None):
        if not valid_password_hash(password_hash):
            raise ValueError('后台密码哈希未配置或格式无效。')
        self.store, self.crm, self.lock, self.owner = store, crm, lock, owner
        self.password_hash = password_hash
        self.connected = connected or (lambda: False)
        self.secure_cookie, self.public_origin, self.demo = secure_cookie, public_origin, demo
        self.clock = clock
        self.sessions: dict[str, dict] = {}
        self.failures: dict[str, deque] = {}
        self.all_failures = deque()
        self.organizer = organizer
        self.customer_service = customer_service
        self.coaching = coaching
        self.materials = materials
        from .document_attachments import DocumentAttachmentService
        self.document_files = DocumentAttachmentService(crm, materials, clock=clock) if materials else None
        self.document_parse_slots = asyncio.Semaphore(2)
        self.runtime = runtime or {}
        visits = visits or getattr(materials, 'visit_service', None)
        if visits is None and materials is not None:
            from .visits import VisitService
            visits = VisitService(crm, materials, lock)
        self.visits = visits
        from .audio import AudioService
        self.audio = audio or AudioService(crm)
        from .review_queue import ReviewQueue
        self.review_queue = ReviewQueue(crm, materials=materials, visits=visits, clock=clock)
        from .spoken_changes import SpokenChangeService
        self.spoken_changes = SpokenChangeService(crm,clock)
        from .sales_workspace import SalesWorkspace
        self.sales_workspace = SalesWorkspace(crm,clock=clock)
        from .account_network import AccountNetwork
        self.accounts = AccountNetwork(crm, self.sales_workspace, clock=clock)
        from .profile_intelligence import ProfileIntelligence
        self.profile_intelligence = profile_intelligence or ProfileIntelligence(crm, self.sales_workspace,
            lock=lock, analyzer=profile_analyzer, researcher=public_researcher, clock=clock)
        from .research_workspace import ResearchWorkspace
        self.research_workspace = research_workspace or ResearchWorkspace(
            crm, self.profile_intelligence, clock=clock)
        self.profile_intelligence.research_scheduler = self.queue_automatic_research
        if self.coaching:
            self.coaching.workspace = self.sales_workspace
            self.coaching.profile_intelligence = self.profile_intelligence
        from .customer_resolution import CustomerResolutionService
        self.resolution = resolution or CustomerResolutionService(crm,self.sales_workspace,lock,clock=clock)
        if self.materials is not None and getattr(self.materials,'resolution',None) is None:
            self.materials.resolution=self.resolution
        from .capture_inbox import CaptureService
        self.captures = captures or CaptureService(crm,self.sales_workspace,lock,
            resolution=self.resolution,organizer=organizer,clock=clock)
        from .sales_discussion import DiscussionService
        self.discussions = discussions or DiscussionService(crm,self.sales_workspace,lock,clock=clock)
        self.discussions.profile_intelligence = self.profile_intelligence
        from .secretary_goals import SecretaryGoals
        self.secretary_goals = SecretaryGoals(self.resolution)
        from .public_pages import PublicPageReader
        self.public_pages = PublicPageReader(clock=clock)
        from .progress_workspace import ProgressWorkspace
        self.progress_workspace = ProgressWorkspace(crm, self.sales_workspace, self.discussions, clock=clock)
        self.review_queue.progress_workspace = self.progress_workspace
        self.timeline = TimelineService(crm, self.sales_workspace, visits=visits,
            discussions=self.discussions, clock=clock)
        self.discussions.timeline = self.timeline
        self.sales_workspace.timeline = self.timeline
        from .exchange_workspace import ExchangeWorkspace
        self.exchange_workspace = ExchangeWorkspace(crm, self.sales_workspace, self.profile_intelligence,
            materials=materials, visits=visits, timeline=self.timeline, organizer=organizer, clock=clock)
        self.review_queue.exchange_workspace = self.exchange_workspace
        from .exchange_records import ExchangeRecords
        self.exchange_records = ExchangeRecords(crm,visits,clock) if visits else None
        self.matters = MatterService(crm, clock=clock)
        crm.matter_service = self.matters
        self.secretary_flow = secretary_flow or SecretaryFlow(crm,self.sales_workspace,lock,
            interpreter=SecretaryInterpreter(organizer),visits=visits,timeline=self.timeline,clock=clock)
        self.secretary_flow.matters = self.matters
        crm.secretary_flow = self.secretary_flow
        self.matters.secretary_flow = self.secretary_flow
        from .matter_router import MatterRouter, DeepSeekMatterModel
        self.secretary_flow.matter_router = MatterRouter(crm, self.matters,
            model=DeepSeekMatterModel(organizer) if organizer else None, clock=clock)
        from .arrangement_reminders import ArrangementReminders
        self.arrangement_reminders = ArrangementReminders(self.secretary_flow.arrangements, clock=clock)
        from .record_lifecycle import RecordLifecycle
        self.record_lifecycle = RecordLifecycle(crm,clock=clock)
        self.organizing: set[int] = set()

    def session(self, request):
        now = self.clock()
        for token in list(self.sessions):
            if self.sessions[token]['expires'] <= now:
                del self.sessions[token]
        return self.sessions.get(request.cookies.get(COOKIE, ''))

    @web.middleware
    async def security(self, request, handler):
        try:
            if request.method not in ('GET', 'HEAD', 'OPTIONS'):
                expected = {f'{request.scheme}://{request.host}'}
                if self.public_origin:
                    expected.add(self.public_origin)
                if request.headers.get('Origin') and request.headers['Origin'] not in expected:
                    return self.harden(response({'error': '请求来源不匹配，请从后台页面操作。'}, 403))
                if request.path in ('/api/audio/transcribe', '/api/materials/upload'):
                    if request.content_type != 'multipart/form-data':
                        return self.harden(response({'error': '请上传文件。'}, 415))
                elif request.content_type != 'application/json':
                    return self.harden(response({'error': '请使用 JSON 请求。'}, 415))
                if request.path not in ('/api/audio/transcribe', '/api/materials/upload') and request.content_length and request.content_length > self.body_limit(request):
                    return self.harden(response({'error': '提交的内容过长。'}, 413))
            if request.path.startswith('/api/') and request.path not in ('/api/session', '/api/login'):
                session = self.session(request)
                if not session:
                    return self.harden(response({'error': '请先登录。'}, 401))
                if request.method not in ('GET', 'HEAD') and not hmac.compare_digest(
                    request.headers.get('X-CSRF-Token', ''), session['csrf']
                ):
                    return self.harden(response({'error': '页面验证已失效，请刷新后重试。'}, 403))
            result = await handler(request)
        except web.HTTPException as error:
            result = response({'error': '请求地址或请求内容无效。'}, error.status)
        except ArrangementConflict as error:
            result = response({'error': str(error), 'latest': getattr(error, 'latest', None)}, 409)
        except (ProfileConflict, TimelineConflict, ProgressConflict, ExchangeConflict, FlowConflict) as error:
            result = response({'error': str(error)}, 409)
        except MatterConflict as error:
            # MatterConflict aliases the older RecordConflict. Preserve the
            # legacy record endpoints' validation status while exposing the
            # new matter revision conflicts as 409.
            status = 409 if request.path.startswith('/api/matters') or re.fullmatch(
                r'/api/secretary/turns/[1-9][0-9]*/matter', request.path) else 400
            result = response({'error': str(error)}, status)
        except (ValueError, KeyError) as error:
            # Application validations contain fixed messages, never echo submitted data.
            result = response({'error': '未找到这条记录。' if isinstance(error, KeyError) else str(error)},
                              404 if isinstance(error, KeyError) else 422 if '/arrangement' in request.path else 400)
        except Exception:
            LOG.error('web_request_failed')
            result = response({'error': '暂时无法完成操作，请刷新核对后再试。'}, 500)
        return self.harden(result)

    def harden(self, result):
        result.headers.update({
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        })
        return result

    @staticmethod
    def body_limit(request):
        # Long transcripts are accepted only at the dedicated material boundary.
        long_source = (re.fullmatch(r'/api/materials(?:/[1-9][0-9]*)?', request.path)
                       or re.fullmatch(r'/api/visits/[1-9][0-9]*/materials', request.path))
        return 4 * 1024 * 1024 if long_source and request.method in ('POST', 'PATCH') else 64 * 1024

    async def body(self, request):
        limit = self.body_limit(request)
        if len(await request.read()) > limit:
            raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=len(request._read_bytes))
        try:
            value = await request.json()
        except (ValueError, UnicodeError):
            raise ValueError('请求内容不是有效 JSON。') from None
        if not isinstance(value, dict):
            raise ValueError('请求内容需要是 JSON 对象。')
        return value

    async def get_session(self, request):
        session = self.session(request)
        return response({'authenticated': bool(session), 'demo': self.demo,
                         'runtime': self.runtime,
                         **({'csrf': session['csrf']} if session else {})})

    async def login(self, request):
        data = await self.body(request)
        now = self.clock()
        for address in list(self.failures):
            while self.failures[address] and self.failures[address][0] <= now - 300:
                self.failures[address].popleft()
            if not self.failures[address]:
                del self.failures[address]
        while self.all_failures and self.all_failures[0] <= now - 300:
            self.all_failures.popleft()
        address = request.remote or 'unknown'
        if len(self.failures.get(address, ())) >= 5 or len(self.all_failures) >= 40:
            return response({'error': '登录尝试过多，请五分钟后重试。'}, 429)
        # Reserve an attempt before yielding to scrypt: concurrent requests
        # cannot all bypass the limit or create unbounded CPU/memory work.
        self.failures.setdefault(address, deque()).append(now)
        self.all_failures.append(now)
        if not await asyncio.to_thread(verify_password, data.get('password'), self.password_hash):
            return response({'error': '密码不正确。'}, 401)
        self.sessions.pop(request.cookies.get(COOKIE, ''), None)
        if len(self.sessions) >= 32:
            del self.sessions[next(iter(self.sessions))]
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.sessions[token] = {'csrf': csrf, 'expires': now + SESSION_SECONDS}
        result = response({'authenticated': True, 'csrf': csrf, 'demo': self.demo, 'runtime': self.runtime})
        result.set_cookie(COOKIE, token, httponly=True, samesite='Strict', secure=self.secure_cookie,
                          max_age=SESSION_SECONDS, path='/')
        return result

    async def logout(self, request):
        self.sessions.pop(request.cookies.get(COOKIE, ''), None)
        result = response({'authenticated': False})
        result.del_cookie(COOKIE, path='/')
        return result

    def number(self, value, *, default=None):
        if value is None and default is not None:
            return default
        if isinstance(value, bool) or not re.fullmatch(r'[1-9][0-9]{0,17}', str(value)):
            raise ValueError('编号或页码无效。')
        return int(value)

    def paging(self, request, default=50):
        return {'page': self.number(request.query.get('page'), default=1),
                'page_size': self.number(request.query.get('page_size'), default=default)}

    async def dashboard(self, request):
        async with self.lock:
            data = self.crm.dashboard(self.owner, self.clock())
            data['stats'].update(self.sales_workspace.dashboard_summary(self.owner))
            data['matters'] = self.matters.list(self.owner, page_size=6)
        data['bot_connected'] = bool(self.connected())
        return response(data)

    async def matter_list(self, request):
        if request.method == 'POST':
            data = await self.body(request)
            async with self.lock:
                return response(self.matters.create(self.owner, data), 201)
        allowed = {'q', 'customer_id', 'opportunity_id', 'status', 'visibility', 'page', 'page_size'}
        if set(request.query) - allowed:
            raise ValueError('事项筛选条件无效。')
        async with self.lock:
            return response(self.matters.list(self.owner, q=request.query.get('q', ''),
                customer_id=self.number(request.query['customer_id']) if request.query.get('customer_id') else None,
                opportunity_id=self.number(request.query['opportunity_id']) if request.query.get('opportunity_id') else None,
                status=request.query.get('status', ''), visibility=request.query.get('visibility', 'active'),
                **self.paging(request, 20)))

    async def matter_detail(self, request):
        identifier = self.number(request.match_info['id'])
        data = await self.body(request) if request.method == 'PATCH' else None
        async with self.lock:
            return response(self.matters.update(self.owner, identifier, data) if data is not None
                else {'matter': self.matters.get(self.owner, identifier)})

    async def matter_candidates(self, request):
        async with self.lock:
            return response(self.matters.candidates(self.owner))

    async def matter_candidate_confirm(self, request):
        data = await self.body(request)
        async with self.lock:
            return response(self.matters.confirm_candidate(self.owner, data))

    async def matter_resolve(self, request):
        if set(request.query) != {'entity_type', 'entity_id'}:
            raise ValueError('请选择原记录或安排。')
        async with self.lock:
            return response(self.matters.resolve(self.owner, request.query['entity_type'],
                self.number(request.query['entity_id'])))

    async def matter_merge(self, request):
        data = await self.body(request)
        async with self.lock:
            return response(self.matters.merge(self.owner, self.number(request.match_info['id']), data))

    async def matter_split(self, request):
        data = await self.body(request)
        async with self.lock:
            return response(self.matters.split(self.owner, self.number(request.match_info['id']), data))

    async def matter_lifecycle(self, request):
        data = await self.body(request) if request.method == 'POST' else None
        identifier = self.number(request.match_info['id'])
        async with self.lock:
            return response(self.matters.lifecycle(self.owner, identifier, data) if data is not None
                else self.matters.lifecycle_preview(self.owner, identifier))

    async def matter_undo(self, request):
        data = await self.body(request)
        async with self.lock:
            return response(self.matters.undo(self.owner, self.number(request.match_info['id']), data))

    async def secretary_turns(self, request):
        async with self.lock:
            if request.method == 'POST':
                data=self.secretary_flow.submit(self.owner,await self.body(request))
                return response({'turn':data},202)
            return response(self.secretary_flow.recent_turns(self.owner))

    async def secretary_turn(self, request):
        async with self.lock:
            identifier=self.number(request.match_info['id'])
            return response({'turn':self.secretary_flow.turn(self.owner,identifier)})

    async def secretary_retry(self, request):
        await self.body(request)
        async with self.lock:
            return response({'turn':self.secretary_flow.retry(self.owner,self.number(request.match_info['id']))},202)

    async def secretary_matter_correction(self, request):
        from .matter_correction import snapshot, correct
        identifier=self.number(request.match_info['id'])
        if request.method=='GET':
            async with self.lock: result=snapshot(self.secretary_flow,self.owner,identifier)
        else:
            data=await self.body(request)
            async with self.lock: result=correct(self.secretary_flow,self.owner,identifier,data)
        return response(result)

    async def secretary_adopt(self, request):
        data=await self.body(request)
        if set(data)!={'index'}:raise ValueError('请选择一条建议。')
        async with self.lock:
            return response({'record':self.secretary_flow.adopt(self.owner,self.number(request.match_info['id']),self.number(data['index']))})

    async def secretary_plans(self, request):
        allowed={'customer_id','contact_id','opportunity_id','visit_id'}
        if set(request.query)-allowed:raise ValueError('计划筛选条件无效。')
        async with self.lock:
            return response(self.secretary_flow.list_plans(self.owner,**{k:self.number(v) for k,v in request.query.items()}))

    async def secretary_plan(self, request):
        async with self.lock:
            return response({'plan':self.secretary_flow.plan(self.owner,self.number(request.match_info['id']))})

    @staticmethod
    def arrangement_pagination(request):
        values = {}
        for key, default in (('limit', 20), ('offset', 0)):
            raw = request.query.get(key, str(default))
            if not re.fullmatch(r'0|[1-9][0-9]{0,6}', raw):
                raise ValueError('分页参数无效。')
            values[key] = int(raw)
        if not 1 <= values['limit'] <= 200:
            raise ValueError('每页数量需要为1至200。')
        return values

    async def secretary_arrangements(self, request):
        allowed = {'view','state','customer_id','contact_id','opportunity_id','matter_id','blocker','limit','offset'}
        if set(request.query) - allowed:
            raise ValueError('安排筛选字段无效。')
        args = self.arrangement_pagination(request)
        for key in ('view','state','blocker'):
            if key in request.query:
                args[key] = request.query[key]
        for key in ('customer_id','contact_id','opportunity_id','matter_id'):
            if key in request.query:
                args[key] = self.number(request.query[key])
        async with self.lock:
            return response(self.secretary_flow.arrangements.list(self.owner, **args))

    async def secretary_arrangement_decision(self, request):
        data = await self.body(request)
        async with self.lock:
            result = self.secretary_flow.arrangements.apply_decision(self.owner, self.number(request.match_info['id']), data)
            self.arrangement_reminders.sweep()
            return response(result)

    async def secretary_arrangement_notices(self, request):
        if set(request.query) - {'limit','offset'}:
            raise ValueError('提醒筛选字段无效。')
        args = self.arrangement_pagination(request)
        async with self.lock:
            return response(self.arrangement_reminders.notices(self.owner, **args))

    async def secretary_arrangement_notice_read(self, request):
        data = await self.body(request)
        if data:
            raise ValueError('标记看过不需要安排变更字段。')
        async with self.lock:
            return response(self.arrangement_reminders.read(self.owner, self.number(request.match_info['id'])))

    async def secretary_settings(self, request):
        async with self.lock:
            return response(self.secretary_flow.update_settings(self.owner,await self.body(request)) if request.method=='PATCH' else self.secretary_flow.settings(self.owner))

    async def secretary_prospects(self, request):
        async with self.lock:return response(self.secretary_flow.prospects(self.owner))

    async def secretary_prospect_link(self, request):
        data=await self.body(request)
        async with self.lock:return response({'prospect':self.secretary_flow.link_prospect(self.owner,self.number(request.match_info['id']),data)})

    async def customers(self, request):
        async with self.lock:
            data = self.crm.list_customers(self.owner, q=request.query.get('q', ''),
                                          stage=request.query.get('stage', ''), **self.paging(request))
        return response(data)

    async def create_customer(self, request):
        data = await self.body(request)
        unit_data = {key: data.pop(key) for key in ('parent_customer_id', 'unit_type') if key in data}
        async with self.lock:
            customer = (self.accounts.create_unit(self.owner, data, unit_data, self.clock()) if unit_data
                        else self.crm.create_customer(self.owner, data, self.clock()))
        return response({'customer': customer}, 201)

    async def customer(self, request):
        identifier = self.number(request.match_info['id'])
        async with self.lock:
            customer = self.crm.get_customer(self.owner, identifier)
            if customer is None:
                raise KeyError()
            records = self.crm.list_records(self.owner, customer_id=identifier, **self.paging(request))
            matters = self.matters.list(self.owner, customer_id=identifier, page_size=20)
        return response({'customer': customer, 'records': records['items'],
                         'matters': matters,
                         'total': records['total'], 'page': records['page'], 'pages': records['pages']})

    async def update_customer(self, request):
        data = await self.body(request)
        async with self.lock:
            customer = self.crm.update_customer(self.owner, self.number(request.match_info['id']), data, self.clock())
            if self.coaching:
                self.coaching.schedule(self.owner, customer['id'])
        return response({'customer': customer})

    async def customer_schema(self, request):
        from .customer_schema import ACCOUNT_FIELDS, CONTACT_FIELDS, PROJECT_FIELDS
        return response({'account_fields': ACCOUNT_FIELDS, 'contact_fields': CONTACT_FIELDS, 'project_fields': PROJECT_FIELDS})

    async def units(self, request):
        async with self.lock:
            return response(self.accounts.units(self.owner))

    async def account_network(self, request):
        identifier = self.number(request.match_info['id'])
        data = await self.body(request) if request.method == 'PATCH' else None
        async with self.lock:
            if data is not None:
                self.accounts.update(self.owner, identifier, data)
            return response(self.accounts.view(self.owner, identifier, include_archived=True))

    async def opportunity_stakeholders(self, request):
        customer_id, project_id = self.number(request.match_info['id']), self.number(request.match_info['opp_id'])
        data = await self.body(request) if request.method == 'POST' else None
        async with self.lock:
            if data is not None:
                if 'archived' in data:
                    contact_id = self.number(data.pop('contact_id', None))
                    result = self.sales_workspace.archive_stakeholder(self.owner, customer_id, project_id, contact_id, data)
                else:
                    result = self.sales_workspace.upsert_stakeholder(self.owner, customer_id, project_id, data)
                return response({'relationship': result})
            return response(self.sales_workspace.stakeholders(self.owner, customer_id, project_id,
                include_archived=request.query.get('include_archived') == 'true'))

    async def opportunity_units(self, request):
        customer_id, project_id = self.number(request.match_info['id']), self.number(request.match_info['opp_id'])
        data = await self.body(request) if request.method == 'POST' else None
        async with self.lock:
            if data is not None:
                if 'archived' in data:
                    participant_id = self.number(data.pop('participant_customer_id', None))
                    result = self.sales_workspace.archive_project_unit(self.owner, customer_id, project_id, participant_id, data)
                else:
                    result = self.sales_workspace.upsert_project_unit(self.owner, customer_id, project_id, data)
                return response({'relationship': result})
            return response(self.sales_workspace.project_units(self.owner, customer_id, project_id,
                include_archived=request.query.get('include_archived') == 'true'))

    async def contact_projects(self, request):
        async with self.lock:
            return response(self.sales_workspace.contact_projects(self.owner, self.number(request.match_info['contact_id']),
                include_archived=True))

    async def profile_knowledge(self, request):
        identifier = self.number(request.match_info['id'])
        project_id = self.number(request.query['opportunity_id']) if request.query.get('opportunity_id') else None
        async with self.lock:
            self.crm._require_customer(self.crm._db, self.owner, identifier)
            service = self.profile_intelligence
            return response({'status': service.status(self.owner, identifier),
                'candidates': service.list_candidates(self.owner, customer_id=identifier, opportunity_id=project_id),
                'plan': service.profile_plan(self.owner, identifier, project_id),
                'project_facts': service.project_facts(self.owner, identifier, project_id) if project_id else None})

    async def profile_settings(self, request):
        data = await self.body(request)
        identifier = self.number(request.match_info['id'])
        async with self.lock:
            result = self.profile_intelligence.configure(self.owner, data, customer_id=identifier)
        return response({'settings': result})

    async def profile_scan(self, request):
        data = await self.body(request)
        if set(data) - {'force'} or type(data.get('force', False)) is not bool:
            raise ValueError('请明确是否整理已有交流。')
        result = await self.profile_intelligence.scan(self.owner, self.number(request.match_info['id']),
            force=data.get('force', False), limit=8)
        return response({'result': result})

    async def profile_research(self, request):
        data = await self.body(request)
        mode = data.pop('mode', 'search')
        identifier = self.number(request.match_info['id'])
        if mode == 'import':
            result = await self.profile_intelligence.import_public_source(self.owner, identifier, data)
        elif mode == 'search' and not data:
            result = await self.profile_intelligence.research(self.owner, identifier, force=True)
        else:
            raise ValueError('公开资料操作无效，请选择检索或粘贴公开资料。')
        return response({'result': result})

    async def profile_candidate(self, request):
        identifier = self.number(request.match_info['candidate_id'])
        data = await self.body(request) if request.method == 'POST' else None
        scope = {key: self.number(request.query[key]) for key in ('opportunity_id', 'contact_id') if request.query.get(key)}
        async with self.lock:
            result = (self.profile_intelligence.decide(self.owner, identifier, data) if data is not None
                      else self.profile_intelligence.preview_candidate(self.owner, identifier, scope) if scope
                      else self.profile_intelligence.get_candidate(self.owner, identifier))
            if result is None:
                raise KeyError()
            if data is not None and data.get('decision') == 'confirm' and self.coaching:
                self.coaching.schedule(self.owner, result['customer_id'])
        return response({'candidate': result})

    async def research_workspace_view(self, request):
        identifier = self.number(request.match_info['id'])
        async with self.lock:
            if request.method == 'GET':
                return response(self.research_workspace.list_runs(self.owner, identifier))
            data = await self.body(request)
            result = self.research_workspace.create(self.owner, identifier, data)
        return response(result)

    def queue_automatic_research(self, owner, customer_id):
        # Automatic discovery and explicit research share the same task lease.
        with self.crm._lock:
            settings = self.crm._db.execute(
                'SELECT research_attempts FROM crm_profile_customer_settings WHERE owner=? AND customer_id=?',
                (owner, customer_id)).fetchone()
            if settings is not None and settings['research_attempts'] >= 5:
                return {'exhausted': True, 'message': '公开研究多次未完成，请到研究准备稿手动重试。'}
        request_id = f'profile-auto:{customer_id}:{int(self.clock() // 300)}'
        return self.research_workspace.queue_automatic(owner, customer_id, request_id)

    async def research_run(self, request):
        identifier = self.number(request.match_info['run_id'])
        async with self.lock:
            if request.method == 'PATCH':
                run = self.research_workspace.edit_draft(self.owner, identifier, await self.body(request))
            else:
                run = self.research_workspace.get(self.owner, identifier)
            if run is None:
                raise KeyError()
        return response({'run': run})

    async def research_confirm(self, request):
        data = await self.body(request)
        async with self.lock:
            result = self.research_workspace.confirm(self.owner, self.number(request.match_info['run_id']), data)
            if self.coaching and any(item.get('status') in ('confirmed', 'already_confirmed')
                                     for item in result['results']):
                self.coaching.schedule(self.owner, result['run']['customer_id'])
        return response(result)

    async def research_control(self, request):
        data = await self.body(request)
        method = self.research_workspace.retry if request.match_info['operation'] == 'retry' else self.research_workspace.cancel
        async with self.lock:
            run = method(self.owner, self.number(request.match_info['run_id']), data)
        return response({'run': run})

    @staticmethod
    def research_goal(text):
        # Only explicit requests enter research. Quoted customer speech stays a record.
        request = re.match(r'^(?:请你?|麻烦你?|能不能|能否|可以|帮我|给我|替我|我想|我要|我希望|想要|了解|研究|检索|搜索|查一查|查一下|查下)', text)
        if not request:
            return False
        return bool(re.search(r'画像|单位背景|公司背景|客户背景|公开资料|采购线索|研究|检索|搜索|(?:了解|查一下|查一查|查下).*(?:单位|公司|集团|银行|医院)', text))

    async def secretary_goal_preview(self, request):
        return response(await self.secretary_goals.preview(self.owner, await self.body(request)))

    async def public_page_preview(self, request):
        data = await self.body(request)
        if set(data) != {'url'}:
            raise ValueError('请只提交要读取的公开网页地址。')
        return response({'source': await self.public_pages.preview(data['url'])})

    async def progress_workspace_view(self, request):
        async with self.lock:
            if request.method == 'POST':
                return response({'run': self.progress_workspace.create(self.owner, await self.body(request))}, 201)
            scope_filter = {key: self.number(request.query[key]) if request.query[key] else None
                for key in ('customer_id', 'contact_id', 'opportunity_id', 'source_record_id', 'record_id') if key in request.query}
            return response(self.progress_workspace.list_runs(self.owner, scope_filter=scope_filter,
                kind=request.query.get('kind'), **self.paging(request, 100)))

    async def progress_action_candidates(self, request):
        scope = {key: self.number(request.query[key]) for key in
                 ('customer_id', 'contact_id', 'opportunity_id') if request.query.get(key)}
        async with self.lock:
            return response(self.progress_workspace.target_candidates(self.owner, scope))

    async def progress_run_view(self, request):
        identifier = self.number(request.match_info['run_id'])
        async with self.lock:
            run = (self.progress_workspace.edit_draft(self.owner, identifier, await self.body(request))
                   if request.method == 'PATCH' else self.progress_workspace.get(self.owner, identifier))
        return response({'run': run})

    async def progress_run_confirm(self, request):
        async with self.lock:
            return response(self.progress_workspace.confirm(self.owner,
                self.number(request.match_info['run_id']), await self.body(request)))

    async def progress_run_control(self, request):
        method = getattr(self.progress_workspace, request.match_info['operation'])
        async with self.lock:
            run = method(self.owner, self.number(request.match_info['run_id']), await self.body(request))
        return response({'run': run})

    async def exchange_workspace_view(self, request):
        kind, identifier = request.match_info['source_type'], self.number(request.match_info['source_id'])
        async with self.lock:
            workspace = (self.exchange_workspace.edit_draft(self.owner, kind, identifier, await self.body(request))
                         if request.method == 'PATCH' else self.exchange_workspace.get(self.owner, kind, identifier))
        return response({'workspace': workspace})

    async def exchange_workspace_prepare(self, request):
        # The service snapshots and validates sources itself; model work must
        # not block other captures or reminder operations on the shared lock.
        workspace = await self.exchange_workspace.prepare(self.owner, request.match_info['source_type'],
            self.number(request.match_info['source_id']), await self.body(request))
        return response({'workspace': workspace})

    async def exchange_workspace_confirm(self, request):
        async with self.lock:
            result = self.exchange_workspace.confirm(self.owner, request.match_info['source_type'],
                self.number(request.match_info['source_id']), await self.body(request))
        return response(result)

    async def exchange_workspace_finish_review(self, request):
        async with self.lock:
            workspace = self.exchange_workspace.finish_review(self.owner,
                request.match_info['source_type'], self.number(request.match_info['source_id']),
                await self.body(request))
        return response({'workspace': workspace})

    async def project_fact(self, request):
        data = await self.body(request)
        async with self.lock:
            result = self.profile_intelligence.save_project_fact(self.owner, self.number(request.match_info['id']),
                self.number(request.match_info['opp_id']), data)
        return response(result)

    async def profile_question_adopt(self, request):
        data = await self.body(request)
        key = data.pop('key', None)
        if not isinstance(key, str) or not key:
            raise ValueError('请刷新后采纳当前探索问题。')
        async with self.lock:
            result = self.profile_intelligence.adopt_question(self.owner, self.number(request.match_info['id']), key, data)
        return response(result)

    async def customer_profile(self, request):
        async with self.lock:
            profile = self.crm.profile(self.owner, self.number(request.match_info['id']))
            if profile is None:
                raise KeyError()
            profile = self.profile_intelligence.enrich_profile(self.owner, profile)
        return response(profile)

    async def customer_fact(self, request):
        data = await self.body(request)
        identifier = self.number(request.match_info['id'])
        async with self.lock:
            self.crm.save_fact(self.owner, identifier, data, self.clock())
            profile = self.crm.profile(self.owner, identifier)
            if self.coaching:
                self.coaching.schedule(self.owner, identifier)
        return response({'profile': profile})

    async def customer_contact(self, request):
        data = await self.body(request)
        identifier = self.number(request.match_info['id'])
        async with self.lock:
            if request.method == 'PATCH':
                contact = self.crm.update_contact(self.owner, identifier,
                    self.number(request.match_info['contact_id']), data, self.clock())
            else:
                contact = self.crm.create_contact(self.owner, identifier, data, self.clock())
            if self.coaching:
                self.coaching.schedule(self.owner, identifier)
        return response({'contact': contact}, 201 if request.method == 'POST' else 200)

    async def customer_command(self, request):
        data = await self.body(request)
        original_transcript = data.pop('original_transcript', None)
        if original_transcript is not None and (not isinstance(original_transcript, str)
                or len(original_transcript) > 20000 or '\x00' in original_transcript):
            raise ValueError('原始转写需要为最多20000字的文本')
        if 'text' not in data or set(data) - {'text', 'customer_id', 'category'}:
            raise ValueError('请提交要整理的文字和可选的客户编号。')
        if 'category' in data:
            resolve_category(data['category'], data['text'], data.get('customer_id'))
        if self.customer_service is None:
            return response({'error': '客户语音整理尚未启用，可先手动添加资料。'}, 503)
        try:
            selected = {}
            if 'customer_id' in data:
                selected['selected_customer_id'] = self.number(data['customer_id']) if data['customer_id'] is not None else None
            if 'category' in data:
                selected['category'] = data['category']
            result = await self.customer_service.handle(self.owner, 'web-customer:' + secrets.token_hex(16),
                                                        data['text'], source='web', force=True, **selected)
        except (ValueError, KeyError):
            raise
        except Exception:
            LOG.warning('web_customer_command_failed')
            return response({'error': '这次整理未完成，原话已保存在待整理，可稍后重试。'}, 502)
        if 'category' in data and result.get('record_id'):
            async with self.lock:
                result['record'] = self.crm.get_record(self.owner, result['record_id'])
        if original_transcript is not None and result.get('record_id'):
            async with self.lock:
                self.crm.save_transcript(self.owner,result['record_id'],original_transcript,data['text'],self.clock())
        if self.exchange_records and result.get('record_id'):
            async with self.lock:
                result['visit_ref']=self.exchange_records.auto_archive(self.owner,result['record_id'])
        return response(result)

    async def reinterpret(self, request):
        data = await self.body(request)
        expected_updated_at = data.pop('expected_updated_at', None)
        if set(data) - {'text', 'content', 'title', 'customer_id', 'status', 'kind', 'category', 'mode', 'remind_at', 'duration_minutes'}:
            raise ValueError('重新整理的字段无效。')
        if self.customer_service is None:
            return response({'error': '重新理解暂未启用，原记录可直接编辑。'}, 503)
        identifier = self.number(request.match_info['id'])
        async with self.lock:
            material_ref = self.materials.source_for_record(self.owner, identifier) if self.materials else None
        if material_ref:
            return response({'error': '这条记录来自完整材料，请在材料页修正转写并重新整理；事项名称和提醒可直接手动修改。',
                             'material_ref': material_ref}, 409)
        mode = data.get('mode', 'note_only')
        if mode not in ('note_only', 'sync_reminder'):
            raise ValueError('请选择修改记录或同时拟修改提醒。')
        selected = {}
        if 'customer_id' in data:
            selected['selected_customer_id'] = self.number(data['customer_id']) if data['customer_id'] is not None else None
        async with self.lock:
            record = self.crm.get_record(self.owner, identifier)
            if record is None:
                raise KeyError()
            previous_customer = record.get('customer_id')
            fields = {key: data[key] for key in ('title', 'content', 'customer_id', 'status', 'kind', 'category') if key in data}
            if 'text' in data:
                if 'content' in fields and fields['content'] != data['text']:
                    raise ValueError('修正文字与记录内容不一致。')
                fields['content'] = data['text']
            content = fields.get('content', record['content'])
            if not isinstance(content, str) or not content.strip() or len(content) > 6000:
                raise ValueError('重新整理的文字需要为 1 至 6000 字。')
            prior = self.crm.record_detail(self.owner, identifier)
            prior_task = prior.get('task')
            if mode == 'sync_reminder' and prior_task and prior_task['status'] == 'pending' and fields.get('status') != 'done':
                if len(fields.get('title', record['title']).strip()) > 120:
                    raise ValueError('需要同步提醒的事项标题请控制在 120 字以内。')
                if 'remind_at' in data:
                    self._validate_schedule(data['remind_at'], data.get('duration_minutes', prior_task['duration_minutes']))
                elif 'duration_minutes' in data:
                    if type(data['duration_minutes']) is not int or not 5 <= data['duration_minutes'] <= 720:
                        raise ValueError('预计用时需要是 5 至 720 分钟。')
            if fields or expected_updated_at is not None:
                from .crm import RecordConflict
                try:
                    record = self.crm.update_record(self.owner, identifier, fields, self.clock(),
                        expected_updated_at=expected_updated_at)
                except RecordConflict as error:
                    return response({'error': str(error)}, 409)
            if fields:
                self.crm.stale_record_drafts(self.owner, identifier, self.clock())
                if fields.get('status') == 'done' and record.get('task_status') == 'pending':
                    self.store.execute(self.owner, 'web:' + secrets.token_hex(16),
                                       {'action': 'complete', 'task_id': record['task_id']}, self.clock())
            detail = self.crm.record_detail(self.owner, identifier)
            active = detail.get('task') if detail.get('task') and detail['task']['status'] == 'pending' else None
            can_change = active and mode == 'sync_reminder' and (data.get('remind_at', active['remind_at']) or 0) > self.clock() + 5
            if can_change:
                when = data.get('remind_at', active['remind_at'])
                message = self._prepare_schedule(identifier, record, when,
                    data.get('duration_minutes', active['duration_minutes']), active=active)
        try:
            result = await self.customer_service.handle(self.owner, 'reinterpret:' + secrets.token_hex(16),
                record['content'], source='web', force=True, record_id=identifier, **selected)
            if can_change:
                result.update(reminder_change=self.crm.record_detail(self.owner, identifier).get('proposal'),
                              message=result['message'] + '\n' + message)
            elif mode == 'sync_reminder' and result.get('active_reminders'):
                result.update(needs_reminder_review=True,
                    warning='记录已保存；交流纪要关联的生效提醒需要逐条核对，请打开对应待办修改后确认。本次没有自动修改这些提醒。')
            if self.coaching and previous_customer and previous_customer != record.get('customer_id'):
                self.coaching.schedule(self.owner, previous_customer)
        except (ValueError, KeyError):
            raise
        except Exception:
            LOG.warning('web_reinterpret_failed')
            return response({'error': '重新理解暂未完成，修正后的文字已保存，可稍后重试。'}, 502)
        return response(result)

    async def coaching_list(self, request):
        async with self.lock:
            result = self.coaching.list_views(self.owner) if self.coaching else {'items': [], 'generating': False}
        return response(result)

    async def customer_coaching(self, request):
        if self.coaching is None:
            return response({'recommendation': None, 'generating': False, 'error': '推进建议暂未启用。'})
        identifier = self.number(request.match_info['id'])
        if request.method == 'POST':
            if await self.body(request):
                raise ValueError('生成建议无需提交其他字段。')
        async with self.lock:
            if request.method == 'POST':
                self.coaching.schedule(self.owner, identifier)
            result = self.coaching.view(self.owner, identifier)
        return response(result, 202 if request.method == 'POST' else 200)

    async def adopt_coaching(self, request):
        if await self.body(request):
            raise ValueError('采纳建议无需提交其他字段。')
        if self.coaching is None:
            return response({'error': '推进建议暂未启用。'}, 503)
        async with self.lock:
            record = self.coaching.adopt(self.owner, self.number(request.match_info['id']),
                                         self.number(request.match_info['version']), self.number(request.match_info['index']))
        return response({'record': record})

    async def coaching_feedback(self, request):
        data = await self.body(request)
        if self.coaching is None:
            return response({'error': '推进建议暂未启用。'}, 503)
        identifier = self.number(request.match_info['id'])
        async with self.lock:
            self.coaching.feedback(self.owner, identifier, self.number(data.get('version')),
                                   self.number(data.get('index')), data.get('status'), data.get('note', ''))
            self.coaching.schedule(self.owner, identifier)
            result = self.coaching.view(self.owner, identifier)
        result['message'] = ('已记录完成反馈；已采纳的跟进事项与关联提醒一并完成。' if data.get('status') == 'completed'
                             else '反馈已保存，将用于下一次建议；已确认的提醒继续有效，需停止或改期请打开跟进事项处理。')
        return response(result)

    async def audio_capabilities(self, request):
        return response(self.audio.capabilities())

    async def voice_settings(self, request):
        async with self.lock:
            result = self.audio.save_settings(self.owner, await self.body(request), self.clock()) if request.method == 'PATCH' else self.audio.settings(self.owner)
        return response(result)

    async def transcribe_audio(self, request):
        from .audio import AudioUnavailable
        if not self.audio.capabilities()['can_transcribe']:
            return response({'error': self.audio.capabilities()['reason']}, 503)
        reader = await request.multipart()
        part = await reader.next()
        if part is None or part.name != 'file' or not part.filename:
            raise ValueError('请选择一个音频文件。')
        raw = bytearray()
        while chunk := await part.read_chunk():
            raw.extend(chunk)
            if len(raw) > self.audio.MAX_BYTES:
                return response({'error': '音频不能超过 6 MB，请拆分较短片段。'}, 413)
        if await reader.next() is not None:
            raise ValueError('每次只能提交一个音频文件。')
        mime = part.headers.get('Content-Type', 'application/octet-stream')
        if mime == 'application/octet-stream':
            import mimetypes
            mime = mimetypes.guess_type(part.filename)[0] or mime
        try:
            result = await self.audio.transcribe(self.owner, bytes(raw), mime)
        except AudioUnavailable as error:
            return response({'error': str(error)}, 502)
        return response(result)

    async def customer_drafts(self, request):
        async with self.lock:
            result = self.crm.list_customer_drafts(self.owner, status=request.query.get('status', 'pending'), **self.paging(request))
        return response(result)

    async def customer_draft(self, request):
        async with self.lock:
            draft = self.crm.get_customer_draft(self.owner, self.number(request.match_info['id']))
            if draft is None:
                raise KeyError()
        return response({'draft': draft})

    async def customer_decide(self, request):
        data = await self.body(request)
        if data:
            raise ValueError('确认时无需提交其他字段。')
        if self.customer_service is None:
            return response({'error': '客户资料确认服务尚未启用。'}, 503)
        async with self.lock:
            result = self.customer_service.decide(self.owner, self.number(request.match_info['id']),
                                                  request.match_info['decision'] == 'confirm')
            draft=self.crm.get_customer_draft(self.owner,self.number(request.match_info['id']))
            if self.exchange_records and draft and draft.get('status')=='confirmed' and draft.get('source_record_id'):
                result['visit_ref']=self.exchange_records.auto_archive(self.owner,draft['source_record_id'])
        return response(result)

    async def records(self, request):
        customer = request.query.get('customer_id')
        async with self.lock:
            work_queue = request.query.get('work_queue')
            if work_queue is not None:
                if any(request.query.get(key) for key in ('queue', 'status', 'kind', 'category')):
                    raise ValueError('工作队列不能同时使用其他记录分类，请先退出队列')
                from .overview import OverviewService
                data = OverviewService(self.crm, self.sales_workspace, self.review_queue, clock=self.clock).queue(
                    self.owner, work_queue, q=request.query.get('q', ''),
                    customer_id=self.number(customer) if customer else None, **self.paging(request, 20))
            else:
                data = self.crm.list_records(self.owner, q=request.query.get('q', ''), status=request.query.get('status', ''),
                                        customer_id=self.number(customer) if customer else None,
                                        kind=request.query.get('kind', ''), queue=request.query.get('queue', ''),
                                        category=request.query.get('category', ''), **self.paging(request))
            ids=[item['id'] for item in data['items']]
            if ids:
                rows=self.crm._db.execute('SELECT id,record_id,status,error,purpose,updated_at FROM crm_captures WHERE owner=? AND record_id IN ('+','.join('?' for _ in ids)+')',[self.owner,*ids]).fetchall()
                captures={row['record_id']:{key:row[key] for key in ('id','status','error','purpose','updated_at')} for row in rows}
                for item in data['items']:item['capture']=captures.get(item['id'])
                links=self.crm._db.execute("SELECT l.*,o.name AS opportunity_name,o.archived FROM crm_opportunity_links l JOIN crm_opportunities o ON o.owner=l.owner AND o.id=l.opportunity_id AND o.customer_id=l.customer_id WHERE l.owner=? AND l.entity_type='record' AND l.entity_id IN ("+','.join('?' for _ in ids)+')',[self.owner,*ids]).fetchall() if work_queue is None else []
                by_id={row['entity_id']:row for row in links}
                for item in data['items']:
                    link=by_id.get(item['id'])
                    if link and not link['archived'] and link['customer_id']==item['customer_id'] and link['source_snapshot']==self.sales_workspace._entity_snapshot(item):
                        item.update(opportunity_id=link['opportunity_id'],opportunity_name=link['opportunity_name'])
        return response(data)

    def require_materials(self):
        if self.materials is None:
            raise ValueError('录音与材料入口尚未启用。')
        return self.materials

    async def material_capabilities(self, request):
        connector = getattr(self.materials, 'connector', None)
        return response({'configured': bool(connector and connector.configured), 'provider': 'listen_note',
                         'categories': CATEGORIES, 'limits': {'manual_text': 500000, 'title': 100},
                         'discovery': False, 'audio_playback': False,
                         'document_upload': {'enabled': self.document_files is not None, 'max_bytes':20*1024*1024,
                             'formats':['pdf','docx','pptx','xlsx','txt','md','csv','png','jpg','jpeg','doc','ppt','xls'],
                             'note':'图片及扫描件保留原件，正文未读出时需补充文字。'},
                         'note': '按聆记中的完整标题读取可访问的最新同名材料；可导入已转写文字。'})

    async def document_upload(self, request):
        from .document_attachments import MAX_BYTES
        if not self.document_files:
            raise ValueError('材料入口尚未启用。')
        if request.content_length and request.content_length > MAX_BYTES + 16384:
            return response({'error':'每份附件不能超过20MB。'}, 413)
        reader = await request.multipart()
        request_id, name, raw = None, None, None
        while (part := await reader.next()) is not None:
            if part.name == 'request_id' and request_id is None and not part.filename:
                value = bytearray()
                while chunk := await part.read_chunk():
                    value.extend(chunk)
                    if len(value) > 800:
                        raise ValueError('上传标识过长。')
                request_id = bytes(value).decode('utf-8')
            elif part.name == 'file' and part.filename and raw is None:
                name, value = part.filename, bytearray()
                while chunk := await part.read_chunk():
                    value.extend(chunk)
                    if len(value) > MAX_BYTES:
                        return response({'error':'每份附件不能超过20MB。'}, 413)
                raw = bytes(value)
            else:
                raise ValueError('每次上传一份附件和一个上传标识。')
        if not request_id or not name or not raw:
            raise ValueError('请选择文件后重新上传。')
        async with self.lock:
            value = self.document_files.upload(self.owner, name, raw, request_id, parsed={
                'text':'','parse_status':'parsing','parse_error':'附件原件已保存，正在读取正文。','truncated':False})
        return response(value, 202)

    async def parse_document_one(self):
        if not self.document_files:
            return False
        from .document_attachments import extract_document
        async with self.lock:
            with self.crm._lock:
                row = self.crm._db.execute("SELECT material_id FROM crm_document_files WHERE owner=? AND parse_status='parsing' ORDER BY created_at,material_id LIMIT 1",(self.owner,)).fetchone()
            if not row:
                return False
            file = self.document_files.get(self.owner, row['material_id'])
        try:
            parsed = await asyncio.wait_for(asyncio.to_thread(extract_document, file['filename'], file['content']), 30)
        except asyncio.TimeoutError:
            parsed = {'text':'','parse_status':'needs_text','parse_error':'原件已保留，正文读取较久，请补充重点文字。','truncated':False}
        async with self.lock:
            self.document_files.finish_parse(self.owner, file['material_id'], parsed)
        return True

    async def document_download(self, request):
        from urllib.parse import quote
        if not self.document_files:
            raise KeyError()
        async with self.lock:
            file = self.document_files.get(self.owner, self.number(request.match_info['id']))
        return web.Response(body=file['content'], content_type='application/octet-stream', headers={
            'Content-Disposition':"attachment; filename=material; filename*=UTF-8''"+quote(file['filename'], safe=''),
            'Cache-Control':'no-store'})

    async def material_list(self, request):
        service = self.require_materials()
        async with self.lock:
            data = service.list(self.owner, q=request.query.get('q', ''), category=request.query.get('category', ''),
                                status=request.query.get('status', ''), **self.paging(request))
            if self.document_files and data['items']:
                identifiers = [item['id'] for item in data['items']]
                with self.crm._lock:
                    documents = self.crm._db.execute(
                        'SELECT material_id,filename,size,parse_status,parse_error,truncated '
                        'FROM crm_document_files WHERE owner=? AND material_id IN (' +
                        ','.join('?' for _ in identifiers) + ')', [self.owner, *identifiers]).fetchall()
                metadata = {row['material_id']: self.document_files.public(row) for row in documents}
                for item in data['items']:
                    if item['id'] in metadata:
                        item['document'] = metadata[item['id']]
        return response(data)

    async def material_create(self, request):
        data = await self.body(request)
        async with self.lock:
            material = self.require_materials().enqueue(self.owner, data)
        return response({'material': material}, 202)

    async def material_detail(self, request):
        async with self.lock:
            detail = self.require_materials().detail(self.owner, self.number(request.match_info['id']))
            if detail is None:
                raise KeyError()
            detail['visit_ref'] = self.visits.source_for_material(self.owner, detail['material']['id']) if self.visits else None
            if self.document_files:
                try: detail['document'] = self.document_files.public(self.document_files.get(self.owner, detail['material']['id']))
                except KeyError: pass
            self._annotate_review_actions(detail, 'material', detail['material']['id'])
            scopes = self.visits.material_action_scopes(self.owner, detail['material']['id']) if self.visits else None
            if scopes:
                for action in (detail.get('analysis') or {}).get('actions', []):
                    if action['id'] in scopes:
                        action.update(scopes[action['id']])
        return response(detail)

    def require_visits(self):
        if self.visits is None:
            raise ValueError('客户交流入口尚未启用。')
        return self.visits

    async def visit_list(self, request):
        paging = self.paging(request)
        page, size = paging['page'], paging['page_size']
        customer = request.query.get('customer_id')
        async with self.lock:
            data = self.require_visits().list(self.owner, q=request.query.get('q', ''),
                customer_id=self.number(customer) if customer else None, limit=size, offset=(page - 1) * size)
        return response({**data, 'page': page, 'pages': max(1, (data['total'] + size - 1) // size)})

    async def visit_create(self, request):
        data = await self.body(request)
        source_id = data.pop('request_key', None)
        async with self.lock:
            visit = self.require_visits().create(self.owner, data, source_id=source_id)
        return response({'visit': visit}, 201)

    async def visit_detail(self, request):
        async with self.lock:
            detail = self.require_visits().detail(self.owner, self.number(request.match_info['id']))
            self._annotate_review_actions(detail, 'visit', detail['visit']['id'])
        return response(detail)

    async def visit_update(self, request):
        data = await self.body(request)
        async with self.lock:
            visit = self.require_visits().update(self.owner, self.number(request.match_info['id']), data)
        return response({'visit': visit})

    async def visit_material(self, request):
        data = await self.body(request)
        source_id = data.pop('request_key', None)
        async with self.lock:
            result = self.require_visits().add_material(self.owner, self.number(request.match_info['id']),
                                                        data, source_id=source_id)
        return response(result, 202)

    async def visit_adopt(self, request):
        data = await self.body(request)
        if 'revision' not in data or set(data) - {'revision', 'project_scope_revision', 'opportunity_id', 'confirm_single_action'}:
            raise ValueError('请核对当前交流版本后采纳。')
        async with self.lock:
            result = self.require_visits().adopt(self.owner, self.number(request.match_info['id']),
                                                request.match_info['key'], data['revision'],
                **{key: value for key, value in data.items() if key != 'revision'})
            result['project_inheritance']=self.sales_workspace.inherit_record_link(self.owner,
                self.number(request.match_info['id']),result['record']['id'],entity_type='visit')
            if result['project_inheritance'].get('warning'):result['warning']=result['project_inheritance']['warning']
            from .matter_adoption import attach_adoption
            result['matter_attribution']=attach_adoption(self.matters,self.owner,'visit',self.number(request.match_info['id']),result['record']['id'])
        return response(result)

    async def visit_merge(self, request):
        data = await self.body(request)
        if set(data) != {'keys', 'revision'}:
            raise ValueError('请选中当前交流中的重复事项并核对版本。')
        async with self.lock:
            detail = self.require_visits().merge_actions(self.owner, self.number(request.match_info['id']),
                                                        data['keys'], data['revision'])
        return response(detail)

    async def material_update(self, request):
        data = await self.body(request)
        async with self.lock:
            if 'text' in data and self.document_files:
                try:
                    document = self.document_files.get(self.owner, self.number(request.match_info['id']))
                except KeyError:
                    document = None
                if document and document['parse_status'] != 'ready' and data['text'] == self.require_materials().detail(self.owner, document['material_id'])['text']:
                    raise ValueError('这份附件正文尚未读出，请粘贴真实材料文字；原件已保留。')
            material = self.require_materials().update(self.owner, self.number(request.match_info['id']), data)
            if 'text' in data and self.document_files:
                self.document_files.text_corrected(self.owner, material['id'])
        return response({'material': material})

    async def material_retry(self, request):
        data = await self.body(request)
        if set(data) != {'revision'}:
            raise ValueError('请核对当前材料版本后重试。')
        async with self.lock:
            material = self.require_materials().retry(self.owner, self.number(request.match_info['id']), data['revision'])
        return response({'material': material}, 202)

    async def material_adopt(self, request):
        data = await self.body(request)
        if 'revision' not in data or set(data) - {'revision', 'visit_revision', 'project_scope_revision', 'opportunity_id', 'confirm_single_action'}:
            raise ValueError('请核对当前材料版本后采纳。')
        async with self.lock:
            result = self.require_materials().adopt(self.owner, self.number(request.match_info['id']),
                                                  self.number(request.match_info['action_id']), data['revision'],
                expected_visit_revision=data.get('visit_revision'),
                **{key: value for key, value in data.items() if key not in ('revision', 'visit_revision')})
            result['project_inheritance']=self.sales_workspace.inherit_record_link(self.owner,
                self.number(request.match_info['id']),result['record']['id'],entity_type='material')
            if result['project_inheritance'].get('warning'):result['warning']=result['project_inheritance']['warning']
            from .matter_adoption import attach_adoption
            result['matter_attribution']=attach_adoption(self.matters,self.owner,'material',self.number(request.match_info['id']),result['record']['id'])
        return response(result)

    async def create_record(self, request):
        data = await self.body(request)
        async with self.lock:
            record = self.crm.create_record(self.owner, data, self.clock())
            visit_ref=self.exchange_records.auto_archive(self.owner,record['id']) if self.exchange_records else None
            if self.coaching and record.get('customer_id'):
                self.coaching.schedule(self.owner, record['customer_id'])
        return response({'record': record,'visit_ref':visit_ref}, 201)

    async def record(self, request):
        async with self.lock:
            detail = self.crm.record_detail(self.owner, self.number(request.match_info['id']))
            if detail is None:
                raise KeyError()
            detail['active_reminders'] = self._active_reminders(detail['record']['id'])
            detail['completion_effects'] = self.sales_workspace.completion_effects(self.owner, detail['record']['id'])
            detail['completion_snapshot'] = detail['completion_effects']['snapshot']
            detail['schedule_snapshot'] = self._schedule_snapshot(detail)
            detail['capture'] = self.captures.for_record(self.owner,detail['record']['id'])
            detail['secretary_plan'] = self.secretary_flow.for_record(self.owner,detail['record']['id'])
            detail['matters'] = self.matters.resolve(self.owner, 'record', detail['record']['id'])['items']
            timeline = getattr(self, 'timeline', None)
            if timeline and detail['record']['kind'] != 'action':
                event = timeline.get_event(self.owner, 'record:' + str(detail['record']['id']))
                detail['record_nature'] = {key: event[key] for key in ('kind', 'needs_review')}
            from .action_provenance import action_origins
            detail['action_origins'] = action_origins(self.crm, self.owner, detail['record']['id'],
                workspace=self.sales_workspace, timeline=timeline)
            link=self.crm._db.execute("SELECT l.*,o.name AS opportunity_name,o.archived FROM crm_opportunity_links l JOIN crm_opportunities o ON o.owner=l.owner AND o.id=l.opportunity_id AND o.customer_id=l.customer_id WHERE l.owner=? AND l.entity_type='record' AND l.entity_id=?",(self.owner,detail['record']['id'])).fetchone()
            if link:
                valid=not link['archived'] and link['customer_id']==detail['record']['customer_id'] and link['source_snapshot']==self.sales_workspace._entity_snapshot(detail['record'])
                detail['record']['project_link_stale']=not valid
                reason = ('归属单位已变化，请核对原项目关联' if link['customer_id'] != detail['record']['customer_id']
                          else '原关联项目已归档，请核对当前项目' if link['archived']
                          else '来源内容或归属版本已变化，请重新核对项目' if not valid else None)
                detail['record']['project_link_stale_reason'] = reason
                detail['project_link_stale_reason'] = reason
                if valid:detail['record'].update(opportunity_id=link['opportunity_id'],opportunity_name=link['opportunity_name'])
            detail['material_ref'] = self.materials.source_for_record(self.owner, detail['record']['id']) if self.materials else None
            detail['visit_ref'] = (self.visits.source_for_material(self.owner, detail['material_ref']['id'])
                                   if self.visits and detail['material_ref'] else None)
            if self.exchange_records and not detail['visit_ref']:
                detail['visit_ref']=self.exchange_records.reference(self.owner,detail['record']['id'])
            self._annotate_review_actions(detail, 'record', detail['record']['id'])
        return response(detail)

    def _annotate_review_actions(self, detail, source_type, identifier):
        candidates = self.review_queue.all_items(self.owner)
        def decorate(actions, kind, source_id):
            for action in actions:
                candidate = next((item for item in candidates if item['source_type']==kind and
                    (item.get('record_id') if kind=='record' else item.get('material_id') if kind=='material' else item.get('visit_id'))==source_id
                    and (item.get('action_key')==action.get('key') if kind=='visit' else item.get('action_id')==action.get('id'))), None)
                if candidate is None and kind=='material':
                    candidate = next((item for item in candidates if item['source_type']=='visit' and any(
                        ref.get('material_id')==source_id and ref.get('action_id')==action.get('id')
                        for ref in item.get('references',[]))),None)
                if candidate:
                    action.update(review_key=candidate['key'],review_signature=candidate['signature'],
                        decision_state=candidate['decision_state'],blocked=candidate['blocked'],
                        review_reasons=candidate['review_reasons'],deferred_until=candidate.get('deferred_until'),
                        visit_revision=candidate.get('visit_revision'))
        decorate((detail.get('analysis') or {}).get('actions',[]) if source_type!='visit' else detail.get('actions',[]),source_type,identifier)
        for entry in detail.get('record_sources',[]):
            decorate((entry.get('analysis') or {}).get('actions',[]),'record',entry['record_id'])

    def _active_reminders(self, identifier):
        rows = []
        ids = [identifier] + [row[0] for row in self.crm._db.execute(
            'SELECT id FROM crm_records WHERE owner=? AND parent_record_id=?', (self.owner, identifier))]
        for record_id in ids:
            detail = self.crm.record_detail(self.owner, record_id)
            task = detail.get('task') if detail else None
            if task and task['status'] == 'pending' and all(item['id'] != task['id'] for item in rows):
                rows.append({**task, 'record_id': record_id, 'customer_name': detail['record'].get('customer_name')})
        return rows

    async def update_record(self, request):
        data = await self.body(request)
        expected_updated_at = data.pop('expected_updated_at', None)
        mode = data.pop('mode', 'note_only')
        if mode not in ('note_only', 'sync_reminder'):
            raise ValueError('请选择修改记录或同时拟修改提醒。')
        when, duration = data.pop('remind_at', None), data.pop('duration_minutes', None)
        extra = {}
        async with self.lock:
            previous = self.crm.get_record(self.owner, self.number(request.match_info['id']))
            if previous is None:
                raise KeyError()
            prior = self.crm.record_detail(self.owner, previous['id'])
            prior_task = prior.get('task')
            if mode == 'sync_reminder' and prior_task and prior_task['status'] == 'pending' and data.get('status') != 'done':
                if len(data.get('title', previous['title']).strip()) > 120:
                    raise ValueError('需要同步提醒的事项标题请控制在 120 字以内。')
                if when is not None:
                    self._validate_schedule(when, duration if duration is not None else prior_task['duration_minutes'])
                elif duration is not None and (type(duration) is not int or not 5 <= duration <= 720):
                    raise ValueError('预计用时需要是 5 至 720 分钟。')
            from .crm import RecordConflict
            try:
                record = self.crm.update_record(self.owner, self.number(request.match_info['id']), data, self.clock(),
                    expected_updated_at=expected_updated_at)
            except RecordConflict as error:
                return response({'error': str(error)}, 409)
            if data.get('status') == 'done' and record.get('task_status') == 'pending':
                self.store.execute(self.owner, 'web:' + secrets.token_hex(16),
                                   {'action': 'complete', 'task_id': record['task_id']}, self.clock())
                record = self.crm.get_record(self.owner, record['id'])
            detail = self.crm.record_detail(self.owner, record['id'])
            active = detail.get('task') if detail.get('task') and detail['task']['status'] == 'pending' else None
            if active:
                if mode == 'sync_reminder' and (when if when is not None else active['remind_at'] or 0) > self.clock() + 5:
                    message = self._prepare_schedule(record['id'], record, when if when is not None else active['remind_at'],
                        duration if duration is not None else active['duration_minutes'], active=active)
                    record = self.crm.get_record(self.owner, record['id'])
                    extra = {'message': message, 'reminder_change': self.crm.record_detail(self.owner, record['id'])['proposal']}
                else:
                    extra = {'active_reminder': {**active, 'record_id': record['id']},
                             'warning': '记录已保存；生效提醒仍按原标题和原时间执行。如需同步修改，请补充未来时间后确认变更。'}
            elif self._active_reminders(record['id']):
                extra = {'active_reminders': self._active_reminders(record['id']), 'needs_reminder_review': True,
                         'warning': '记录已保存；关联待办的提醒需要逐条核对，请打开对应待办修改后确认。'}
            if self.coaching and record.get('customer_id'):
                self.coaching.schedule(self.owner, record['customer_id'])
            if self.coaching and previous and previous.get('customer_id') and previous['customer_id'] != record.get('customer_id'):
                self.coaching.schedule(self.owner, previous['customer_id'])
        return response({'record': record, **extra})

    async def activity(self, request):
        data = await self.body(request)
        async with self.lock:
            activity = self.crm.add_activity(self.owner, self.number(request.match_info['id']), data.get('content'), self.clock())
            record = self.crm.get_record(self.owner, self.number(request.match_info['id']))
            if self.coaching and record.get('customer_id'):
                self.coaching.schedule(self.owner, record['customer_id'])
        return response({'activity': activity}, 201)

    async def organize_activity(self, request):
        data = await self.body(request)
        body = data.get('body', data.get('content'))
        if not isinstance(body, str) or not body.strip() or len(body) > 6000:
            raise ValueError('交流内容需要为 1 至 6000 字。')
        async with self.lock:
            parent = self.crm.get_record(self.owner, self.number(request.match_info['id']))
            if parent is None:
                raise KeyError()
            from .spoken_changes import CHANGE_INTENT
            if CHANGE_INTENT.search(body):
                result=self.spoken_changes.propose(self.owner,parent['id'],body,
                    data.get('request_id') or 'web-spoken:'+secrets.token_hex(16))
                return response(result)
            record = self.crm.create_record(self.owner, {'title': '后续交流：' + parent['title'][:100],
                'content': body.strip(), 'customer_id': parent['customer_id'], 'kind': 'note',
                'category':'conversation','parent_record_id': parent['id']}, self.clock())
            visit_ref=self.exchange_records.auto_archive(self.owner,record['id']) if self.exchange_records else None
        if self.customer_service:
            result = await self.customer_service.handle(self.owner, 'activity:' + secrets.token_hex(16),
                record['content'], source='web', force=True, selected_customer_id=record['customer_id'], record_id=record['id'])
            return response({**result, 'record': self.crm.get_record(self.owner, record['id']), 'visit_ref':visit_ref}, 201)
        return response({'record': record, 'visit_ref':visit_ref, 'message': '后续交流已独立保存，AI整理暂未启用。'}, 201)

    async def organize(self, request):
        await self.body(request)
        identifier = self.number(request.match_info['id'])
        async with self.lock:
            material_ref = self.materials.source_for_record(self.owner, identifier) if self.materials else None
            capture=self.captures.for_record(self.owner,identifier)
        if capture and capture['status'] in ('queued','processing'):
            return response({'error':'这条原话正在后台整理，完成后会自动更新。'},409)
        if material_ref:
            return response({'error': '请打开完整材料重新整理，摘要和摘录不能代替完整转写。', 'material_ref': material_ref}, 409)
        if identifier in self.organizing:
            return response({'error': '这条交流记录正在整理，请稍候。'}, 409)
        if self.organizer is None:
            return response({'error': '当前未启用 AI 整理，可以先手动补充跟进事项。'}, 503)
        from .crm import analysis_fingerprint
        async with self.lock:
            record = self.crm.get_record(self.owner, identifier)
            if record is None:
                raise KeyError()
            old = self.crm.get_analysis(self.owner, identifier)
            if old and not old.get('stale') and any(action.get('adopted_record_id') for action in old['actions']):
                return response({'error': '本次整理已采纳，请修改原记录后重整，或添加新的后续交流。'}, 409)
            if old and any(action.get('adopted_record_id') and self.crm.record_detail(self.owner, action['adopted_record_id']).get('task')
                           and self.crm.record_detail(self.owner, action['adopted_record_id'])['task']['status'] == 'pending'
                           for action in old['actions']):
                return response({'error': '已有生效提醒。请先核对原提醒，或保存为新的后续交流记录。'}, 409)
            customer = self.crm.get_customer(self.owner, record['customer_id']) if record['customer_id'] else None
            fingerprint = analysis_fingerprint(record)
            context = {'customer': customer or {}, 'record': {'title': record['title'], 'content': record['content']}}
            if customer:
                recent = self.crm.list_records(self.owner, customer_id=customer['id'], page_size=6)['items']
                context['recent_records'] = [{key: item[key] for key in ('title', 'content', 'status')}
                                             for item in recent if item['id'] != identifier][:5]
        self.organizing.add(identifier)
        try:
            try:
                analysis = await asyncio.wait_for(self.organizer.organize(
                    record['content'] or record['original_content'] or record['title'], self.clock(), context), timeout=90)
            except Exception:
                LOG.warning('crm_organize_failed')
                return response({'error': '这次自动整理未完成，原记录仍在，请稍后重试或手动添加跟进。'}, 502)
            analysis['input_fingerprint'] = fingerprint
            async with self.lock:
                result = self.crm.save_analysis(self.owner, identifier, analysis, self.clock())
                if self.coaching and customer:
                    self.coaching.schedule(self.owner, customer['id'])
                extra = self.customer_service.prepare_analysis_result(self.owner, identifier) if self.customer_service else {}
                project_warnings=[]
                for item in self.crm.get_analysis(self.owner,identifier)['actions']:
                    if item.get('adopted_record_id'):
                        inheritance=self.sales_workspace.inherit_record_link(self.owner,identifier,item['adopted_record_id'])
                        if inheritance.get('warning'):project_warnings.append(inheritance['warning'])
                if project_warnings:extra={**extra,'warning':' '.join(dict.fromkeys(project_warnings))}
            return response({'analysis': result, **extra})
        finally:
            self.organizing.discard(identifier)

    async def adopt(self, request):
        data = await self.body(request)
        if set(data)-{'visit_revision'}:
            raise ValueError('采纳字段无效，请核对当前交流版本')
        identifier = self.number(request.match_info['id'])
        action_id = self.number(request.match_info['action_id'])
        async with self.lock:
            record = self.crm.adopt_action(self.owner, identifier, action_id, self.clock(),
                expected_visit_revision=data.get('visit_revision'))
            capture=self.captures.for_record(self.owner,identifier)
            if capture and capture['purpose'] in ('action','schedule') and capture['record']['kind']=='action' and capture['record']['proposal_id'] is None:
                # Once a concrete action is adopted, the raw thought is its
                # source. Counting both as work would duplicate the same TODO.
                self.crm.update_record(self.owner,identifier,{'kind':'note'},self.clock())
            analysis = self.crm.get_analysis(self.owner, identifier)
            action = next((a for a in analysis['actions'] if a['id'] == action_id), None)
            from .action_contract import can_schedule
            terms = record.get('action_terms') or {}
            when = terms.get('execution_at') if action and can_schedule(terms) else None
            if when is not None and when > self.clock() + 5 and record['proposal_id'] is None:
                message = self.store.execute(self.owner, f'web-adopt:{identifier}:{action_id}:{analysis["version"]}',
                    {'action': 'propose', 'title': record['title'], 'remind_at': when,
                     'duration_minutes': terms.get('duration_minutes') or 30}, self.clock())
                match = re.match(r'^已整理，待你确认：P([0-9]+)', message)
                if match:
                    record = self.crm.link_proposal(self.owner, record['id'], int(match.group(1)), self.clock())
            inheritance=self.sales_workspace.inherit_record_link(self.owner,identifier,record['id'])
            from .matter_adoption import attach_adoption
            attribution=attach_adoption(self.matters,self.owner,'record',identifier,record['id'])
        return response({'record': record,'project_inheritance':inheritance,'matter_attribution':attribution,
            **({'warning':inheritance['warning']} if inheritance.get('warning') else {})})

    async def schedule(self, request):
        data = await self.body(request)
        if set(data)-{'expected_schedule_snapshot', 'title', 'remind_at', 'duration_minutes'}:
            raise ValueError('拟定日程字段无效')
        now = self.clock()
        when = _timestamp(data.get('remind_at'))
        duration = data.get('duration_minutes', 30)
        if not now + 5 < when < now + 10 * 366 * 86400:
            raise ValueError('请选择未来十年内的具体提醒时间。')
        if type(duration) is not int or not 5 <= duration <= 720:
            raise ValueError('预计用时需要是 5 至 720 分钟。')
        identifier = self.number(request.match_info['id'])
        async with self.lock:
            detail = self.crm.record_detail(self.owner, identifier)
            if detail is None:
                raise KeyError()
            self._require_schedule_snapshot(detail, data.get('expected_schedule_snapshot'))
            proposal, task = detail.get('proposal'), detail.get('task')
            if task and task['status'] == 'pending':
                message = self._prepare_schedule(identifier, {**detail['record'], 'title': data.get('title', detail['record']['title'])},
                                                 when, duration, active=task)
            elif proposal and proposal['status'] == 'pending':
                command = {'action': 'reschedule_proposal', 'proposal_id': proposal['id'],
                           'remind_at': when, 'duration_minutes': duration}
                message = self.store.execute(self.owner, 'web:' + secrets.token_hex(16), command, now)
                # The public edit has its own seen revision even on a frozen clock.
                self.crm._db.execute('UPDATE proposals SET updated_at=? WHERE owner=? AND id=?',
                    (max(now, proposal['updated_at']+.000001), self.owner, proposal['id']))
            else:
                message = self._prepare_schedule(identifier, detail['record'], when, duration)
            result = self.crm.record_detail(self.owner, identifier)
            if result['record']['status'] == 'done':
                self.crm.update_record(self.owner, identifier, {'status': 'following'}, now)
                result = self.crm.record_detail(self.owner, identifier)
            result['schedule_snapshot'] = self._schedule_snapshot(result)
        return response({**result, 'message': message})

    def _schedule_snapshot(self, detail):
        record = detail['record']
        link = self.crm._db.execute("SELECT * FROM crm_opportunity_links WHERE owner=? AND entity_type='record' AND entity_id=?",
                                    (self.owner, record['id'])).fetchone()
        project = self.crm._db.execute('SELECT id,customer_id,name,scope,archived,revision FROM crm_opportunities WHERE owner=? AND id=?',
            (self.owner, link['opportunity_id'])).fetchone() if link and link['opportunity_id'] else None
        value = {'owner': self.owner, 'record': {key: record.get(key) for key in
            ('id', 'title', 'content', 'kind', 'status', 'customer_id', 'updated_at', 'proposal_id', 'action_terms', 'terms_updated_at')},
            'task': detail.get('task'), 'proposal': detail.get('proposal'), 'project_link': dict(link) if link else None,
            'project': dict(project) if project else None}
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()

    def _require_schedule_snapshot(self, detail, expected):
        if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
            raise ValueError('缺少所见日程版本，请打开事项核对当前安排后再操作')
        if not hmac.compare_digest(expected, self._schedule_snapshot(detail)):
            raise ProgressConflict('事项或当前安排已有变化，未修改日程；请刷新核对后重新提交')

    def _prepare_schedule(self, identifier, record, when, duration, *, active=None):
        now = self.clock()
        when = self._validate_schedule(when, duration)
        command = {'action': 'propose_change' if active else 'propose', 'title': record['title'],
                   'remind_at': when, 'duration_minutes': duration}
        if active:
            command['task_id'] = active['id']
        message = self.store.execute(self.owner, 'web:' + secrets.token_hex(16), command, now)
        match = re.search(r'P([0-9]+)', message)
        if not match:
            raise ValueError(message)
        self.crm.link_proposal(self.owner, identifier, int(match.group(1)), now)
        return message

    def _validate_schedule(self, when, duration):
        now = self.clock()
        when = _timestamp(when)
        if not now + 5 < when < now + 10 * 366 * 86400:
            raise ValueError('请选择未来十年内的具体提醒时间。')
        if type(duration) is not int or not 5 <= duration <= 720:
            raise ValueError('预计用时需要是 5 至 720 分钟。')
        return when

    async def cancel_record(self, request):
        data = await self.body(request)
        if set(data)-{'expected_schedule_snapshot'}:
            raise ValueError('取消提醒只需提交所见日程版本。')
        async with self.lock:
            detail = self.crm.record_detail(self.owner, self.number(request.match_info['id']))
            if detail is None:
                raise KeyError()
            self._require_schedule_snapshot(detail, data.get('expected_schedule_snapshot'))
            if detail.get('task') and detail['task']['status'] == 'pending':
                message = self.store.execute(self.owner, 'web:' + secrets.token_hex(16),
                    {'action': 'cancel', 'task_id': detail['task']['id']}, self.clock())
            elif detail.get('proposal') and detail['proposal']['status'] == 'pending':
                message = self.store.execute(self.owner, 'web:' + secrets.token_hex(16),
                    {'action': 'reject', 'proposal_id': detail['proposal']['id']}, self.clock())
            else:
                message = '当前没有需要取消的提醒。'
            result = self.crm.record_detail(self.owner, detail['record']['id'])
            result['schedule_snapshot'] = self._schedule_snapshot(result)
        return response({**result, 'message': message})

    def _calendar_projects(self, items):
        from .overview import OverviewService
        ids = {item['record_id'] for item in items if item.get('record_id')}
        metadata = OverviewService(self.crm, self.sales_workspace, clock=self.clock).project_context(self.owner, ids) if ids else {}
        empty = {'opportunity_id': None, 'opportunity_name': None, 'opportunity_scope': None,
                 'project_link_stale': False, 'project_link_stale_reason': None}
        return [{**item, **metadata.get(item.get('record_id'), empty)} for item in items]

    def _proposal_failure(self, proposal):
        """Readable web corrections; the Store/voice reply remains unchanged."""
        when, now = proposal.get('remind_at'), self.clock()
        hint = '调整开始时间或预计用时，保存拟安排后再确认；旧安排在确认前保持。'
        if when is None:
            error = '请先补充具体提醒时间，再确认日程。'
        elif when <= now:
            error = '拟安排时间已过，请改为未来时间。'
        elif proposal.get('deadline_at') is not None and when + proposal['duration_minutes']*60 > proposal['deadline_at']:
            error = '按预计用时无法在截止时间前完成，请核对开始时间或用时。'
        else:
            end = when + proposal['duration_minutes']*60
            rows = [dict(row) for row in self.crm._db.execute(
                "SELECT * FROM tasks WHERE owner=? AND status='pending' AND remind_at<? AND remind_at+duration_minutes*60>? AND id!=? ORDER BY remind_at,id LIMIT 10",
                (self.owner, end, when, proposal.get('target_task_id') or -1))]
            conflicts = self._calendar_projects(self.crm._task_context(self.owner, rows))
            if conflicts:
                for item in conflicts:
                    item['task_id'] = item['id']
                    item['end_at'] = item['remind_at'] + item['duration_minutes']*60
                return {'error': '拟安排与已确认日程冲突，尚未生效。', 'conflicts': conflicts, 'correction_hint': hint}
            error = '原安排或提案状态已有变化，请刷新核对后重新拟定。'
        return {'error': error, 'conflicts': [], 'correction_hint': hint}

    async def next_visit(self, request):
        data = await self.body(request)
        if data.get('remind_at') is not None:
            when = _timestamp(data['remind_at'])
            if not self.clock() + 5 < when < self.clock() + 10 * 366 * 86400:
                raise ValueError('请选择未来十年内的具体提醒时间。')
            duration = data.get('duration_minutes', 30)
            if type(duration) is not int or not 5 <= duration <= 720:
                raise ValueError('预计用时需要是 5 至 720 分钟。')
        async with self.lock:
            parent = self.crm.get_record(self.owner, self.number(request.match_info['id']))
            if parent is None:
                raise KeyError()
            record = self.crm.create_record(self.owner, {'title': data.get('title', '下次拜访：' + parent['title'][:100]),
                'content': '来源记录：#' + str(parent['id']), 'customer_id': parent['customer_id'],
                'status': 'following', 'kind': 'action', 'parent_record_id': parent['id']}, self.clock())
            inheritance = self.sales_workspace.inherit_record_link(self.owner, parent['id'], record['id'])
            message = '已添加下次拜访，待你补充时间。'
            if data.get('remind_at') is not None:
                message = self._prepare_schedule(record['id'], record, data['remind_at'], data.get('duration_minutes', 30))
            result = self.crm.record_detail(self.owner, record['id'])
        result.update(message=message, project_inheritance=inheritance)
        if inheritance.get('warning'):
            result['warning'] = inheritance['warning']
        return response(result, 201)

    async def confirm(self, request):
        data = await self.body(request)
        identifier = self.number(request.match_info['id'])
        async with self.lock:
            detail = self.crm.record_detail(self.owner, identifier)
            if detail is None:
                raise KeyError()
            proposal = detail.get('proposal')
            if not proposal:
                return response({'error': '请先设置跟进时间，再确认提醒。'}, 409)
            if detail['record']['status'] == 'done':
                return response({'error': '这条事项已完成，请新建下次跟进。'}, 409)
            if (set(data) != {'proposal_id', 'updated_at'} or self.number(data.get('proposal_id')) != proposal['id']
                    or data.get('updated_at') != proposal['updated_at']):
                return response({'error': '待确认提案已经变化，请刷新后核对。'}, 409)
            message = self.store.execute(self.owner, 'web:' + secrets.token_hex(16),
                                         {'action': 'confirm', 'proposal_id': proposal['id']}, self.clock())
            result = self.crm.record_detail(self.owner, identifier)
            if result['proposal']['status'] != 'confirmed':
                return response(self._proposal_failure(result['proposal']), 409)
            if result['record']['status'] == 'unfiled':
                self.crm.update_record(self.owner, identifier, {'status': 'following'}, self.clock())
                result = self.crm.record_detail(self.owner, identifier)
        return response({**result, 'message': message})

    async def decide_proposal(self, request):
        data = await self.body(request)
        identifier = self.number(request.match_info['id'])
        async with self.lock:
            proposal = self.crm.get_proposal(self.owner, identifier)
            if proposal is None:
                raise KeyError()
            if set(data) != {'updated_at'} or data.get('updated_at') != proposal['updated_at']:
                return response({'error': '提案内容已经变化，请刷新后核对再确认或撤回。'}, 409)
            message = self.store.execute(self.owner, 'web:' + secrets.token_hex(16),
                {'action': request.match_info['decision'], 'proposal_id': identifier}, self.clock())
            proposal = self.crm.get_proposal(self.owner, identifier)
            expected = 'confirmed' if request.match_info['decision'] == 'confirm' else 'rejected'
            if proposal['status'] == 'confirmed':
                link = self.crm._db.execute('SELECT record_id FROM crm_record_proposals WHERE owner=? AND proposal_id=?',
                                            (self.owner, identifier)).fetchone()
                if link:
                    record = self.crm.get_record(self.owner, link['record_id'])
                    if record and record['status'] == 'unfiled':
                        self.crm.update_record(self.owner, record['id'], {'status': 'following'}, self.clock())
        return response({'proposal': proposal, 'message': message} if proposal['status'] == expected
                        else self._proposal_failure(proposal), 200 if proposal['status'] == expected else 409)

    async def review_inbox(self, request):
        async with self.lock:
            return response(self.review_queue.list(self.owner, **self.paging(request),
                state=request.query.get('state','pending'),
                customer_id=self.number(request.query['customer_id']) if request.query.get('customer_id') else None))

    async def review_decision(self, request):
        data = await self.body(request)
        async with self.lock:
            item = self.review_queue.decide(self.owner, data)
        return response({'item':item})

    async def customer_workbench(self,request):
        async with self.lock:
            result=self.sales_workspace.workbench(self.owner,self.number(request.match_info['id']),
                visits=self.visits,materials=self.materials,review_queue=self.review_queue)
        return response(result)

    async def opportunity_list(self,request):
        async with self.lock:
            result=self.sales_workspace.opportunities(self.owner,self.number(request.match_info['id']),
                include_archived=request.query.get('include_archived')=='true')
        return response(result)

    async def opportunity_create(self,request):
        data=await self.body(request)
        async with self.lock:
            result=self.sales_workspace.create_opportunity(self.owner,self.number(request.match_info['id']),data)
        return response({'opportunity':result},201)

    async def opportunity_update(self,request):
        data=await self.body(request)
        async with self.lock:
            result=self.sales_workspace.update_opportunity(self.owner,self.number(request.match_info['id']),
                self.number(request.match_info['opp_id']),data)
        return response({'opportunity':result})

    async def opportunity_link(self,request):
        data=await self.body(request)
        if set(data)!={'entity_type','entity_id','opportunity_id'}:raise ValueError('项目关联字段无效')
        async with self.lock:
            result=self.sales_workspace.link(self.owner,**data)
        return response({'link':result})

    async def priorities(self,request):
        async with self.lock: result=self.sales_workspace.priorities(self.owner)
        return response(result)

    async def priority_decision(self,request):
        data=await self.body(request)
        async with self.lock: result=self.sales_workspace.decide_priority(self.owner,data)
        return response({'item':result})

    async def lifecycle_list(self,request):
        async with self.lock:
            result=self.record_lifecycle.list(self.owner,visibility=request.query.get('visibility','archived'),
                q=request.query.get('q',''),**self.paging(request))
        return response(result)

    async def lifecycle_record(self,request):
        identifier=self.number(request.match_info['id'])
        data=await self.body(request) if request.method=='POST' else None
        async with self.lock:
            result=(self.record_lifecycle.move(self.owner,identifier,data) if data is not None
                else self.record_lifecycle.preview(self.owner,identifier))
        return response(result)

    async def priority_archives(self,request):
        async with self.lock:
            result=self.sales_workspace.priority_archives(self.owner,q=request.query.get('q',''),**self.paging(request))
        return response(result)

    async def priority_archive_restore(self,request):
        data=await self.body(request)
        async with self.lock:result=self.sales_workspace.restore_priority_archive(self.owner,data)
        return response(result)

    async def customer_candidates(self,request):
        data=await self.body(request)
        if 'text' not in data or set(data)-{'text','customer_id'}:raise ValueError('请提交需要核对归属的文字')
        result=await self.resolution.resolve(self.owner,data['text'],context_customer_id=data.get('customer_id'))
        return response(result)

    async def capture_list(self,request):
        async with self.lock:
            result=self.captures.list(self.owner,status=request.query.get('status',''),**self.paging(request,20))
        return response(result)

    async def capture_create(self,request):
        data=await self.body(request)
        async with self.lock:result=self.captures.capture(self.owner,data)
        return response({'capture':result,'message':'原话已保存，秘书会在后台理解。你可以继续记录，稍后再核对。'},202)

    async def capture_detail(self,request):
        async with self.lock:result=self.captures.get(self.owner,self.number(request.match_info['id']))
        return response({'capture':result})

    async def capture_classify(self,request):
        data=await self.body(request)
        async with self.lock:result=self.captures.classify(self.owner,self.number(request.match_info['id']),data)
        return response(result)

    async def capture_retry(self,request):
        data=await self.body(request)
        if data:raise ValueError('重新整理无需额外字段')
        async with self.lock:result=self.captures.retry(self.owner,self.number(request.match_info['id']))
        return response({'capture':result},202)

    async def overview(self,request):
        from .overview import OverviewService
        async with self.lock:
            result=OverviewService(self.crm,self.sales_workspace,self.review_queue,clock=self.clock).get(self.owner)
            result['secretary_plans']=[p for p in self.secretary_flow.list_plans(self.owner)['items']
                if p['status'] in ('preparing','needs_attention')]
            result['matters'] = self.matters.list(self.owner, page_size=6)
            for project in result.get('projects', []):
                project['matters'] = self.matters.list(self.owner, customer_id=project['customer_id'],
                    opportunity_id=project.get('opportunity_id') or project['id'], page_size=6)
        return response(result)

    async def discussion_list(self,request):
        if request.method=='POST':
            data=await self.body(request)
            async with self.lock:result=self.discussions.create_thread(self.owner,data)
            return response(result,201)
        async with self.lock:
            result=self.discussions.list_threads(self.owner,
                customer_id=self.number(request.query['customer_id']) if request.query.get('customer_id') else None,
                opportunity_id=self.number(request.query['opportunity_id']) if request.query.get('opportunity_id') else None,
                contact_id=self.number(request.query['contact_id']) if request.query.get('contact_id') else None)
        return response(result)

    def timeline_scope(self, data):
        return {key: self.number(data[key]) for key in ('customer_id', 'contact_id', 'opportunity_id')
                if key in data and data[key] not in (None, '')}

    async def timeline_view(self, request):
        async with self.lock:
            result = self.timeline.view(self.owner, self.timeline_scope(request.query),
                kind=request.query.get('kind', ''), q=request.query.get('q', ''),
                page=self.number(request.query.get('page', '1')),
                page_size=self.number(request.query.get('page_size', '20')))
        return response(result)

    async def timeline_event(self, request):
        async with self.lock:
            event = self.timeline.get_event(self.owner, request.match_info['event_key'])
        return response({'event': event})

    async def timeline_context(self, request):
        data = await self.body(request)
        async with self.lock:
            event = self.timeline.save_context(self.owner, request.match_info['event_key'], data)
        return response({'event': event})

    async def timeline_record(self, request):
        data = await self.body(request)
        if not isinstance(data, dict):
            raise ValueError('请提供记录内容与归属')
        scope = self.timeline_scope(data)
        payload = {key: value for key, value in data.items()
                   if key not in ('customer_id', 'contact_id', 'opportunity_id')}
        async with self.lock:
            result = self.timeline.create_record(self.owner, scope, payload)
        return response(result, 201 if result.get('created') else 200)

    async def discussion_detail(self,request):
        async with self.lock:result=self.discussions.get_thread(self.owner,self.number(request.match_info['id']))
        return response(result)

    async def discussion_message(self,request):
        result=await self.discussions.send_message(self.owner,self.number(request.match_info['id']),await self.body(request))
        return response(result)

    async def discussion_title(self,request):
        data=await self.body(request)
        if set(data)!={'title','expected_updated_at'}:
            raise ValueError('请提供讨论标题和核对过的更新时间。')
        async with self.lock:
            result=self.discussions.rename_thread(self.owner,self.number(request.match_info['id']),
                data['title'],data['expected_updated_at'])
        return response(result)

    async def discussion_adopt(self,request):
        data=await self.body(request)
        async with self.lock:
            result=self.discussions.adopt(self.owner,self.number(request.match_info['id']),
                self.number(request.match_info['message_id']),self.number(request.match_info['index']),data)
        return response({'record':result})

    async def complete_outcome(self,request):
        data=await self.body(request)
        if 'expected_completion_snapshot' in data:
            raise ValueError('请使用当前完成反馈窗口提供的核对版本')
        data = dict(data)
        data['expected_completion_snapshot'] = data.pop('expected_snapshot', None)
        async with self.lock:result=self.sales_workspace.complete_record(self.owner,self.number(request.match_info['id']),data)
        return response(result)

    async def source_decision(self,request):
        data=await self.body(request)
        if set(data)-{'revision','use','reason'}:raise ValueError('来源使用决定字段无效')
        async with self.lock:
            result=self.require_visits().decide_source(self.owner,self.number(request.match_info['id']),
                self.number(request.match_info['material_id']),data.get('revision'),data.get('use'),data.get('reason',''))
        return response(result)

    async def material_version(self,request):
        async with self.lock:
            result=self.require_materials().get_version(self.owner,self.number(request.match_info['id']),
                self.number(request.match_info['version_id']))
        return response({'version':result})

    async def archive_record(self,request):
        data=await self.body(request)
        if not self.exchange_records:raise ValueError('客户交流入口尚未启用')
        async with self.lock:
            result=self.exchange_records.archive(self.owner,self.number(request.match_info['id']),data)
        return response({'visit_ref':result})

    async def action_terms(self,request):
        data=await self.body(request)
        expected=data.pop('expected_updated_at',None)
        async with self.lock:
            prior = self.crm.get_record(self.owner, self.number(request.match_info['id']))
            if prior is None:
                raise KeyError()
            from .action_contract import TERM_FIELDS
            if set(data)-set(TERM_FIELDS):
                raise ValueError('行动时间与责任字段无效')
            from .crm import _text
            for key, value in data.items():
                if key.endswith('_evidence'):
                    _text(value or '', '行动依据', 2000)
            old = prior.get('action_terms', {})
            terms = {**old, **data}
            for family in ('deadline', 'check'):
                at, day = family+'_at', family+'_date'
                if terms.get(day) is not None and terms.get(day) != old.get(day) and terms.get(at) == old.get(at):
                    terms[at] = None
                elif terms.get(at) is not None and terms.get(at) != old.get(at) and terms.get(day) == old.get(day):
                    terms[day] = None
            for family, keys in (('executor', ('executor_kind',)), ('duration', ('duration_minutes',)),
                                 ('execution', ('execution_at',)), ('deadline', ('deadline_at', 'deadline_date')),
                                 ('check', ('check_at', 'check_date'))):
                evidence = family+'_evidence'
                changed = any(terms.get(key) != old.get(key) for key in keys)
                active = any(terms.get(key) is not None for key in keys)
                candidate = data.get(evidence)
                reused_quote = candidate and candidate in [old.get(key) for key in TERM_FIELDS if key.endswith('_evidence')]
                terms[evidence] = (candidate if candidate and not reused_quote else '用户核对本次计划安排') if changed and active else old.get(evidence, '') if active else ''
            record=self.crm.save_action_terms(self.owner,self.number(request.match_info['id']),terms,self.clock(),
                expected_updated_at=expected,explicit=True)
        return response({'record':record,'message':'行动资料已保存；生效提醒仍需单独拟定并确认。'})

    async def complete(self, request):
        await self.body(request)
        identifier = self.number(request.match_info['id'])
        async with self.lock:
            if self.crm.get_task(self.owner, identifier) is None:
                raise KeyError()
            message = self.store.execute(self.owner, 'web:' + secrets.token_hex(16),
                                         {'action': 'complete', 'task_id': identifier}, self.clock())
        return response({'message': message})

    async def agenda(self, request):
        async with self.lock:
            data = self.crm.agenda(self.owner, period=request.query.get('period', 'day'),
                                   date=request.query.get('date'), now=self.clock(), **self.paging(request, 200))
            data['items'] = self._calendar_projects(data['items'])
            for item in data['items']:
                item['matters'] = self.matters.resolve(self.owner, 'task', item['id'])['items']
            from .planning import planning_nodes
            data['planning_nodes']=planning_nodes(self.crm,self.owner,period=request.query.get('period','day'),
                date=request.query.get('date'),now=self.clock())
            data['secretary_plans']=[p for p in self.secretary_flow.list_plans(self.owner,limit=200)['items']
                if p.get('date') and data['start']<=datetime.fromisoformat(p['date']).replace(tzinfo=SHANGHAI).timestamp()<data['end']
                and p['status'] not in ('cancelled','recapped') and not p.get('task_id')]
            for item in data['secretary_plans']:
                item['matters'] = self.matters.resolve(self.owner, 'plan', item['id'])['items']
        return response(data)

    async def static(self, request):
        name = request.match_info.get('name', 'index.html')
        if name not in ('index.html', 'app.js', 'app.css', 'record-lifecycle.js', 'secretary-history.js', 'account-intelligence.js', 'account-intelligence.css',
                        'customer-timeline.js', 'customer-timeline.css', 'secretary-workspace.js', 'secretary-workspace.css', 'secretary-flow.js', 'secretary-attachments.js', 'secretary-flow.css', 'matters.js', 'matters.css', 'matter-routing.js', 'arrangement-queue.js', 'arrangement-queue.css', 'detail-progress.js', 'detail-progress.css'):
            raise web.HTTPNotFound()
        mime = 'text/html' if name.endswith('.html') else 'application/javascript' if name.endswith('.js') else 'text/css'
        body = (STATIC / name).read_bytes()
        if name == 'index.html':
            # Browsers should load a matching asset set after a local update,
            # even when an embedded browser retains an earlier resource copy.
            for asset in ('app.js', 'app.css', 'record-lifecycle.js', 'secretary-history.js', 'account-intelligence.js', 'account-intelligence.css',
                          'customer-timeline.js', 'customer-timeline.css', 'secretary-workspace.js', 'secretary-workspace.css', 'secretary-flow.js', 'secretary-attachments.js', 'secretary-flow.css', 'matters.js', 'matters.css', 'matter-routing.js', 'arrangement-queue.js', 'arrangement-queue.css', 'detail-progress.js', 'detail-progress.css'):
                if f'/static/{asset}"'.encode() not in body:
                    continue
                version = hashlib.sha256((STATIC / asset).read_bytes()).hexdigest()[:16]
                body = body.replace(f'/static/{asset}"'.encode(), f'/static/{asset}?v={version}"'.encode())
        return web.Response(body=body, content_type=mime)

    def application(self):
        app = web.Application(middlewares=[self.security], client_max_size=21 * 1024 * 1024)
        app.add_routes([
            web.get('/', self.static), web.get('/static/{name}', self.static),
            web.get('/api/timeline', self.timeline_view),
            web.get('/api/timeline/events/{event_key}', self.timeline_event),
            web.post('/api/timeline/events/{event_key}/context', self.timeline_context),
            web.post('/api/timeline/records', self.timeline_record),
            web.get('/api/session', self.get_session), web.post('/api/login', self.login), web.post('/api/logout', self.logout),
            web.get('/api/dashboard', self.dashboard),
            web.get('/api/matters', self.matter_list), web.post('/api/matters', self.matter_list),
            web.get('/api/matters/candidates', self.matter_candidates),
            web.post('/api/matters/candidates/confirm', self.matter_candidate_confirm),
            web.get('/api/matters/resolve', self.matter_resolve),
            web.post('/api/matters/operations/{id}/undo', self.matter_undo),
            web.get('/api/matters/{id}', self.matter_detail), web.patch('/api/matters/{id}', self.matter_detail),
            web.post('/api/matters/{id}/merge', self.matter_merge),
            web.post('/api/matters/{id}/split', self.matter_split),
            web.get('/api/matters/{id}/lifecycle', self.matter_lifecycle),
            web.post('/api/matters/{id}/lifecycle', self.matter_lifecycle),
            web.get('/api/secretary/turns',self.secretary_turns),web.post('/api/secretary/turns',self.secretary_turns),
            web.get('/api/secretary/turns/{id}',self.secretary_turn),web.post('/api/secretary/turns/{id}/retry',self.secretary_retry),
            web.post('/api/secretary/turns/{id}/adopt',self.secretary_adopt),
            web.get('/api/secretary/turns/{id}/matter',self.secretary_matter_correction),
            web.post('/api/secretary/turns/{id}/matter',self.secretary_matter_correction),
            web.get('/api/secretary/plans',self.secretary_plans),web.get('/api/secretary/plans/{id}',self.secretary_plan),
            web.get('/api/secretary/arrangements',self.secretary_arrangements),
            web.post('/api/secretary/plans/{id}/arrangement-decisions',self.secretary_arrangement_decision),
            web.get('/api/secretary/arrangement-notices',self.secretary_arrangement_notices),
            web.post('/api/secretary/arrangement-notices/{id}/read',self.secretary_arrangement_notice_read),
            web.get('/api/secretary/settings',self.secretary_settings),web.patch('/api/secretary/settings',self.secretary_settings),
            web.get('/api/secretary/prospects',self.secretary_prospects),
            web.post('/api/secretary/prospects/{id}/link',self.secretary_prospect_link),
            web.get('/api/overview',self.overview),
            web.get('/api/captures',self.capture_list),web.post('/api/captures',self.capture_create),
            web.get('/api/captures/{id}',self.capture_detail),
            web.post('/api/captures/{id}/classify',self.capture_classify),
            web.post('/api/captures/{id}/retry',self.capture_retry),
            web.get('/api/sales-discussions',self.discussion_list),web.post('/api/sales-discussions',self.discussion_list),
            web.get('/api/sales-discussions/{id}',self.discussion_detail),
            web.post('/api/sales-discussions/{id}/title',self.discussion_title),
            web.post('/api/sales-discussions/{id}/messages',self.discussion_message),
            web.post('/api/sales-discussions/{id}/messages/{message_id}/actions/{index}/adopt',self.discussion_adopt),
            web.get('/api/review-inbox', self.review_inbox),
            web.post('/api/review-decisions', self.review_decision),
            web.get('/api/priorities',self.priorities),web.post('/api/priority-decisions',self.priority_decision),
            web.get('/api/record-lifecycle',self.lifecycle_list),
            web.get('/api/records/{id}/lifecycle',self.lifecycle_record),web.post('/api/records/{id}/lifecycle',self.lifecycle_record),
            web.get('/api/priority-archives',self.priority_archives),web.post('/api/priority-archives/restore',self.priority_archive_restore),
            web.post('/api/customer-candidates',self.customer_candidates),
            web.post('/api/opportunity-links',self.opportunity_link),
            web.post('/api/proposals/{id}/{decision:confirm|reject}', self.decide_proposal),
            web.get('/api/audio/capabilities', self.audio_capabilities),
            web.get('/api/materials/capabilities', self.material_capabilities),
            web.get('/api/visits', self.visit_list), web.post('/api/visits', self.visit_create),
            web.get('/api/visits/{id}', self.visit_detail), web.patch('/api/visits/{id}', self.visit_update),
            web.post('/api/visits/{id}/materials', self.visit_material),
            web.post('/api/visits/{id}/actions/{key}/adopt', self.visit_adopt),
            web.post('/api/visits/{id}/merge', self.visit_merge),
            web.post('/api/visits/{id}/sources/{material_id}/decision',self.source_decision),
            web.post('/api/materials/upload', self.document_upload),
            web.get('/api/materials/{id}/file', self.document_download),
            web.get('/api/materials', self.material_list), web.post('/api/materials', self.material_create),
            web.get('/api/materials/{id}', self.material_detail), web.patch('/api/materials/{id}', self.material_update),
            web.post('/api/materials/{id}/retry', self.material_retry),
            web.post('/api/materials/{id}/actions/{action_id}/adopt', self.material_adopt),
            web.get('/api/materials/{id}/versions/{version_id}',self.material_version),
            web.post('/api/audio/transcribe', self.transcribe_audio),
            web.get('/api/voice-settings', self.voice_settings), web.patch('/api/voice-settings', self.voice_settings),
            web.get('/api/customers', self.customers), web.post('/api/customers', self.create_customer),
            web.get('/api/customers/{id}', self.customer), web.patch('/api/customers/{id}', self.update_customer),
            web.get('/api/customer-schema', self.customer_schema),
            web.get('/api/units', self.units),
            web.get('/api/customers/{id}/account-network', self.account_network),
            web.patch('/api/customers/{id}/account-network', self.account_network),
            web.get('/api/customers/{id}/opportunities/{opp_id}/stakeholders', self.opportunity_stakeholders),
            web.post('/api/customers/{id}/opportunities/{opp_id}/stakeholders', self.opportunity_stakeholders),
            web.get('/api/customers/{id}/opportunities/{opp_id}/units', self.opportunity_units),
            web.post('/api/customers/{id}/opportunities/{opp_id}/units', self.opportunity_units),
            web.post('/api/customers/{id}/opportunities/{opp_id}/facts', self.project_fact),
            web.get('/api/contacts/{contact_id}/projects', self.contact_projects),
            web.get('/api/customers/{id}/profile-intelligence', self.profile_knowledge),
            web.get('/api/customers/{id}/research-workspace', self.research_workspace_view),
            web.post('/api/customers/{id}/research-workspace', self.research_workspace_view),
            web.get('/api/research-runs/{run_id}', self.research_run),
            web.patch('/api/research-runs/{run_id}', self.research_run),
            web.post('/api/research-runs/{run_id}/confirm', self.research_confirm),
            web.post('/api/research-runs/{run_id}/{operation:retry|cancel}', self.research_control),
            web.post('/api/secretary-goals/preview', self.secretary_goal_preview),
            web.post('/api/public-pages/preview', self.public_page_preview),
            web.get('/api/progress-workspaces', self.progress_workspace_view),
            web.get('/api/progress-action-candidates', self.progress_action_candidates),
            web.post('/api/progress-workspaces', self.progress_workspace_view),
            web.get('/api/progress-runs/{run_id}', self.progress_run_view),
            web.patch('/api/progress-runs/{run_id}', self.progress_run_view),
            web.post('/api/progress-runs/{run_id}/confirm', self.progress_run_confirm),
            web.post('/api/progress-runs/{run_id}/{operation:retry|cancel}', self.progress_run_control),
            web.get('/api/exchange-workspaces/{source_type:record|material|visit}/{source_id}', self.exchange_workspace_view),
            web.patch('/api/exchange-workspaces/{source_type:record|material|visit}/{source_id}', self.exchange_workspace_view),
            web.post('/api/exchange-workspaces/{source_type:record|material|visit}/{source_id}/prepare', self.exchange_workspace_prepare),
            web.post('/api/exchange-workspaces/{source_type:record|material|visit}/{source_id}/confirm', self.exchange_workspace_confirm),
            web.post('/api/exchange-workspaces/{source_type:record|material|visit}/{source_id}/finish-review', self.exchange_workspace_finish_review),
            web.patch('/api/customers/{id}/profile-intelligence/settings', self.profile_settings),
            web.post('/api/customers/{id}/profile-intelligence/scan', self.profile_scan),
            web.post('/api/customers/{id}/profile-intelligence/research', self.profile_research),
            web.post('/api/customers/{id}/profile-questions/adopt', self.profile_question_adopt),
            web.get('/api/profile-suggestions/{candidate_id}', self.profile_candidate),
            web.post('/api/profile-suggestions/{candidate_id}', self.profile_candidate),
            web.get('/api/customers/{id}/profile', self.customer_profile),
            web.get('/api/customers/{id}/workbench',self.customer_workbench),
            web.get('/api/customers/{id}/opportunities',self.opportunity_list),
            web.post('/api/customers/{id}/opportunities',self.opportunity_create),
            web.patch('/api/customers/{id}/opportunities/{opp_id}',self.opportunity_update),
            web.get('/api/coaching', self.coaching_list),
            web.get('/api/customers/{id}/coaching', self.customer_coaching),
            web.post('/api/customers/{id}/coaching', self.customer_coaching),
            web.post('/api/customers/{id}/coach-feedback', self.coaching_feedback),
            web.post('/api/customers/{id}/coaching/{version}/actions/{index}/adopt', self.adopt_coaching),
            web.post('/api/customers/{id}/facts', self.customer_fact),
            web.post('/api/customers/{id}/contacts', self.customer_contact),
            web.patch('/api/customers/{id}/contacts/{contact_id}', self.customer_contact),
            web.post('/api/customer-command', self.customer_command),
            web.get('/api/customer-drafts', self.customer_drafts),
            web.get('/api/customer-drafts/{id}', self.customer_draft),
            web.post('/api/customer-drafts/{id}/{decision:confirm|reject}', self.customer_decide),
            web.get('/api/records', self.records), web.post('/api/records', self.create_record),
            web.get('/api/records/{id}', self.record), web.patch('/api/records/{id}', self.update_record),
            web.post('/api/records/{id}/activities', self.activity), web.post('/api/records/{id}/schedule', self.schedule),
            web.post('/api/records/{id}/activities/organize', self.organize_activity),
            web.post('/api/records/{id}/cancel', self.cancel_record), web.post('/api/records/{id}/next-visit', self.next_visit),
            web.post('/api/records/{id}/reinterpret', self.reinterpret),
            web.post('/api/records/{id}/organize', self.organize), web.post('/api/records/{id}/actions/{action_id}/adopt', self.adopt),
            web.post('/api/records/{id}/confirm', self.confirm), web.post('/api/tasks/{id}/complete', self.complete),
            web.post('/api/records/{id}/complete-outcome',self.complete_outcome),
            web.post('/api/records/{id}/archive-exchange',self.archive_record),
            web.patch('/api/records/{id}/terms',self.action_terms),
            web.get('/api/agenda', self.agenda),
        ])
        async def capture_worker():
            while True:
                try:
                    if not await self.captures.process_one():await asyncio.sleep(.5)
                except asyncio.CancelledError:raise
                except Exception:
                    LOG.error('capture_worker_failed')
                    await asyncio.sleep(1)
        async def start_captures(app):
            app[CAPTURE_WORKER]=asyncio.create_task(capture_worker())
            async def document_worker():
                while True:
                    try:
                        if not await self.parse_document_one():
                            await asyncio.sleep(.25)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        LOG.warning('document_worker_retry_pending')
                        await asyncio.sleep(1)
            app[DOCUMENT_WORKER]=asyncio.create_task(document_worker())
            async def secretary_worker():
                while True:
                    try:
                        if not await self.secretary_flow.process_one():await asyncio.sleep(.5)
                    except asyncio.CancelledError:raise
                    except Exception:
                        LOG.warning('secretary_worker_retry_pending')
                        await asyncio.sleep(1)
            app[SECRETARY_WORKER]=asyncio.create_task(secretary_worker())
            async def profile_worker():
                while True:
                    try:
                        await self.profile_intelligence.scan(self.owner, limit=3)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        LOG.warning('profile_worker_retry_pending')
                    await asyncio.sleep(30)
            app[PROFILE_WORKER] = asyncio.create_task(profile_worker())
            async def research_worker():
                while True:
                    try:
                        result = await self.research_workspace.process_pending(self.owner, limit=1)
                        if not result['processed']:
                            await asyncio.sleep(.5)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        LOG.warning('research_worker_retry_pending')
                        await asyncio.sleep(1)
            app[RESEARCH_WORKER] = asyncio.create_task(research_worker())
            async def progress_worker():
                while True:
                    try:
                        result = await self.progress_workspace.process_pending(self.owner, limit=1)
                        if not result['processed']:
                            await asyncio.sleep(.5)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        LOG.warning('progress_worker_retry_pending')
                        await asyncio.sleep(1)
            app[PROGRESS_WORKER] = asyncio.create_task(progress_worker())
        async def stop_captures(app):
            document_task=app.get(DOCUMENT_WORKER)
            if document_task:
                document_task.cancel();await asyncio.gather(document_task,return_exceptions=True)
            secretary_task=app.get(SECRETARY_WORKER)
            if secretary_task:
                secretary_task.cancel();await asyncio.gather(secretary_task,return_exceptions=True)
            self.secretary_flow.close()
            progress_task = app.get(PROGRESS_WORKER)
            if progress_task:
                progress_task.cancel()
                await asyncio.gather(progress_task, return_exceptions=True)
            self.progress_workspace.close()
            research_task = app.get(RESEARCH_WORKER)
            if research_task:
                research_task.cancel()
                await asyncio.gather(research_task, return_exceptions=True)
            self.research_workspace.close()
            profile_task = app.get(PROFILE_WORKER)
            if profile_task:
                profile_task.cancel(); await asyncio.gather(profile_task, return_exceptions=True)
            self.profile_intelligence.close()
            task=app.get(CAPTURE_WORKER)
            if task:
                task.cancel();await asyncio.gather(task,return_exceptions=True)
            await self.discussions.close()
        app.on_startup.append(start_captures)
        app.on_shutdown.append(stop_captures)
        async def arrangement_lifecycle(app):
            async def worker():
                while True:
                    try:
                        async with self.lock:
                            self.arrangement_reminders.sweep()
                            for _ in range(20):
                                notice = self.arrangement_reminders.claim_due()
                                if not notice:
                                    break
                                self.arrangement_reminders.publish(notice['id'], notice['token'])
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        LOG.warning('arrangement_worker_retry_pending')
                    await asyncio.sleep(1)
            task = asyncio.create_task(worker())
            app[ARRANGEMENT_WORKER] = task
            try:
                yield
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        app.cleanup_ctx.append(arrangement_lifecycle)
        return app


def create_app(store, crm, lock, owner, password_hash, **kwargs):
    return WebCRM(store, crm, lock, owner, password_hash, **kwargs).application()


def demo_main():
    """Loopback-only isolated preview: no real credentials, no reminder network."""
    import argparse
    import tempfile
    from .customer_store import CustomerStore
    from .customer_service import CustomerService
    from .coaching_service import CoachingService
    from .store import Store

    args = argparse.ArgumentParser()
    args.add_argument('--demo', action='store_true', required=True)
    args.add_argument('--port', type=int, default=8787)
    options = args.parse_args()
    with tempfile.TemporaryDirectory(prefix='secretary-web-demo-') as folder:
        path = Path(folder) / 'demo.sqlite3'
        store, crm = Store(path), CustomerStore(path)
        now = time.time()
        a = crm.create_customer('demo', {'name': '青禾科技（演示）', 'contact': '林女士', 'phone': '',
            'stage': 'proposal', 'amount_cents': 18000000, 'notes': '正在评估数据库加密与密钥管理方案'}, now-86400*5)
        b = crm.create_customer('demo', {'name': '远山制造（演示）', 'contact': '陈先生', 'phone': '',
            'stage': 'qualified', 'amount_cents': 6500000, 'notes': ''}, now-86400*3)
        crm.create_record('demo', {'title': '补充年度方案的交付明细', 'content': '客户希望先看到实施时间表，回公司后整理方案。',
            'customer_id': a['id'], 'status': 'following'}, now-7200)
        crm.create_record('demo', {'title': '确认下次需求沟通的参与人', 'content': '技术负责人也需要参加，等客户确认时间。',
            'customer_id': b['id'], 'status': 'following'}, now-3600)
        crm.capture_message('demo', 'demo-voice', '今天见了新的渠道客户，对华东区域合作感兴趣，回来整理交流要点。', 'voice', now-600)
        class DemoOrganizer:
            async def organize(self, content, now, context):
                return {'summary': '演示整理：客户表达了合作兴趣，后续需要补齐需求和沟通安排。',
                        'key_points': ['演示内容：保留本次交流的关键信息，方便下次拜访前回顾。'],
                        'open_questions': ['下次沟通的日期和参与人尚待确认。'],
                        'actions': [{'title': '整理本次交流要点并确认下一次沟通', 'kind': 'suggestion',
                                     'reason': '演示建议，请结合实际交流决定是否采纳。',
                                     'owner_hint': '我', 'remind_at': None}]}
        lock = asyncio.Lock()
        class DemoCustomerParser:
            async def parse(self, text, now, context=None):
                if context and context.get('customer'):
                    return {'intent': 'note', 'customer_name': context['customer']['name'], 'contact_name': '',
                            'basic': {}, 'basic_evidence': {}, 'contact': {}, 'contact_evidence': {}, 'attributes': []}
                if text == '新增客户华东研究院，正在做数据安全改造，李经理负责技术评审。':
                    return {'intent': 'create', 'customer_name': '华东研究院', 'contact_name': '李经理',
                            'basic': {}, 'basic_evidence': {}, 'contact': {'name': '李经理', 'role': '负责技术评审'},
                            'contact_evidence': {'name': '李经理', 'role': '李经理负责技术评审'},
                            'attributes': [{'key': 'security_scenarios', 'value': '数据安全改造',
                                            'evidence': '正在做数据安全改造', 'basis': 'reported', 'target': 'account'}]}
                return {'intent': 'clarify', 'question': '当前为演示环境，语音理解请在正式企业微信或正式后台使用。这里可以体验手动画像和联系人管理。'}
        crm.save_fact('demo', a['id'], {'key': 'crypto_needs', 'value': '数据库字段加密、集中密钥管理',
                      'basis': 'reported', 'evidence': '演示资料'}, now)
        crm.save_fact('demo', a['id'], {'key': 'next_visit_goal', 'value': '和技术负责人确认部署边界及测试方案',
                      'basis': 'observation', 'evidence': '演示观察'}, now)
        demo_contact = crm.find_contacts_exact('demo', '林女士', a['id'])[0]
        crm.update_contact('demo', a['id'], demo_contact['id'], {'role': '技术负责人'}, now)
        class DemoCoach:
            async def advise(self, profile, now):
                return {'summary': '演示建议：先明确数据库加密与密钥管理的试点范围，再形成方案。',
                        'objective': '确认一个可验证的试点场景',
                        'rationale': '已有技术方向，系统范围、接口约束和验收指标仍需核实。',
                        'next_moves': [{'title': '与技术负责人确认试点和验收指标',
                                        'reason': '避免方案范围和客户预期不一致。',
                                        'contact_hint': '技术负责人（以实际客户资料为准）',
                                        'preparation': '准备系统清单、密钥管理架构和接口问题清单。',
                                        'talk_track': '您希望先在哪个系统验证？哪些性能与业务指标必须满足？',
                                        'success_signal': '双方确认试点系统、参与人和验收指标。'}],
                        'questions': ['谁负责技术验收，谁确认采购预算？'],
                        'risks': ['当前资料不足以判断采购流程和预算。']}
        coaching = CoachingService(crm, DemoCoach(), lock)
        from .materials import MaterialService
        materials = MaterialService(crm, lock, organizer=DemoOrganizer(), customer_parser=DemoCustomerParser(), coaching=coaching)
        materials.enqueue('demo', {'provider': 'manual', 'title': '项目会议（演示）', 'category': 'meeting',
            'text': '会议讨论数据安全试点。我们先核对数据库字段加密范围，再确认参与技术验证的人。'})
        app = create_app(store, crm, lock, 'demo', hash_password('demo-only-preview'), demo=True,
                         organizer=DemoOrganizer(), coaching=coaching, materials=materials,
                         customer_service=CustomerService(crm, DemoCustomerParser(), lock,
                                                          organizer=DemoOrganizer(), coach=coaching))
        async def cleanup_coaching(app):
            await coaching.close()
        async def material_worker():
            while True:
                if not await materials.process_one():
                    await asyncio.sleep(.2)
        async def start_materials(app):
            app['demo_material_worker'] = asyncio.create_task(material_worker())
        async def cleanup_materials(app):
            app['demo_material_worker'].cancel()
            await asyncio.gather(app['demo_material_worker'], return_exceptions=True)
            await materials.close()
        app.on_startup.append(start_materials)
        app.on_cleanup.append(cleanup_materials)
        app.on_cleanup.append(cleanup_coaching)
        try:
            web.run_app(app, host='127.0.0.1', port=options.port, access_log=None)
        finally:
            crm.close()
            store.close()


if __name__ == '__main__':
    demo_main()
