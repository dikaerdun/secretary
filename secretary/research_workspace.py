"""Recoverable public unit research drafts; only explicit reviews write facts.

Sources and candidates reuse ProfileIntelligence. Draft edits and job checkpoints
are sidecars, so searching, retrying or reviewing never rewrites customer data.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import time
import uuid
from urllib.parse import urlsplit

from .crm import _identifier, _owner, _text
from .customer_schema import ACCOUNT_BACKGROUND_KEYS, ACCOUNT_FIELDS
from .profile_intelligence import ProfileConflict, _hash, _json, _rule_extract
from .public_research import PublicResearchError, normalize_domains, public_url, public_research_capabilities, public_source_identity_reason
from .store import _timestamp


class ResearchConflict(ProfileConflict):
    """The displayed draft or unit identity changed; retain inputs and refresh."""


class _LeaseLost(Exception):
    pass


class _SourceLimit(ValueError):
    pass


async def _await(value):
    return await value if inspect.isawaitable(value) else value


class ResearchWorkspace:
    def __init__(self, crm, profile_intelligence, *, researcher=None, clock=time.time):
        self.crm, self.profile, self.clock = crm, profile_intelligence, clock
        self._researcher, self.closed = researcher, False
        with crm._lock:
            crm._db.executescript("""
                CREATE TABLE IF NOT EXISTS crm_research_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,owner TEXT NOT NULL,customer_id INTEGER NOT NULL,
                    mode TEXT NOT NULL,identity_json TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'queued',
                    stage TEXT NOT NULL DEFAULT 'queued',revision INTEGER NOT NULL DEFAULT 1,
                    queries_json TEXT NOT NULL DEFAULT '[]',errors_json TEXT NOT NULL DEFAULT '[]',
                    lease_token TEXT,lease_until REAL,created_at REAL NOT NULL,updated_at REAL NOT NULL,
                    UNIQUE(owner,id),FOREIGN KEY(owner,customer_id) REFERENCES crm_customers(owner,id));
                CREATE INDEX IF NOT EXISTS crm_research_run_queue ON crm_research_runs(owner,status,lease_until,id);
                CREATE TABLE IF NOT EXISTS crm_research_requests (
                    owner TEXT NOT NULL,request_id TEXT NOT NULL,signature TEXT NOT NULL,run_id INTEGER NOT NULL,
                    PRIMARY KEY(owner,request_id),FOREIGN KEY(owner,run_id) REFERENCES crm_research_runs(owner,id));
                CREATE TABLE IF NOT EXISTS crm_research_sources (
                    owner TEXT NOT NULL,run_id INTEGER NOT NULL,source_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'saved',error TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(owner,run_id,source_id),
                    FOREIGN KEY(owner,run_id) REFERENCES crm_research_runs(owner,id));
                CREATE TABLE IF NOT EXISTS crm_research_drafts (
                    owner TEXT NOT NULL,run_id INTEGER NOT NULL,candidate_id INTEGER NOT NULL,value TEXT NOT NULL,
                    selected INTEGER NOT NULL DEFAULT 0,captured_json TEXT NOT NULL,
                    PRIMARY KEY(owner,run_id,candidate_id),
                    FOREIGN KEY(owner,run_id) REFERENCES crm_research_runs(owner,id),
                    FOREIGN KEY(owner,candidate_id) REFERENCES crm_profile_candidates(owner,id));
                CREATE TABLE IF NOT EXISTS crm_research_batches (
                    owner TEXT NOT NULL,request_id TEXT NOT NULL,run_id INTEGER NOT NULL,signature TEXT NOT NULL,
                    payload_json TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'processing',results_json TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL,updated_at REAL NOT NULL,PRIMARY KEY(owner,request_id),
                    FOREIGN KEY(owner,run_id) REFERENCES crm_research_runs(owner,id));
                CREATE TABLE IF NOT EXISTS crm_research_batch_items (
                    owner TEXT NOT NULL,request_id TEXT NOT NULL,candidate_id INTEGER NOT NULL,ordinal INTEGER NOT NULL,
                    decision_json TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'queued',result_json TEXT,
                    PRIMARY KEY(owner,request_id,candidate_id),
                    FOREIGN KEY(owner,request_id) REFERENCES crm_research_batches(owner,request_id));
            """)

    @property
    def researcher(self):
        return self._researcher if self._researcher is not None else self.profile.researcher

    def capabilities(self):
        return {**public_research_capabilities(self.researcher),
                "analysis_mode": "model" if self.profile.analyzer else "rules", "quick": True, "deep": True}

    def _require(self, db, owner, run_id):
        row = db.execute("SELECT * FROM crm_research_runs WHERE owner=? AND id=?", (owner, run_id)).fetchone()
        if row is None:
            raise KeyError("未找到你的研究草稿")
        return row

    def _identity(self, db, owner, customer_id):
        row = db.execute("SELECT name FROM crm_customers WHERE owner=? AND id=?", (owner, customer_id)).fetchone()
        if row is None:
            raise KeyError("未找到你的单位")
        name = _text(row["name"], "单位正式名称", 200, required=True).strip()
        settings = db.execute("SELECT official_domains_json FROM crm_profile_customer_settings WHERE owner=? AND customer_id=?", (owner, customer_id)).fetchone()
        return {"name": name, "official_domains": normalize_domains(json.loads(settings[0]) if settings else [])}

    def _matches_identity(self, db, run):
        return self._identity(db, run["owner"], run["customer_id"]) == json.loads(run["identity_json"])

    @staticmethod
    def _revision(data, row):
        if type(data.get("expected_revision")) is not int or data["expected_revision"] != row["revision"]:
            raise ResearchConflict("草稿已更新，请重新查看；当前输入仍可保留。")

    def create(self, owner, customer_id, data):
        owner, customer_id = _owner(owner), _identifier(customer_id)
        if not isinstance(data, dict) or set(data) != {"mode", "request_id"} or data.get("mode") not in ("quick", "deep"):
            raise ValueError("请选择快速或深入研究，并提供请求编号。")
        request_id = _text(data["request_id"], "研究请求编号", 200, required=True).strip()
        if not request_id:
            raise ValueError("研究请求编号不能为空")
        signature, now = _hash([customer_id, data["mode"]]), _timestamp(self.clock())
        with self.crm._transaction() as db:
            identity = self._identity(db, owner, customer_id)
            prior = db.execute("SELECT * FROM crm_research_requests WHERE owner=? AND request_id=?", (owner, request_id)).fetchone()
            if prior:
                if prior["signature"] != signature:
                    raise ResearchConflict("同一请求编号不能用于不同的研究。")
                run_id, created, reused = prior["run_id"], False, False
            else:
                old = db.execute("SELECT * FROM crm_research_runs WHERE owner=? AND customer_id=? AND mode=? AND identity_json=? AND status IN ('queued','searching','analyzing','ready','partial') AND created_at>=? ORDER BY id DESC LIMIT 1",
                    (owner, customer_id, data["mode"], _json(identity), now - 86400)).fetchone()
                created, reused = old is None, old is not None
                run_id = old["id"] if old else db.execute("INSERT INTO crm_research_runs(owner,customer_id,mode,identity_json,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                    (owner, customer_id, data["mode"], _json(identity), now, now)).lastrowid
                db.execute("INSERT INTO crm_research_requests VALUES (?,?,?,?)", (owner, request_id, signature, run_id))
        return {"run": self.get(owner, run_id), "created": created, "reused": reused}

    def queue_automatic(self, owner, customer_id, request_id):
        """Backend scheduler shares active manual/deep work instead of searching twice."""
        owner, customer_id = _owner(owner), _identifier(customer_id)
        request_id = _text(request_id, "自动研究请求编号", 200, required=True).strip()
        with self.crm._lock:
            identity = self._identity(self.crm._db, owner, customer_id)
            settings = self.crm._db.execute("SELECT enabled,auto_research FROM crm_profile_settings WHERE owner=?", (owner,)).fetchone()
            if settings is None or not settings["enabled"] or not settings["auto_research"] or not identity["official_domains"]:
                return {"run": None, "created": False, "reused": False, "blocked": True,
                        "message": "自动研究已暂停或尚未核对官方域名，手动研究任务继续保留。"}
            row = self.crm._db.execute("SELECT id FROM crm_research_runs WHERE owner=? AND customer_id=? AND identity_json=? AND status IN ('queued','searching','analyzing') ORDER BY id DESC LIMIT 1", (owner, customer_id, _json(identity))).fetchone()
            # Hold the reentrant connection lock, not an outer SQL transaction:
            # settings cannot change between this check and the queue write.
            if row:
                return {"run": self.get(owner, row[0]), "created": False, "reused": True}
            return self.create(owner, customer_id, {"mode": "quick", "request_id": request_id})

    def _render(self, db, run):
        owner, run_id, customer_id = run["owner"], run["id"], run["customer_id"]
        result = {key: run[key] for key in ("id", "customer_id", "mode", "status", "stage", "revision", "created_at", "updated_at")}
        items = []
        for draft in db.execute("SELECT d.*,c.id AS cid FROM crm_research_drafts d JOIN crm_profile_candidates c ON c.owner=d.owner AND c.id=d.candidate_id WHERE d.owner=? AND d.run_id=? ORDER BY d.candidate_id", (owner, run_id)):
            row = db.execute("SELECT * FROM crm_profile_candidates WHERE owner=? AND id=?", (owner, draft["candidate_id"])).fetchone()
            candidate = self.profile._candidate(db, row)
            captured = json.loads(draft["captured_json"])
            candidate.update(selected=bool(draft["selected"]), draft_value=draft["value"],
                captured_current_fact_id=captured["id"] if captured else None, captured_current_value=captured,
                changed_since_capture=captured != candidate["current_value"])
            items.append(candidate)
        sources = [dict(row) for row in db.execute("SELECT s.id,s.url,s.title,s.published_at,s.fetched_at,s.identity_verified,s.identity_reason,r.status,r.error FROM crm_research_sources r JOIN crm_profile_public_sources s ON s.owner=r.owner AND s.id=r.source_id WHERE r.owner=? AND r.run_id=? ORDER BY s.id", (owner, run_id))]
        for source in sources:
            source["identity_verified"] = bool(source["identity_verified"])
        # Historical/rejected drafts remain visible, but are no longer usable
        # clues. Confirmed fields are accounted for by the latest fact below.
        present = {x["key"] for x in items if x["scope"] == "account" and x["status"] == "pending"}
        known = {x["key"] for x in db.execute("SELECT f.key FROM crm_customer_facts f WHERE f.owner=? AND f.customer_id=? AND f.contact_id IS NULL AND trim(f.value)!='' AND f.id=(SELECT MAX(n.id) FROM crm_customer_facts n WHERE n.owner=f.owner AND n.customer_id=f.customer_id AND n.contact_id IS NULL AND n.key=f.key)", (owner, customer_id))}
        gaps = [{"key": key, "label": ACCOUNT_FIELDS[key]["label"], "scope": "account", "message": "未找到可安全填写的公开依据，可在交流中逐步补充。"}
                for key in sorted(ACCOUNT_BACKGROUND_KEYS - present - known)]
        recommendations = []
        if sources:
            recommendations.append({"kind": "verification", "title": "核对单位身份与资料时效", "reason": "公开资料属于观察，发布日与获取日不同；历史公告不能当成当前承诺。", "scope": "account", "opportunity_id": None,
                "source_ids": [x["id"] for x in sources[:5]], "analysis_mode": "rules"})
        if any(x["scope"] == "project" for x in items):
            recommendations.append({"kind": "verification", "title": "将公开采购线索核对到具体项目", "reason": "预算、需求与采购节点不能成为整个单位的通用事实。", "scope": "project", "opportunity_id": None,
                "source_ids": [x["id"] for x in sources[:5]], "analysis_mode": "rules"})
        errors = json.loads(run["errors_json"])
        messages = {"queued": "研究已排队，关闭页面不会丢失任务。", "searching": "正在检索公开资料。", "analyzing": "资料已保存，正在形成带证据的草稿。",
            "ready": "研究草稿已生成，请编辑并核对后采用。" if items else "资料已分析，未发现可安全填写的新字段；可查看来源或补充交流。",
            "partial": "部分检索或分析未完成，已有来源和草稿已保留，可重试。", "failed": "研究未完成，原资料未改动；可配置服务或重试。",
            "cancelled": "研究已取消，已保存的来源和草稿继续保留。", "needs_review": "单位名称或官方域名已变化，请核对身份后重新发起研究。"}
        if result["status"] == "ready" and not sources:
            messages["ready"] = "检索已完成，本次没有找到符合范围的公开资料；请核对单位正式名称或补充官网域名后重试，原画像保留。"
        result.update(items=items, sources=sources, summary={"source_count": len(sources), "candidate_count": len(items),
            "selected_count": sum(x["selected"] for x in items), "confirmed_count": sum(x["status"] == "confirmed" for x in items),
            "failed_count": len(errors) + sum(x["status"] == "failed" for x in sources)}, gaps=gaps,
            recommendations=recommendations, errors=errors, message=messages[result["status"]], capabilities=self.capabilities())
        return result

    def get(self, owner, run_id):
        owner, run_id = _owner(owner), _identifier(run_id)
        with self.crm._transaction() as db:
            row = db.execute("SELECT * FROM crm_research_runs WHERE owner=? AND id=?", (owner, run_id)).fetchone()
            if row is None:
                return None
            self.profile._invalidate(db, owner, self.profile._sources(db, owner, row["customer_id"]), row["customer_id"])
            if row["status"] != "cancelled" and not self._matches_identity(db, row):
                db.execute("UPDATE crm_research_runs SET status='needs_review',stage='needs_review',lease_token=NULL,lease_until=NULL,revision=revision+1,updated_at=? WHERE owner=? AND id=? AND status!='needs_review'", (self.clock(), owner, run_id))
                row = self._require(db, owner, run_id)
            return self._render(db, row)

    def list_runs(self, owner, customer_id):
        owner, customer_id = _owner(owner), _identifier(customer_id)
        with self.crm._lock:
            self.crm._require_customer(self.crm._db, owner, customer_id)
            ids = [row[0] for row in self.crm._db.execute("SELECT id FROM crm_research_runs WHERE owner=? AND customer_id=? ORDER BY id DESC LIMIT 30", (owner, customer_id))]
        return {"runs": [self.get(owner, x) for x in ids], "capabilities": self.capabilities()}

    def edit_draft(self, owner, run_id, data):
        owner, run_id = _owner(owner), _identifier(run_id)
        if not isinstance(data, dict) or set(data) != {"expected_revision", "items"} or not isinstance(data["items"], list) or not 1 <= len(data["items"]) <= 60:
            raise ValueError("请提供需要保存的草稿字段。")
        with self.crm._transaction() as db:
            run = self._require(db, owner, run_id)
            self._revision(data, run)
            if run["status"] in ("cancelled", "needs_review") or not self._matches_identity(db, run):
                raise ResearchConflict("研究身份或状态已变化，请重新查看。")
            seen = set()
            for item in data["items"]:
                if not isinstance(item, dict) or set(item) - {"candidate_id", "value", "selected"} or "candidate_id" not in item:
                    raise ValueError("草稿字段无效")
                candidate_id = _identifier(item["candidate_id"])
                if candidate_id in seen:
                    raise ValueError("同一草稿字段不能重复保存")
                seen.add(candidate_id)
                row = db.execute("SELECT * FROM crm_research_drafts WHERE owner=? AND run_id=? AND candidate_id=?", (owner, run_id, candidate_id)).fetchone()
                if row is None:
                    raise KeyError("该字段不属于此研究草稿")
                value = _text(item["value"], "草稿内容", 2000, required=True).strip() if "value" in item else row["value"]
                if "selected" in item and type(item["selected"]) is not bool:
                    raise ValueError("是否采用必须明确选择")
                selected = int(item.get("selected", bool(row["selected"])))
                db.execute("UPDATE crm_research_drafts SET value=?,selected=? WHERE owner=? AND run_id=? AND candidate_id=?", (value, selected, owner, run_id, candidate_id))
            db.execute("UPDATE crm_research_runs SET revision=revision+1,updated_at=? WHERE owner=? AND id=?", (self.clock(), owner, run_id))
        return self.get(owner, run_id)

    def _control(self, owner, run_id, data, cancel):
        owner, run_id = _owner(owner), _identifier(run_id)
        if not isinstance(data, dict) or set(data) != {"expected_revision"}:
            raise ValueError("请核对当前研究版本")
        with self.crm._transaction() as db:
            run = self._require(db, owner, run_id)
            self._revision(data, run)
            if not cancel and not self._matches_identity(db, run):
                raise ResearchConflict("单位身份已变化，请重新发起研究。")
            if not cancel:
                db.execute("UPDATE crm_research_sources SET status='saved',error='' WHERE owner=? AND run_id=? AND status='failed'", (owner, run_id))
            db.execute("UPDATE crm_research_runs SET status=?,stage=?,revision=revision+1,lease_token=NULL,lease_until=NULL,queries_json=?,errors_json='[]',updated_at=? WHERE owner=? AND id=?",
                ("cancelled" if cancel else "queued", "cancelled" if cancel else "queued", run["queries_json"] if cancel else "[]", self.clock(), owner, run_id))
        return self.get(owner, run_id)

    def retry(self, owner, run_id, data):
        return self._control(owner, run_id, data, False)

    def cancel(self, owner, run_id, data):
        return self._control(owner, run_id, data, True)

    def _claim(self, owner):
        if self.closed:
            return None
        with self.crm._transaction() as db:
            now = self.clock()
            row = db.execute("SELECT * FROM crm_research_runs WHERE owner=? AND (status='queued' OR (status IN ('searching','analyzing') AND COALESCE(lease_until,0)<=?)) ORDER BY id LIMIT 1", (owner, now)).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            db.execute("UPDATE crm_research_runs SET status='searching',stage='searching',lease_token=?,lease_until=?,updated_at=? WHERE owner=? AND id=?", (token, now + 180, now, owner, row["id"]))
            return {**dict(row), "lease_token": token}

    def _guard(self, db, run, stage=None):
        live = self._require(db, run["owner"], run["id"])
        if live["lease_token"] != run["lease_token"] or live["status"] not in ("searching", "analyzing") or self.closed:
            raise _LeaseLost()
        if not self._matches_identity(db, live):
            db.execute("UPDATE crm_research_runs SET status='needs_review',stage='needs_review',lease_token=NULL,lease_until=NULL,revision=revision+1,updated_at=? WHERE owner=? AND id=?", (self.clock(), run["owner"], run["id"]))
            return False
        db.execute("UPDATE crm_research_runs SET lease_until=?,updated_at=?" + (",status=?,stage=?" if stage else "") + " WHERE owner=? AND id=?",
            [self.clock() + 180, self.clock()] + ([stage, stage] if stage else []) + [run["owner"], run["id"]])
        return True

    def _save_source(self, db, run, data):
        if not isinstance(data, dict) or set(data) - {"url", "title", "text", "published_at", "fetched_at", "entity_name", "identity_reason"}:
            raise ValueError("公开资料字段无效")
        identity = json.loads(run["identity_json"])
        url = public_url(data.get("url"))
        title = _text(data.get("title"), "公开资料标题", 500, required=True)
        text = _text(data.get("text"), "公开资料原文", 20000, required=True)
        published = _text(data["published_at"], "发布日期", 100, required=True) if data.get("published_at") is not None else None
        fetched = _timestamp(data.get("fetched_at", self.clock()))
        entity = _text(data.get("entity_name", identity["name"]), "单位名称", 200, required=True)
        host = urlsplit(url).hostname.lower()
        official = any(host == d or host.endswith("." + d) for d in identity["official_domains"])
        if not official and (entity != identity["name"] or identity["name"] not in title + "\n" + text):
            raise ValueError("资料尚未匹配单位身份")
        digest = _hash([url, title, text, published])
        existing = db.execute("SELECT id FROM crm_profile_public_sources WHERE owner=? AND customer_id=? AND dedupe_key=?", (run["owner"], run["customer_id"], digest)).fetchone()
        linked = existing and db.execute("SELECT 1 FROM crm_research_sources WHERE owner=? AND run_id=? AND source_id=?", (run["owner"], run["id"], existing[0])).fetchone()
        if not linked and db.execute("SELECT count(*) FROM crm_research_sources WHERE owner=? AND run_id=?", (run["owner"], run["id"])).fetchone()[0] >= 15:
            raise _SourceLimit()
        db.execute("INSERT OR IGNORE INTO crm_profile_public_sources(owner,customer_id,url,title,text,entity_name,published_at,fetched_at,identity_verified,identity_reason,dedupe_key,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (run["owner"], run["customer_id"], url, title, text, entity, published, fetched, int(official), public_source_identity_reason(data, official), digest, self.clock()))
        source_id = db.execute("SELECT id FROM crm_profile_public_sources WHERE owner=? AND customer_id=? AND dedupe_key=?", (run["owner"], run["customer_id"], digest)).fetchone()[0]
        db.execute("INSERT OR IGNORE INTO crm_research_sources(owner,run_id,source_id) VALUES (?,?,?)", (run["owner"], run["id"], source_id))
        source = next(x for x in self.profile._sources(db, run["owner"], run["customer_id"]) if x["type"] == "public" and x["id"] == source_id)
        # A regular profile worker must not steal a slow/cancelled research
        # source. Existing source jobs retain their original management policy.
        db.execute("INSERT OR IGNORE INTO crm_profile_source_jobs(owner,source_type,source_id,fingerprint,customer_id,status,updated_at) VALUES (?,'public',?,?,?,'research_managed',?)", (run["owner"], source_id, source["fingerprint"], run["customer_id"], self.clock()))

    def _record_schedule(self, db, run, success):
        owner, customer_id, now = run["owner"], run["customer_id"], self.clock()
        db.execute("INSERT OR IGNORE INTO crm_profile_customer_settings(owner,customer_id) VALUES (?,?)", (owner, customer_id))
        if success:
            db.execute("UPDATE crm_profile_customer_settings SET last_research_at=?,research_error='',research_attempts=0,research_retry_at=0 WHERE owner=? AND customer_id=?", (now, owner, customer_id))
        else:
            settings = db.execute("SELECT research_attempts FROM crm_profile_customer_settings WHERE owner=? AND customer_id=?", (owner, customer_id)).fetchone()
            db.execute("UPDATE crm_profile_customer_settings SET last_research_at=NULL,research_error=?,research_attempts=research_attempts+1,research_retry_at=? WHERE owner=? AND customer_id=?", ("公开研究部分步骤未完成，已有资料保留，可稍后重试。", now + min(86400, 300 * 2 ** min(settings[0], 9)), owner, customer_id))

    async def _process(self, run):
        owner, run_id, token = run["owner"], run["id"], run["lease_token"]
        try:
            if self.researcher is None:
                with self.crm._transaction() as db:
                    if self._guard(db, run):
                        db.execute("UPDATE crm_research_runs SET status='failed',stage='review',lease_token=NULL,lease_until=NULL,revision=revision+1,errors_json=?,updated_at=? WHERE owner=? AND id=?",
                            (_json([{"stage": "searching", "message": "公开检索服务未配置，可先粘贴公开资料。"}]), self.clock(), owner, run_id))
                        self._record_schedule(db, run, False)
                return
            themes = ["overview"] if run["mode"] == "quick" else ["background", "digitalization", "procurement"]
            complete = json.loads(run["queries_json"])
            identity = json.loads(run["identity_json"])
            errors = json.loads(run["errors_json"])
            for theme in themes:
                if theme in complete:
                    continue
                with self.crm._transaction() as db:
                    if not self._guard(db, run, "searching"):
                        return
                context = {"customer": {"id": run["customer_id"], "name": identity["name"]}, "official_domains": identity["official_domains"],
                    "research_domains": identity["official_domains"], "mode": run["mode"], "theme": theme}
                try:
                    # Native search may also read two public result pages when
                    # citations are absent: 40s search + 20s reading, bounded.
                    timeout = 70 if getattr(self.researcher, 'provider_id', None) == 'deepseek' else 45
                    rows = await asyncio.wait_for(_await(self.researcher.research(context)), timeout)
                    if not isinstance(rows, list) or len(rows) > 20:
                        raise ValueError("公开检索结果数量无效")
                    with self.crm._transaction() as db:
                        if not self._guard(db, run):
                            return
                        for item in rows[:5]:
                            try:
                                self._save_source(db, run, item)
                            except _SourceLimit:
                                error = {"stage": "searching", "message": "本次已保留15份来源，新增资料未纳入；请先核对现有草稿。"}
                                if error not in errors:
                                    errors.append(error)
                            except ValueError:
                                errors.append({"stage": "searching", "message": "一份公开资料未通过地址、单位身份或内容校验。"})
                        complete.append(theme)
                        db.execute("UPDATE crm_research_runs SET queries_json=?,errors_json=?,updated_at=? WHERE owner=? AND id=?", (_json(complete), _json(errors), self.clock(), owner, run_id))
                except _LeaseLost:
                    raise
                except Exception as error:
                    message = str(error) if isinstance(error, PublicResearchError) else "一组公开检索未完成，可重试；已存资料保留。"
                    errors.append({"stage": "searching", "message": message})
                    with self.crm._transaction() as db:
                        if not self._guard(db, run):
                            return
                        db.execute("UPDATE crm_research_runs SET errors_json=?,updated_at=? WHERE owner=? AND id=?", (_json(errors), self.clock(), owner, run_id))
            with self.crm._transaction() as db:
                if not self._guard(db, run, "analyzing"):
                    return
                source_ids = [x[0] for x in db.execute("SELECT source_id FROM crm_research_sources WHERE owner=? AND run_id=? AND status!='analyzed' ORDER BY source_id LIMIT 15", (owner, run_id))]
            for source_id in source_ids:
                with self.crm._transaction() as db:
                    if not self._guard(db, run, "analyzing"):
                        return
                    source = next((x for x in self.profile._sources(db, owner, run["customer_id"]) if x["type"] == "public" and x["id"] == source_id), None)
                    context = self.profile._context(db, owner, run["customer_id"])
                try:
                    if source is None:
                        raise ValueError("公开来源已失效")
                    attributes = await asyncio.wait_for(_await(self.profile.analyzer.extract(source, context)), 60) if self.profile.analyzer else _rule_extract(source, context)
                    with self.crm._transaction() as db:
                        if not self._guard(db, run):
                            return
                        fresh = next((x for x in self.profile._sources(db, owner, run["customer_id"]) if x["type"] == "public" and x["id"] == source_id), None)
                        if not fresh or fresh["fingerprint"] != source["fingerprint"]:
                            raise ValueError("来源在分析期间变化")
                        self.profile._store_candidates(db, owner, source, attributes, context)
                        for candidate in db.execute("SELECT * FROM crm_profile_candidates WHERE owner=? AND customer_id=? AND source_type='public' AND source_id=? AND source_fingerprint=? AND status IN ('pending','confirmed','rejected') ORDER BY id", (owner, run["customer_id"], source_id, source["fingerprint"])):
                            current = self.profile._current(db, owner, run["customer_id"], candidate["scope"], candidate["key"], candidate["opportunity_id"], candidate["contact_id"])
                            db.execute("INSERT OR IGNORE INTO crm_research_drafts VALUES (?,?,?,?,0,?)", (owner, run_id, candidate["id"], candidate["value"], _json(current)))
                        db.execute("UPDATE crm_research_sources SET status='analyzed',error='' WHERE owner=? AND run_id=? AND source_id=?", (owner, run_id, source_id))
                        db.execute("INSERT OR IGNORE INTO crm_profile_source_jobs(owner,source_type,source_id,fingerprint,customer_id,status,updated_at) VALUES (?,'public',?,?,?,'complete',?)", (owner, source_id, source["fingerprint"], run["customer_id"], self.clock()))
                        db.execute("UPDATE crm_profile_source_jobs SET status='complete',error='',updated_at=? WHERE owner=? AND source_type='public' AND source_id=? AND fingerprint=? AND status='research_managed'", (self.clock(), owner, source_id, source["fingerprint"]))
                        db.execute("UPDATE crm_research_runs SET revision=revision+1,updated_at=? WHERE owner=? AND id=?", (self.clock(), owner, run_id))
                except _LeaseLost:
                    raise
                except Exception:
                    with self.crm._transaction() as db:
                        if not self._guard(db, run):
                            return
                        db.execute("UPDATE crm_research_sources SET status='failed',error=? WHERE owner=? AND run_id=? AND source_id=?", ("资料已保存但画像分析未完成，可重试。", owner, run_id, source_id))
            with self.crm._transaction() as db:
                if not self._guard(db, run):
                    return
                count = db.execute("SELECT count(*) FROM crm_research_sources WHERE owner=? AND run_id=?", (owner, run_id)).fetchone()[0]
                failed = db.execute("SELECT count(*) FROM crm_research_sources WHERE owner=? AND run_id=? AND status!='analyzed'", (owner, run_id)).fetchone()[0]
                status = "partial" if count and (errors or failed) else "failed" if errors and not count else "ready"
                db.execute("UPDATE crm_research_runs SET status=?,stage='review',errors_json=?,lease_token=NULL,lease_until=NULL,revision=revision+1,updated_at=? WHERE owner=? AND id=?", (status, _json(errors[:30]), self.clock(), owner, run_id))
                self._record_schedule(db, run, status == "ready" or count > failed)
        except asyncio.CancelledError:
            with self.crm._transaction() as db:
                db.execute("UPDATE crm_research_runs SET status='queued',stage='queued',lease_token=NULL,lease_until=NULL,updated_at=? WHERE owner=? AND id=? AND lease_token=? AND status IN ('searching','analyzing')", (self.clock(), owner, run_id, token))
            raise
        except _LeaseLost:
            return

    async def process_pending(self, owner, limit=1):
        owner = _owner(owner)
        if type(limit) is not int or not 1 <= limit <= 3:
            raise ValueError("每轮最多处理三个研究任务")
        ids = []
        for _ in range(limit):
            run = self._claim(owner)
            if run is None:
                break
            await self._process(run)
            ids.append(run["id"])
        return {"processed": len(ids), "run_ids": ids}

    @staticmethod
    def _item_result(candidate_id, status, code, message, fact_id=None):
        result = {"candidate_id": candidate_id, "status": status, "code": code, "message": message}
        if fact_id is not None:
            result["fact_id"] = fact_id
        return result

    def _recovered_decision(self, owner, candidate_id, body):
        with self.crm._lock:
            row = self.crm._db.execute("SELECT * FROM crm_profile_candidates WHERE owner=? AND id=?", (owner, candidate_id)).fetchone()
            if row is None or row["status"] != "confirmed" or row["scope"] != "account":
                return None
            fact = self.crm._db.execute("SELECT * FROM crm_customer_facts WHERE owner=? AND customer_id=? AND id=? AND contact_id IS NULL", (owner, row["customer_id"], row["confirmed_fact_id"])).fetchone()
            if fact is None or fact["value"] != body["value"] or fact["basis"] != "observation":
                return None
            for audit in self.crm._db.execute("SELECT details_json FROM crm_profile_candidate_decisions WHERE owner=? AND candidate_id=? AND decision='confirmed' ORDER BY id DESC", (owner, candidate_id)):
                if json.loads(audit[0]) == body:
                    return row["confirmed_fact_id"]
        return None

    def confirm(self, owner, run_id, data):
        owner, run_id = _owner(owner), _identifier(run_id)
        if not isinstance(data, dict) or set(data) != {"request_id", "expected_revision", "items"} or not isinstance(data["items"], list) or not 1 <= len(data["items"]) <= 20:
            raise ValueError("请提供本次要采用的草稿字段。")
        request_id = _text(data["request_id"], "采用请求编号", 200, required=True).strip()
        if not request_id:
            raise ValueError("采用请求编号不能为空")
        signature = _hash([run_id, data])
        with self.crm._transaction() as db:
            run = self._require(db, owner, run_id)
            prior = db.execute("SELECT * FROM crm_research_batches WHERE owner=? AND request_id=?", (owner, request_id)).fetchone()
            if prior:
                if prior["signature"] != signature or prior["run_id"] != run_id:
                    raise ResearchConflict("同一采用请求编号不能用于不同内容。")
                replayed = prior["status"] == "complete"
            else:
                self._revision(data, run)
                if run["status"] not in ("ready", "partial") or not self._matches_identity(db, run):
                    raise ResearchConflict("研究尚未完成或单位身份已变化，请重新查看。")
                prepared, keys = [], set()
                for index, item in enumerate(data["items"]):
                    allowed = {"candidate_id", "expected_candidate_revision", "expected_fact_id", "value", "verify_public", "verify_entity"}
                    if not isinstance(item, dict) or set(item) - allowed or not {"candidate_id", "expected_candidate_revision", "expected_fact_id"} <= set(item):
                        raise ValueError("请先核对字段版本和已有资料")
                    candidate_id = _identifier(item["candidate_id"])
                    draft = db.execute("SELECT d.*,c.scope,c.key,c.source_type FROM crm_research_drafts d JOIN crm_profile_candidates c ON c.owner=d.owner AND c.id=d.candidate_id WHERE d.owner=? AND d.run_id=? AND d.candidate_id=? AND c.customer_id=? AND EXISTS(SELECT 1 FROM crm_research_sources s WHERE s.owner=d.owner AND s.run_id=d.run_id AND s.source_id=c.source_id)", (owner, run_id, candidate_id, run["customer_id"])).fetchone()
                    if draft is None:
                        raise KeyError("该候选不属于此研究草稿")
                    if draft["scope"] != "account" or draft["source_type"] != "public" or draft["key"] not in ACCOUNT_BACKGROUND_KEYS:
                        raise ValueError("批量采用仅支持单位背景；项目、人物与权限请分别核对。")
                    if draft["key"] in keys:
                        raise ValueError("同一字段有多个候选，请选一个后采用。")
                    keys.add(draft["key"])
                    if type(item["expected_candidate_revision"]) is not int or item["expected_candidate_revision"] < 1:
                        raise ValueError("候选版本无效")
                    if item["expected_fact_id"] is not None:
                        _identifier(item["expected_fact_id"])
                    for key in ("verify_public", "verify_entity"):
                        if key in item and type(item[key]) is not bool:
                            raise ValueError("资料核对必须明确勾选")
                    value = _text(item.get("value", draft["value"]), "确认内容", 2000, required=True).strip()
                    body = {"decision": "confirm", "expected_revision": item["expected_candidate_revision"],
                            "expected_fact_id": item["expected_fact_id"], "value": value, "basis": "observation",
                            "verify_public": item.get("verify_public", False), "verify_entity": item.get("verify_entity", False)}
                    prepared.append((candidate_id, index, body))
                now = self.clock()
                db.execute("INSERT INTO crm_research_batches(owner,request_id,run_id,signature,payload_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?)", (owner, request_id, run_id, signature, _json(data), now, now))
                for candidate_id, index, body in prepared:
                    db.execute("INSERT INTO crm_research_batch_items(owner,request_id,candidate_id,ordinal,decision_json) VALUES (?,?,?,?,?)", (owner, request_id, candidate_id, index, _json(body)))
                    db.execute("UPDATE crm_research_drafts SET value=?,selected=1 WHERE owner=? AND run_id=? AND candidate_id=?", (body["value"], owner, run_id, candidate_id))
                db.execute("UPDATE crm_research_runs SET revision=revision+1,updated_at=? WHERE owner=? AND id=?", (now, owner, run_id))
                replayed = False
        if not replayed:
            with self.crm._lock:
                rows = [dict(x) for x in self.crm._db.execute("SELECT * FROM crm_research_batch_items WHERE owner=? AND request_id=? ORDER BY ordinal", (owner, request_id))]
            for item in rows:
                if item["status"] == "complete":
                    continue
                candidate_id, body = item["candidate_id"], json.loads(item["decision_json"])
                fact_id = self._recovered_decision(owner, candidate_id, body) if item["status"] == "applying" else None
                if fact_id is not None:
                    result = self._item_result(candidate_id, "already_confirmed", 200, "上次采用已完成，已恢复结果且未重复写入。", fact_id)
                else:
                    with self.crm._transaction() as db:
                        live = self._require(db, owner, run_id)
                        if live["status"] in ("cancelled", "needs_review") or not self._matches_identity(db, live):
                            result = self._item_result(candidate_id, "conflict", 409, "研究身份或状态已变化，原草稿保留。")
                        else:
                            candidate = db.execute("SELECT * FROM crm_profile_candidates WHERE owner=? AND id=?", (owner, candidate_id)).fetchone()
                            if candidate["status"] == "confirmed":
                                fact = db.execute("SELECT * FROM crm_customer_facts WHERE owner=? AND id=?", (owner, candidate["confirmed_fact_id"])).fetchone()
                                if fact and fact["value"] == body["value"] and fact["basis"] == "observation":
                                    result = self._item_result(candidate_id, "already_confirmed", 200, "该候选此前已采用，本次没有重复写入。", fact["id"])
                                else:
                                    result = self._item_result(candidate_id, "conflict", 409, "该候选已采用为其他内容，不能用旧候选改写；草稿仍保留。")
                            elif candidate["status"] != "pending":
                                result = self._item_result(candidate_id, "conflict", 409, "该候选已处理或来源已变化，请重新核对。")
                            else:
                                db.execute("UPDATE crm_research_batch_items SET status='applying' WHERE owner=? AND request_id=? AND candidate_id=?", (owner, request_id, candidate_id))
                                result = None
                    if result is None:
                        try:
                            candidate = self.profile.decide(owner, candidate_id, body)
                            result = self._item_result(candidate_id, "confirmed", 200, "已作为公开观察保存，历史继续保留。", candidate["confirmed_fact_id"])
                        except ProfileConflict:
                            result = self._item_result(candidate_id, "conflict", 409, "已有资料或候选已变化，请刷新核对；草稿保留。")
                        except (KeyError, ValueError):
                            result = self._item_result(candidate_id, "blocked", 400, "请核对原文可信度、发布日期和单位身份后采用。")
                        except Exception:
                            committed = self._recovered_decision(owner, candidate_id, body)
                            result = (self._item_result(candidate_id, "already_confirmed", 200, "保存已完成，已核对审计并恢复回执，未重复写入。", committed)
                                      if committed is not None else self._item_result(candidate_id, "failed", 500, "本项暂未保存，草稿已保留，可重试。"))
                with self.crm._transaction() as db:
                    db.execute("UPDATE crm_research_batch_items SET status='complete',result_json=? WHERE owner=? AND request_id=? AND candidate_id=?", (_json(result), owner, request_id, candidate_id))
            with self.crm._transaction() as db:
                results = [json.loads(row[0]) for row in db.execute("SELECT result_json FROM crm_research_batch_items WHERE owner=? AND request_id=? ORDER BY ordinal", (owner, request_id))]
                db.execute("UPDATE crm_research_batches SET status='complete',results_json=?,updated_at=? WHERE owner=? AND request_id=?", (_json(results), self.clock(), owner, request_id))
                db.execute("UPDATE crm_research_runs SET revision=revision+1,updated_at=? WHERE owner=? AND id=?", (self.clock(), owner, run_id))
        else:
            results = json.loads(prior["results_json"])
        success = sum(x["status"] in ("confirmed", "already_confirmed") for x in results)
        return {"run": self.get(owner, run_id), "results": results,
                "status": "complete" if success == len(results) else "partial" if success else "blocked", "replayed": replayed}

    def close(self):
        self.closed = True
