"""One exchange, one durable review surface, existing services remain truth.

This module deliberately does not replace analyses, facts, visits or schedules.
It stores editable review state and a write-ahead, per-item decision journal.
No model is called while holding a database transaction.
"""
from __future__ import annotations

import asyncio
import copy
from datetime import date
import hashlib
import json
import re
import time

from .action_contract import TERM_FIELDS, EXECUTOR_KINDS
from .adoption_edits import edit_unconfirmed_action
from .crm import _owner, _identifier, _text, analysis_fingerprint, RecordConflict
from .profile_intelligence import ProfileConflict, _rule_extract, _evidence_fragments, _explicit_commitment
from .store import _timestamp
from .customer_timeline import TimelineConflict
from .action_people import (normalize_contact_ids, action_contact_options,
                            validate_action_contacts, bind_action_contacts, action_people_receipt)


class ExchangeConflict(ValueError):
    """The versions the user reviewed no longer describe current truth."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _version(value, label="版本"):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError(label + "无效")
    return value


class ExchangeWorkspace:
    def __init__(self, crm, workspace, profile_intelligence, *, materials=None, visits=None,
                 timeline=None, organizer=None, clock=time.time):
        self.crm, self.workspace, self.profile = crm, workspace, profile_intelligence
        self.materials, self.visits, self.timeline = materials, visits, timeline
        self.organizer, self.clock = organizer, clock
        with crm._transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS crm_exchange_workspaces (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL,
                  source_type TEXT NOT NULL,source_id INTEGER NOT NULL,
                  revision INTEGER NOT NULL DEFAULT 1,source_revision TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'draft',drafts_json TEXT NOT NULL DEFAULT '{}',
                  message TEXT NOT NULL DEFAULT '',lease TEXT,created_at REAL NOT NULL,updated_at REAL NOT NULL,
                  UNIQUE(owner,source_type,source_id));
                CREATE TABLE IF NOT EXISTS crm_exchange_batches (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,owner TEXT NOT NULL,workspace_id INTEGER NOT NULL,
                  request_id TEXT NOT NULL,signature TEXT NOT NULL,request_json TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'processing',response_json TEXT,
                  created_at REAL NOT NULL,updated_at REAL NOT NULL,
                  UNIQUE(owner,request_id));
                CREATE TABLE IF NOT EXISTS crm_exchange_batch_items (
                  owner TEXT NOT NULL,batch_id INTEGER NOT NULL,item_id TEXT NOT NULL,
                  item_json TEXT NOT NULL,request_json TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
                  stage TEXT NOT NULL DEFAULT '',checkpoint_json TEXT NOT NULL DEFAULT '{}',
                  result_json TEXT,updated_at REAL NOT NULL,
                  PRIMARY KEY(owner,batch_id,item_id));
            """)
            # NULL distinguishes historical drafts from a newly opened empty
            # workspace. The bookmark is independent of the business revision.
            for table, columns in (("crm_exchange_workspaces", {
                    "review_pending": "INTEGER", "review_revision": "INTEGER NOT NULL DEFAULT 0",
                    "review_source_guard": "TEXT"}), ("crm_exchange_batches", {
                    "review_revision": "INTEGER", "workspace_revision": "INTEGER"})):
                existing = {row[1] for row in db.execute("PRAGMA table_info(" + table + ")")}
                for name, definition in columns.items():
                    if name not in existing:
                        db.execute("ALTER TABLE " + table + " ADD COLUMN " + name + " " + definition)

    @staticmethod
    def _source_key(kind, identifier):
        if kind not in ("record", "material", "visit"):
            raise ValueError("交流来源类型无效")
        return kind, _identifier(identifier)

    @staticmethod
    def _source_revision(packet):
        excluded = {"material_revision", "visit_revision", "processing_status", "event_key", "revision"}
        return _hash({key: value for key, value in packet.items() if key not in excluded})

    @staticmethod
    def _proposal_snapshot(proposal):
        return {key: proposal.get(key) for key in ("id", "title", "remind_at", "duration_minutes", "deadline_at",
                "schedule_note", "target_task_id", "expected_task_revision", "change_kind", "created_at", "updated_at", "status", "task_id")}

    @staticmethod
    def _context_matches(stored, draft):
        def normalized(key, value):
            return sorted(value or [], key=lambda item: item["contact_id"]) if key == "contact_relations" else value
        return all(normalized(key, stored.get(key)) == normalized(key, value) for key, value in draft.items())

    def _prepare_source_guard(self, db, owner, kind, identifier):
        """Read canonical evidence without detail methods that resume writes.

        Used inside the candidate writer transaction. Material.detail and
        Visit.detail intentionally reconcile/bridge drafts, so cannot be used
        here. The timeline projection is raw SQL only, including all linked
        evidence, source choices, scope and valid source semantics.
        """
        reader = self.timeline
        if reader is None:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='crm_timeline_context_history'").fetchone():
                # Optional-timeline integrations keep their original schema.
                # Guard the same owned source graph, without creating tables.
                refs, attachments = [(kind, identifier)], []
                if kind == "visit":
                    for table, field, part in (("crm_visit_sources", "material_id", "material"), ("crm_visit_records", "record_id", "record")):
                        if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                            links = [dict(row) for row in db.execute("SELECT * FROM " + table + " WHERE owner=? AND visit_id=? ORDER BY " + field, (owner, identifier))]
                            attachments.extend(links)
                            refs.extend((part, row[field]) for row in links)
                    attachments.extend(dict(row) for row in db.execute(
                        "SELECT * FROM crm_visit_source_choices WHERE owner=? AND visit_id=? ORDER BY material_id", (owner, identifier)))
                graph = []
                for part, entity_id in refs:
                    table = {"record": "crm_records", "material": "crm_materials", "visit": "crm_visits"}[part]
                    row = db.execute("SELECT * FROM " + table + " WHERE owner=? AND id=?", (owner, entity_id)).fetchone()
                    if row is None:
                        raise ExchangeConflict("原来源不可用，旧整理回复未写入。")
                    operational = {"updated_at", "status", "proposal_id", "classified"} if part == "record" else {"updated_at"}
                    raw = {key: value for key, value in dict(row).items() if key not in operational}
                    version = db.execute("SELECT * FROM crm_material_versions WHERE owner=? AND material_id=? AND id=?", (owner, entity_id, row["current_version_id"])).fetchone() if part == "material" else None
                    link = db.execute("SELECT * FROM crm_opportunity_links WHERE owner=? AND entity_type=? AND entity_id=?", (owner, part, entity_id)).fetchone()
                    graph.append([part, raw, dict(version) if version else None, dict(link) if link else None])
                return _hash([graph, attachments])
            from .customer_timeline import TimelineService
            reader = object.__new__(TimelineService)
            reader.crm, reader.workspace, reader.clock = self.crm, self.workspace, self.clock
            reader.visits, reader.discussions = None, None
        event = reader._events(db, owner).get(kind + ":" + str(identifier))
        if event is None:
            raise ExchangeConflict("原来源不可用，旧整理回复未写入。")
        valid_context = not event.get("_context_stale") and not event.get("_source_needs_review")
        return _hash([event["_source_snapshot"], event["kind"] if valid_context else None,
                      event.get("occurred_at") if valid_context else None,
                      self.visits._project_graph(db, self.visits._require(db, owner, identifier)) if kind == "visit" and self.visits else None,
                      [dict(row) for row in db.execute("SELECT visit_id,role FROM crm_visit_records WHERE owner=? AND record_id=?", (owner, identifier))]
                      if kind == "record" and db.execute("SELECT 1 FROM sqlite_master WHERE name='crm_visit_records'").fetchone() else None])

    def _source(self, owner, kind, identifier):
        """Content/scope revision excludes adoption and schedule lifecycle state."""
        packet, refs, actions, drafts, previous, warnings = {}, [], [], [], [], []
        if kind == "record":
            record = self.crm.get_record(owner, identifier)
            if record is None or record.get("hidden", False):
                raise KeyError("未找到你的交流记录")
            analysis = self.crm.get_analysis(owner, identifier)
            packet = {"type": kind, "id": identifier, "title": record["title"], "text": record["content"],
                      "original_text": record["original_content"], "customer_id": record["customer_id"],
                      "category": record["category"], "occurred_at": None, "recorded_at": record["created_at"],
                      "record_id": identifier}
            refs = [("record", identifier)]
            if analysis:
                actions = [{**item, "_origin": "record", "_id": item["id"], "_source_id": identifier,
                            "_source_version": analysis["input_fingerprint"], "_stale": analysis["stale"]}
                           for item in analysis["actions"]]
                previous = analysis.get("previous_actions", [])
                if analysis["stale"]:
                    warnings.append("原文已纠正；旧行动依据已过期，已采用事项需逐项核对。")
            if hasattr(self.crm, "list_customer_drafts"):
                with self.crm._lock:
                    drafts = [self.crm._draft_public(row) for row in self.crm._db.execute(
                        "SELECT * FROM crm_customer_drafts WHERE owner=? AND source_record_id=? ORDER BY id",
                        (owner, identifier))]
        elif kind == "material":
            if self.materials is None:
                raise ValueError("录音资料服务尚未连接")
            detail = self.materials.detail(owner, identifier)
            if detail is None:
                raise KeyError("未找到你的录音资料")
            material = detail["material"]
            packet = {"type": kind, "id": identifier, "title": material["title"], "text": detail.get("text", ""),
                      "original_text": (detail.get("original_version") or {}).get("text", detail.get("text", "")),
                      "customer_id": material["customer_id"], "category": material.get("category"),
                      "occurred_at": material.get("occurred_at"), "recorded_at": material["created_at"],
                      "record_id": material.get("record_id"), "material_revision": material["revision"],
                      "version_id": material.get("current_version_id"), "processing_status": material["status"]}
            refs = [("material", identifier)]
            actions = [{**item, "_origin": "material", "_id": item["id"], "_source_id": identifier,
                        "_source_version": material["revision"], "_stale": material["status"] != "review"}
                       for item in (detail.get("analysis") or {}).get("actions", [])]
            drafts, previous = detail.get("customer_drafts", []), detail.get("previous_adoptions", [])
            warnings.extend(detail.get("warnings", []))
            grouped = self.visits.material_action_scopes(owner, identifier) if self.visits else None
            if grouped:
                for action in actions:
                    if action["_id"] in grouped:
                        action.update(copy.deepcopy(grouped[action["_id"]]))
                group_visit = next((action.get("visit_id") for action in actions if action.get("visit_id")), None)
                if group_visit:
                    packet["exchange_source_revision"] = self._source(owner, "visit", group_visit)[0]["revision"]
        else:
            if self.visits is None:
                raise ValueError("拜访交流服务尚未连接")
            detail = self.visits.detail(owner, identifier)
            visit = detail["visit"]
            sources = detail.get("sources", [])
            records = detail.get("record_sources", [])
            included = [row for row in sources if row.get("source_use", "included") == "included"]
            refs = [("material", row["material_id"]) for row in included]
            refs.extend(("record", row.get("record_id", row.get("id"))) for row in records)
            raw_parts = [{"type": "material", "id": row["material_id"], "role": row.get("role"),
                          "title": row.get("material", {}).get("title", ""), "category": row.get("material", {}).get("category"),
                          "text": row.get("text", ""), "original_text": (row.get("original_version") or {}).get("text"),
                          "version_id": row.get("material", {}).get("current_version_id"),
                          "occurred_at": row.get("material", {}).get("occurred_at"),
                          "recorded_at": row.get("material", {}).get("created_at"), "processing_status": row.get("material", {}).get("status"),
                          "source_use": row.get("source_use", "included")}
                         for row in sources]
            raw_parts.extend({"type": "record", "id": row.get("record_id", row.get("id")),
                              "title": row.get("record", {}).get("title", ""), "category": row.get("record", {}).get("category"),
                              "text": row.get("record", {}).get("content", row.get("text", "")),
                              "original_text": row.get("record", {}).get("original_content"), "role": row.get("role"),
                              "occurred_at": None, "recorded_at": row.get("record", {}).get("created_at"), "processing_status": None,
                              "source_use": "included"}
                             for row in records)
            packet = {"type": kind, "id": identifier, "title": visit["title"],
                      "text": "\n\n".join(row["text"] for row in raw_parts if row.get("text") and row.get("source_use", "included") == "included"),
                      "original_text": None, "customer_id": visit["customer_id"],
                      "occurred_at": visit.get("occurred_at"), "recorded_at": visit["created_at"],
                      "visit_revision": visit["revision"], "sources": raw_parts}
            actions = [{**item, "_origin": "visit", "_id": item["key"], "_source_id": identifier,
                        "_source_version": visit["revision"], "_stale": False} for item in detail["actions"]]
            drafts, previous, warnings = detail.get("customer_drafts", []), detail.get("previous_adoptions", []), detail.get("warnings", [])
            # A material's recap warning describes that source, never every
            # recording in the combined visit. Keep the canonical warning text
            # and qualify only its scope in this review DTO.
            warning_titles = {}
            for part in sources:
                for warning in part.get("warnings", []):
                    warning_titles.setdefault(warning, []).append(part.get("material", {}).get("title", "录音资料"))
            warnings = [("来源“" + "、".join(dict.fromkeys(warning_titles[warning])) + "”：" + warning)
                        if warning in warning_titles else warning for warning in warnings]
        with self.crm._lock:
            linked = next((source for source in self.profile._sources(self.crm._db, owner, packet["customer_id"])
                           if (source["type"], source["id"]) in refs), None)
        packet["opportunity_id"] = linked.get("opportunity_id") if linked else None
        if linked and kind in ("record", "material"):
            for key in ("visit_id", "role"):
                if key in linked:
                    packet[key] = linked[key]
        if kind == "visit":
            with self.crm._lock:
                direct = self.visits._source_project(self.crm._db, owner, "visit", identifier)
                states = [self.visits._source_project(self.crm._db, owner, part, entity_id) for part, entity_id in refs]
                projects = {state["opportunity_id"] for state in states if state["valid"] and state["opportunity_id"]}
                packet["opportunity_id"] = (direct["opportunity_id"] if direct["valid"] and direct["opportunity_id"] else
                    next(iter(projects)) if len(projects) == 1 and not any(state["invalid"] for state in states) else None)
                packet["project_sources"] = states
                packet["visit_project"] = direct
                for part in packet["sources"]:
                    part["project_scope"] = self.visits._source_project(self.crm._db, owner, part["type"], part["id"])
        packet["event_kind"] = "reflection" if packet.get("category") in ("idea", "visit_review") else "communication"
        if self.timeline is not None:
            try:
                with self.crm._lock:
                    event = self.timeline._events(self.crm._db, owner)[kind + ":" + str(identifier)]
                if kind != "visit" and not event.get("_source_needs_review"):
                    packet["opportunity_id"] = event.get("opportunity_id") or packet["opportunity_id"]
                if not event.get("_context_stale") and not event.get("_source_needs_review"):
                    packet["occurred_at"] = event.get("occurred_at")
                    packet["event_kind"] = event["kind"]
                packet["event_key"] = event["key"]
            except KeyError:
                pass
        # A visit's CAS also includes newly adopted refs. The raw revision only
        # describes evidence; each action keeps the stronger canonical visit CAS.
        packet["revision"] = self._source_revision(packet)
        return packet, refs, actions, drafts, previous, warnings

    def _row(self, owner, source, create=True):
        with self.crm._transaction() as db:
            row = db.execute("SELECT * FROM crm_exchange_workspaces WHERE owner=? AND source_type=? AND source_id=?",
                             (owner, source["type"], source["id"])).fetchone()
            if row is None and create:
                now = _timestamp(self.clock())
                db.execute("INSERT INTO crm_exchange_workspaces(owner,source_type,source_id,source_revision,created_at,updated_at,review_pending,review_revision) VALUES (?,?,?,?,?,?,0,0)",
                           (owner, source["type"], source["id"], source["revision"], now, now))
                row = db.execute("SELECT * FROM crm_exchange_workspaces WHERE owner=? AND source_type=? AND source_id=?",
                                 (owner, source["type"], source["id"])).fetchone()
            return dict(row) if row else None

    def _review_pending(self, db, row):
        if row["review_pending"] is not None:
            return bool(row["review_pending"])
        # Legacy inference is deliberately read-only and conservative. A GET
        # alone never creates a bookmark; ambiguous saved work remains visible.
        batches = db.execute("SELECT * FROM crm_exchange_batches WHERE owner=? AND workspace_id=? ORDER BY id DESC",
                             (row["owner"], row["id"])).fetchall()
        if batches:
            latest = batches[0]
            if latest["response_json"]:
                response = json.loads(latest["response_json"])
                completed = response.get("status") == "complete" and all(
                    item.get("status") in ("confirmed", "already_confirmed") for item in response.get("results", []))
                if (completed and response.get("workspace", {}).get("revision") == row["revision"]
                        and row["updated_at"] <= latest["updated_at"]):
                    return False
            return True
        return bool(json.loads(row["drafts_json"])) or row["status"] in ("ready", "preparing", "partial", "stale")

    def _mark_review(self, db, owner, workspace_id, now, source_guard):
        db.execute("UPDATE crm_exchange_workspaces SET review_pending=1,review_revision=review_revision+1,"
                   "review_source_guard=?,updated_at=? WHERE owner=? AND id=?",
                   (source_guard, now, owner, workspace_id))
        return dict(db.execute("SELECT * FROM crm_exchange_workspaces WHERE owner=? AND id=?",
                               (owner, workspace_id)).fetchone())

    def get_by_id(self, owner, workspace_id):
        owner, workspace_id = _owner(owner), _identifier(workspace_id)
        with self.crm._lock:
            row = self.crm._db.execute("SELECT * FROM crm_exchange_workspaces WHERE owner=? AND id=?",
                                       (owner, workspace_id)).fetchone()
        if row is None:
            raise KeyError("未找到你的交流准备稿")
        return self.get(owner, row["source_type"], row["source_id"])

    def _review_events(self, db, owner):
        if self.timeline is not None:
            return self.timeline._events(db, owner)
        from .customer_timeline import TimelineService
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='crm_timeline_context_history'").fetchone():
            reader = object.__new__(TimelineService)
            reader.crm, reader.workspace, reader.clock = self.crm, self.workspace, self.clock
            reader.visits, reader.discussions = None, None
            return reader._events(db, owner)
        # Optional timeline integrations have no context tables. Read just the
        # existing owned source rows, without installing schemas during a GET.
        events = {}
        for kind, table in (("record", "crm_records"), ("material", "crm_materials"), ("visit", "crm_visits")):
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                continue
            for source in db.execute("SELECT * FROM " + table + " WHERE owner=?", (owner,)):
                raw = dict(source)
                if raw.get("hidden") or raw.get("duplicate_of"):
                    continue
                text = raw.get("content", "")
                if kind == "material":
                    if raw.get("record_id"):
                        linked = db.execute("SELECT hidden FROM crm_records WHERE owner=? AND id=?", (owner, raw["record_id"])).fetchone()
                        if linked is None or linked["hidden"]:
                            continue
                    version = db.execute("SELECT text FROM crm_material_versions WHERE owner=? AND material_id=? AND id=?",
                                         (owner, raw["id"], raw.get("current_version_id"))).fetchone()
                    text = version["text"] if version else ""
                customer = db.execute("SELECT name FROM crm_customers WHERE owner=? AND id=?", (owner, raw.get("customer_id"))).fetchone()
                project_id = self.profile._project_for(db, owner, kind, raw)
                key = kind + ":" + str(raw["id"])
                events[key] = {"key": key, "title": raw["title"], "text": text, "customer_id": raw.get("customer_id"),
                               "customer_name": customer["name"] if customer else None, "opportunity_id": project_id,
                               "opportunity_name": None, "needs_review": False, "revision": _hash(raw)}
                if kind == "material":
                    events[key]["source_state"] = raw["status"]
        for key, event in list(events.items()):
            if not key.startswith("visit:"):
                continue
            texts, unavailable = [], False
            for table, field, kind in (("crm_visit_sources", "material_id", "material"),
                                       ("crm_visit_records", "record_id", "record")):
                if not db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                    continue
                for link in db.execute("SELECT * FROM " + table + " WHERE owner=? AND visit_id=?",
                                       (owner, int(key.split(":", 1)[1]))):
                    child = events.get(kind + ":" + str(link[field]))
                    if child is None:
                        unavailable = True
                    elif child["text"]:
                        choice = (db.execute("SELECT use_status FROM crm_visit_source_choices WHERE owner=? AND visit_id=? AND material_id=?",
                                  (owner, link["visit_id"], link[field])).fetchone() if kind == "material" else None)
                        if choice is None or choice["use_status"] != "excluded":
                            texts.append(child["text"])
            if unavailable:
                events.pop(key)
            else:
                event["text"] = "\n\n".join(texts)
        return events

    def review_entries(self, owner):
        """Pure discovery: no detail/resume, automatic creation or business writes."""
        owner = _owner(owner)
        entries = []
        with self.crm._lock:
            db = self.crm._db
            events = self._review_events(db, owner)
            for row in db.execute("SELECT * FROM crm_exchange_workspaces WHERE owner=? ORDER BY updated_at DESC,id DESC", (owner,)):
                if not self._review_pending(db, row):
                    continue
                event = events.get(row["source_type"] + ":" + str(row["source_id"]))
                if event is None:
                    continue  # Owner, hidden record and unavailable source graph.
                if event.get("_source_needs_review") and any("invalid_source" in part for part in event.get("_sources", [])):
                    continue
                drafts = json.loads(row["drafts_json"])
                try:
                    source_guard = self._prepare_source_guard(db, owner, row["source_type"], row["source_id"])
                except (KeyError, ValueError):
                    continue
                stale = bool(row["review_source_guard"] and row["review_source_guard"] != source_guard)
                status = "stale" if stale or event.get("needs_review") else row["status"]
                if event.get("source_state") not in (None, "review"):
                    status = "waiting_source"
                titles = [entry.get("draft", {}).get("title", "") for entry in drafts.values() if entry.get("selected")]
                snippet = "；".join(title for title in titles if title) or event.get("text", "")
                entries.append({"workspace_id": row["id"], "source_kind": row["source_type"], "source_id": row["source_id"],
                    "revision": row["revision"], "source_revision": row["source_revision"], "review_revision": row["review_revision"],
                    "review_pending": True, "kind": "exchange", "title": "继续核对：" + event["title"], "source_title": event["title"],
                    "scope": {"type": "project" if event.get("opportunity_id") else "customer", "customer_id": event["customer_id"],
                              "opportunity_id": event.get("opportunity_id"), "project_name": event.get("opportunity_name")},
                    "customer_id": event["customer_id"], "customer_name": event.get("customer_name"), "status": status,
                    "pending_count": sum(bool(entry.get("selected")) for entry in drafts.values()), "snippet": snippet[:1200],
                    "updated_at": row["updated_at"], "version": _hash([row["revision"], row["review_revision"], source_guard, event["revision"]]),
                    "errors": ["原交流或归属已有变化，请打开原准备稿重新核对。"] if status == "stale" else []})
        return entries

    def finish_review(self, owner, source_type, source_id, data):
        owner = _owner(owner)
        if not isinstance(data, dict) or set(data) - {"expected_revision", "source_revision", "expected_review_revision"}:
            raise ValueError("结束核对字段无效")
        if "expected_review_revision" not in data:
            raise ValueError("请核对当前准备稿版本后结束本次核对")
        with self.crm._lock:
            view = self.get(owner, source_type, source_id)
            self._guard(view, data)
            with self.crm._transaction() as db:
                db.execute("UPDATE crm_exchange_workspaces SET review_pending=0,review_revision=review_revision+1,"
                           "revision=revision+1,updated_at=? WHERE owner=? AND id=? AND revision=? AND review_revision=?",
                           (self.clock(), owner, view["id"], view["revision"], view["review_revision"]))
        return self.get(owner, source_type, source_id)

    def _options(self, owner, source):
        customer_id = source["customer_id"]
        if not customer_id:
            return {"contacts": [], "projects": []}
        with self.crm._lock:
            contacts = [dict(row) for row in self.crm._db.execute(
                "SELECT p.id,p.customer_id,p.name,p.department,c.name AS unit_name FROM crm_contacts p JOIN crm_customers c ON c.owner=p.owner AND c.id=p.customer_id WHERE p.owner=? AND p.customer_id=? AND p.archived=0 ORDER BY p.id",
                (owner, customer_id))]
        projects = self.workspace.opportunities(owner, customer_id)["items"]
        if source.get("opportunity_id"):
            members = self.workspace.stakeholders(owner, customer_id, source["opportunity_id"])["items"]
            for person in members:
                if person.get("archived") or not person.get("membership_valid", True):
                    continue
                if not any(row["id"] == person["contact_id"] for row in contacts):
                    contacts.append({"id": person["contact_id"], "customer_id": person["contact_customer_id"],
                                     "name": person.get("contact_name", ""), "department": person.get("contact_department", ""),
                                     "unit_name": person.get("unit_name", "")})
        return {"contacts": contacts, "projects": [{key: project.get(key) for key in ("id", "customer_id", "name", "archived")} for project in projects]}

    def _item(self, identifier, kind, source, *, current, draft, evidence="", status="pending", versions=None,
              scope=None, label="", extra=None):
        return {"id": identifier, "kind": kind, "group": kind, "status": status, "selected": False,
                "draft": draft, "current": current, "scope": scope or {"customer_id": source["customer_id"],
                "opportunity_id": source.get("opportunity_id"), "contact_id": None}, "evidence": evidence,
                "version": _hash([identifier, source["revision"], current, versions or {}, scope or {}]),
                "versions": versions or {}, "label": label, **(extra or {})}

    def get(self, owner, source_type, source_id):
        owner = _owner(owner)
        kind, identifier = self._source_key(source_type, source_id)
        source, refs, actions, drafts, previous, warnings = self._source(owner, kind, identifier)
        row = self._row(owner, source)
        saved = json.loads(row["drafts_json"])
        options = self._options(owner, source)
        items, impacts = [], []
        with self.crm._lock:
            db = self.crm._db
            candidates = [dict(candidate) for candidate in db.execute("SELECT * FROM crm_profile_candidates WHERE owner=? ORDER BY id", (owner,))
                          if (candidate["source_type"], candidate["source_id"]) in refs]
        for candidate_row in candidates:
            candidate = self.profile.get_candidate(owner, candidate_row["id"])
            item_id = "profile:" + str(candidate["id"])
            scope_patch = saved.get(item_id, {}).get("scope", {})
            if candidate["scope"] == "stakeholder":
                scope_patch = {key: value for key, value in saved.get(item_id, {}).get("draft", {}).items()
                               if key in ("contact_id", "opportunity_id") and value is not None}
            if scope_patch and candidate["status"] == "pending":
                try:
                    candidate = self.profile.preview_candidate(owner, candidate["id"], scope_patch)
                except (ValueError, KeyError):
                    warnings.append("已保存的画像归属已不可用，请重新核对。")
            stakeholder = candidate["scope"] == "stakeholder"
            draft = {"roles": [], "contact_id": candidate["contact_id"], "opportunity_id": candidate["opportunity_id"],
                     "basis": candidate["basis"]} if stakeholder else {"value": candidate["value"], "basis": candidate["basis"]}
            project_revision = None
            current = candidate["current_value"]
            if stakeholder and candidate["opportunity_id"]:
                people = self.workspace.stakeholders(owner, candidate["customer_id"], candidate["opportunity_id"])
                project_revision = people["project_revision"]
                current = next((person for person in people["items"] if person["contact_id"] == candidate["contact_id"]), None)
            item = self._item(item_id, "relationship" if stakeholder else "profile", source,
                current=current, draft=draft, evidence=candidate["evidence"], status=candidate["status"],
                versions={"candidate_revision": candidate["revision"], "current_fact_id": candidate["current_fact_id"], "project_revision": project_revision},
                scope={"scope": candidate["scope"], "customer_id": candidate["customer_id"], "contact_id": candidate["contact_id"],
                       "opportunity_id": candidate["opportunity_id"], "options": options},
                label=candidate["label"], extra={"candidate": candidate, "subtype": "stakeholder" if stakeholder else "fact"})
            items.append(item)
            with self.crm._lock:
                fresh_source = next((fresh for fresh in self.profile._sources(self.crm._db, owner, candidate["customer_id"])
                                     if fresh["type"] == candidate_row["source_type"] and fresh["id"] == candidate_row["source_id"]), None)
            if candidate["status"] == "confirmed" and (not fresh_source or fresh_source["fingerprint"] != candidate_row["source_fingerprint"]):
                impacts.append({"kind": "profile", "id": candidate["confirmed_fact_id"], "item_id": item_id,
                                "label": candidate["label"], "source": {key: (fresh_source or candidate.get("source", {})).get(key) for key in ("type", "id", "title")},
                                "message": "来源已纠正；已确认资料保留，需核对是否补充新的更正资料。"})
        for draft in drafts:
            identifier_ = draft.get("id", draft.get("draft_id"))
            original = self.crm.get_customer_draft(owner, identifier_) if identifier_ else None
            if not original:
                continue
            with self.crm._lock:
                actual = self.crm._db.execute("SELECT * FROM crm_customer_drafts WHERE owner=? AND id=?", (owner, identifier_)).fetchone()
                payload = json.loads(actual["payload_json"])
                target = self.crm._changes(self.crm._db, owner, payload)
            items.append(self._item("customer:" + str(identifier_), "customer", source, current=target,
                draft={"changes": [{key: change[key] for key in ("target", "key", "after", "basis")} for change in original["changes"]]},
                evidence=original["source_content"] or original["source_text"], status=original["status"],
                versions={"draft_updated_at": original["updated_at"]}, label="客户资料变更", extra={"customer_draft": original}))
        if self.timeline and source.get("event_key"):
            event = self.timeline.get_event(owner, source["event_key"])
            relations = [{"contact_id": relation["contact_id"], "relation": relation["relation"]}
                         for relation in event.get("contact_relations", []) if relation.get("valid", True)]
            current = {"contact_relations": relations, **{key: event.get(key) for key in ("kind", "occurred_at", "related_event_key")},
                       "separate_event": bool(event.get("separate_event", False))}
            items.append(self._item("relationship:" + source["event_key"], "relationship", source,
                current=current, draft=copy.deepcopy(current), versions={"event_revision": event["revision"], "options_revision": _hash(options)},
                label="交流日期与人物归属", extra={"subtype": "timeline_context", "scope": {"customer_id": source["customer_id"],
                    "opportunity_id": source.get("opportunity_id"), "contact_id": None, "options": options}}))
            for relation in event.get("contact_relations", []):
                if not relation.get("valid", True):
                    impacts.append({"kind": "relationship", "contact_id": relation["contact_id"], "event_key": source["event_key"],
                                    "label": relation.get("contact_name", relation.get("name", "人物归属")),
                                    "source": {key: source.get(key) for key in ("type", "id", "title")},
                                    "message": "来源或人物关系已变化；历史核对保留，新整理不沿用失效关系。"})
        for action in actions:
            item_id = "action:" + action["_origin"] + ":" + str(action["_source_id"]) + ":" + str(action["_id"])
            record_id = action.get("adopted_record_id") or action.get("record_id")
            adopted = self.crm.get_record(owner, record_id) if record_id else None
            action_scope = {"customer_id": adopted["customer_id"] if adopted else source["customer_id"],
                            "opportunity_id": source.get("opportunity_id"), "contact_id": None}
            project_scope = copy.deepcopy(action.get("project_scope"))
            scope_patch = saved.get(item_id, {}).get("scope", {})
            if project_scope and action["_origin"] in ("visit", "material") and not adopted:
                if scope_patch:
                    with self.crm._lock:
                        try:
                            project_scope = self.visits._action_project_scope(self.crm._db, owner,
                                self.visits._require(self.crm._db, owner, action.get("visit_id", action["_source_id"])), action,
                                opportunity_id=scope_patch.get("opportunity_id"),
                                confirm_single_action=scope_patch.get("confirm_single_action", False))
                        except (ValueError, KeyError) as exc:
                            warnings.append(str(exc))
                action_scope["opportunity_id"] = project_scope["opportunity_id"] if project_scope else None
                action_scope["opportunity_name"] = project_scope.get("opportunity_name") if project_scope else None
            if adopted:
                with self.crm._lock:
                    raw = self.crm._require_record(self.crm._db, owner, adopted["id"])
                    action_scope["opportunity_id"] = self.profile._project_for(self.crm._db, owner, "record", raw)
            if action_scope["opportunity_id"]:
                with self.crm._lock:
                    project = self.workspace._require_opportunity(self.crm._db, owner, action_scope["customer_id"], action_scope["opportunity_id"])
                    action_scope["opportunity_name"] = project["name"]
            original = {key: action[key] for key in ("title", *sorted(TERM_FIELDS)) if key in action}
            current = {"action": {key: value for key, value in action.items() if not key.startswith("_")}, "record": adopted}
            versions = {"source_version": action["_source_version"], "record_updated_at": adopted["updated_at"] if adopted else None,
                        "terms_updated_at": adopted.get("terms_updated_at") if adopted else None}
            if project_scope:
                versions["project_scope_revision"] = project_scope["revision"]
                if action.get("visit_revision"):
                    versions["visit_revision"] = action["visit_revision"]
            with self.crm._lock:
                people_options = (action_contact_options(self.crm._db, owner, action_scope["customer_id"],
                                                        action_scope.get("opportunity_id"), self.workspace)
                                  if action_scope["customer_id"] else {"contacts": [], "version": _hash([owner, None])})
            versions["action_contact_version"] = people_options["version"]
            status = "confirmed" if adopted else "stale" if action.get("_stale") else "blocked" if action.get("needs_review") else "pending"
            reasons = list(action.get("review_reasons", []))
            if project_scope and project_scope["explicit"] and project_scope["status"] != "needs_review" and not adopted:
                reasons = list(action.get("source_review_reasons", []))
                status = "blocked" if reasons else "pending"
            item = self._item(item_id, "action", source, current=current, draft=original,
                evidence=action.get("evidence") or action.get("reason", ""), status=status, versions=versions,
                scope=action_scope, label=action["title"], extra={"origin": action["_origin"], "action_id": action["_id"], "origin_id": action["_source_id"],
                                            "review_reasons": reasons, "project_scope": project_scope,
                                            "contact_ids": [], "action_contact_options": people_options,
                                            "expected_contact_version": people_options["version"]})
            items.append(item)
            proposal = self.crm.get_proposal(owner, adopted["proposal_id"]) if adopted and adopted["proposal_id"] else None
            schedule = {"remind_at": proposal["remind_at"] if proposal else action.get("execution_at", action.get("remind_at")),
                        "duration_minutes": proposal["duration_minutes"] if proposal else action.get("duration_minutes"),
                        "deadline_at": proposal["deadline_at"] if proposal else action.get("deadline_at"),
                        "schedule_note": proposal["schedule_note"] if proposal else "用户核对交流准备稿后安排"}
            items.append(self._item("schedule:" + item_id, "schedule", source, current={"record_id": record_id, "proposal": proposal},
                draft=schedule, evidence=item["evidence"], status="confirmed" if proposal and proposal["status"] == "confirmed" else "pending",
                versions={"action_version": item["version"], "proposal_updated_at": proposal["updated_at"] if proposal else None},
                scope=copy.deepcopy(action_scope), label="安排提醒：" + action["title"], extra={"action_item_id": item_id}))
            if adopted and (row["source_revision"] != source["revision"] or action.get("_stale")):
                impacts.append({"kind": "action", "id": adopted["id"], "status": adopted["status"],
                    "label": adopted["title"], "source": {key: source.get(key) for key in ("type", "id", "title")},
                    "proposal_id": adopted["proposal_id"], "task_id": proposal.get("task_id") if proposal else None,
                    "message": "原依据已变化；既有行动和提醒仍保留，请明确决定是否更正或取消。"})
        for old in previous:
            old_id = old.get("record_id", old.get("id"))
            old_record = self.crm.get_record(owner, old_id) if old_id else None
            old_proposal = self.crm.get_proposal(owner, old_record["proposal_id"]) if old_record and old_record["proposal_id"] else None
            impacts.append({"kind": "previous_action", "id": old_id, "status": old_record["status"] if old_record else old.get("status"),
                            "label": old_record["title"] if old_record else old.get("title", "历史行动"),
                            "source": {key: source.get(key) for key in ("type", "id", "title")},
                            "proposal_id": old_proposal["id"] if old_proposal else None,
                            "task_id": old_proposal["task_id"] if old_proposal else None,
                            "message": "历史已采用事项保留；来源更新不等于事项完成或提醒取消。"})
        for item in items:
            entry = saved.get(item["id"], {})
            item["selected"] = entry.get("selected", False)
            item["draft"].update(copy.deepcopy(entry.get("draft", {})))
            if item["kind"] == "action":
                item["contact_ids"] = copy.deepcopy(entry.get("contact_ids", []))
        stale = row["source_revision"] != source["revision"]
        with self.crm._lock:
            review_pending = self._review_pending(self.crm._db, row)
        return {"id": row["id"], "workspace_id": row["id"], "source_kind": kind, "source_id": identifier,
                "review_pending": review_pending, "review_revision": row["review_revision"],
                "source": source, "source_revision": source["revision"], "revision": row["revision"],
                "status": "stale" if stale else "waiting_source" if source.get("processing_status") not in (None, "review") else row["status"],
                "message": row["message"] or ("录音资料仍在转写或整理队列，原文已保留；完成后可继续准备。" if source.get("processing_status") not in (None, "review") else ""), "items": items,
                "groups": {group: [item["id"] for item in items if item["group"] == group] for group in
                           ("profile", "customer", "relationship", "action", "schedule")},
                "warnings": list(dict.fromkeys(warnings)), "correction_impacts": impacts,
                "capabilities": {"profile_analysis": "model" if self.profile.analyzer else "rules",
                    "organizer": bool(self.organizer and getattr(self.organizer, "api_key", True)), "timeline": bool(self.timeline),
                    "material_prepare": bool(self.materials), "original_preserved": True}}

    def _guard(self, view, data):
        if type(data.get("expected_revision")) is not int or data["expected_revision"] != view["revision"]:
            raise ExchangeConflict("准备稿已更新，请刷新核对后继续。")
        if _version(data.get("source_revision"), "来源版本") != view["source_revision"]:
            raise ExchangeConflict("原文、纳入状态或归属已变化，请重新整理；已有资料保留。")
        if "expected_review_revision" in data and (type(data["expected_review_revision"]) is not int
                or data["expected_review_revision"] != view["review_revision"]):
            raise ExchangeConflict("这份准备稿的核对已由新操作更新，请重新打开后继续。")

    def _check_draft(self, item, draft):
        if not isinstance(draft, dict):
            raise ValueError("准备稿内容需为对象")
        draft = copy.deepcopy(draft)
        kind = item["kind"]
        if kind == "profile":
            if set(draft) - {"value", "basis"}:
                raise ValueError("画像准备稿字段无效")
            if "value" in draft:
                draft["value"] = _text(draft["value"], "画像内容", 2000, required=True).strip()
            if "basis" in draft and draft["basis"] not in ("reported", "observation"):
                raise ValueError("信息性质无效")
        elif kind == "action":
            if set(draft) - ({"title"} | TERM_FIELDS):
                raise ValueError("行动准备稿字段无效")
            if "title" in draft:
                draft["title"] = _text(draft["title"], "行动标题", 120, required=True).strip()
            for key, value in draft.items():
                if key == "executor_kind" and value not in EXECUTOR_KINDS:
                    raise ValueError("执行主体无效")
                if key.endswith("_evidence"):
                    _text(value, "用户补充依据", 2000)
                elif key in ("execution_at", "check_at", "deadline_at") and value is not None:
                    _timestamp(value)
                elif key in ("check_date", "deadline_date") and value is not None:
                    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                        raise ValueError("日期必须为YYYY-MM-DD")
                elif key == "duration_minutes" and value is not None and (type(value) is not int or not 5 <= value <= 720):
                    raise ValueError("预计用时需为5至720分钟")
        elif kind == "schedule":
            if set(draft) - {"remind_at", "duration_minutes", "deadline_at", "schedule_note"}:
                raise ValueError("日程准备稿字段无效")
            for key in ("remind_at", "deadline_at"):
                if key in draft and draft[key] is not None:
                    _timestamp(draft[key])
            if draft.get("duration_minutes") is not None and (type(draft["duration_minutes"]) is not int or not 5 <= draft["duration_minutes"] <= 720):
                raise ValueError("预计用时需为5至720分钟")
            if "schedule_note" in draft:
                _text(draft["schedule_note"], "安排说明", 500)
        elif kind == "customer":
            if set(draft) - {"changes"} or not isinstance(draft.get("changes"), list) or len(draft["changes"]) > 50:
                raise ValueError("客户资料修改字段无效")
            allowed = {(row["target"], row["key"]) for row in item["customer_draft"]["changes"]}
            originals = {(row["target"], row["key"]): row for row in item["customer_draft"]["changes"]}
            seen = set()
            for row in draft["changes"]:
                if not isinstance(row, dict) or set(row) - {"target", "key", "after", "basis"}:
                    raise ValueError("客户资料修改格式无效")
                pair = (row.get("target"), row.get("key"))
                if pair not in allowed or pair in seen or "after" not in row:
                    raise ValueError("仅可修改原准备稿已有字段")
                seen.add(pair)
                if row["target"] in ("basic", "contact") and "basis" in row and row["basis"] != originals[pair]["basis"]:
                    raise ValueError("基本资料的信息性质来自核对依据，不能在这里改为推测。")
                if len(_json(row)) > 6000:
                    raise ValueError("客户资料修改内容过长")
            if seen != allowed:
                raise ValueError("客户资料需完整核对原准备稿字段；不采用时请取消选择整项。")
        elif item.get("subtype") == "timeline_context":
            if set(draft) - {"kind", "occurred_at", "contact_relations", "related_event_key", "separate_event"}:
                raise ValueError("人物归属准备稿字段无效")
            if "contact_relations" in draft:
                relations = draft["contact_relations"]
                if not isinstance(relations, list) or len(relations) > 50:
                    raise ValueError("人物关系数量无效")
                seen = set()
                for relation in relations:
                    if not isinstance(relation, dict) or set(relation) != {"contact_id", "relation"} or relation["relation"] not in ("direct", "about"):
                        raise ValueError("人物关系格式无效")
                    contact = _identifier(relation["contact_id"])
                    if (contact, relation["relation"]) in seen:
                        raise ValueError("人物关系重复")
                    seen.add((contact, relation["relation"]))
            if "occurred_at" in draft and draft["occurred_at"] is not None:
                _timestamp(draft["occurred_at"])
            if "kind" in draft and draft["kind"] not in ("communication", "reflection", "discussion", "result"):
                raise ValueError("历程类型无效")
            if "separate_event" in draft and type(draft["separate_event"]) is not bool:
                raise ValueError("独立事件标记无效")
        else:
            fields = {"contact_id", "opportunity_id", "roles", "stance", "influence", "engagement", "concerns", "next_step", "basis"}
            if set(draft) - fields:
                raise ValueError("项目人物准备稿字段无效")
            for key in ("contact_id", "opportunity_id"):
                if draft.get(key) is not None:
                    _identifier(draft[key])
            if "roles" in draft and (not isinstance(draft["roles"], list) or len(draft["roles"]) > 10):
                raise ValueError("项目角色无效")
            for key in ("concerns", "next_step"):
                if key in draft:
                    _text(draft[key], "人物资料", 4000)
        return draft

    def _requests(self, view, data, *, owner):
        rows = data.get("items")
        if not isinstance(rows, list) or not rows or len(rows) > 50:
            raise ValueError("每次需选择1至50项准备稿")
        lookup = {item["id"]: item for item in view["items"]}
        checked, seen = [], set()
        allowed = {"id", "expected_version", "draft", "selected", "scope", "expected_fact_id", "verify_public", "verify_entity",
                   "contact_ids", "expected_contact_version"}
        for row in rows:
            if not isinstance(row, dict) or set(row) - allowed or not isinstance(row.get("id"), str) or row["id"] not in lookup or row["id"] in seen:
                raise ValueError("准备稿选择无效、重复或不属于本次交流")
            seen.add(row["id"])
            _version(row.get("expected_version"), "条目版本")
            item = lookup[row["id"]]
            normalized = copy.deepcopy(row)
            if "contact_ids" in row or "expected_contact_version" in row:
                if item["kind"] != "action":
                    raise ValueError("待办联系人只能在行动条目明确选择")
                normalized["contact_ids"] = normalize_contact_ids(row.get("contact_ids", item.get("contact_ids", [])))
                if "expected_contact_version" in row:
                    _version(row["expected_contact_version"], "联系人选项版本")
            if "selected" in row and type(row["selected"]) is not bool:
                raise ValueError("选择标记无效")
            if "draft" in row:
                normalized["draft"] = self._check_draft(item, row["draft"])
            if "scope" in row:
                scope = row["scope"]
                allowed_scope = {"opportunity_id", "confirm_single_action"} if item["kind"] == "action" else {"contact_id", "opportunity_id"}
                if item["kind"] not in ("profile", "action") or not isinstance(scope, dict) or set(scope) - allowed_scope:
                    raise ValueError("画像归属选择无效")
                for key, value in scope.items():
                    if key == "confirm_single_action":
                        if type(value) is not bool:
                            raise ValueError("请明确是一项跨项目行动，不能自动合并独立事项")
                    else:
                        _identifier(value)
                if item["kind"] == "action":
                    if item.get("origin") not in ("visit", "material") or not item.get("project_scope") or item["status"] == "confirmed" or (scope and item.get("origin") == "material" and not item["current"]["action"].get("visit_id")):
                        raise ValueError("请从原来源核对项目；已采用事项不会通过准备稿改归属")
                    if scope:
                        with self.crm._lock:
                            self.visits._action_project_scope(self.crm._db, owner,
                                self.visits._require(self.crm._db, owner, item["current"]["action"].get("visit_id", item["origin_id"])),
                                item["current"]["action"], opportunity_id=scope.get("opportunity_id"),
                                confirm_single_action=scope.get("confirm_single_action", False))
            if row.get("expected_fact_id") is not None:
                _identifier(row["expected_fact_id"])
            for key in ("verify_public", "verify_entity"):
                if key in row and type(row[key]) is not bool:
                    raise ValueError("资料核对标记无效")
            checked.append((item, normalized))
        return checked

    def _preflight_contacts(self, owner, checked):
        # Validate the entire batch before saving a claim or adopting any fact.
        # A saved explicit selection is used only for this exact action, never
        # copied from the meeting or inferred from its title.
        for item, request in checked:
            if item["kind"] != "action":
                continue
            ids = request.get("contact_ids", item.get("contact_ids", []))
            expected = request.get("expected_contact_version")
            if not item["scope"]["customer_id"]:
                if ids:
                    raise ValueError("请先核对待办所属单位，再明确选择联系人；尚未采用")
                continue
            if ids and item["version"] != request["expected_version"]:
                raise ExchangeConflict("待办或联系人选项已变化，请刷新后重新核对；尚未采用。")
            if ids and self.timeline is None:
                raise ValueError("人物历程服务尚未连接，所选联系人尚未采用")
            validate_action_contacts(self.crm._db, owner, item["scope"]["customer_id"],
                item["scope"].get("opportunity_id"), self.workspace, ids,
                expected_version=expected or item["versions"].get("action_contact_version"))

    def _settle_review(self, owner, batch, results, source_revision):
        if (batch.get("review_revision") is None or batch.get("workspace_revision") is None
                or not results or any(item["status"] not in ("confirmed", "already_confirmed") for item in results)):
            return
        row = self.crm._db.execute("SELECT * FROM crm_exchange_workspaces WHERE owner=? AND id=?",
                                   (owner, batch["workspace_id"])).fetchone()
        if (row is None or row["review_revision"] != batch["review_revision"]
                or row["revision"] != batch["workspace_revision"]):
            return
        current = self.get(owner, row["source_type"], row["source_id"])
        if current["source_revision"] != self._journal_source_revision(owner, batch, source_revision):
            return
        with self.crm._transaction() as db:
            db.execute("UPDATE crm_exchange_workspaces SET review_pending=0 WHERE owner=? AND id=? AND review_revision=? AND revision=?",
                       (owner, row["id"], batch["review_revision"], batch["workspace_revision"]))

    def _batch_contact_versions(self, owner, batch):
        versions = {}
        for row in self.crm._db.execute("SELECT item_json FROM crm_exchange_batch_items WHERE owner=? AND batch_id=?",
                                       (owner, batch["id"])):
            action = json.loads(row["item_json"])
            if action["kind"] == "action" and action["scope"]["customer_id"]:
                options = action_contact_options(self.crm._db, owner, action["scope"]["customer_id"],
                                                 action["scope"].get("opportunity_id"), self.workspace)
                versions[action["id"]] = options["version"]
        return versions

    def _accepted_contact_version(self, owner, batch, item, request):
        accepted = request.get("expected_contact_version") or item["versions"].get("action_contact_version")
        for row in self.crm._db.execute("SELECT checkpoint_json,result_json FROM crm_exchange_batch_items "
                "WHERE owner=? AND batch_id=? ORDER BY rowid", (owner, batch["id"])):
            if not row["result_json"] or json.loads(row["result_json"])["status"] not in ("confirmed", "already_confirmed"):
                continue
            checkpoint = json.loads(row["checkpoint_json"])
            before = checkpoint.get("contact_versions_before", {}).get(item["id"])
            after = checkpoint.get("contact_versions_after", {}).get(item["id"])
            if before == accepted and after:
                accepted = after
        return accepted

    def _save_drafts(self, db, view, checked, now, *, confirming=False):
        row = db.execute("SELECT * FROM crm_exchange_workspaces WHERE owner=? AND id=?", (view["_owner"], view["id"])).fetchone()
        if row["revision"] != view["revision"]:
            raise ExchangeConflict("准备稿刚被更新，请刷新后继续。")
        drafts = json.loads(row["drafts_json"])
        before = _json(drafts)
        for item, request in checked:
            entry = drafts.setdefault(item["id"], {})
            if "draft" in request:
                entry.setdefault("draft", {}).update(request["draft"])
            if "scope" in request:
                entry["scope"] = request["scope"]
            if "contact_ids" in request:
                entry["contact_ids"] = request["contact_ids"]
            entry["selected"] = request.get("selected", True if confirming else entry.get("selected", False))
        if _json(drafts) != before:
            db.execute("UPDATE crm_exchange_workspaces SET drafts_json=?,revision=revision+1,updated_at=? WHERE owner=? AND id=?",
                       (_json(drafts), now, view["_owner"], view["id"]))

    def edit_draft(self, owner, source_type, source_id, data):
        owner = _owner(owner)
        if not isinstance(data, dict) or set(data) - {"expected_revision", "source_revision", "expected_review_revision", "items"}:
            raise ValueError("准备稿保存字段无效")
        with self.crm._lock:
            view = self.get(owner, source_type, source_id)
            self._guard(view, data)
            checked = self._requests(view, data, owner=owner)
            if any(item["version"] != request["expected_version"] for item, request in checked):
                raise ExchangeConflict("资料、人物或行动已变化，请核对最新条目后再保存。")
            self._preflight_contacts(owner, checked)
            view["_owner"] = owner
            with self.crm._transaction() as db:
                self._save_drafts(db, view, checked, self.clock())
                self._mark_review(db, owner, view["id"], self.clock(),
                                  self._prepare_source_guard(db, owner, view["source"]["type"], view["source"]["id"]))
        return self.get(owner, source_type, source_id)

    async def prepare(self, owner, source_type, source_id, data=None):
        owner = _owner(owner)
        data = data or {}
        if not isinstance(data, dict) or set(data) - {"expected_revision", "source_revision", "expected_review_revision", "force"} or type(data.get("force", False)) is not bool:
            raise ValueError("交流整理请求无效")
        view = self.get(owner, source_type, source_id)
        if "expected_revision" in data or "source_revision" in data or "expected_review_revision" in data:
            self._guard(view, data)
        kind, identifier = self._source_key(source_type, source_id)
        with self.crm._lock:
            source, refs, _, _, _, _ = self._source(owner, kind, identifier)
            source_guard = self._prepare_source_guard(self.crm._db, owner, kind, identifier)
        token = _hash([view["id"], self.clock(), view["revision"], time.monotonic()])
        with self.crm._transaction() as db:
            current = db.execute("SELECT * FROM crm_exchange_workspaces WHERE owner=? AND id=?", (owner, view["id"])).fetchone()
            if current["status"] == "preparing" and self.clock() - current["updated_at"] < 240:
                return view
            if current["revision"] != view["revision"] or current["review_revision"] != view["review_revision"]:
                raise ExchangeConflict("准备稿已更新，请刷新后继续整理。")
            self._mark_review(db, owner, view["id"], self.clock(), source_guard)
            db.execute("UPDATE crm_exchange_workspaces SET status='preparing',lease=?,message='',updated_at=? WHERE owner=? AND id=?",
                       (token, self.clock(), owner, view["id"]))
        errors = []
        try:
            if kind == "record":
                analysis = self.crm.get_analysis(owner, identifier)
                if analysis is None or analysis["stale"] or data.get("force"):
                    input_fingerprint = analysis_fingerprint(self.crm.get_record(owner, identifier))
                    if self.organizer and getattr(self.organizer, "api_key", True):
                        context = self.profile._context(self.crm._db, owner, source["customer_id"]) if source["customer_id"] else {"customer": {}, "contacts": [], "projects": []}
                        analysis_data = await asyncio.wait_for(self.organizer.organize(source["text"], self.clock(), context), 90)
                    else:
                        context = self.profile._context(self.crm._db, owner, source["customer_id"]) if source["customer_id"] else {"customer": {}, "contacts": [], "projects": []}
                        clauses = [fragment["text"] for fragment in _evidence_fragments(source, context)
                                   if (_explicit_commitment(fragment["text"], context) or
                                       re.search(r"准备|计划|建议|要发|跟进|回访|先问|问清|核实", fragment["text"]))]
                        analysis_data = {"summary": source["text"][:4000] or "待补充原文", "key_points": [], "open_questions": [],
                            "actions": [{"title": clause[:120], "kind": "commitment" if _explicit_commitment(clause, context) else "suggestion",
                                         "reason": '原话：“' + clause[:1500] + '”', "evidence": clause[:1500], "owner_hint": "", "remind_at": None}
                                        for clause in clauses[:6]]}
                    analysis_data["input_fingerprint"] = input_fingerprint
                    with self.crm._lock:
                        active = self.crm._db.execute("SELECT lease FROM crm_exchange_workspaces WHERE owner=? AND id=?", (owner, view["id"])).fetchone()
                        if self._source(owner, kind, identifier)[0]["revision"] != source["revision"]:
                            raise ExchangeConflict("整理过程中原文或归属变化；旧回复未写入。")
                        if not active or active["lease"] != token:
                            raise ExchangeConflict("本次整理已由新请求接管；晚到回复未写入。")
                        self.crm.save_analysis(owner, identifier, analysis_data, self.clock())
            with self.crm._lock:
                sources = [item for item in self.profile._sources(self.crm._db, owner, source["customer_id"])
                           if (item["type"], item["id"]) in refs]
            for fact_source in sources:
                with self.crm._transaction() as db:
                    job = db.execute("SELECT * FROM crm_profile_source_jobs WHERE owner=? AND source_type=? AND source_id=? AND fingerprint=?",
                                     (owner, fact_source["type"], fact_source["id"], fact_source["fingerprint"])).fetchone()
                    if job and job["status"] == "complete" and not data.get("force"):
                        continue
                    db.execute("INSERT INTO crm_profile_source_jobs(owner,source_type,source_id,fingerprint,customer_id,status,updated_at) VALUES (?,?,?,?,?,'exchange_managed',?) "
                               "ON CONFLICT(owner,source_type,source_id,fingerprint) DO UPDATE SET status='exchange_managed',updated_at=excluded.updated_at",
                               (owner, fact_source["type"], fact_source["id"], fact_source["fingerprint"], fact_source["customer_id"], self.clock()))
                    context = self.profile._context(db, owner, fact_source["customer_id"])
                attributes = await asyncio.wait_for(self.profile.analyzer.extract(fact_source, context), 90) if self.profile.analyzer else _rule_extract(fact_source, context)
                with self.crm._transaction() as db:
                    fresh = next((item for item in self.profile._sources(db, owner, fact_source["customer_id"])
                                  if item["type"] == fact_source["type"] and item["id"] == fact_source["id"]), None)
                    active = db.execute("SELECT lease FROM crm_exchange_workspaces WHERE owner=? AND id=?", (owner, view["id"])).fetchone()
                    if (not fresh or fresh["fingerprint"] != fact_source["fingerprint"] or active["lease"] != token
                            or self._prepare_source_guard(db, owner, kind, identifier) != source_guard):
                        raise ExchangeConflict("来源或本次整理已更新；晚到结果未写入。")
                    self.profile._store_candidates(db, owner, fresh, attributes, context)
                    db.execute("UPDATE crm_profile_source_jobs SET status='complete',attempts=attempts+1,error='',updated_at=? WHERE owner=? AND source_type=? AND source_id=? AND fingerprint=?",
                               (self.clock(), owner, fresh["type"], fresh["id"], fresh["fingerprint"]))
        except asyncio.CancelledError:
            with self.crm._transaction() as db:
                db.execute("UPDATE crm_exchange_workspaces SET status='draft',lease=NULL,message='整理中断，原文和准备稿保留，可重新整理。' WHERE owner=? AND id=? AND lease=?", (owner, view["id"], token))
            raise
        except Exception as exc:
            errors.append(str(exc) if isinstance(exc, (ValueError, KeyError)) else "本次整理未完成；原文和已有准备稿保留，可重试。")
        latest = self._source(owner, kind, identifier)[0]
        stale = latest["revision"] != source["revision"]
        with self.crm._transaction() as db:
            db.execute("UPDATE crm_exchange_workspaces SET status=?,source_revision=?,revision=revision+1,lease=NULL,message=?,updated_at=? WHERE owner=? AND id=? AND lease=?",
                       ("stale" if stale else "partial" if errors else "ready", source["revision"], "；".join(errors), self.clock(), owner, view["id"], token))
        return self.get(owner, kind, identifier)

    def _checkpoint(self, owner, batch_id, item_id, stage, values):
        with self.crm._transaction() as db:
            row = db.execute("SELECT checkpoint_json FROM crm_exchange_batch_items WHERE owner=? AND batch_id=? AND item_id=?", (owner, batch_id, item_id)).fetchone()
            checkpoint = json.loads(row[0])
            checkpoint.update(values)
            db.execute("UPDATE crm_exchange_batch_items SET stage=?,checkpoint_json=?,updated_at=? WHERE owner=? AND batch_id=? AND item_id=?",
                       (stage, _json(checkpoint), self.clock(), owner, batch_id, item_id))
        return checkpoint

    def _profile_decision(self, item, request):
        draft = {**item["draft"], **request.get("draft", {})}
        body = {"decision": "confirm", "expected_revision": item["versions"]["candidate_revision"],
                "value": draft["value"], "basis": draft["basis"]}
        for field, scope in (("contact_id", "contact"), ("opportunity_id", "project")):
            if item["scope"]["scope"] == scope and item["scope"].get(field) is not None:
                body[field] = item["scope"][field]
        body.update(request.get("scope", {}))
        for key in ("expected_fact_id", "verify_public", "verify_entity"):
            if key in request:
                body[key] = request[key]
        if "expected_fact_id" not in body:
            raise ValueError("请核对当前资料后提交 expected_fact_id，空值也需明确。")
        return body

    def _recovered_profile(self, owner, item, body):
        candidate = self.profile.get_candidate(owner, item["candidate"]["id"])
        if not candidate or candidate["status"] != "confirmed":
            return None
        if not any(row["decision"] == "confirmed" and json.loads(row["details_json"]) == body for row in candidate["decision_history"]):
            return None
        return {"candidate_id": candidate["id"], "fact_id": candidate["confirmed_fact_id"], "candidate": candidate}

    def _confirmed_fact(self, owner, candidate):
        if not candidate.get("confirmed_fact_id"):
            return None
        table = "crm_project_profile_facts" if candidate["scope"] == "project" else "crm_customer_facts"
        row = self.crm._db.execute("SELECT value,basis FROM " + table + " WHERE owner=? AND id=?",
                                   (owner, candidate["confirmed_fact_id"])).fetchone()
        return dict(row) if row else None

    def _action_timeline_plan(self, owner, source):
        """Preflight mandatory scope, keep optional stale people out of adoption."""
        if not self.timeline or not source.get("event_key") or not source.get("customer_id"):
            return None, None
        key = source["event_key"]
        event = self.timeline._events(self.crm._db, owner).get(key)
        if event is None:
            raise ExchangeConflict("原来源已不可用，请刷新核对后采用；没有建立待办。")
        if event.get("_source_needs_review") and source.get("opportunity_id") is not None:
            raise ExchangeConflict("来源的项目范围已失效，请先核对归属；没有建立待办。")
        if event.get("needs_review"):
            return None, "行动已采用；原来源历程需要重新核对，未沿用失效的人物或项目背景。"
        if source["type"] == "visit" and event.get("opportunity_id") != source.get("opportunity_id"):
            # A combined event may project its first material. Each action uses
            # only its own references; that optional event projection cannot
            # move it back to an unrelated project or imply meeting attendance.
            return None, "行动已采用并按其来源核对项目；整场交流的人物背景未自动沿用，请按需核对。"
        scope = {"customer_id": source["customer_id"], "opportunity_id": source.get("opportunity_id")}
        self.timeline.history_context(owner, scope, event_keys=[key])
        return key, None

    @staticmethod
    def _adopted_identity(db, owner, item):
        if item["origin"] == "record":
            row = db.execute("SELECT child_record_id AS record_id FROM crm_analysis_actions WHERE owner=? AND parent_record_id=? AND action_id=?",
                             (owner, item["origin_id"], item["action_id"])).fetchone()
        elif item["origin"] == "material":
            row = db.execute("SELECT record_id FROM crm_material_actions WHERE owner=? AND material_id=? AND id=?",
                             (owner, item["origin_id"], item["action_id"])).fetchone()
        else:
            row = db.execute("SELECT record_id FROM crm_visit_adoptions WHERE owner=? AND visit_id=? AND action_key=?",
                             (owner, item["origin_id"], item["action_id"])).fetchone()
        return row["record_id"] if row else None

    def _reconcile_completed_receipt(self, owner, batch, response):
        """Correct an old false timeline failure using exact adoption FKs only.

        No source, record or fact is re-applied. Preserve the previous decision
        inside the item journal, and leave every other business failure intact.
        """
        known_errors = {"所选历程归属或依据已失效，请重新核对后讨论", "行动的原话或归属已变化，请先明确核对；没有覆盖已有的人物关联"}
        entries = {row["item_id"]: row for row in self.crm._db.execute(
            "SELECT * FROM crm_exchange_batch_items WHERE owner=? AND batch_id=?", (owner, batch["id"]))}
        updates = []
        for index, receipt in enumerate(response.get("results", [])):
            entry = entries.get(receipt["item_id"])
            if receipt["status"] != "blocked" or receipt.get("error") not in known_errors or not entry:
                continue
            item, checkpoint = json.loads(entry["item_json"]), json.loads(entry["checkpoint_json"])
            if item["kind"] != "action" or entry["stage"] != "draft_edited" or not checkpoint.get("record_id"):
                continue
            record_id = self._adopted_identity(self.crm._db, owner, item)
            record = self.crm.get_record(owner, record_id) if record_id and record_id == checkpoint["record_id"] else None
            if record is None:
                continue
            corrected = {"item_id": receipt["item_id"], "status": "already_confirmed", "previous_receipt": copy.deepcopy(receipt),
                "result": {"record_id": record_id, "record": record, "proposal_id": record["proposal_id"],
                    "timeline_status": "needs_review", "timeline_event_key": response.get("workspace", {}).get("source", {}).get("event_key"),
                    "message": "该行动此前已采用，本次没有重复建立；原来源历程关联仍需核对，旧失败回执已保留。"}}
            response["results"][index] = corrected
            updates.append(corrected)
        if not updates:
            return response
        response["status"] = "complete" if all(item["status"] in ("confirmed", "already_confirmed") for item in response["results"]) else "partial"
        if response.get("workspace"):
            source = response["workspace"]["source"]
            try:
                response["workspace"] = self.get(owner, source["type"], source["id"])
            except KeyError:
                pass  # The historical receipt still proves adoption after a source was hidden.
        with self.crm._transaction() as db:
            for corrected in updates:
                db.execute("UPDATE crm_exchange_batch_items SET result_json=?,updated_at=? WHERE owner=? AND batch_id=? AND item_id=?",
                    (_json(corrected), self.clock(), owner, batch["id"], corrected["item_id"]))
            db.execute("UPDATE crm_exchange_batches SET status=?,response_json=?,updated_at=? WHERE owner=? AND id=?",
                       (response["status"], _json(response), self.clock(), owner, batch["id"]))
        return response

    @staticmethod
    def _orphan_edit_safe(record, item, request, checkpoint):
        draft = {**item["draft"], **request.get("draft", {})}
        desired = draft.get("title", record["title"]) == record["title"] and all(
            record.get("action_terms", {}).get(key) == value for key, value in draft.items() if key in TERM_FIELDS)
        original = item["draft"].get("title") == record["title"] and all(
            record.get("action_terms", {}).get(key) == value for key, value in item["draft"].items() if key in TERM_FIELDS)
        untouched = record["updated_at"] == checkpoint.get("adoption_started_at") and record.get("terms_updated_at") == checkpoint.get("adoption_started_at")
        if not desired and not (original and untouched):
            raise ExchangeConflict("中断期间已采用行动被修改；没有用最新版本覆盖它，请从行动页核对。")

    def _recover_operation(self, owner, batch, entry, view):
        """Recover a generic response failure using exact canonical identities.

        This never relaxes a business CAS failure. Existing adoption foreign
        keys and idempotent command IDs are the receipts, rather than a latest
        row, matching title, or timestamp guess.
        """
        item = json.loads(entry["item_json"])
        if item["kind"] == "action":
            db = self.crm._db
            record_id = self._adopted_identity(db, owner, item)
            if not record_id:
                return None
            checkpoint = db.execute("SELECT checkpoint_json FROM crm_exchange_batch_items WHERE owner=? AND batch_id=? AND item_id=?",
                                    (owner, batch["id"], item["id"])).fetchone()
            if not json.loads(checkpoint[0]).get("record_id"):
                record = self.crm.get_record(owner, record_id)
                self._orphan_edit_safe(record, item, json.loads(entry["request_json"]), json.loads(checkpoint[0]))
                self._checkpoint(owner, batch["id"], item["id"], "adoption_recovered", {"record_id": record["id"],
                    "record_updated_at": record["updated_at"], "terms_updated_at": record.get("terms_updated_at")})
        elif item["kind"] == "relationship":
            draft = {**item["draft"], **json.loads(entry["request_json"]).get("draft", {})}
            if item.get("subtype") != "timeline_context":
                project_id, contact_id = draft.get("opportunity_id"), draft.get("contact_id")
                if not project_id or not contact_id:
                    return None
                people = self.workspace.stakeholders(owner, item["scope"]["customer_id"], project_id)
                person = next((person for person in people["items"] if person["contact_id"] == contact_id), None)
                comparisons = {key: value for key, value in draft.items() if key not in ("opportunity_id", "contact_id")}
                comparisons["evidence"] = item["evidence"]
                if not person or not person.get("membership_valid") or any(person.get(key) != value for key, value in comparisons.items()):
                    return None
                return "already_confirmed", {"opportunity_id": project_id, "contact_id": contact_id, "project_revision": people["project_revision"]}
            key = item["id"].split(":", 1)[1]
            row = self.crm._db.execute("SELECT data_json FROM crm_timeline_contexts WHERE owner=? AND event_key=?", (owner, key)).fetchone()
            stored = json.loads(row[0]) if row else {}
            if not self._context_matches(stored, draft):
                return None
            event = self.timeline.get_event(owner, key)
            if event.get("needs_review"):
                return None
            return "already_confirmed", {"event_key": key, "event_revision": event["revision"]}
        elif item["kind"] not in ("customer", "schedule"):
            return None
        fresh = self.crm._db.execute("SELECT * FROM crm_exchange_batch_items WHERE owner=? AND batch_id=? AND item_id=?",
                                     (owner, batch["id"], item["id"])).fetchone()
        return self._apply(owner, batch, dict(fresh), view)

    def _journal_source_revision(self, owner, batch, original_revision):
        """Accept only exact source-semantic writes recorded by this batch.

        A crash after save_context may precede its completion receipt. The
        write-ahead expected revision and matching canonical context recover
        that narrow transition; external text/scope edits cannot match it.
        """
        accepted = original_revision
        for row in self.crm._db.execute(
                "SELECT * FROM crm_exchange_batch_items WHERE owner=? AND batch_id=? ORDER BY rowid", (owner, batch["id"])):
            checkpoint = json.loads(row["checkpoint_json"])
            if checkpoint.get("source_revision_before") != accepted or not checkpoint.get("source_revision_after"):
                continue
            item = json.loads(row["item_json"])
            if item.get("subtype") != "timeline_context":
                continue
            draft = {**item["draft"], **json.loads(row["request_json"]).get("draft", {})}
            key = item["id"].split(":", 1)[1]
            stored = self.crm._db.execute("SELECT data_json FROM crm_timeline_contexts WHERE owner=? AND event_key=?", (owner, key)).fetchone()
            values = json.loads(stored[0]) if stored else {}
            if not self._context_matches(values, draft):
                continue
            event = self.timeline.get_event(owner, key)
            if event.get("needs_review"):
                continue
            accepted = checkpoint["source_revision_after"]
        return accepted

    def _apply(self, owner, batch, entry, view):
        item, request = json.loads(entry["item_json"]), json.loads(entry["request_json"])
        checkpoint = json.loads(entry["checkpoint_json"])
        draft = {**item["draft"], **request.get("draft", {})}
        kind, now = item["kind"], self.clock()
        if kind == "profile":
            body = self._profile_decision(item, request)
            recovered = self._recovered_profile(owner, item, body)
            if recovered:
                return "already_confirmed", recovered
            if item["candidate"]["status"] == "confirmed":
                fact = self._confirmed_fact(owner, item["candidate"])
                if fact and fact == {"value": body["value"], "basis": body["basis"]}:
                    return "already_confirmed", {"candidate_id": item["candidate"]["id"], "fact_id": item["candidate"]["confirmed_fact_id"],
                                                  "message": "此历史候选已确认，不重复写入；当前资料如有后续变化，以画像当前值为准。"}
                raise ValueError("该候选已确认；修改后的内容尚未保存，请从画像编辑或新核实记录继续，不能把旧候选回执当作新保存。")
            candidate = self.profile.decide(owner, item["candidate"]["id"], body)
            return "confirmed", {"candidate_id": candidate["id"], "fact_id": candidate["confirmed_fact_id"], "candidate": candidate}
        if kind == "customer":
            original_id = item["customer_draft"]["id"]
            original = self.crm.get_customer_draft(owner, original_id)
            if original["status"] == "confirmed" and not checkpoint.get("draft_id"):
                if draft["changes"] != item["customer_draft"]["changes"]:
                    raise ValueError("基本资料准备稿已确认，新的修改请使用单位/联系人编辑入口。")
                return "already_confirmed", original
            draft_id = checkpoint.get("draft_id", original_id)
            if draft["changes"] != item["draft"]["changes"]:
                with self.crm._lock:
                    row = self.crm._db.execute("SELECT payload_json,source_record_id FROM crm_customer_drafts WHERE owner=? AND id=?", (owner, original_id)).fetchone()
                    payload = json.loads(row[0])
                    for change in draft["changes"]:
                        target, key = change["target"], change["key"]
                        if target in ("basic", "contact"):
                            payload[target][key] = change["after"]
                        else:
                            field = next(field for field in payload["attributes"] if field["target"] == target and field["key"] == key)
                            field["value"] = change["after"]
                            if "basis" in change:
                                field["basis"] = change["basis"]
                    created = self.crm.create_customer_draft(owner, payload, now,
                        source_id="exchange-customer:" + str(batch["id"]) + ":" + str(original_id), source_record_id=row["source_record_id"])
                draft_id = created["id"]
                self._checkpoint(owner, batch["id"], item["id"], "draft_created", {"draft_id": draft_id})
            result = self.crm.confirm_customer_draft(owner, draft_id, now)
            return "confirmed", result
        if kind == "relationship":
            if item.get("subtype") == "timeline_context":
                if checkpoint.get("source_revision_after"):
                    recovered = self._recover_operation(owner, batch, entry, view)
                    if recovered:
                        return recovered
                allowed = {person["id"] for person in self._options(owner, view["source"])["contacts"]}
                if any(relation["contact_id"] not in allowed for relation in draft.get("contact_relations", [])):
                    raise ValueError("人物已归档或不属于本单位/本项目明确参与的人，请先核对项目范围。")
                prospective = copy.deepcopy(view["source"])
                for target, field in (("event_kind", "kind"), ("occurred_at", "occurred_at")):
                    if field in draft:
                        prospective[target] = draft[field]
                self._checkpoint(owner, batch["id"], item["id"], "context_started", {
                    "source_revision_before": view["source_revision"],
                    "source_revision_after": self._source_revision(prospective)})
                result = self.timeline.save_context(owner, item["id"].split(":", 1)[1],
                    {**draft, "expected_revision": item["versions"]["event_revision"]})
                actual = self._source(owner, view["source"]["type"], view["source"]["id"])[0]
                if actual["revision"] != self._source_revision(prospective):
                    raise ExchangeConflict("来源在归属核对期间发生其他变化，请重新核对；已保存核对保留。")
                self._checkpoint(owner, batch["id"], item["id"], "context_applied", {})
                return "confirmed", {"event_key": result["key"], "event_revision": result["revision"]}
            customer = item["scope"]["customer_id"]
            project_id, contact_id = draft.get("opportunity_id"), draft.get("contact_id")
            if not project_id or not contact_id or not draft.get("roles"):
                raise ValueError("请明确项目、联系人与其本项目角色；权限不会写为通用画像。")
            if draft.get("basis") == "reported" and item["candidate"]["basis"] == "observation":
                raise ValueError("个人判断的项目权限线索不能升级为交流明确信息；请补充核实后的原话。")
            revision = item["versions"].get("project_revision")
            if revision is None:
                raise ValueError("请先保存项目归属并核对当前决策关系，再确认。")
            # Earlier confirmed roles in this locked journal are known writes,
            # not unseen external updates. Preserve all original item scopes.
            own_revisions = [revision]
            for earlier in self.crm._db.execute("SELECT result_json FROM crm_exchange_batch_items WHERE owner=? AND batch_id=? AND status='complete' AND result_json IS NOT NULL", (owner, batch["id"])):
                receipt = json.loads(earlier[0])
                result = receipt.get("result", {})
                if receipt["status"] in ("confirmed", "already_confirmed") and result.get("opportunity_id") == project_id and result.get("project_revision"):
                    own_revisions.append(result["project_revision"])
            actual_revision = self.workspace._require_opportunity(self.crm._db, owner, customer, project_id)["revision"]
            if actual_revision == max(own_revisions):
                revision = actual_revision
            data = {key: value for key, value in draft.items() if key != "opportunity_id"}
            data.update({"expected_revision": revision, "evidence": item["evidence"]})
            project = self.workspace.upsert_stakeholder(owner, customer, project_id, data)
            return "confirmed", {"opportunity_id": project_id, "contact_id": contact_id, "project_revision": project["revision"]}
        if kind == "action":
            adopted_id = item["current"].get("record", {}).get("id") if item["current"].get("record") else None
            if adopted_id and not checkpoint.get("record_id"):
                return "already_confirmed", {"record_id": adopted_id, "record": self.crm.get_record(owner, adopted_id),
                                              **action_people_receipt(self.timeline, owner, adopted_id),
                                              "message": "该行动已采用；修改既有事项请使用行动编辑或改期入口。"}
            contact_ids = request.get("contact_ids", item.get("contact_ids", []))
            # The batch's original options token was checked before any writes.
            # Its own preceding relationship confirmations may legitimately
            # change that token; current membership is checked again here.
            if item["scope"]["customer_id"]:
                validate_action_contacts(self.crm._db, owner, item["scope"]["customer_id"],
                    item["scope"].get("opportunity_id"), self.workspace, contact_ids,
                    expected_version=self._accepted_contact_version(owner, batch, item, request)
                    if contact_ids and not checkpoint.get("action_people_bound") else None)
            project_id = item["scope"].get("opportunity_id")
            if project_id:
                project = self.workspace._require_opportunity(self.crm._db, owner, view["source"]["customer_id"], project_id)
                if project["archived"]:
                    raise ValueError("项目已归档，请恢复或核对新项目后采用行动。")
            action_source = {**view["source"], "opportunity_id": project_id}
            timeline_key, timeline_warning = self._action_timeline_plan(owner, action_source)
            if not checkpoint.get("record_id"):
                existing_adoption = self._adopted_identity(self.crm._db, owner, item)
                if existing_adoption and not checkpoint.get("adoption_started_at"):
                    raise ExchangeConflict("该行动已从其他入口采用，请核对当前事项；本批旧准备稿没有覆盖用户后续编辑。")
                resumed_adoption = entry["stage"] == "adopting" or existing_adoption is not None
                if not checkpoint.get("adoption_started_at"):
                    checkpoint = self._checkpoint(owner, batch["id"], item["id"], "adopting", {"adoption_started_at": now})
                if item["origin"] == "record":
                    adopted = self.crm.adopt_action(owner, item["origin_id"], item["action_id"], now)
                elif item["origin"] == "material":
                    scope = item.get("project_scope") or {}
                    seen_visit_revision = item["versions"].get("visit_revision")
                    if item["current"]["action"].get("visit_id"):
                        # Our earlier actions may bump only adoption state. The
                        # group's raw evidence CAS remains in source_revision.
                        seen_visit_revision = self.visits.detail(owner, item["current"]["action"]["visit_id"])["visit"]["revision"]
                    result = self.materials.adopt(owner, item["origin_id"], item["action_id"], item["versions"]["source_version"],
                        expected_visit_revision=seen_visit_revision,
                        project_scope_revision=item["versions"].get("project_scope_revision"),
                        opportunity_id=scope.get("opportunity_id") if scope.get("explicit") else None,
                        confirm_single_action=scope.get("confirm_single_action", False))
                    adopted = result["record"]
                else:
                    detail = self.visits.detail(owner, item["origin_id"])
                    latest_action = next((row for row in detail["actions"] if row["key"] == item["action_id"]), None)
                    compared = ("title", "kind", "evidence", "reason", "needs_review", "review_reasons", "identities", *TERM_FIELDS)
                    original_action = item["current"]["action"]
                    if not latest_action or any(latest_action.get(key) != original_action.get(key) for key in compared):
                        raise ExchangeConflict("拜访行动依据或歧义已变化，请核对后继续。")
                    # Earlier items in this exact journal may adopt other
                    # actions, bumping the visit's full adoption fingerprint.
                    # Raw source CAS and this action comparison stay intact.
                    scope = item.get("project_scope") or {}
                    result = self.visits.adopt(owner, item["origin_id"], item["action_id"], detail["visit"]["revision"],
                        project_scope_revision=item["versions"].get("project_scope_revision"),
                        opportunity_id=scope.get("opportunity_id") if scope.get("explicit") else None,
                        confirm_single_action=scope.get("confirm_single_action", False))
                    adopted = result["record"]
                if resumed_adoption:
                    self._orphan_edit_safe(adopted, item, request, checkpoint)
                link = self.crm._db.execute("SELECT * FROM crm_opportunity_links WHERE owner=? AND entity_type='record' AND entity_id=?",
                                            (owner, adopted["id"])).fetchone()
                checkpoint = self._checkpoint(owner, batch["id"], item["id"], "adopted", {"record_id": adopted["id"],
                    "record_updated_at": adopted["updated_at"], "terms_updated_at": adopted.get("terms_updated_at"),
                    "project_link": dict(link) if link else None})
            record = self.crm.get_record(owner, checkpoint["record_id"])
            link = self.crm._db.execute("SELECT * FROM crm_opportunity_links WHERE owner=? AND entity_type='record' AND entity_id=?",
                                        (owner, record["id"])).fetchone()
            if "project_link" in checkpoint and (dict(link) if link else None) != checkpoint["project_link"]:
                raise ExchangeConflict("中断期间待办项目归属已被修改；没有覆盖后续归属，请核对当前事项。")
            matches = (draft.get("title", record["title"]) == record["title"] and all(
                record.get("action_terms", {}).get(key) == value for key, value in draft.items() if key in TERM_FIELDS))
            if not matches:
                record = edit_unconfirmed_action(self.crm, owner, record["id"], draft, now,
                    expected_updated_at=checkpoint["record_updated_at"], expected_terms_updated_at=checkpoint.get("terms_updated_at"))
            checkpoint = self._checkpoint(owner, batch["id"], item["id"], "draft_edited", {"record_updated_at": record["updated_at"],
                "terms_updated_at": record.get("terms_updated_at")})
            if project_id:
                # Only refresh our own exact link after our draft edit. Never
                # replace a user's existing assignment (including unassigned).
                if link is None or link["opportunity_id"] == project_id:
                    self.workspace.link(owner, "record", record["id"], project_id)
                    applied_link = self.crm._db.execute("SELECT * FROM crm_opportunity_links WHERE owner=? AND entity_type='record' AND entity_id=?",
                                                        (owner, record["id"])).fetchone()
                    checkpoint = self._checkpoint(owner, batch["id"], item["id"], "draft_edited", {"project_link": dict(applied_link)})
            result = {"record_id": record["id"], "record": record, "proposal_id": record["proposal_id"]}
            if timeline_key and not checkpoint.get("action_people_bound"):
                try:
                    self.timeline.link_action(owner, record["id"], {"customer_id": record["customer_id"],
                        "opportunity_id": project_id}, source_event_key=timeline_key)
                except TimelineConflict:
                    timeline_warning = "行动已采用；来源历程在关联期间变化，未覆盖旧人物关联，请重新核对。"
                result["timeline_status"] = "needs_review" if timeline_warning else "linked"
            if self.timeline and record["customer_id"] and not checkpoint.get("action_people_bound"):
                event = self.timeline.get_event(owner, "record:" + str(record["id"]))
                expected_people_revision = checkpoint.get("action_people_revision")
                if expected_people_revision is None:
                    checkpoint = self._checkpoint(owner, batch["id"], item["id"], "draft_edited",
                        {"action_people_revision": event["revision"], "action_contact_ids": contact_ids})
                    expected_people_revision = event["revision"]
                if event["revision"] != expected_people_revision:
                    # Resume a successful bind whose receipt was interrupted,
                    # but never overwrite a later user's change to this event.
                    actual = {person["contact_id"] for person in event["contact_relations"] if person.get("valid")}
                    if (event.get("needs_review") or event["kind"] != "reflection" or event.get("occurred_at") is not None
                            or actual != set(contact_ids) or any(person["relation"] != "about" for person in event["contact_relations"] if person.get("valid"))):
                        raise ExchangeConflict("中断期间待办人物或时间已被修改；没有覆盖后续核对。")
                else:
                    bind_action_contacts(self.timeline, owner, record["id"], contact_ids,
                                         expected_revision=expected_people_revision)
                self._checkpoint(owner, batch["id"], item["id"], "draft_edited", {"action_people_bound": True})
            result.update(action_people_receipt(self.timeline, owner, record["id"]))
            if timeline_warning:
                result.update(timeline_status="needs_review", timeline_event_key=view["source"].get("event_key"), message=timeline_warning)
            return "confirmed", result
        # Schedules require a separately selected item and an existing/adopted TODO.
        record_id = checkpoint.get("record_id") or item["current"].get("record_id")
        if not record_id:
            with self.crm._lock:
                dependency = self.crm._db.execute("SELECT result_json FROM crm_exchange_batch_items WHERE owner=? AND batch_id=? AND item_id=? AND status='complete'", (owner, batch["id"], item["action_item_id"])).fetchone()
            receipt = json.loads(dependency[0]) if dependency and dependency[0] else {}
            record_id = receipt.get("result", {}).get("record_id")
        if not record_id:
            raise ValueError("请同时采用对应行动，或先采用行动后再安排提醒。")
        record = self.crm.get_record(owner, record_id)
        if record is None or record["status"] == "done" or record.get("hidden", False):
            raise ValueError("对应行动已完成或不可用，不能新增提醒。")
        if draft.get("remind_at") is None or draft["remind_at"] <= now:
            raise ValueError("请补充明确的未来提醒时间；检查日或截止日不自动当作执行时间。")
        if draft.get("duration_minutes") is None:
            raise ValueError("请核对预计用时后再安排提醒；未知用时不会默认30分钟，原行动和日程保留。")
        if "record_updated_at" in checkpoint and record["updated_at"] != checkpoint["record_updated_at"]:
            raise ExchangeConflict("中断期间行动已变化；旧提醒确认没有覆盖新事项。")
        if "terms_updated_at" in checkpoint and record.get("terms_updated_at") != checkpoint["terms_updated_at"]:
            raise ExchangeConflict("中断期间执行责任、检查日或截止日已变化；请核对当前行动后再安排。")
        if not checkpoint.get("record_updated_at"):
            checkpoint = self._checkpoint(owner, batch["id"], item["id"], "schedule_started", {"record_id": record_id,
                "record_updated_at": record["updated_at"], "terms_updated_at": record.get("terms_updated_at")})
        proposal_id = checkpoint.get("proposal_id")
        if proposal_id is None:
            proposal = self.crm.get_proposal(owner, record["proposal_id"]) if record["proposal_id"] else None
            if proposal and (proposal.get("change_kind") == "cancel" or proposal.get("target_task_id") is not None):
                raise ValueError("这里仅安排新提醒；已有日程的改期或取消提案请从原日程入口明确核对，本包不会确认它。")
            if proposal and proposal["status"] == "confirmed":
                task = self.crm.get_task(owner, proposal["task_id"]) if proposal["task_id"] else None
                if not task or task["status"] != "pending":
                    raise ValueError("原日程已结束或取消；请从行动重新安排入口核对，旧回执不代表提醒仍有效。")
                if any(proposal[key] != draft.get(key) for key in ("remind_at", "duration_minutes", "deadline_at")):
                    raise ValueError("已有正式日程，请明确改期；本包不会改动原提醒。")
                return "already_confirmed", {"record_id": record_id, "proposal_id": proposal["id"], "task_id": proposal["task_id"]}
            if proposal and proposal["status"] == "pending" and all(proposal[key] == draft.get(key) for key in ("remind_at", "duration_minutes", "deadline_at")):
                proposal_id = proposal["id"]
            else:
                if proposal and proposal["status"] == "pending":
                    expected = item["versions"]["proposal_updated_at"]
                    if item["current"].get("proposal") and proposal["id"] == item["current"]["proposal"]["id"] and proposal["updated_at"] != expected:
                        raise ExchangeConflict("待确认提案刚被修改，准备稿未撤回或覆盖它。")
                    self.crm.execute(owner, "exchange-supersede:" + str(batch["id"]) + ":" + item["id"],
                        {"action": "reject", "proposal_id": proposal["id"]}, now)
                key = "exchange-propose:" + str(batch["id"]) + ":" + item["id"]
                intent = {"title": record["title"], **draft}
                if checkpoint.get("proposal_intent") and checkpoint["proposal_intent"] != intent:
                    raise ExchangeConflict("中断期间提醒准备内容发生变化，请重新核对。")
                checkpoint = self._checkpoint(owner, batch["id"], item["id"], "proposing", {"proposal_intent": intent})
                self.crm.execute(owner, key, {"action": "propose", "title": record["title"], **draft}, now)
                with self.crm._lock:
                    # execute serializes command_results. Its own replay returns
                    # the same P number; never infer ownership from a global MAX.
                    reply = self.crm._db.execute("SELECT reply FROM command_results WHERE owner=? AND source_id=?", (owner, key)).fetchone()[0]
                match = re.search(r"P(\d+)", reply or "")
                if not match:
                    raise ValueError(reply or "提醒提案未建立，已有行动保留。")
                proposal_id = int(match.group(1))
                proposed = self.crm.get_proposal(owner, proposal_id)
                if not proposed or any(proposed.get(field) != value for field, value in intent.items()):
                    raise ExchangeConflict("本次新提案在中断期间已被修改；没有确认未核对的新时间。")
                self.crm.link_proposal(owner, record_id, proposal_id, now)
            checkpoint = self._checkpoint(owner, batch["id"], item["id"], "proposed", {"record_id": record_id, "proposal_id": proposal_id,
                "record_updated_at": self.crm.get_record(owner, record_id)["updated_at"],
                "proposal_snapshot": self._proposal_snapshot(self.crm.get_proposal(owner, proposal_id))})
        proposal = self.crm.get_proposal(owner, proposal_id)
        if proposal and (proposal.get("change_kind") == "cancel" or proposal.get("target_task_id") is not None):
            raise ValueError("已有日程的改期或取消需要在原日程入口核对，本包不会替你确认。")
        snapshot = checkpoint.get("proposal_snapshot")
        if not proposal or not snapshot:
            raise ExchangeConflict("缺少本次提案的已核对版本，请从提醒入口核对；原行动和提案保留。")
        actual = self._proposal_snapshot(proposal)
        if proposal["status"] == "confirmed":
            own_confirm = self.crm._db.execute("SELECT 1 FROM command_results WHERE owner=? AND source_id=?",
                (owner, "exchange-confirm:" + str(batch["id"]) + ":" + item["id"])).fetchone()
            operational = {"status", "updated_at", "task_id"}
            if not own_confirm or any(actual[key] != value for key, value in snapshot.items() if key not in operational):
                raise ExchangeConflict("日程已被其他操作确认或修改；没有把旧批次当作新安排的回执。")
            task = self.crm.get_task(owner, proposal["task_id"]) if proposal["task_id"] else None
            if not task or task["status"] != "pending" or any(task.get(key) != proposal.get(key) for key in ("title", "remind_at", "duration_minutes", "deadline_at")):
                raise ExchangeConflict("已确认日程后来已结束、取消或改期；旧回执不代表原提醒仍有效。")
            return "already_confirmed", {"record_id": record_id, "proposal_id": proposal_id, "task_id": proposal["task_id"]}
        if actual != snapshot:
            raise ExchangeConflict("提醒提案在中断期间已变化，请核对当前标题和时间后再确认。")
        expected = item["versions"]["proposal_updated_at"]
        if item["current"].get("proposal") and item["current"]["proposal"]["id"] == proposal_id and proposal["updated_at"] != expected:
            raise ExchangeConflict("提醒提案已有变化，请核对当前时间后再确认。")
        self.crm.execute(owner, "exchange-confirm:" + str(batch["id"]) + ":" + item["id"], {"action": "confirm", "proposal_id": proposal_id}, now)
        proposal = self.crm.get_proposal(owner, proposal_id)
        if proposal["status"] != "confirmed":
            raise ExchangeConflict("提醒尚未生效：时间冲突、已过期或原事项变化。行动及准备稿保留，请核对提案。")
        return "confirmed", {"record_id": record_id, "proposal_id": proposal_id, "task_id": proposal["task_id"]}

    def confirm(self, owner, source_type, source_id, data):
        owner = _owner(owner)
        kind, identifier = self._source_key(source_type, source_id)
        if not isinstance(data, dict) or set(data) - {"request_id", "expected_revision", "source_revision", "expected_review_revision", "items"}:
            raise ValueError("交流确认字段无效")
        request_id = _text(data.get("request_id"), "请求编号", 160, required=True).strip()
        if not request_id:
            raise ValueError("请求编号不能为空白")
        signature = _hash([kind, identifier, data])
        with self.crm._lock:
            prior = self.crm._db.execute("SELECT * FROM crm_exchange_batches WHERE owner=? AND request_id=?", (owner, request_id)).fetchone()
            if prior and prior["signature"] != signature:
                raise ExchangeConflict("同一请求编号对应另一份确认内容，请为新编辑重新生成请求编号。")
            if prior and prior["response_json"]:
                return self._reconcile_completed_receipt(owner, prior, json.loads(prior["response_json"]))
            view = self.get(owner, kind, identifier)
            if not prior:
                self._guard(view, data)
                checked = self._requests(view, data, owner=owner)
                if any(request.get("selected") is False for _, request in checked):
                    raise ValueError("确认请求不能包含明确未选中的条目。")
                self._preflight_contacts(owner, checked)
                view["_owner"] = owner
                with self.crm._transaction() as db:
                    self._save_drafts(db, view, checked, self.clock(), confirming=True)
                    claim = self._mark_review(db, owner, view["id"], self.clock(),
                        self._prepare_source_guard(db, owner, kind, identifier))
                    batch_id = db.execute("INSERT INTO crm_exchange_batches(owner,workspace_id,request_id,signature,request_json,created_at,updated_at,review_revision,workspace_revision) VALUES (?,?,?,?,?,?,?,?,?)",
                        (owner, view["id"], request_id, signature, _json(data), self.clock(), self.clock(),
                         claim["review_revision"], claim["revision"])).lastrowid
                    for item, request in checked:
                        # Store the original AI value/current target separately
                        # from the user draft, including inline unsaved edits.
                        db.execute("INSERT INTO crm_exchange_batch_items(owner,batch_id,item_id,item_json,request_json,updated_at) VALUES (?,?,?,?,?,?)",
                                   (owner, batch_id, item["id"], _json(item), _json(request), self.clock()))
                prior = self.crm._db.execute("SELECT * FROM crm_exchange_batches WHERE owner=? AND id=?", (owner, batch_id)).fetchone()
            batch = dict(prior)
            entries = [dict(row) for row in self.crm._db.execute("SELECT * FROM crm_exchange_batch_items WHERE owner=? AND batch_id=? ORDER BY rowid", (owner, batch["id"]))]
            # Confirm the user's source meaning before facts, regardless of
            # frontend group order. Selecting an action still never schedules.
            entries.sort(key=lambda entry: 0 if json.loads(entry["item_json"]).get("subtype") == "timeline_context"
                         else 2 if json.loads(entry["item_json"])["kind"] == "schedule" else 1)
            results = []
            for entry in entries:
                if entry["result_json"]:
                    results.append(json.loads(entry["result_json"]))
                    continue
                item, request = json.loads(entry["item_json"]), json.loads(entry["request_json"])
                try:
                    if view["source_revision"] != self._journal_source_revision(owner, batch, data["source_revision"]):
                        raise ExchangeConflict("来源已经变化；尚未确认项保留，请重新整理后核对。")
                    if item["kind"] == "action" and item["scope"].get("opportunity_id"):
                        project = self.workspace._require_opportunity(self.crm._db, owner, item["scope"]["customer_id"],
                                                                      item["scope"]["opportunity_id"])
                        if project["archived"]:
                            raise ValueError("项目已归档，请恢复或核对新项目后采用行动。")
                    if entry["status"] == "pending" and item["version"] != request["expected_version"]:
                        raise ExchangeConflict("该项资料或目标刚被更新，请核对最新内容后继续。")
                    if item["status"] in ("stale", "blocked", "rejected"):
                        raise ValueError("该建议来源已过期、被拒绝或需先核对歧义；已有资料保留。")
                    with self.crm._transaction() as db:
                        db.execute("UPDATE crm_exchange_batch_items SET status='applying',updated_at=? WHERE owner=? AND batch_id=? AND item_id=?", (self.clock(), owner, batch["id"], item["id"]))
                    if item["kind"] == "relationship" and item.get("subtype") != "timeline_context":
                        checkpoint = json.loads(entry["checkpoint_json"])
                        if "contact_versions_before" not in checkpoint:
                            self._checkpoint(owner, batch["id"], item["id"], entry["stage"],
                                {"contact_versions_before": self._batch_contact_versions(owner, batch)})
                    status, result = self._apply(owner, batch, entry, view)
                    receipt = {"item_id": item["id"], "status": status, "result": result}
                except Exception as exc:
                    recovered = None
                    if item["kind"] == "profile":
                        try:
                            recovered = self._recovered_profile(owner, item, self._profile_decision(item, request))
                        except (ValueError, KeyError):
                            pass
                    elif not isinstance(exc, (ValueError, KeyError)):
                        try:
                            recovered_operation = self._recover_operation(owner, batch, entry, view)
                            if recovered_operation:
                                status, result = recovered_operation
                                recovered = result
                        except Exception as recovery_error:
                            if isinstance(recovery_error, (ValueError, KeyError)):
                                exc = recovery_error
                    if recovered:
                        receipt = {"item_id": item["id"], "status": "already_confirmed", "result": recovered}
                    else:
                        receipt = {"item_id": item["id"], "status": "conflict" if isinstance(exc, (ExchangeConflict, ProfileConflict, RecordConflict)) else "blocked" if isinstance(exc, (ValueError, KeyError)) else "failed",
                                   "error": str(exc) if isinstance(exc, (ValueError, KeyError)) else "该项未能完成，请核对回执后重试；原文和其他已确认资料保留。"}
                        stored_checkpoint = self.crm._db.execute("SELECT stage,checkpoint_json FROM crm_exchange_batch_items WHERE owner=? AND batch_id=? AND item_id=?", (owner, batch["id"], item["id"])).fetchone()
                        progress = json.loads(stored_checkpoint["checkpoint_json"])
                        if progress.get("record_id") or progress.get("proposal_id"):
                            receipt["result"] = {key: progress[key] for key in ("record_id", "proposal_id", "draft_id") if key in progress}
                            receipt["result"]["stage"] = stored_checkpoint["stage"]
                if (item["kind"] == "relationship" and item.get("subtype") != "timeline_context"
                        and receipt["status"] in ("confirmed", "already_confirmed")):
                    current_entry = self.crm._db.execute("SELECT * FROM crm_exchange_batch_items WHERE owner=? AND batch_id=? AND item_id=?",
                                                        (owner, batch["id"], item["id"])).fetchone()
                    if "contact_versions_after" not in json.loads(current_entry["checkpoint_json"]):
                        self._checkpoint(owner, batch["id"], item["id"], current_entry["stage"],
                            {"contact_versions_after": self._batch_contact_versions(owner, batch)})
                with self.crm._transaction() as db:
                    db.execute("UPDATE crm_exchange_batch_items SET status='complete',result_json=?,updated_at=? WHERE owner=? AND batch_id=? AND item_id=?", (_json(receipt), self.clock(), owner, batch["id"], item["id"]))
                results.append(receipt)
                # Later selected items see scope changes caused by customer
                # confirmation, never silently inherit another customer.
                view = self.get(owner, kind, identifier)
            self._settle_review(owner, batch, results, data["source_revision"])
            response = {"request_id": request_id, "status": "complete" if all(row["status"] in ("confirmed", "already_confirmed") for row in results) else "partial",
                        "results": results, "workspace": self.get(owner, kind, identifier)}
            with self.crm._transaction() as db:
                db.execute("UPDATE crm_exchange_batches SET status=?,response_json=?,updated_at=? WHERE owner=? AND id=?",
                           (response["status"], _json(response), self.clock(), owner, batch["id"]))
            return response
