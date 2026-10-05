"""Explicit person/event history over original CRM sources, without scheduling.

This sidecar does not rewrite recordings or infer attendance from project roles.
Every confirmed context is bound to the actual source, and derived duplicates
are grouped only by durable relationships already stored by the application.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from contextlib import contextmanager

from .crm import _identifier, _owner, _pagination, _text
from .store import _timestamp
from .action_contract import TERM_FIELDS


KINDS = ("communication", "reflection", "discussion", "result")
_KEY = re.compile(r"^(record|visit|material|discussion|activity|outcome):([1-9][0-9]*)$")
_TABLES = {"record": "crm_records", "visit": "crm_visits", "material": "crm_materials",
           "discussion": "crm_sales_discussions", "activity": "crm_activities", "outcome": "crm_action_outcomes"}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _key(value):
    value = _text(value, "历程编号", 80, required=True)
    match = _KEY.fullmatch(value)
    if match is None:
        raise ValueError("历程编号无效")
    _identifier(int(match[2]))
    return match[1], int(match[2])


def _bounded_excerpt(value, limit=20000):
    if len(value) <= limit:
        return value
    marker = "\n【原文过长，已省略中间部分；打开来源可核对完整内容】\n"
    head = (limit - len(marker)) // 2
    return value[:head] + marker + value[-(limit-len(marker)-head):]


class TimelineConflict(ValueError):
    """The source or context changed after it was shown to the user."""


class TimelineService:
    def __init__(self, crm, workspace, *, visits=None, discussions=None, clock=time.time):
        self.crm, self.workspace = crm, workspace
        self.visits, self.discussions, self.clock = visits, discussions, clock
        with crm._transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS crm_timeline_contexts (
                    owner TEXT NOT NULL,event_key TEXT NOT NULL,data_json TEXT NOT NULL,
                    source_snapshot TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,updated_at REAL NOT NULL,
                    PRIMARY KEY(owner,event_key),CHECK(revision>0));
                CREATE TABLE IF NOT EXISTS crm_timeline_context_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,owner TEXT NOT NULL,event_key TEXT NOT NULL,
                    data_json TEXT NOT NULL,source_snapshot TEXT NOT NULL,revision INTEGER NOT NULL,
                    created_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS crm_timeline_history_owner
                    ON crm_timeline_context_history(owner,event_key,id);
                CREATE TABLE IF NOT EXISTS crm_timeline_requests (
                    owner TEXT NOT NULL,request_id TEXT NOT NULL,payload_hash TEXT NOT NULL,
                    record_id INTEGER NOT NULL,created_at REAL NOT NULL,
                    PRIMARY KEY(owner,request_id),
                FOREIGN KEY(owner,record_id) REFERENCES crm_records(owner,id));
            """)
        workspace.timeline = self

    @contextmanager
    def _transaction(self):
        """Use a savepoint when an adoption already holds the CRM transaction."""
        with self.crm._lock:
            db = self.crm._db
            if not db.in_transaction:
                with self.crm._transaction() as connection:
                    yield connection
            else:
                name = "timeline_" + uuid.uuid4().hex
                db.execute("SAVEPOINT " + name)
                try:
                    yield db
                    db.execute("RELEASE " + name)
                except BaseException:
                    db.execute("ROLLBACK TO " + name)
                    db.execute("RELEASE " + name)
                    raise

    @staticmethod
    def _exists(db, table):
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None

    def _rows(self, db, table, owner, extra=""):
        if not self._exists(db, table):
            return []
        return [dict(row) for row in db.execute("SELECT * FROM " + table + " WHERE owner=? " + extra + " ORDER BY id", (owner,))]

    def _contact(self, db, owner, identifier):
        row = db.execute("SELECT p.*,c.name AS unit_name FROM crm_contacts p JOIN crm_customers c "
                         "ON c.owner=p.owner AND c.id=p.customer_id WHERE p.owner=? AND p.id=?", (owner, identifier)).fetchone() if self._exists(db, "crm_contacts") else None
        if row is None:
            raise KeyError("未找到你的联系人")
        return {"id": row["id"], "contact_id": row["id"], "customer_id": row["customer_id"],
                "name": row["name"], "department": row["department"] if "department" in row.keys() else "",
                "unit_name": row["unit_name"], "archived": bool(row["archived"]) if "archived" in row.keys() else False}

    def _project(self, db, owner, identifier):
        row = db.execute("SELECT id,customer_id,name,archived FROM crm_opportunities WHERE owner=? AND id=?", (owner, identifier)).fetchone() if self._exists(db, "crm_opportunities") else None
        if row is None:
            raise KeyError("未找到你的项目")
        return {"id": row["id"], "customer_id": row["customer_id"], "name": row["name"], "archived": bool(row["archived"])}

    def _participates(self, db, owner, person, project_id, *, active=True):
        project = self._project(db, owner, project_id)
        if active and (project["archived"] or person["archived"]):
            return False
        if not hasattr(self.workspace, "stakeholders"):
            return False
        people = self.workspace.stakeholders(owner, project["customer_id"], project_id, include_archived=not active)["items"]
        return any(item["contact_id"] == person["id"] and item.get("membership_valid", False)
                   and (not active or not item.get("archived", False)) for item in people)

    def _scope(self, db, owner, scope, *, creating=False, events=None):
        if (not isinstance(scope, dict) or set(scope) - {"customer_id", "contact_id", "opportunity_id"}
                or ("customer_id" in scope) == ("contact_id" in scope)):
            raise ValueError("请明确一个单位或联系人范围")
        opportunity_id = _identifier(scope["opportunity_id"]) if scope.get("opportunity_id") is not None else None
        if "contact_id" in scope:
            person = self._contact(db, owner, _identifier(scope["contact_id"]))
            result = {"type": "contact", **{key: person[key] for key in ("id", "customer_id", "name", "department", "unit_name", "archived")}}
            projects = []
            if hasattr(self.workspace, "contact_projects"):
                for item in self.workspace.contact_projects(owner, person["id"], include_archived=True)["items"]:
                    if item.get("membership_valid") and not item.get("archived"):
                        projects.append(self._project(db, owner, item["opportunity_id"]))
            if creating and person["archived"]:
                raise ValueError("联系人已归档，仍可回看历史；不能新建当前交流")
            if not creating:
                history = events if events is not None else self._events(db, owner)
                for event in history.values():
                    if (not event["needs_review"] and event["opportunity_id"] is not None
                            and any(rel["contact_id"] == person["id"] and rel.get("valid") for rel in event["contact_relations"])
                            and not any(item["id"] == event["opportunity_id"] for item in projects)):
                        projects.append(self._project(db, owner, event["opportunity_id"]))
            if opportunity_id is not None:
                project = self._project(db, owner, opportunity_id)
                known = any(item["id"] == opportunity_id for item in projects)
                if not known and not creating:
                    # Explicit historical event membership remains readable even
                    # after project participation has been archived.
                    known = any(any(rel["contact_id"] == person["id"] and rel.get("valid") for rel in event["contact_relations"])
                                and event["opportunity_id"] == opportunity_id for event in (events if events is not None else self._events(db, owner)).values())
                if not known or (creating and not self._participates(db, owner, person, opportunity_id)):
                    raise ValueError("该联系人未明确参与这个项目，请核对项目与人物范围")
                if not any(item["id"] == project["id"] for item in projects):
                    projects.append(project)
        else:
            customer_id = _identifier(scope["customer_id"])
            self.crm._require_customer(db, owner, customer_id)
            customer = dict(db.execute("SELECT * FROM crm_customers WHERE owner=? AND id=?", (owner, customer_id)).fetchone())
            result = {"type": "customer", "id": customer_id, "customer_id": customer_id,
                      "name": customer["name"], "archived": bool(customer.get("archived", False))}
            projects = [self._project(db, owner, row["id"]) for row in self._rows(db, "crm_opportunities", owner) if row["customer_id"] == customer_id]
            if opportunity_id is not None:
                project = self._project(db, owner, opportunity_id)
                if project["customer_id"] != customer_id:
                    raise ValueError("这个项目属于另一个单位")
                if creating and project["archived"]:
                    raise ValueError("项目已归档，不能新建当前交流")
        result["opportunity_id"] = opportunity_id
        return result, sorted(projects, key=lambda item: (item["archived"], item["id"]))

    def _base_event(self, db, owner, entity_type, row, *, text=None, kind=None, customer_id=None):
        customer_id = row.get("customer_id") if customer_id is None else customer_id
        customer = db.execute("SELECT name FROM crm_customers WHERE owner=? AND id=?", (owner, customer_id)).fetchone()
        category = row.get("category", "")
        kind = kind or ("reflection" if category in ("idea", "visit_review") else "communication")
        text = row.get("content", "") if text is None else text
        return {"key": entity_type + ":" + str(row["id"]), "entity_type": entity_type, "entity_id": row["id"],
                "kind": kind, "title": row.get("title", "进展反馈"), "excerpt": text[:280], "text": text,
                "occurred_at": row.get("occurred_at"), "recorded_at": row["created_at"],
                "customer_id": customer_id, "customer_name": customer["name"] if customer else None,
                "opportunity_id": row.get("opportunity_id"), "opportunity_name": None, "opportunity_archived": False,
                "contact_relations": [], "needs_review": False, "related_event_key": None,
                "source_refs": [{"type": entity_type, "id": row["id"], "title": row.get("title", "进展反馈")}],
                "actions": [], "_raw": dict(row), "_sources": [], "_intrinsic_relations": []}

    def _events(self, db, owner):
        """Read sources only; do not call detail methods that resume processing."""
        events, records, materials = {}, {}, {}
        contexts = {row["event_key"]: row for row in self._rows_without_id(db, "crm_timeline_contexts", owner)}
        for row in self._rows(db, "crm_records", owner, "AND hidden=0"):
            records[row["id"]] = row
            events["record:" + str(row["id"])] = self._base_event(db, owner, "record", row)
        for row in self._rows(db, "crm_materials", owner, "AND duplicate_of IS NULL"):
            if row.get("record_id") and row["record_id"] not in records:
                continue
            version = db.execute("SELECT * FROM crm_material_versions WHERE owner=? AND material_id=? AND id=?", (owner, row["id"], row.get("current_version_id"))).fetchone()
            materials[row["id"]] = row
            event = self._base_event(db, owner, "material", row, text=version["text"] if version else "")
            event["_sources"] = [dict(version)] if version else []
            event["provider"] = row["provider"]
            event["source_state"] = row["status"]
            events[event["key"]] = event
        for row in self._rows(db, "crm_visits", owner):
            event = self._base_event(db, owner, "visit", row)
            events[event["key"]] = event
        for row in self._rows(db, "crm_sales_discussions", owner):
            messages = self._rows(db, "crm_sales_discussion_messages", owner)
            messages = [message for message in messages if message["thread_id"] == row["id"]]
            text = "\n\n".join(("我的问题：" if message["role"] == "user" else "历史AI建议：") + message["text"] for message in messages)
            event = self._base_event(db, owner, "discussion", row, text=text, kind="discussion")
            event["messages"] = [{key: message[key] for key in ("id", "role", "text", "status", "created_at")} for message in messages]
            last = max((message["created_at"] for message in messages), default=row["created_at"])
            event["occurred_at"] = last
            event["last_activity_at"] = last
            event["_sources"] = messages
            if row.get("contact_id") is not None:
                event["_intrinsic_relations"] = [{"contact_id": row["contact_id"], "relation": "about"}]
            source_id = row.get("source_record_id")
            if source_id:
                event["related_event_key"] = "record:" + str(source_id)
            events[event["key"]] = event
        outcomes = self._rows(db, "crm_action_outcomes", owner)
        duplicate_activities = set()
        for row in outcomes:
            record = records.get(row["record_id"])
            if record is None:
                continue
            text = "完成结果：" + (row["result"] or "未补充") + ("\n下一步：" + row["next_step"] if row["next_step"] else "")
            event = self._base_event(db, owner, "outcome", {**row, "title": "完成反馈 · " + record["title"]}, text=text, kind="result", customer_id=record["customer_id"])
            event["related_event_key"] = "record:" + str(record["id"])
            event["_sources"] = [record]
            events[event["key"]] = event
            for activity in self._rows(db, "crm_activities", owner):
                if activity["record_id"] == record["id"] and activity["created_at"] == row["created_at"] and activity["content"] == text:
                    duplicate_activities.add(activity["id"])
        for row in self._rows(db, "crm_activities", owner):
            if row["id"] in duplicate_activities or row["record_id"] not in records:
                continue
            record = records[row["record_id"]]
            event = self._base_event(db, owner, "activity", {**row, "title": "跟进进展 · " + record["title"]}, text=row["content"], kind="result", customer_id=record["customer_id"])
            event["related_event_key"] = "record:" + str(record["id"])
            event["_sources"] = [record]
            events[event["key"]] = event

        material_records = {}
        for record in records.values():
            source = re.fullmatch(r"material:([1-9][0-9]*):[0-9a-f]{64}:note", record.get("source_id") or "")
            if source and record["kind"] == "note" and int(source[1]) in materials:
                identifier = int(source[1])
                material_records.setdefault(identifier, set()).add(record["id"])
                if record["id"] != materials[identifier].get("record_id"):
                    events["record:" + str(record["id"])]["historical_source"] = {
                        "type": "material", "id": identifier, "label": "材料历史整理记录"}

        # Source associations are explicit persistence, never title/time matching.
        membership = {}
        for table, field, source_type in (("crm_visit_sources", "material_id", "material"), ("crm_visit_records", "record_id", "record")):
            for row in self._rows_without_id(db, table, owner):
                child_key, parent_key = source_type + ":" + str(row[field]), "visit:" + str(row["visit_id"])
                child, parent = events.get(child_key), events.get(parent_key)
                if child is None or parent is None:
                    if parent:
                        parent["needs_review"] = True
                        parent["_sources"].append({"invalid_source": dict(row)})
                    continue
                if child.get("historical_source"):
                    membership[child_key] = parent_key
                    continue
                child["source_role"] = row["role"]
                membership[child_key] = parent_key
                parent["_sources"].append({"association": dict(row), "event": child["_raw"], "sources": child["_sources"]})
                parent["source_refs"].extend(child["source_refs"])
                choice = db.execute("SELECT use_status,reason,association_json,revision FROM crm_visit_source_choices WHERE owner=? AND visit_id=? AND material_id=?", (owner, row["visit_id"], row[field])).fetchone() if source_type == "material" and self._exists(db, "crm_visit_source_choices") else None
                if choice:
                    parent["_sources"].append(dict(choice))
                    child["excluded_from_visit"] = choice["use_status"] == "excluded"
                separated = bool(json.loads(contexts[child_key]["data_json"]).get("separate_event")) if child_key in contexts else False
                if not separated and not child.get("excluded_from_visit") and child["text"]:
                    parent["text"] += ("\n\n" if parent["text"] else "") + child["text"]
                if child["customer_id"] not in (None, parent["customer_id"]):
                    parent["needs_review"] = True
        for identifier, material in materials.items():
            material_key = "material:" + str(identifier)
            # Reorganizing captures a new immutable generated note. Older
            # captures remain source history, rather than extra conversations.
            # Group by the durable source identity, never displayed text/date;
            # the context pass below still preserves explicit separate_event.
            record_ids = material_records.get(identifier, set()) | {material.get("record_id")}
            for record_id in sorted(value for value in record_ids if value is not None):
                record_key = "record:" + str(record_id)
                if record_key in events:
                    membership[record_key] = membership.get(material_key, material_key)
                    if not events[record_key].get("historical_source"):
                        events[material_key]["source_refs"].extend(events[record_key]["source_refs"])
                        events[material_key]["_sources"].append(events[record_key]["_raw"])
        # Source-only stable signatures intentionally exclude later record edit
        # timestamps bumped by progress notes; original text/identity still bind.
        links = {(row["entity_type"], row["entity_id"]): row for row in self._rows_without_id(db, "crm_opportunity_links", owner)}
        for key, event in events.items():
            relation = links.get((event["entity_type"], event["entity_id"]))
            if event["entity_type"] in ("activity", "outcome"):
                relation = links.get(("record", event["_raw"]["record_id"]))
            if relation and relation["opportunity_id"] is not None:
                project = self._project(db, owner, relation["opportunity_id"])
                event["opportunity_id"], event["opportunity_name"] = project["id"], project["name"]
                event["opportunity_archived"] = project["archived"]
                source_type = "record" if event["entity_type"] in ("activity", "outcome") else event["entity_type"]
                raw = records[event["_raw"]["record_id"]] if source_type == "record" and event["entity_type"] != "record" else event["_raw"]
                if relation["customer_id"] != event["customer_id"] or self.workspace._link_snapshot(db, owner, source_type, raw) != relation["source_snapshot"]:
                    event["needs_review"] = True
            elif event["opportunity_id"] is not None:
                project = self._project(db, owner, event["opportunity_id"])
                event["opportunity_name"] = project["name"]
                event["opportunity_archived"] = project["archived"]
            operational = {"updated_at", "status", "proposal_id", "classified"} if event["entity_type"] == "record" else {"updated_at"}
            raw = {name: value for name, value in event["_raw"].items() if name not in operational}
            event["_source_snapshot"] = _hash([raw, event["_sources"], relation, membership.get(key)])
            self._apply_context(db, owner, event, contexts.get(key))
            if event["entity_type"] == "record":
                event["revision"] = _hash([event["revision"], event["_raw"].get("status"), event["_raw"].get("proposal_id")])
            if key in membership and not event.get("separate_event"):
                event["merged_into"] = membership[key]
            event["text_length"] = len(event["text"])
            event["text_truncated"] = event["text_length"] > 20000
            event["text"] = _bounded_excerpt(event["text"])
            event["excerpt"] = event["text"][:280]
        # Member source contexts contribute to the one exchange card. They must
        # retain their own stale state, and an explicit separation removes them.
        for key, event in events.items():
            parent = events.get(event.get("merged_into"))
            if parent and not event.get("historical_source"):
                parent["source_refs"].extend(event["source_refs"])
                if not parent["_has_explicit_people"]:
                    parent["contact_relations"] = self._merge_relations(parent["contact_relations"], event["contact_relations"])
                parent["needs_review"] = parent["needs_review"] or event["_source_needs_review"] or (
                    event["_context_stale"] and not parent["_has_explicit_people"])
                if parent["opportunity_id"] is None and not event["needs_review"] and event["opportunity_id"] is not None:
                    parent["opportunity_id"], parent["opportunity_name"] = event["opportunity_id"], event["opportunity_name"]
                    parent["opportunity_archived"] = event["opportunity_archived"]
                parent["revision"] = _hash([parent["revision"], key, event["revision"]])
        self._attach_actions(db, owner, events, records)
        contacts = []
        if self._exists(db, "crm_contacts"):
            contacts = [self._contact(db, owner, row["id"]) for row in self._rows(db, "crm_contacts", owner)]
        for event in events.values():
            event["contacts"] = [person for person in contacts if not person["archived"] and
                                 (person["customer_id"] == event["customer_id"] or
                                  (event["opportunity_id"] is not None and self._participates(db, owner, person, event["opportunity_id"])))]
            event["candidate_contacts"] = [{**person, "evidence": person["name"], "reason": "原文提到同名线索，请核对直接参与或只是关于此人"}
                                           for person in contacts if not person["archived"] and person["name"] and person["name"] in event["text"]
                                           and not any(rel["contact_id"] == person["id"] and rel.get("valid") for rel in event["contact_relations"])]
            event["source_refs"] = self._unique_refs(event["source_refs"])
        return events

    def _rows_without_id(self, db, table, owner):
        if not self._exists(db, table):
            return []
        return [dict(row) for row in db.execute("SELECT * FROM " + table + " WHERE owner=?", (owner,))]

    @staticmethod
    def _unique_refs(values):
        result, seen = [], set()
        for value in values:
            identity = (value["type"], value["id"])
            if identity not in seen:
                result.append(value)
                seen.add(identity)
        return result

    @staticmethod
    def _merge_relations(first, second):
        result = list(first)
        for item in second:
            prior = next((relation for relation in result if relation["contact_id"] == item["contact_id"]), None)
            if prior is None:
                result.append(item)
            elif (not prior.get("valid") and item.get("valid")) or (prior["relation"] == "about" and item["relation"] == "direct" and item.get("valid")):
                result[result.index(prior)] = item
        return result

    def _apply_context(self, db, owner, event, context):
        data = json.loads(context["data_json"]) if context else {}
        stale = bool(context and context["source_snapshot"] != event["_source_snapshot"])
        event["_source_needs_review"] = event["needs_review"]
        event["_context_stale"] = stale
        event["_has_explicit_people"] = "contact_relations" in data
        event["needs_review"] = event["needs_review"] or stale
        for key in ("kind", "occurred_at", "related_event_key", "separate_event"):
            if key in data:
                event[key] = data[key]
        relations = data.get("contact_relations", [])
        event["contact_relations"] = []
        for relation in [*event["_intrinsic_relations"], *relations]:
            try:
                person = self._contact(db, owner, relation["contact_id"])
            except KeyError:
                continue
            intrinsic = relation in event["_intrinsic_relations"]
            event["contact_relations"] = self._merge_relations(event["contact_relations"], [{**person, "relation": relation["relation"], "valid": (intrinsic or not stale) and not event["needs_review"]}])
        event["context_revision"] = context["revision"] if context else 0
        event["revision"] = _hash([event["_source_snapshot"], data, event["context_revision"]])
        event["context_history"] = [{"revision": row["revision"], "context": json.loads(row["data_json"]), "recorded_at": row["created_at"]}
                                    for row in db.execute("SELECT * FROM crm_timeline_context_history WHERE owner=? AND event_key=? ORDER BY id DESC LIMIT 50", (owner, event["key"]))]

    def _attach_actions(self, db, owner, events, records):
        continuations = {row["next_record_id"]: row for row in self._rows(db, "crm_action_outcomes", owner)
                         if row["next_record_id"] is not None}
        for record in records.values():
            if record["kind"] != "action":
                continue
            event = events["record:" + str(record["id"])]
            continuation = continuations.get(record["id"])
            if continuation and not event["context_revision"]:
                parent = events.get("record:" + str(continuation["record_id"]))
                outcome = events.get("outcome:" + str(continuation["id"]))
                if (parent and outcome and parent["_raw"].get("kind") == "action"
                        and record.get("parent_record_id") == parent["entity_id"]
                        and parent["customer_id"] == event["customer_id"]
                        and parent["opportunity_id"] == event["opportunity_id"]
                        and not parent["needs_review"] and not event["needs_review"]):
                    event["contact_relations"] = [{**relation, "relation": "about"}
                                                   for relation in parent["contact_relations"] if relation.get("valid")]
                    event["related_event_key"] = outcome["key"]
                    event["revision"] = _hash([event["revision"], parent["revision"], outcome["_source_snapshot"]])
                elif parent and parent["contact_relations"]:
                    event["needs_review"] = True
            terms = db.execute("SELECT terms_json FROM crm_action_terms WHERE owner=? AND record_id=?", (owner, record["id"])).fetchone()
            terms = json.loads(terms["terms_json"]) if terms else {}
            action = {"id": record["id"], "title": record["title"], "customer_id": record["customer_id"],
                      "customer_name": event["customer_name"], "opportunity_id": event["opportunity_id"],
                      "opportunity_name": event["opportunity_name"], "opportunity_archived": event["opportunity_archived"],
                      "entity_type": "record", "entity_id": record["id"], "executor_kind": terms.get("executor_kind", "unknown"),
                      "status": record["status"], "source_record_id": record.get("parent_record_id"), "event_key": event["key"],
                      "contact_relations": event["contact_relations"], "needs_review": event["needs_review"],
                      "content": _bounded_excerpt(record["content"], 2000),
                      "action_terms": {field: terms[field] for field in TERM_FIELDS if field in terms}, "active_schedule": False}
            proposal = db.execute("SELECT p.id,p.status,p.remind_at,COALESCE(p.task_id,p.target_task_id) AS task_id,t.status AS task_status,t.remind_at AS task_remind_at "
                                  "FROM proposals p LEFT JOIN tasks t ON t.owner=p.owner AND t.id=COALESCE(p.task_id,p.target_task_id) "
                                  "WHERE p.owner=? AND (p.id=? OR p.id IN (SELECT proposal_id FROM crm_record_proposals WHERE owner=? AND record_id=?)) "
                                  "ORDER BY p.id DESC LIMIT 1", (owner, record.get("proposal_id"), owner, record["id"])).fetchone()
            if proposal:
                active = proposal["task_id"] is not None and proposal["task_status"] == "pending"
                action.update(proposal_id=proposal["id"], proposal_status=proposal["status"], task_id=proposal["task_id"],
                              task_status=proposal["task_status"], remind_at=proposal["task_remind_at"] if active else None,
                              proposed_remind_at=proposal["remind_at"] if proposal["status"] == "pending" else None,
                              active_schedule=active)
            event["revision"] = _hash([event["revision"], action])
            event["action"] = action
            parent_keys = []
            if record.get("parent_record_id"):
                parent_keys.append("record:" + str(record["parent_record_id"]))
            if event.get("related_event_key"):
                parent_keys.append(event["related_event_key"])
            for table, field, keytype in (("crm_visit_adoptions", "visit_id", "visit"), ("crm_material_actions", "material_id", "material"), ("crm_sales_discussion_adoptions", "thread_id", "discussion")):
                if self._exists(db, table):
                    for row in db.execute("SELECT " + field + " FROM " + table + " WHERE owner=? AND record_id=?", (owner, record["id"])):
                        parent_keys.append(keytype + ":" + str(row[field]))
            for key in set(parent_keys):
                parent = events.get(key)
                if parent:
                    if not any(item["id"] == action["id"] for item in parent["actions"]):
                        parent["actions"].append(action)
                    merged_parent = events.get(parent.get("merged_into"))
                    if merged_parent and not any(item["id"] == action["id"] for item in merged_parent["actions"]):
                        merged_parent["actions"].append(action)
        # Feedback inherits explicit action-person context, not every person in
        # the same project or the source meeting.
        for event in events.values():
            if event["entity_type"] in ("activity", "outcome"):
                parent = events.get("record:" + str(event["_raw"]["record_id"]))
                if parent:
                    event["contact_relations"] = self._merge_relations(event["contact_relations"], parent["contact_relations"])
                    event["revision"] = _hash([event["revision"], parent["revision"]])

    @staticmethod
    def _public_event(event):
        return {key: value for key, value in event.items() if not key.startswith("_")}

    def get_event(self, owner, event_key):
        owner = _owner(owner)
        _key(event_key)
        with self.crm._lock:
            event = self._events(self.crm._db, owner).get(event_key)
            if event is None:
                raise KeyError("未找到你的历程来源")
            return self._public_event(event)

    def _context_values(self, db, owner, event, data):
        allowed = {"expected_revision", "kind", "occurred_at", "contact_relations", "related_event_key", "separate_event"}
        if not isinstance(data, dict) or set(data) - allowed:
            raise ValueError("历程核对字段无效")
        _text(data.get("expected_revision"), "历程版本", 64, required=True)
        result = {key: value for key, value in data.items() if key != "expected_revision"}
        if "kind" in result and result["kind"] not in KINDS:
            raise ValueError("历程类型无效")
        if event["entity_type"] == "discussion" and result.get("kind", "discussion") != "discussion":
            raise ValueError("AI讨论必须保留讨论标记")
        if event["entity_type"] in ("outcome", "activity") and result.get("kind", "result") != "result":
            raise ValueError("真实反馈必须保留结果标记")
        if result.get("occurred_at") is not None:
            result["occurred_at"] = _timestamp(result["occurred_at"])
            if result["occurred_at"] > _timestamp(self.clock()) + 60:
                raise ValueError("实际发生时间不能是未来时间，请在日程安排未来计划")
        if "separate_event" in result and type(result["separate_event"]) is not bool:
            raise ValueError("独立事件选项无效")
        if result.get("separate_event") and event["entity_type"] not in ("record", "material"):
            raise ValueError("只有原记录或材料可从交流独立出来")
        if "contact_relations" in result:
            values = result["contact_relations"]
            if not isinstance(values, list) or len(values) > 50:
                raise ValueError("每次历程最多核对50位联系人")
            relations, seen = [], set()
            for relation in values:
                if not isinstance(relation, dict) or set(relation) != {"contact_id", "relation"} or relation["relation"] not in ("direct", "about"):
                    raise ValueError("人物关系必须为直接沟通或关于此人")
                contact_id = _identifier(relation["contact_id"])
                if contact_id in seen:
                    raise ValueError("同一事件不能重复核对同一联系人")
                person = self._contact(db, owner, contact_id)
                if person["archived"]:
                    raise ValueError("联系人已归档，不能新增当前活动关联")
                if person["customer_id"] != event["customer_id"] and event["opportunity_id"] is not None and not self._participates(db, owner, person, event["opportunity_id"]):
                    raise ValueError("跨单位联系人未明确参与此项目")
                relations.append({"contact_id": contact_id, "relation": relation["relation"]})
                seen.add(contact_id)
            result["contact_relations"] = sorted(relations, key=lambda item: item["contact_id"])
        if "related_event_key" in result and result["related_event_key"] is not None:
            _key(result["related_event_key"])
            target = self._events(db, owner).get(result["related_event_key"])
            if target is None:
                raise KeyError("未找到你的关联历程")
            if target["customer_id"] != event["customer_id"]:
                raise ValueError("关联历程必须属于同一单位")
            current, seen = target, {event["key"]}
            events = self._events(db, owner)
            while current:
                if current["key"] in seen:
                    raise ValueError("关联历程不能形成循环")
                seen.add(current["key"])
                current = events.get(current.get("related_event_key"))
        return result

    def _write_context(self, db, owner, event, values):
        previous = db.execute("SELECT * FROM crm_timeline_contexts WHERE owner=? AND event_key=?", (owner, event["key"])).fetchone()
        data = json.loads(previous["data_json"]) if previous else {}
        prospective = {**data, **values}
        if not values and not previous:
            return False
        if prospective == data and previous and previous["source_snapshot"] == event["_source_snapshot"]:
            return False
        now, revision = _timestamp(self.clock()), previous["revision"] + 1 if previous else 1
        db.execute("INSERT INTO crm_timeline_contexts VALUES (?,?,?,?,?,?,?) ON CONFLICT(owner,event_key) DO UPDATE SET "
                   "data_json=excluded.data_json,source_snapshot=excluded.source_snapshot,revision=excluded.revision,updated_at=excluded.updated_at",
                   (owner, event["key"], _json(prospective), event["_source_snapshot"], revision, previous["created_at"] if previous else now, now))
        db.execute("INSERT INTO crm_timeline_context_history(owner,event_key,data_json,source_snapshot,revision,created_at) VALUES (?,?,?,?,?,?)",
                   (owner, event["key"], _json(prospective), event["_source_snapshot"], revision, now))
        return True

    def save_context(self, owner, event_key, data):
        owner = _owner(owner)
        _key(event_key)
        with self._transaction() as db:
            event = self._events(db, owner).get(event_key)
            if event is None:
                raise KeyError("未找到你的历程来源")
            values = self._context_values(db, owner, event, data)
            if event["revision"] != data["expected_revision"]:
                raise TimelineConflict("原话、归属或人物核对已变化，请刷新后重新核对；已保存内容没有被覆盖")
            self._write_context(db, owner, event, values)
            return self._public_event(self._events(db, owner)[event_key])

    def create_record(self, owner, scope, data):
        owner = _owner(owner)
        allowed = {"request_id", "text", "original_transcript", "title", "kind", "occurred_at", "contact_relations", "related_event_key"}
        if not isinstance(data, dict) or set(data) - allowed or data.get("kind") not in ("communication", "reflection"):
            raise ValueError("新记录字段或类型无效")
        request_id = _text(data.get("request_id"), "记录请求编号", 200, required=True)
        text = _text(data.get("text"), "原话", 20000, required=True)
        original = _text(data["original_transcript"], "原始语音转写", 20000, required=True) if "original_transcript" in data else text
        if "contact_relations" in data and not isinstance(data["contact_relations"], list):
            raise ValueError("人物关系必须是列表")
        title = _text(data.get("title", text.strip().splitlines()[0][:120]), "记录标题", 120, required=True).strip()
        payload = _hash([scope, data])
        with self._transaction() as db:
            prior = db.execute("SELECT * FROM crm_timeline_requests WHERE owner=? AND request_id=?", (owner, request_id)).fetchone()
            if prior:
                if prior["payload_hash"] != payload:
                    raise TimelineConflict("此记录请求已保存其他内容，请使用新请求编号")
                record = self.crm.get_record(owner, prior["record_id"])
                if record is None:
                    raise TimelineConflict("此前保存的来源已失效，不能重建重复记录")
                return {"record": record, "event": self.get_event(owner, "record:" + str(record["id"])), "created": False}
            resolved, _ = self._scope(db, owner, scope, creating=True)
            customer_id = self._project(db, owner, resolved["opportunity_id"])["customer_id"] if resolved["opportunity_id"] is not None else resolved["customer_id"]
            now = _timestamp(self.clock())
            identifier = db.execute("INSERT INTO crm_records(owner,title,content,original_content,source,status,customer_id,classified,kind,parent_record_id,category,created_at,updated_at) "
                                    "VALUES (?,?,?,?,'web','unfiled',?,1,'note',NULL,?,?,?)",
                                    (owner, title, text, original, customer_id, "visit_review" if data["kind"] == "reflection" else "conversation", now, now)).lastrowid
            record = self.crm.get_record(owner, identifier)
            if resolved["opportunity_id"] is not None:
                source = self.workspace._entity(db, owner, "record", record["id"])
                snapshot = self.workspace._link_snapshot(db, owner, "record", source)
                db.execute("INSERT INTO crm_opportunity_links VALUES (?,'record',?,?,?,?,?,?)",
                           (owner, record["id"], customer_id, resolved["opportunity_id"], 1, snapshot, now))
                db.execute("INSERT INTO crm_opportunity_link_history(owner,entity_type,entity_id,customer_id,opportunity_id,revision,source_snapshot,created_at) VALUES (?,'record',?,?,?,?,?,?)",
                           (owner, record["id"], customer_id, resolved["opportunity_id"], 1, snapshot, now))
            event_key = "record:" + str(record["id"])
            event = self._events(db, owner)[event_key]
            values = {key: value for key, value in data.items() if key in {"kind", "occurred_at", "contact_relations", "related_event_key"}}
            values.setdefault("occurred_at", None)
            relations = values.get("contact_relations", [])
            if resolved["type"] == "contact":
                focus = {"contact_id": resolved["id"], "relation": "direct" if data["kind"] == "communication" else "about"}
                if not any(relation.get("contact_id") == resolved["id"] for relation in relations if isinstance(relation, dict)):
                    relations = [*relations, focus]
            values["contact_relations"] = relations
            checked = self._context_values(db, owner, event, {"expected_revision": event["revision"], **values})
            self._write_context(db, owner, event, checked)
            db.execute("INSERT INTO crm_timeline_requests VALUES (?,?,?,?,?)", (owner, request_id, payload, record["id"], _timestamp(self.clock())))
            return {"record": record, "event": self._public_event(self._events(db, owner)[event_key]), "created": True}

    @staticmethod
    def _matches(event, scope, *, project=True):
        if project and scope["opportunity_id"] is not None and event["opportunity_id"] != scope["opportunity_id"]:
            return False
        if scope["type"] == "customer":
            return event["customer_id"] == scope["customer_id"]
        return not event["needs_review"] and any(relation["contact_id"] == scope["id"] and relation.get("valid") for relation in event["contact_relations"])

    @staticmethod
    def _sort(event):
        return (event["occurred_at"] if event["occurred_at"] is not None else event["recorded_at"], event["recorded_at"], event["key"])

    def _selected(self, events, scope, exclude_discussion_id=None):
        return [event for event in events.values() if self._matches(event, scope) and not event.get("merged_into")
                and not event.get("action") and not (event["entity_type"] == "discussion" and event["entity_id"] == exclude_discussion_id)]

    def _summary(self, events, scope, items):
        actions = [event["action"] for event in events.values() if event.get("action") and event["action"]["status"] != "done"
                   and self._matches(event, scope) and not event["needs_review"]]
        actions.sort(key=lambda item: item["id"], reverse=True)
        latest = next((event for event in sorted(items, key=self._sort, reverse=True)
                       if event["kind"] == "communication" and not event["needs_review"] and not event.get("historical_source")
                       and (scope["type"] == "customer" or any(relation["contact_id"] == scope["id"]
                            and relation["relation"] == "direct" and relation.get("valid") for relation in event["contact_relations"]))), None)
        return {"latest_communication": ({key: latest[key] for key in ("key", "title", "occurred_at", "recorded_at", "excerpt")} if latest else None),
                "open_actions": actions, "waiting": [action for action in actions if action["executor_kind"] in ("customer", "team")],
                "needs_review_count": sum(1 for event in items if event["needs_review"])}

    def view(self, owner, scope, *, kind="", q="", page=1, page_size=20):
        owner, offset = _owner(owner), _pagination(page, page_size)
        q = _text(q, "关键词", 200).strip()
        if kind not in ("", *KINDS):
            raise ValueError("历程类型筛选无效")
        with self.crm._lock:
            db = self.crm._db
            events = self._events(db, owner)
            resolved, projects = self._scope(db, owner, scope, events=events)
            all_items = self._selected(events, resolved)
            items = [event for event in all_items if (not kind or event["kind"] == kind)
                     and (not q or q.casefold() in (event["title"] + "\n" + event["text"]).casefold())]
            items.sort(key=self._sort, reverse=True)
            possible = []
            if resolved["type"] == "contact":
                for event in events.values():
                    if event.get("action") or event.get("merged_into") or self._matches(event, resolved):
                        continue
                    if resolved["opportunity_id"] is not None and event["opportunity_id"] != resolved["opportunity_id"]:
                        continue
                    candidates = [person for person in event["candidate_contacts"] if person["id"] == resolved["id"]]
                    previous = any(person["contact_id"] == resolved["id"] for person in event["contact_relations"])
                    if candidates or previous:
                        possible.append(self._public_event(event))
            possible.sort(key=self._sort, reverse=True)
            contacts = [self._contact(db, owner, row["id"]) for row in self._rows(db, "crm_contacts", owner)]
            contacts = [person for person in contacts if not person["archived"] and
                        (person["customer_id"] == resolved["customer_id"] or
                         (resolved["opportunity_id"] is not None and
                          self._participates(db, owner, person, resolved["opportunity_id"])))]
            return {"scope": resolved, "projects": projects, "contacts": contacts, "items": [self._public_event(event) for event in items[offset:offset+page_size]],
                    "total": len(items), "page": page, "pages": max(1, (len(items)+page_size-1)//page_size), "page_size": page_size,
                    "summary": self._summary(events, resolved, all_items), "possible_related": possible[:50]}

    def history_context(self, owner, scope, *, event_keys=None, limit=15, exclude_discussion_id=None):
        owner = _owner(owner)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("历程上下文数量无效")
        if exclude_discussion_id is not None:
            _identifier(exclude_discussion_id)
        if event_keys is not None:
            if not isinstance(event_keys, list) or len(event_keys) > 6:
                raise ValueError("请至多选择6条不同历程")
            for key in event_keys:
                _key(key)
            if len(set(event_keys)) != len(event_keys):
                raise ValueError("请至多选择6条不同历程")
        with self.crm._lock:
            db = self.crm._db
            events = self._events(db, owner)
            resolved, _ = self._scope(db, owner, scope, events=events)
            all_items = self._selected(events, resolved, exclude_discussion_id)
            current = sorted([event for event in all_items if not event["needs_review"]], key=self._sort, reverse=True)
            if event_keys is not None:
                selected, chosen = [], set()
                for key in event_keys:
                    _key(key)
                    event = events.get(key)
                    if event is None:
                        raise KeyError("未找到你的所选历程")
                    if not self._matches(event, resolved) or event["needs_review"] or (event["entity_type"] == "discussion" and event["entity_id"] == exclude_discussion_id):
                        raise TimelineConflict("所选历程归属或依据已失效，请重新核对后讨论")
                    selected.append(event)
                    chosen.add(event["key"])
                # Chosen evidence is important history, not a frozen view of
                # the customer. Always bring in the newest current context.
                selected.extend(event for event in current if event["key"] not in chosen)
            else:
                selected = current
            summary = self._summary(events, resolved, all_items)
            clean = []
            for event in selected[:max(limit, len(event_keys or []))]:
                item = {key: event[key] for key in ("key", "entity_type", "entity_id", "kind", "title", "text", "text_length", "text_truncated", "occurred_at", "recorded_at", "customer_id", "customer_name", "opportunity_id", "opportunity_name", "opportunity_archived", "contact_relations", "related_event_key", "source_refs", "revision")}
                item["nature"] = {"communication": "沟通原文或用户记录；人物关系来自明确关联，不表示所有原话已验证", "reflection": "用户个人思考，不是客户承诺", "discussion": "历史AI讨论，不是客户事实", "result": "用户记录的推进反馈"}[event["kind"]]
                clean.append(item)
            result = {"scope": resolved, "events": clean, "open_actions": summary["open_actions"], "waiting": summary["waiting"],
                      "latest_communication": summary["latest_communication"],
                      "needs_review_count": summary["needs_review_count"],
                      "scope_event_revisions": [{"key": event["key"], "revision": event["revision"]} for event in current]}
            result["fingerprint"] = _hash(result)
            return result

    def link_action(self, owner, record_id, scope, *, source_event_key=None):
        owner, record_id = _owner(owner), _identifier(record_id)
        with self._transaction() as db:
            resolved, _ = self._scope(db, owner, scope, creating=True)
            record = self.crm._require_record(db, owner, record_id)
            if record["kind"] != "action":
                raise ValueError("只能关联明确采纳的下一步行动")
            if resolved["type"] == "customer" and record["customer_id"] != resolved["customer_id"]:
                raise ValueError("行动属于另一个单位")
            if resolved["opportunity_id"] is not None:
                project = self._project(db, owner, resolved["opportunity_id"])
                if record["customer_id"] != project["customer_id"]:
                    raise ValueError("行动与焦点项目所属单位不同")
            elif record["customer_id"] != resolved["customer_id"]:
                raise ValueError("行动属于另一个单位，请明确参与项目")
            event_key = "record:" + str(record_id)
            event = self._events(db, owner)[event_key]
            if event["needs_review"]:
                raise TimelineConflict("行动的原话或归属已变化，请先明确核对；没有覆盖已有的人物关联")
            if resolved["opportunity_id"] is not None and event["opportunity_id"] != resolved["opportunity_id"]:
                raise ValueError("行动的实际项目与焦点项目不同，不能自动改换项目或人物归属")
            if source_event_key is not None:
                self.history_context(owner, scope, event_keys=[source_event_key])
            values = {}
            if resolved["type"] == "contact":
                people = [{"contact_id": item["contact_id"], "relation": item["relation"]} for item in event["contact_relations"] if item.get("valid")]
                if not any(item["contact_id"] == resolved["id"] for item in people):
                    people.append({"contact_id": resolved["id"], "relation": "about"})
                values["contact_relations"] = people
            if source_event_key is not None:
                values["related_event_key"] = source_event_key
            checked = self._context_values(db, owner, event, {"expected_revision": event["revision"], **values})
            self._write_context(db, owner, event, checked)
            return self._public_event(self._events(db, owner)[event_key])
