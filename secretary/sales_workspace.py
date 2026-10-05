"""Explicit project context and evidence-based sales follow-up, without automation.

Company-level legacy money/stage stay untouched. Project links and completion
outcomes retain original entity IDs; only explicit schedule confirmation creates
tasks. All mutations share the CRM transaction and owner boundary.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import date, datetime

from .crm import MAX_AMOUNT_CENTS, STAGES, _identifier, _owner, _public, _text, _pagination, _search, analysis_fingerprint
from .account_network import same_tree_ids
from .customer_schema import CONTACT_FIELDS
from .store import SHANGHAI, _duration, _timestamp
from .action_origin_scope import init_scope_schema, save_targets


AMOUNT_TYPES = ("unknown", "estimate", "budget", "quote", "contract")
APPROVALS = ("unknown", "unconfirmed", "approved")
STAKEHOLDER_ROLES = ("economic_buyer", "final_approver", "technical_reviewer", "business_owner",
                     "procurement", "security_compliance", "champion", "influencer", "user", "gatekeeper")
PROJECT_UNIT_ROLES = ("demand", "user", "technical", "procurement", "payer", "approver", "sponsor", "other")
_OP_TEXT = {"name": 120, "scope": 4000, "procurement": 4000, "decision_chain": 4000,
            "blockers": 4000, "milestones": 4000, "notes": 10000}
_ENTITY_TABLES = {"record": "crm_records", "visit": "crm_visits", "material": "crm_materials"}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _signature(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


class SalesWorkspace:
    def __init__(self, crm, clock=time.time):
        self.crm, self.clock = crm, clock
        with crm._transaction() as db:
            init_scope_schema(db)
            db.execute("""CREATE TABLE IF NOT EXISTS crm_opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL,
                customer_id INTEGER NOT NULL, name TEXT NOT NULL, stage TEXT NOT NULL,
                amount_cents INTEGER, amount_type TEXT NOT NULL, approval TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT '', procurement TEXT NOT NULL DEFAULT '',
                decision_chain TEXT NOT NULL DEFAULT '', blockers TEXT NOT NULL DEFAULT '',
                milestones TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
                contact_ids_json TEXT NOT NULL DEFAULT '[]', archived INTEGER NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                UNIQUE(owner,id), UNIQUE(owner,customer_id,id),
                FOREIGN KEY(owner,customer_id) REFERENCES crm_customers(owner,id),
                CHECK(revision>0), CHECK(archived IN (0,1)),
                CHECK(amount_cents IS NULL OR amount_cents BETWEEN 0 AND 999999999999),
                CHECK(stage IN ('lead','contact','qualified','proposal','negotiation','won','lost')),
                CHECK(amount_type IN ('unknown','estimate','budget','quote','contract')),
                CHECK(approval IN ('unknown','unconfirmed','approved')))""")
            db.execute("CREATE INDEX IF NOT EXISTS crm_opportunities_customer ON crm_opportunities(owner,customer_id,archived,id)")
            db.execute("""CREATE TABLE IF NOT EXISTS crm_opportunity_links (
                owner TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL,
                customer_id INTEGER, opportunity_id INTEGER, revision INTEGER NOT NULL,
                source_snapshot TEXT NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY(owner,entity_type,entity_id),
                FOREIGN KEY(owner,customer_id,opportunity_id) REFERENCES crm_opportunities(owner,customer_id,id),
                CHECK(entity_type IN ('record','visit','material')))""")
            db.execute("""CREATE TABLE IF NOT EXISTS crm_opportunity_link_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, entity_type TEXT NOT NULL,
                entity_id INTEGER NOT NULL, customer_id INTEGER, opportunity_id INTEGER,
                revision INTEGER NOT NULL, source_snapshot TEXT NOT NULL, created_at REAL NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS crm_action_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, record_id INTEGER NOT NULL,
                request_id TEXT NOT NULL, request_signature TEXT NOT NULL, result TEXT NOT NULL,
                next_step TEXT NOT NULL, next_record_id INTEGER, proposal_id INTEGER,
                source_snapshot TEXT NOT NULL, created_at REAL NOT NULL,
                UNIQUE(owner,request_id), UNIQUE(owner,record_id),
                FOREIGN KEY(owner,record_id) REFERENCES crm_records(owner,id),
                FOREIGN KEY(owner,next_record_id) REFERENCES crm_records(owner,id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS crm_sales_priority_decisions (
                owner TEXT NOT NULL, candidate_key TEXT NOT NULL, signature TEXT NOT NULL,
                decision TEXT NOT NULL, until_at REAL, note TEXT NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY(owner,candidate_key), CHECK(decision IN ('dismiss','defer','reset')))""")
            db.execute("""CREATE TABLE IF NOT EXISTS crm_sales_priority_archives (
                owner TEXT NOT NULL,candidate_key TEXT NOT NULL,item_json TEXT NOT NULL,
                archived INTEGER NOT NULL DEFAULT 1 CHECK(archived IN (0,1)),
                revision INTEGER NOT NULL DEFAULT 1,updated_at REAL NOT NULL,
                PRIMARY KEY(owner,candidate_key))""")
            db.execute("""CREATE TABLE IF NOT EXISTS crm_opportunity_stakeholders (
                owner TEXT NOT NULL, opportunity_id INTEGER NOT NULL, contact_id INTEGER NOT NULL,
                contact_customer_id INTEGER NOT NULL, roles_json TEXT NOT NULL DEFAULT '[]',
                stance TEXT NOT NULL DEFAULT 'unknown', influence TEXT NOT NULL DEFAULT 'unknown',
                engagement TEXT NOT NULL DEFAULT 'unknown', concerns TEXT NOT NULL DEFAULT '',
                next_step TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '',
                basis TEXT NOT NULL DEFAULT 'reported', verified_at REAL,
                archived INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY(owner,opportunity_id,contact_id),
                FOREIGN KEY(owner,opportunity_id) REFERENCES crm_opportunities(owner,id),
                FOREIGN KEY(owner,contact_customer_id,contact_id) REFERENCES crm_contacts(owner,customer_id,id),
                CHECK(archived IN (0,1)), CHECK(revision>0),
                CHECK(stance IN ('unknown','supportive','neutral','opposed')),
                CHECK(influence IN ('unknown','high','medium','low')),
                CHECK(engagement IN ('unknown','not_contacted','indirect','direct')),
                CHECK(basis IN ('reported','observation')))""")
            db.execute("""CREATE TABLE IF NOT EXISTS crm_opportunity_units (
                owner TEXT NOT NULL, opportunity_id INTEGER NOT NULL, participant_customer_id INTEGER NOT NULL,
                roles_json TEXT NOT NULL DEFAULT '[]', evidence TEXT NOT NULL DEFAULT '', basis TEXT NOT NULL DEFAULT 'reported',
                archived INTEGER NOT NULL DEFAULT 0,revision INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,updated_at REAL NOT NULL,
                PRIMARY KEY(owner,opportunity_id,participant_customer_id),
                FOREIGN KEY(owner,opportunity_id) REFERENCES crm_opportunities(owner,id),
                FOREIGN KEY(owner,participant_customer_id) REFERENCES crm_customers(owner,id),
                CHECK(archived IN (0,1)),CHECK(revision>0), CHECK(basis IN ('reported','observation')))""")
            db.execute("""CREATE TABLE IF NOT EXISTS crm_opportunity_people_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,owner TEXT NOT NULL,opportunity_id INTEGER NOT NULL,
                entity_type TEXT NOT NULL,entity_id INTEGER NOT NULL,payload_json TEXT NOT NULL,created_at REAL NOT NULL,
                FOREIGN KEY(owner,opportunity_id) REFERENCES crm_opportunities(owner,id),
                CHECK(entity_type IN ('contact','unit')))""")
            # Idempotent additive migration. Legacy participation has no inferred role.
            if self._exists(db, "crm_contacts"):
                for project in db.execute("SELECT * FROM crm_opportunities").fetchall():
                    for contact_id in json.loads(project["contact_ids_json"]):
                        contact = db.execute("SELECT * FROM crm_contacts WHERE owner=? AND id=?", (project["owner"], contact_id)).fetchone()
                        if contact is not None:
                            db.execute("INSERT OR IGNORE INTO crm_opportunity_stakeholders "
                                       "(owner,opportunity_id,contact_id,contact_customer_id,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                                       (project["owner"], project["id"], contact_id, contact["customer_id"], project["created_at"], project["updated_at"]))
        crm._record_title_relation_guard = self

    @staticmethod
    def _exists(db, table):
        return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())

    def _opportunity(self, row):
        if row is None:
            return None
        result = _public(row)
        result["contact_ids"] = json.loads(result.pop("contact_ids_json"))
        result["version"] = result["revision"]
        if self._exists(self.crm._db, "crm_opportunity_stakeholders"):
            people = self._stakeholders(self.crm._db, row["owner"], row, include_archived=True)
            result["stakeholders"] = [item for item in people if not item["archived"]]
            result["stakeholders_history"] = [item for item in people if item["archived"]]
            result["project_units"] = self._project_units(self.crm._db, row["owner"], row, include_archived=True)
        return result

    def _require_opportunity(self, db, owner, customer_id, identifier):
        row = db.execute("SELECT * FROM crm_opportunities WHERE owner=? AND customer_id=? AND id=?",
                         (owner, customer_id, identifier)).fetchone()
        if row is None:
            raise KeyError("未找到你的商机")
        return row

    def _op_values(self, db, owner, customer_id, data, *, partial=False, project_id=None):
        allowed = set(_OP_TEXT) | {"stage", "amount_cents", "amount_type", "approval", "contact_ids", "archived"}
        if not isinstance(data, dict) or set(data) - (allowed | ({"expected_revision"} if partial else set())):
            raise ValueError("商机字段无效")
        values = ({key: value for key, value in data.items() if key != "expected_revision"} if partial else
                  {**{key: "" for key in _OP_TEXT}, "stage": "lead", "amount_cents": None,
                   "amount_type": "unknown", "approval": "unknown", "contact_ids": [], "archived": False, **data})
        for key, limit in _OP_TEXT.items():
            if key in values:
                values[key] = _text(values[key], key, limit, required=key == "name").strip()
        for key, choices in (("stage", STAGES), ("amount_type", AMOUNT_TYPES), ("approval", APPROVALS)):
            if key in values and values[key] not in choices:
                raise ValueError("商机阶段、金额类型或审批状态无效")
        if "amount_cents" in values and values["amount_cents"] is not None:
            if type(values["amount_cents"]) is not int or not 0 <= values["amount_cents"] <= MAX_AMOUNT_CENTS:
                raise ValueError("商机金额必须为有效的整数分或留空")
        if "archived" in values:
            if type(values["archived"]) is not bool:
                raise ValueError("商机归档标记必须为布尔值")
            values["archived"] = int(values["archived"])
        if "contact_ids" in values:
            contacts = values.pop("contact_ids")
            if not isinstance(contacts, list) or len(contacts) > 50:
                raise ValueError("商机联系人最多50位")
            if len(set(_identifier(value) for value in contacts)) != len(contacts):
                raise ValueError("商机联系人不能重复")
            for identifier in contacts:
                if not self._exists(db, "crm_contacts") or not db.execute(
                    "SELECT 1 FROM crm_contacts WHERE owner=? AND id=? AND archived=0",
                    (owner, identifier)).fetchone():
                    raise ValueError("商机联系人必须属于你的单位且未归档")
                contact = db.execute("SELECT customer_id FROM crm_contacts WHERE owner=? AND id=?", (owner, identifier)).fetchone()
                if contact["customer_id"] not in self._allowed_units(db, owner, customer_id, project_id):
                    raise ValueError("商机联系人需属于同一单位树或已明确参与的项目单位")
            values["contact_ids_json"] = _json(contacts)
        return values

    def opportunities(self, owner, customer_id, include_archived=False):
        owner, customer_id = _owner(owner), _identifier(customer_id)
        if type(include_archived) is not bool:
            raise ValueError("商机归档筛选无效")
        with self.crm._lock:
            db = self.crm._db
            self.crm._require_customer(db, owner, customer_id)
            rows = db.execute("SELECT * FROM crm_opportunities WHERE owner=? AND customer_id=?" +
                              ("" if include_archived else " AND archived=0") + " ORDER BY id",
                              (owner, customer_id)).fetchall()
            return {"items": [self._opportunity(row) for row in rows], "total": len(rows),
                    "include_archived": include_archived}

    def dashboard_summary(self, owner):
        """In-progress project money by provenance, separate from legacy company money."""
        owner = _owner(owner)
        with self.crm._lock:
            rows = self.crm._db.execute(
                "SELECT amount_cents,amount_type FROM crm_opportunities "
                "WHERE owner=? AND archived=0 AND stage NOT IN ('won','lost')", (owner,)).fetchall()
            total, unknown, by_type = 0, 0, {}
            for row in rows:
                amount = row['amount_cents']
                if amount is None:
                    unknown += 1
                    continue
                total += amount
                bucket = by_type.setdefault(row['amount_type'], {'amount_cents': 0, 'known_count': 0})
                bucket['amount_cents'] += amount
                bucket['known_count'] += 1
            return {'project_pipeline_cents': total, 'active_projects': len(rows),
                    'project_unknown_amounts': unknown, 'project_amount_types': by_type}

    def create_opportunity(self, owner, customer_id, data):
        owner, customer_id, now = _owner(owner), _identifier(customer_id), _timestamp(self.clock())
        with self.crm._transaction() as db:
            self.crm._require_customer(db, owner, customer_id)
            values = self._op_values(db, owner, customer_id, data)
            keys = list(values)
            identifier = db.execute("INSERT INTO crm_opportunities(owner,customer_id," + ",".join(keys) +
                ",created_at,updated_at) VALUES (" + ",".join("?" for _ in range(len(keys)+4)) + ")",
                [owner, customer_id, *[values[key] for key in keys], now, now]).lastrowid
            self._sync_contacts(db, owner, identifier, json.loads(values["contact_ids_json"]), now)
            return self._opportunity(self._require_opportunity(db, owner, customer_id, identifier))

    def _allowed_units(self, db, owner, customer_id, project_id=None):
        allowed = same_tree_ids(db, owner, customer_id)
        if project_id is not None:
            allowed.update(row["participant_customer_id"] for row in db.execute(
                "SELECT participant_customer_id,roles_json,evidence FROM crm_opportunity_units WHERE owner=? AND opportunity_id=? AND archived=0",
                (owner, project_id)) if row["participant_customer_id"] in allowed or (json.loads(row["roles_json"]) and row["evidence"].strip()))
        return allowed

    @staticmethod
    def _roles(value, choices, label):
        if not isinstance(value, list) or len(value)>len(choices) or any(not isinstance(item, str) or item not in choices for item in value):
            raise ValueError(label+"无效")
        if len(set(value)) != len(value):
            raise ValueError(label+"不能重复")
        return _json([item for item in choices if item in value])

    @staticmethod
    def _expected_project(row, data):
        expected = data.get("expected_revision")
        if type(expected) is not int or expected != row["revision"]:
            raise ValueError("商机已更新，请刷新后核对再保存")

    def _people_history(self, db, owner, project_id, entity_type, entity_id, row, now):
        db.execute("INSERT INTO crm_opportunity_people_history(owner,opportunity_id,entity_type,entity_id,payload_json,created_at) VALUES (?,?,?,?,?,?)",
                   (owner, project_id, entity_type, entity_id, _json(_public(row)), now))

    @staticmethod
    def _bump_project(db, owner, project_id, now):
        db.execute("UPDATE crm_opportunities SET revision=revision+1,updated_at=? WHERE owner=? AND id=?", (now, owner, project_id))

    def _sync_contacts(self, db, owner, project_id, contact_ids, now):
        """Legacy membership edits preserve project roles and archive removals."""
        if not self._exists(db, "crm_contacts"):
            return
        selected = set(contact_ids)
        rows = db.execute("SELECT * FROM crm_opportunity_stakeholders WHERE owner=? AND opportunity_id=?", (owner, project_id)).fetchall()
        known = {row["contact_id"]: row for row in rows}
        for identifier in selected:
            if identifier not in known:
                contact = db.execute("SELECT customer_id FROM crm_contacts WHERE owner=? AND id=?", (owner, identifier)).fetchone()
                db.execute("INSERT INTO crm_opportunity_stakeholders(owner,opportunity_id,contact_id,contact_customer_id,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                           (owner, project_id, identifier, contact["customer_id"], now, now))
            elif known[identifier]["archived"]:
                db.execute("UPDATE crm_opportunity_stakeholders SET archived=0,revision=revision+1,updated_at=? WHERE owner=? AND opportunity_id=? AND contact_id=?",
                           (now, owner, project_id, identifier))
            else:
                continue
            row = db.execute("SELECT * FROM crm_opportunity_stakeholders WHERE owner=? AND opportunity_id=? AND contact_id=?", (owner, project_id, identifier)).fetchone()
            self._people_history(db, owner, project_id, "contact", identifier, row, now)
        for row in rows:
            if row["contact_id"] not in selected and not row["archived"]:
                db.execute("UPDATE crm_opportunity_stakeholders SET archived=1,revision=revision+1,updated_at=? WHERE owner=? AND opportunity_id=? AND contact_id=?",
                           (now, owner, project_id, row["contact_id"]))
                saved = db.execute("SELECT * FROM crm_opportunity_stakeholders WHERE owner=? AND opportunity_id=? AND contact_id=?", (owner, project_id, row["contact_id"])).fetchone()
                self._people_history(db, owner, project_id, "contact", row["contact_id"], saved, now)

    def _stakeholders(self, db, owner, project, include_archived=False):
        if not self._exists(db, "crm_contacts"):
            return []
        allowed = self._allowed_units(db, owner, project["customer_id"], project["id"])
        department = "p.department" if "department" in {row["name"] for row in db.execute("PRAGMA table_info(crm_contacts)")} else "''"
        rows = db.execute("SELECT s.*,p.name,p.role,p.phone,"+department+" AS contact_department,p.archived AS contact_archived,c.name AS unit_name "
                          "FROM crm_opportunity_stakeholders s JOIN crm_contacts p "
                          "ON p.owner=s.owner AND p.customer_id=s.contact_customer_id AND p.id=s.contact_id "
                          "JOIN crm_customers c ON c.owner=s.owner AND c.id=s.contact_customer_id "
                          "WHERE s.owner=? AND s.opportunity_id=?"+("" if include_archived else " AND s.archived=0")+" ORDER BY s.contact_id",
                          (owner, project["id"])).fetchall()
        items = []
        for row in rows:
            item = _public(row)
            item["roles"] = json.loads(item.pop("roles_json"))
            item["contact_name"], item["contact_role"] = item.pop("name"), item.pop("role")
            item["contact_archived"] = bool(item["contact_archived"])
            item["membership_valid"] = not item["contact_archived"] and item["contact_customer_id"] in allowed
            item["project_customer_id"] = project["customer_id"]
            item["roles_unconfirmed"] = not bool(item["roles"])
            item["personal_facts"] = (self._personal_facts(db, owner, item["contact_customer_id"], item["contact_id"])
                                      if item["membership_valid"] and not item["archived"] else [])
            items.append(item)
        return items

    def _personal_facts(self, db, owner, customer_id, contact_id):
        """Current person facts, separately from project authority and contact data.

        Latest-field selection prevents an older value reviving after a clear.
        Corrected/hidden/reassigned source quotes cannot inform fresh AI advice;
        their confirmed historical facts remain in the original profile history.
        """
        if not self._exists(db, "crm_customer_facts"):
            return []
        keys = [key for key in CONTACT_FIELDS if key != "authority"]
        marks = ",".join("?" for _ in keys)
        rows = db.execute("SELECT * FROM crm_customer_facts WHERE owner=? AND customer_id=? AND contact_id=? AND id IN "
                          "(SELECT MAX(id) FROM crm_customer_facts WHERE owner=? AND customer_id=? AND contact_id=? AND key IN ("+marks+") GROUP BY key) "
                          "ORDER BY id DESC LIMIT 10", [owner, customer_id, contact_id, owner, customer_id, contact_id, *keys]).fetchall()
        result = []
        for row in rows:
            if not row["value"].strip():
                continue
            source_record_id = row["source_record_id"]
            source = {"type": "manual", "recorded_at": row["updated_at"], "occurred_at": None}
            if source_record_id is not None:
                record = db.execute("SELECT * FROM crm_records WHERE owner=? AND id=?", (owner, source_record_id)).fetchone()
                if (record is None or record["hidden"] or record["customer_id"] not in (None, customer_id)
                        or not row["evidence"] or row["evidence"] not in record["content"]
                        or row["evidence"] not in record["original_content"]):
                    continue
                source = {"type": "record", "record_id": record["id"], "recorded_at": record["created_at"], "occurred_at": None}
                if self._exists(db, "crm_visit_records") and self._exists(db, "crm_visits"):
                    visit = db.execute("SELECT v.customer_id,v.occurred_at FROM crm_visit_records r JOIN crm_visits v "
                                       "ON v.owner=r.owner AND v.id=r.visit_id WHERE r.owner=? AND r.record_id=?", (owner, source_record_id)).fetchone()
                    if visit is not None and visit["customer_id"] == customer_id:
                        source["occurred_at"] = visit["occurred_at"]
            result.append({"id": row["id"], "key": row["key"], "label": CONTACT_FIELDS[row["key"]]["label"],
                           "value": row["value"], "basis": row["basis"], "evidence": row["evidence"],
                           "source_quote": row["evidence"], "source_record_id": source_record_id,
                           "updated_at": row["updated_at"], "recorded_at": row["updated_at"], "source": source})
        return result

    def stakeholders(self, owner, customer_id, opportunity_id, include_archived=False):
        owner, customer_id, project_id = _owner(owner), _identifier(customer_id), _identifier(opportunity_id)
        if type(include_archived) is not bool:
            raise ValueError("项目成员归档筛选无效")
        with self.crm._lock:
            project = self._require_opportunity(self.crm._db, owner, customer_id, project_id)
            items = self._stakeholders(self.crm._db, owner, project, include_archived)
            return {"items": items, "total": len(items), "project_revision": project["revision"], "include_archived": include_archived,
                    "supports_verify_now": True}

    def upsert_stakeholder(self, owner, customer_id, opportunity_id, data):
        owner, customer_id, project_id, now = _owner(owner), _identifier(customer_id), _identifier(opportunity_id), _timestamp(self.clock())
        fields = {"contact_id", "roles", "stance", "influence", "engagement", "concerns", "next_step", "evidence", "basis", "verified_at", "expected_revision", "verify_now"}
        if not isinstance(data, dict) or set(data)-fields:
            raise ValueError("项目决策关系字段无效")
        if 'verify_now' in data:
            if type(data['verify_now']) is not bool:
                raise ValueError('本次核对确认必须为布尔值')
            if 'verified_at' in data:
                raise ValueError('本次核对和历史核实时间不能同时填写')
        contact_id = _identifier(data.get("contact_id"))
        with self.crm._transaction() as db:
            project = self._require_opportunity(db, owner, customer_id, project_id)
            self._expected_project(project, data)
            if project["archived"]:
                raise ValueError("已归档项目不能修改决策关系，请先恢复项目")
            contact = db.execute("SELECT * FROM crm_contacts WHERE owner=? AND id=?", (owner, contact_id)).fetchone() if self._exists(db, "crm_contacts") else None
            if contact is None:
                raise KeyError("未找到你的联系人")
            old = db.execute("SELECT * FROM crm_opportunity_stakeholders WHERE owner=? AND opportunity_id=? AND contact_id=?", (owner, project_id, contact_id)).fetchone()
            if contact["archived"] or contact["customer_id"] not in self._allowed_units(db, owner, customer_id, project_id):
                raise ValueError("联系人已归档或不属于同单位树/已明确参与的项目单位；历史关系仍保留")
            defaults = {"roles_json": "[]", "stance": "unknown", "influence": "unknown", "engagement": "unknown",
                        "concerns": "", "next_step": "", "evidence": "", "basis": "reported", "verified_at": None, "archived": 0}
            values = {key: old[key] for key in defaults} if old else dict(defaults)
            for field in fields-{"contact_id", "expected_revision", "roles", "verify_now"}:
                if field in data:
                    values[field] = data[field]
            if "roles" in data:
                values["roles_json"] = self._roles(data["roles"], STAKEHOLDER_ROLES, "项目角色")
            for field, choices in (("stance", ("unknown", "supportive", "neutral", "opposed")),
                                   ("influence", ("unknown", "high", "medium", "low")),
                                   ("engagement", ("unknown", "not_contacted", "indirect", "direct")),
                                   ("basis", ("reported", "observation"))):
                if values[field] not in choices:
                    raise ValueError("项目立场、影响力、接触状态或依据类型无效")
            for field in ("concerns", "next_step", "evidence"):
                values[field] = _text(values[field], field, 4000).strip()
            if data.get('verify_now'):
                if not (json.loads(values['roles_json']) and values['basis'] == 'reported'
                        and values['evidence'] and values['engagement'] in ('direct', 'indirect')):
                    raise ValueError('本次核对需要明确项目角色、交流依据和直接或间接接触情况')
                values['verified_at'] = now
            if values["verified_at"] is not None:
                values["verified_at"] = _timestamp(values["verified_at"])
                if values["verified_at"] > now:
                    raise ValueError("核实时间不能在未来")
            values["archived"] = 0
            changed = old is None or any(old[key] != value for key, value in values.items())
            if changed:
                if (old is None or old["archived"]) and db.execute("SELECT COUNT(*) FROM crm_opportunity_stakeholders WHERE owner=? AND opportunity_id=? AND archived=0", (owner, project_id)).fetchone()[0] >= 50:
                    raise ValueError("项目有效联系人最多50位")
                keys = list(values)
                db.execute("INSERT INTO crm_opportunity_stakeholders(owner,opportunity_id,contact_id,contact_customer_id,"+",".join(keys)+",created_at,updated_at) VALUES ("+",".join("?" for _ in range(len(keys)+6))+") "
                           "ON CONFLICT(owner,opportunity_id,contact_id) DO UPDATE SET "+",".join(key+"=excluded."+key for key in keys)+",revision=crm_opportunity_stakeholders.revision+1,updated_at=excluded.updated_at",
                           [owner, project_id, contact_id, contact["customer_id"], *[values[key] for key in keys], now, now])
                saved = db.execute("SELECT * FROM crm_opportunity_stakeholders WHERE owner=? AND opportunity_id=? AND contact_id=?", (owner, project_id, contact_id)).fetchone()
                self._people_history(db, owner, project_id, "contact", contact_id, saved, now)
                self._refresh_contact_ids(db, owner, project_id)
                self._bump_project(db, owner, project_id, now)
            return self._opportunity(self._require_opportunity(db, owner, customer_id, project_id))

    @staticmethod
    def _refresh_contact_ids(db, owner, project_id):
        ids = [row[0] for row in db.execute("SELECT contact_id FROM crm_opportunity_stakeholders WHERE owner=? AND opportunity_id=? AND archived=0 ORDER BY contact_id", (owner, project_id))]
        db.execute("UPDATE crm_opportunities SET contact_ids_json=? WHERE owner=? AND id=?", (_json(ids), owner, project_id))

    def archive_stakeholder(self, owner, customer_id, opportunity_id, contact_id, data):
        return self._archive_relation(owner, customer_id, opportunity_id, "contact", contact_id, data)

    def archive_project_unit(self, owner, customer_id, opportunity_id, participant_customer_id, data):
        return self._archive_relation(owner, customer_id, opportunity_id, "unit", participant_customer_id, data)

    def _archive_relation(self, owner, customer_id, opportunity_id, kind, entity_id, data):
        owner, customer_id, project_id, entity_id, now = _owner(owner), _identifier(customer_id), _identifier(opportunity_id), _identifier(entity_id), _timestamp(self.clock())
        if not isinstance(data, dict) or set(data)-{"expected_revision", "archived"} or type(data.get("archived")) is not bool:
            raise ValueError("项目关系归档字段无效")
        table, field = ("crm_opportunity_stakeholders", "contact_id") if kind == "contact" else ("crm_opportunity_units", "participant_customer_id")
        with self.crm._transaction() as db:
            project = self._require_opportunity(db, owner, customer_id, project_id)
            self._expected_project(project, data)
            if project["archived"]:
                raise ValueError("已归档项目不能修改关系，请先恢复项目")
            row = db.execute("SELECT * FROM "+table+" WHERE owner=? AND opportunity_id=? AND "+field+"=?", (owner, project_id, entity_id)).fetchone()
            if row is None:
                raise KeyError("未找到你的项目关系")
            archived = int(data["archived"])
            if not archived and kind == "contact":
                contact = db.execute("SELECT * FROM crm_contacts WHERE owner=? AND id=?", (owner, entity_id)).fetchone()
                if contact is None or contact["archived"] or contact["customer_id"] not in self._allowed_units(db, owner, customer_id, project_id):
                    raise ValueError("无法恢复已归档或不在项目单位范围内的联系人")
                count = db.execute("SELECT COUNT(*) FROM crm_opportunity_stakeholders WHERE owner=? AND opportunity_id=? AND archived=0", (owner, project_id)).fetchone()[0]
                if row["archived"] and count>=50:
                    raise ValueError("项目有效联系人最多50位")
            if not archived and kind == "unit" and row["archived"]:
                if entity_id not in same_tree_ids(db, owner, customer_id) and (not row["evidence"].strip() or not json.loads(row["roles_json"])):
                    raise ValueError("不同单位树参与项目需明确参与角色和依据，请编辑关系后恢复")
                if db.execute("SELECT COUNT(*) FROM crm_opportunity_units WHERE owner=? AND opportunity_id=? AND archived=0", (owner, project_id)).fetchone()[0]>=50:
                    raise ValueError("项目有效参与单位最多50个")
            if row["archived"] != archived:
                db.execute("UPDATE "+table+" SET archived=?,revision=revision+1,updated_at=? WHERE owner=? AND opportunity_id=? AND "+field+"=?", (archived, now, owner, project_id, entity_id))
                saved = db.execute("SELECT * FROM "+table+" WHERE owner=? AND opportunity_id=? AND "+field+"=?", (owner, project_id, entity_id)).fetchone()
                self._people_history(db, owner, project_id, kind, entity_id, saved, now)
                if kind == "contact":
                    self._refresh_contact_ids(db, owner, project_id)
                self._bump_project(db, owner, project_id, now)
            return self._opportunity(self._require_opportunity(db, owner, customer_id, project_id))

    def _project_units(self, db, owner, project, include_archived=False):
        result = []
        allowed = self._allowed_units(db, owner, project["customer_id"], project["id"])
        rows = db.execute("SELECT u.*,c.name AS unit_name FROM crm_opportunity_units u JOIN crm_customers c "
                          "ON c.owner=u.owner AND c.id=u.participant_customer_id WHERE u.owner=? AND u.opportunity_id=?"+
                          ("" if include_archived else " AND u.archived=0")+" ORDER BY u.participant_customer_id", (owner, project["id"])).fetchall()
        for row in rows:
            item = _public(row)
            item["roles"] = json.loads(item.pop("roles_json"))
            item["is_primary"] = item["participant_customer_id"] == project["customer_id"]
            item["explicit"] = True
            item["membership_valid"] = not item["archived"] and item["participant_customer_id"] in allowed
            result.append(item)
        if not any(item["is_primary"] and not item["archived"] for item in result):
            self.crm._require_customer(db, owner, project["customer_id"])
            customer = db.execute("SELECT name FROM crm_customers WHERE owner=? AND id=?", (owner, project["customer_id"])).fetchone()
            result.insert(0, {"participant_customer_id": project["customer_id"], "unit_name": customer["name"],
                              "roles": [], "is_primary": True, "explicit": False, "archived": False,
                              "evidence": "", "basis": "reported", "revision": 0, "membership_valid": True})
        return result

    def project_units(self, owner, customer_id, opportunity_id, include_archived=False):
        owner, customer_id, project_id = _owner(owner), _identifier(customer_id), _identifier(opportunity_id)
        if type(include_archived) is not bool:
            raise ValueError("项目单位归档筛选无效")
        with self.crm._lock:
            project = self._require_opportunity(self.crm._db, owner, customer_id, project_id)
            items = self._project_units(self.crm._db, owner, project, include_archived)
            return {"items": items, "total": len(items), "project_revision": project["revision"], "include_archived": include_archived}

    def upsert_project_unit(self, owner, customer_id, opportunity_id, data):
        owner, customer_id, project_id, now = _owner(owner), _identifier(customer_id), _identifier(opportunity_id), _timestamp(self.clock())
        if not isinstance(data, dict) or set(data)-{"participant_customer_id", "roles", "evidence", "basis", "expected_revision"}:
            raise ValueError("项目参与单位字段无效")
        unit_id = _identifier(data.get("participant_customer_id"))
        with self.crm._transaction() as db:
            project = self._require_opportunity(db, owner, customer_id, project_id)
            self._expected_project(project, data)
            self.crm._require_customer(db, owner, unit_id)
            if project["archived"]:
                raise ValueError("已归档项目不能修改参与单位，请先恢复项目")
            old = db.execute("SELECT * FROM crm_opportunity_units WHERE owner=? AND opportunity_id=? AND participant_customer_id=?", (owner, project_id, unit_id)).fetchone()
            roles = self._roles(data.get("roles", json.loads(old["roles_json"]) if old else []), PROJECT_UNIT_ROLES, "单位参与角色")
            evidence = _text(data.get("evidence", old["evidence"] if old else ""), "参与依据", 4000).strip()
            basis = data.get("basis", old["basis"] if old else "reported")
            if basis not in ("reported", "observation"):
                raise ValueError("参与依据类型无效")
            if unit_id not in same_tree_ids(db, owner, customer_id) and (not evidence or not json.loads(roles)):
                raise ValueError("不同单位树参与项目需明确参与角色和依据")
            values = {"roles_json": roles, "evidence": evidence, "basis": basis, "archived": 0}
            if old is None or any(old[key] != value for key, value in values.items()):
                if (old is None or old["archived"]) and db.execute("SELECT COUNT(*) FROM crm_opportunity_units WHERE owner=? AND opportunity_id=? AND archived=0", (owner, project_id)).fetchone()[0]>=50:
                    raise ValueError("项目有效参与单位最多50个")
                db.execute("INSERT INTO crm_opportunity_units(owner,opportunity_id,participant_customer_id,roles_json,evidence,basis,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?) "
                           "ON CONFLICT(owner,opportunity_id,participant_customer_id) DO UPDATE SET roles_json=excluded.roles_json,evidence=excluded.evidence,basis=excluded.basis,archived=0,revision=crm_opportunity_units.revision+1,updated_at=excluded.updated_at",
                           (owner, project_id, unit_id, roles, evidence, basis, now, now))
                saved = db.execute("SELECT * FROM crm_opportunity_units WHERE owner=? AND opportunity_id=? AND participant_customer_id=?", (owner, project_id, unit_id)).fetchone()
                self._people_history(db, owner, project_id, "unit", unit_id, saved, now)
                self._bump_project(db, owner, project_id, now)
            return self._opportunity(self._require_opportunity(db, owner, customer_id, project_id))

    def contact_projects(self, owner, contact_id, include_archived=False):
        owner, contact_id = _owner(owner), _identifier(contact_id)
        if type(include_archived) is not bool:
            raise ValueError("联系人项目归档筛选无效")
        with self.crm._lock:
            db = self.crm._db
            if not self._exists(db, "crm_contacts") or not db.execute("SELECT 1 FROM crm_contacts WHERE owner=? AND id=?", (owner, contact_id)).fetchone():
                raise KeyError("未找到你的联系人")
            projects = db.execute("SELECT o.* FROM crm_opportunities o JOIN crm_opportunity_stakeholders s ON s.owner=o.owner AND s.opportunity_id=o.id "
                                  "WHERE o.owner=? AND s.contact_id=?"+("" if include_archived else " AND s.archived=0 AND o.archived=0")+" ORDER BY o.id", (owner, contact_id)).fetchall()
            items = []
            for project in projects:
                relation = next(item for item in self._stakeholders(db, owner, project, True) if item["contact_id"] == contact_id)
                customer = db.execute("SELECT name FROM crm_customers WHERE owner=? AND id=?", (owner, project["customer_id"])).fetchone()
                items.append({"opportunity_id": project["id"], "opportunity_name": project["name"], "customer_id": project["customer_id"],
                              "customer_name": customer["name"], "project_archived": bool(project["archived"]), "project_revision": project["revision"], **relation})
            return {"items": items, "total": len(items), "include_archived": include_archived}

    def relation_history(self, owner, customer_id, opportunity_id):
        owner, customer_id, project_id = _owner(owner), _identifier(customer_id), _identifier(opportunity_id)
        with self.crm._lock:
            self._require_opportunity(self.crm._db, owner, customer_id, project_id)
            items = []
            for row in self.crm._db.execute("SELECT * FROM crm_opportunity_people_history WHERE owner=? AND opportunity_id=? ORDER BY id DESC LIMIT 100", (owner, project_id)):
                item = _public(row)
                payload = json.loads(item.pop("payload_json"))
                payload["roles"] = json.loads(payload.pop("roles_json"))
                item["relationship"] = payload
                items.append(item)
            return {"items": items, "total": len(items), "limit": 100}

    def _project_people(self, project):
        return [{**item, "id": item["contact_id"], "name": item["contact_name"], "role": item["contact_role"]}
                for item in project.get("stakeholders", []) if item["membership_valid"] and not item["archived"]]

    def update_opportunity(self, owner, customer_id, id, data):
        owner, customer_id, identifier = _owner(owner), _identifier(customer_id), _identifier(id)
        now = _timestamp(self.clock())
        with self.crm._transaction() as db:
            row = self._require_opportunity(db, owner, customer_id, identifier)
            values = self._op_values(db, owner, customer_id, data, partial=True, project_id=identifier)
            expected = data.get("expected_revision")
            if type(expected) is not int or expected != row["revision"]:
                raise ValueError("商机已更新，请刷新后核对再保存")
            values = {key: value for key, value in values.items() if row[key] != value}
            if row["archived"] and "contact_ids_json" in values and data.get("archived") is not False:
                raise ValueError("已归档项目不能修改关系，请先恢复项目")
            if values:
                keys = list(values)
                db.execute("UPDATE crm_opportunities SET " + ",".join(key+"=?" for key in keys) +
                           ",revision=revision+1,updated_at=? WHERE owner=? AND customer_id=? AND id=? AND revision=?",
                           [*[values[key] for key in keys], now, owner, customer_id, identifier, expected])
                if "contact_ids_json" in values:
                    self._sync_contacts(db, owner, identifier, json.loads(values["contact_ids_json"]), now)
            return self._opportunity(self._require_opportunity(db, owner, customer_id, identifier))

    def _entity(self, db, owner, entity_type, entity_id):
        if entity_type not in _ENTITY_TABLES:
            raise ValueError("商机关联对象类型无效")
        table = _ENTITY_TABLES[entity_type]
        row = db.execute("SELECT * FROM " + table + " WHERE owner=? AND id=?" +
                         (" AND hidden=0" if entity_type == "record" else ""), (owner, entity_id)).fetchone() if self._exists(db, table) else None
        if row is None:
            raise KeyError("未找到你的来源对象")
        return row

    @staticmethod
    def _entity_snapshot(row):
        fields = ("id", "customer_id", "title", "content", "original_content", "revision", "current_version_id", "occurred_at")
        return _signature({key: row[key] for key in fields if key in row.keys()})

    def prepare_record_title_rebase(self, db, owner, old):
        """Read current explicit relations before an ordinary note title edit.

        The CRM caller has checked CAS and the actual normalized title-only
        diff. A live timeline is required to rule out composed/historical notes;
        neither this hook nor a GET constructs a sidecar or guesses relations.
        """
        timeline = getattr(self, 'timeline', None)
        if timeline is None or timeline.crm is not self.crm or old['kind'] != 'note' or not old['content'].strip() or old['hidden']:
            return None
        key = 'record:' + str(old['id'])
        try:
            event = timeline._events(db, owner).get(key)
        except (KeyError, ValueError, TypeError):
            return None
        if event is None or event.get('merged_into') or event.get('historical_source'):
            return None
        link = db.execute("SELECT * FROM crm_opportunity_links WHERE owner=? AND entity_type='record' AND entity_id=?",
                          (owner, old['id'])).fetchone()
        current_link = None
        if link is not None and link['opportunity_id'] is not None:
            project = db.execute('SELECT * FROM crm_opportunities WHERE owner=? AND id=?',
                                 (owner, link['opportunity_id'])).fetchone()
            if (project is not None and not project['archived']
                    and project['customer_id'] == old['customer_id'] == link['customer_id']
                    and link['source_snapshot'] == self._link_snapshot(db, owner, 'record', old)):
                current_link = dict(link)
        context = None
        row = db.execute('SELECT * FROM crm_timeline_contexts WHERE owner=? AND event_key=?', (owner, key)).fetchone()
        if row is not None and not event['needs_review'] and not event.get('opportunity_archived'):
            try:
                data = json.loads(row['data_json'])
                timeline._context_values(db, owner, event, {'expected_revision': event['revision'], **data})
            except (KeyError, ValueError, TypeError):
                pass
            else:
                context = {name: row[name] for name in ('revision', 'data_json', 'source_snapshot')}
        return {'link': current_link, 'context': context, 'event_key': key} if current_link or context else None

    def apply_record_title_rebase(self, db, owner, old, current, plan, now):
        """Carry only captured current choices atomically; AI stamps stay old."""
        changed = {key for key in old.keys() if key != 'updated_at' and old[key] != current[key]}
        if changed != {'title'} or current['kind'] != 'note' or not current['content'].strip():
            raise ValueError('标题承接只允许普通记录的标题改名')
        timeline = self.timeline
        link = plan['link']
        if link is not None:
            snapshot = self._link_snapshot(db, owner, 'record', current)
            count = db.execute("UPDATE crm_opportunity_links SET source_snapshot=?,revision=revision+1,updated_at=? "
                               "WHERE owner=? AND entity_type='record' AND entity_id=? AND revision=? AND source_snapshot=?",
                               (snapshot, now, owner, old['id'], link['revision'], link['source_snapshot'])).rowcount
            if count != 1:
                raise ValueError('标题编辑期间原项目关联已变化')
            db.execute('INSERT INTO crm_opportunity_link_history(owner,entity_type,entity_id,customer_id,opportunity_id,revision,source_snapshot,created_at) '
                       "VALUES (?,'record',?,?,?,?,?,?)",
                       (owner, old['id'], link['customer_id'], link['opportunity_id'], link['revision']+1, snapshot, now))
        if plan['context'] is not None:
            previous = db.execute('SELECT * FROM crm_timeline_contexts WHERE owner=? AND event_key=?', (owner, plan['event_key'])).fetchone()
            if previous is None or any(previous[name] != value for name, value in plan['context'].items()):
                raise ValueError('标题编辑期间原历程核对已变化')
            # Link version contributes to the source stamp, so reconstruct after
            # the project write. Empty values retain exact kinds/times/people.
            event = timeline._events(db, owner)[plan['event_key']]
            timeline._write_context(db, owner, event, {})

    def _link_snapshot(self, db, owner, entity_type, row):
        if entity_type != "visit":
            return self._entity_snapshot(row)
        state = {"visit": self._entity_snapshot(row), "materials": [], "records": [], "choices": []}
        if self._exists(db, "crm_visit_sources"):
            for relation in db.execute("SELECT material_id,role FROM crm_visit_sources WHERE owner=? AND visit_id=? ORDER BY material_id", (owner, row["id"])):
                try:
                    source = self._entity(db, owner, "material", relation["material_id"])
                    snapshot = self._entity_snapshot(source)
                except KeyError:
                    snapshot = "missing"
                state["materials"].append([dict(relation), snapshot])
        if self._exists(db, "crm_visit_records"):
            for relation in db.execute("SELECT record_id FROM crm_visit_records WHERE owner=? AND visit_id=? ORDER BY record_id", (owner, row["id"])):
                try:
                    source = self._entity(db, owner, "record", relation["record_id"])
                    snapshot = self._entity_snapshot(source)
                except KeyError:
                    snapshot = "missing"
                state["records"].append([relation["record_id"], snapshot])
        if self._exists(db, "crm_visit_source_choices"):
            state["choices"] = [dict(choice) for choice in db.execute("SELECT material_id,use_status,revision FROM crm_visit_source_choices WHERE owner=? AND visit_id=? ORDER BY material_id", (owner, row["id"]))]
        return _signature(state)

    def link(self, owner, entity_type="record", entity_id=None, opportunity_id=None):
        owner, entity_id, now = _owner(owner), _identifier(entity_id), _timestamp(self.clock())
        if opportunity_id is not None:
            _identifier(opportunity_id)
        with self.crm._transaction() as db:
            entity = self._entity(db, owner, entity_type, entity_id)
            if opportunity_id is not None:
                if entity["customer_id"] is None:
                    raise ValueError("请先核对来源的客户归属，再选择商机")
                opportunity = self._require_opportunity(db, owner, entity["customer_id"], opportunity_id)
                if opportunity["archived"]:
                    raise ValueError("已归档商机不能新增来源关联")
            snapshot = self._link_snapshot(db, owner, entity_type, entity)
            previous = db.execute("SELECT * FROM crm_opportunity_links WHERE owner=? AND entity_type=? AND entity_id=?",
                                  (owner, entity_type, entity_id)).fetchone()
            if previous and previous["opportunity_id"] == opportunity_id and previous["source_snapshot"] == snapshot:
                return _public(previous)
            revision = previous["revision"]+1 if previous else 1
            db.execute("INSERT INTO crm_opportunity_links VALUES (?,?,?,?,?,?,?,?) "
                       "ON CONFLICT(owner,entity_type,entity_id) DO UPDATE SET customer_id=excluded.customer_id,"
                       "opportunity_id=excluded.opportunity_id,revision=excluded.revision,source_snapshot=excluded.source_snapshot,updated_at=excluded.updated_at",
                       (owner, entity_type, entity_id, entity["customer_id"], opportunity_id, revision, snapshot, now))
            db.execute("INSERT INTO crm_opportunity_link_history(owner,entity_type,entity_id,customer_id,opportunity_id,revision,source_snapshot,created_at) "
                       "VALUES (?,?,?,?,?,?,?,?)", (owner, entity_type, entity_id, entity["customer_id"], opportunity_id, revision, snapshot, now))
            return _public(db.execute("SELECT * FROM crm_opportunity_links WHERE owner=? AND entity_type=? AND entity_id=?",
                                     (owner, entity_type, entity_id)).fetchone())

    def inherit_record_link(self, owner, parent_record_id, child_record_id, *, entity_type="record"):
        """Carry a current, explicitly confirmed source project to an adopted action.

        Adoption callers check their own source/action revisions before calling.
        This additional check never guesses from a customer, overwrites a child's
        explicit decision, or refreshes a source link invalidated by new content.
        """
        owner, parent_id, child_id = _owner(owner), _identifier(parent_record_id), _identifier(child_record_id)
        now = _timestamp(self.clock())
        with self.crm._transaction() as db:
            parent = self._entity(db, owner, entity_type, parent_id)
            child = self._entity(db, owner, "record", child_id)
            link = db.execute("SELECT * FROM crm_opportunity_links WHERE owner=? AND entity_type=? AND entity_id=?",
                              (owner, entity_type, parent_id)).fetchone()
            if link is None or link["opportunity_id"] is None:
                return {"linked": False}
            project = db.execute("SELECT * FROM crm_opportunities WHERE owner=? AND customer_id=? AND id=?",
                                 (owner, parent["customer_id"], link["opportunity_id"])).fetchone()
            if (link["customer_id"] != parent["customer_id"] or project is None or project["archived"]
                    or self._link_snapshot(db, owner, entity_type, parent) != link["source_snapshot"]):
                return {"linked": False, "warning": "来源的项目关联已变化或失效；待办已保留，请重新核对其项目归属。"}
            if child["customer_id"] != parent["customer_id"]:
                return {"linked": False, "warning": "待办与来源的客户归属不同；没有继承项目，请重新核对。"}
            related = child["kind"] == "action"
            if entity_type == "record":
                related = related and child["parent_record_id"] == parent_id
            elif entity_type == "material":
                related = related and "record_id" in parent.keys() and child["parent_record_id"] == parent["record_id"]
            else:
                related = related and self._exists(db, "crm_visit_adoptions") and bool(db.execute(
                    "SELECT 1 FROM crm_visit_adoptions WHERE owner=? AND visit_id=? AND record_id=?",
                    (owner, parent_id, child_id)).fetchone())
            if not related:
                return {"linked": False, "warning": "这条待办不是该来源的已采纳行动；没有自动继承项目，请手动核对。"}
            snapshot = self._entity_snapshot(child)
            previous = db.execute("SELECT * FROM crm_opportunity_links WHERE owner=? AND entity_type='record' AND entity_id=?",
                                  (owner, child_id)).fetchone()
            if previous is not None:
                if (previous["opportunity_id"] == project["id"] and previous["customer_id"] == child["customer_id"]
                        and previous["source_snapshot"] == snapshot):
                    return {"linked": True, "opportunity_id": project["id"]}
                # Even an explicit "unassigned" or stale child link remains a
                # human decision; inheritance must not silently replace it.
                return {"linked": False, "opportunity_id": previous["opportunity_id"],
                        "warning": "已保留待办原有的项目归属处理；没有覆盖，请手动核对。"}
            db.execute("INSERT INTO crm_opportunity_links VALUES (?,'record',?,?,?,?,?,?)",
                       (owner, child_id, child["customer_id"], project["id"], 1, snapshot, now))
            db.execute("INSERT INTO crm_opportunity_link_history(owner,entity_type,entity_id,customer_id,opportunity_id,revision,source_snapshot,created_at) "
                       "VALUES (?,'record',?,?,?,?,?,?)", (owner, child_id, child["customer_id"], project["id"], 1, snapshot, now))
            return {"linked": True, "opportunity_id": project["id"]}

    def _records(self, owner, customer_id):
        rows = self.crm._db.execute("SELECT id FROM crm_records WHERE owner=? AND customer_id=? AND hidden=0 ORDER BY updated_at DESC,id DESC",
                                    (owner, customer_id)).fetchall()
        records = [self.crm.get_record(owner, row["id"]) for row in rows]
        return [{**record, "snapshot": analysis_fingerprint(record)} for record in records]

    @staticmethod
    def _portion(items, limit=200):
        return {"items": items[:limit], "total": len(items), "limit": limit, "truncated": len(items) > limit}

    def _context_sources(self, owner, customer_id, table, service=None):
        db = self.crm._db
        if not self._exists(db, table):
            return []
        where = "owner=? AND customer_id=?" + (" AND duplicate_of IS NULL" if table == "crm_materials" else "")
        rows = db.execute("SELECT * FROM "+table+" WHERE "+where+" ORDER BY updated_at DESC,id DESC", (owner, customer_id)).fetchall()
        if service is not None and hasattr(service, "_public"):
            return [service._public(db, row) if table == "crm_visits" else service._public(row) for row in rows]
        fields = ("id", "title", "provider", "category", "status", "revision", "record_id", "customer_id", "occurred_at", "created_at", "updated_at", "error")
        return [{key: row[key] for key in fields if key in row.keys()} for row in rows]

    def workbench(self, owner, customer_id, visits=None, materials=None, review_queue=None):
        owner, customer_id = _owner(owner), _identifier(customer_id)
        with self.crm._lock:
            db = self.crm._db
            self.crm._require_customer(db, owner, customer_id)
            customer = self.crm.get_customer(owner, customer_id)
            profile = self.crm.profile(owner, customer_id) if hasattr(self.crm, "profile") else {"customer": customer}
            records = self._records(owner, customer_id)
            open_actions = [row for row in records if row["kind"] == "action" and row["status"] != "done"]
            waiting = [row for row in open_actions if row.get("action_terms", {}).get("executor_kind") in ("customer", "team")]
            visit_items = self._context_sources(owner, customer_id, "crm_visits", visits)
            material_items = self._context_sources(owner, customer_id, "crm_materials", materials)
            links = [_public(row) for row in db.execute("SELECT * FROM crm_opportunity_links WHERE owner=? AND customer_id=? ORDER BY entity_type,entity_id", (owner, customer_id))]
            for link in links:
                try:
                    entity = self._entity(db, owner, link["entity_type"], link["entity_id"])
                    link["stale"] = entity["customer_id"] != customer_id or self._link_snapshot(db, owner, link["entity_type"], entity) != link["source_snapshot"]
                except KeyError:
                    link["stale"] = True
            corrections = [_public(row) for row in db.execute("SELECT t.* FROM crm_record_transcripts t JOIN crm_records r "
                "ON r.owner=t.owner AND r.id=t.record_id WHERE t.owner=? AND r.customer_id=? AND r.hidden=0 ORDER BY t.updated_at DESC",
                (owner, customer_id))] if self._exists(db, "crm_record_transcripts") else []
            reviews = ([item for item in review_queue.all_items(owner) if item.get("customer_id") == customer_id and item.get("decision_state") != "dismissed"]
                       if review_queue is not None else [])
            outcomes = [_public(row) for row in db.execute("SELECT o.* FROM crm_action_outcomes o JOIN crm_records r "
                "ON r.owner=o.owner AND r.id=o.record_id WHERE o.owner=? AND r.customer_id=? ORDER BY o.id DESC", (owner, customer_id))]
            link_history = [_public(row) for row in db.execute("SELECT * FROM crm_opportunity_link_history WHERE owner=? AND customer_id=? ORDER BY id DESC", (owner, customer_id))]
            # Schema reads preserve full source identity; optional providers are
            # accepted as adapters but no extraction/model job runs during reads.
            return {"customer": customer, "profile": profile, "opportunities": self.opportunities(owner, customer_id),
                    "records": self._portion(records), "visits": self._portion(visit_items), "materials": self._portion(material_items),
                    "open_actions": open_actions, "waiting": waiting, "review_queue": self._portion(reviews),
                    "legacy_unassigned_amount": {"amount_cents": customer["amount_cents"], "amount_type": "unknown", "approval": "unknown", "source": "customer", "customer_id": customer_id},
                    "legacy_unassigned_stage": customer["stage"], "source_corrections": corrections,
                    "opportunity_links": links, "opportunity_link_history": link_history, "outcomes": outcomes}

    def customer_candidates(self, owner, text):
        owner, text = _owner(owner), _text(text, "客户匹配原文", 20000, required=True)
        folded, items = text.casefold(), []
        with self.crm._lock:
            db = self.crm._db
            for row in db.execute("SELECT * FROM crm_customers WHERE owner=? ORDER BY id", (owner,)):
                reasons = []
                names = [("company", row["name"]), *[("alias", name) for name in json.loads(row["aliases_json"])]]
                for kind, value in names:
                    if value.strip() and value.casefold() in folded:
                        reasons.append({"kind": kind, "value": value})
                if self._exists(db, "crm_contacts"):
                    for person in db.execute("SELECT id,name FROM crm_contacts WHERE owner=? AND customer_id=? AND archived=0", (owner, row["id"])):
                        if person["name"].strip() and person["name"].casefold() in folded:
                            reasons.append({"kind": "contact", "value": person["name"], "contact_id": person["id"]})
                for opportunity in db.execute("SELECT id,name FROM crm_opportunities WHERE owner=? AND customer_id=? AND archived=0", (owner, row["id"])):
                    if opportunity["name"].casefold() in folded:
                        reasons.append({"kind": "opportunity", "value": opportunity["name"], "opportunity_id": opportunity["id"]})
                if reasons:
                    items.append({"customer_id": row["id"], "name": row["name"], "reasons": reasons})
        return {"status": "multiple" if len(items)>1 else "single" if items else "unassigned", "items": items,
                "requires_confirmation": True, "selected_customer_id": None,
                "message": "发现多个客户依据，请核对归属。" if len(items)>1 else "候选归属需确认。" if items else "未找到明确客户依据，可先保留原文。"}

    def _outcome_result(self, db, owner, row):
        outcome = _public(row)
        return {"outcome": outcome, "record": self.crm.get_record(owner, row["record_id"]),
                "next_record": self.crm.get_record(owner, row["next_record_id"]) if row["next_record_id"] else None,
                "proposal": self.crm.get_proposal(owner, row["proposal_id"]) if row["proposal_id"] else None,
                "message": "已记录完成结果；下一步仍待你核对。" if row["next_record_id"] else "已记录完成结果。"}

    def _completion_snapshot(self, db, owner, record):
        """Bind completion to every owner-scoped row it may change or inherit."""
        proposals = [dict(row) for row in db.execute(
            "SELECT * FROM proposals WHERE owner=? AND (id=? OR id IN "
            "(SELECT proposal_id FROM crm_record_proposals WHERE owner=? AND record_id=?)) ORDER BY id",
            (owner, record['proposal_id'], owner, record['id']))]
        tasks = [dict(row) for row in db.execute(
            "SELECT t.* FROM tasks t WHERE t.owner=? AND EXISTS (SELECT 1 FROM proposals p "
            "WHERE p.owner=t.owner AND (p.task_id=t.id OR p.target_task_id=t.id) AND "
            "(p.id=? OR p.id IN (SELECT proposal_id FROM crm_record_proposals WHERE owner=? AND record_id=?))) ORDER BY t.id",
            (owner, record['proposal_id'], owner, record['id']))]
        terms = db.execute('SELECT * FROM crm_action_terms WHERE owner=? AND record_id=?',
                           (owner, record['id'])).fetchone()
        link = db.execute("SELECT * FROM crm_opportunity_links WHERE owner=? AND entity_type='record' AND entity_id=?",
                          (owner, record['id'])).fetchone()
        project = db.execute('SELECT * FROM crm_opportunities WHERE owner=? AND id=?',
                             (owner, link['opportunity_id'])).fetchone() if link and link['opportunity_id'] else None
        return _signature(['completion-v1', dict(record), dict(terms) if terms else None,
                           proposals, tasks, dict(link) if link else None, dict(project) if project else None])

    def completion_snapshot(self, owner, record_id):
        owner, record_id = _owner(owner), _identifier(record_id)
        with self.crm._lock:
            record = self.crm._require_record(self.crm._db, owner, record_id)
            if record['hidden']:
                raise KeyError('未找到你的记录')
            return self._completion_snapshot(self.crm._db, owner, record)

    def completion_effects(self, owner, record_id):
        """Read exactly the pending rows complete_record changes, with its token."""
        owner, record_id = _owner(owner), _identifier(record_id)
        with self.crm._lock:
            db = self.crm._db
            record = self.crm._require_record(db, owner, record_id)
            parameters = (owner, record['proposal_id'], owner, record_id)
            # The snapshot deliberately covers both task references and all
            # statuses. Completion itself uses COALESCE and only pending tasks.
            tasks = [dict(row) for row in db.execute(
                "SELECT DISTINCT t.id,t.title,t.remind_at,t.duration_minutes,t.status,t.revision "
                "FROM proposals p JOIN tasks t ON t.owner=p.owner "
                "AND t.id=COALESCE(p.task_id,p.target_task_id) WHERE p.owner=? AND t.status='pending' "
                "AND (p.id=? OR p.id IN (SELECT proposal_id FROM crm_record_proposals "
                "WHERE owner=? AND record_id=?)) ORDER BY t.id", parameters)]
            proposals = [dict(row) for row in db.execute(
                "SELECT id,title,remind_at,change_kind,status FROM proposals WHERE owner=? AND status='pending' "
                "AND (id=? OR id IN (SELECT proposal_id FROM crm_record_proposals "
                "WHERE owner=? AND record_id=?) OR target_task_id IN ("
                "SELECT t.id FROM proposals p JOIN tasks t ON t.owner=p.owner "
                "AND t.id=COALESCE(p.task_id,p.target_task_id) WHERE p.owner=? AND t.status='pending' "
                "AND (p.id=? OR p.id IN (SELECT proposal_id FROM crm_record_proposals "
                "WHERE owner=? AND record_id=?)))) ORDER BY id", (*parameters, *parameters))]
            # Completing each task also rejects every same-owner pending change
            # targeting it. The existing token still binds record-linked rows;
            # it does not detect edits to these additional target-only proposals.
            return {'scope': 'record_completion', 'record_id': record_id,
                    'snapshot': self._completion_snapshot(db, owner, record),
                    'pending_tasks': tasks, 'pending_proposals': proposals,
                    'pending_task_count': len(tasks), 'pending_proposal_count': len(proposals)}

    def complete_record(self, owner, record_id, data):
        owner, record_id, now = _owner(owner), _identifier(record_id), _timestamp(self.clock())
        allowed = {"request_id", "result", "next_step", "next_title", "remind_at", "duration_minutes", "expected_snapshot",
                   "expected_completion_snapshot", "next_executor_kind"}
        if not isinstance(data, dict) or set(data)-allowed:
            raise ValueError("完成反馈字段无效")
        request_id = _text(data.get("request_id"), "完成请求编号", 200, required=True)
        result = _text(data.get("result", ""), "完成结果", 4000).strip()
        next_step = _text(data.get("next_step", ""), "下一步", 4000).strip()
        title = _text(data.get("next_title", next_step[:120]), "下一步标题", 120, required=bool(next_step)).strip()
        if title and not next_step:
            raise ValueError("请填写下一步内容后再指定标题")
        next_executor = data.get('next_executor_kind', 'unknown')
        if not isinstance(next_executor, str) or next_executor not in ('unknown', 'self', 'customer', 'team'):
            raise ValueError('请核对下一步由我、客户、内部团队还是待核实的责任人推进')
        if not next_step and next_executor != 'unknown':
            raise ValueError('请先填写实际下一步，再确认下一步责任人')
        when = _timestamp(data["remind_at"]) if data.get("remind_at") is not None else None
        raw_duration = data.get("duration_minutes", 30)
        if when is not None and (type(raw_duration) is not int or not 5 <= raw_duration <= 720):
            raise ValueError("请补充新日程预计用时，需要5—720分钟的整数")
        # Explicit unknown is valid for an unscheduled follow-up. Omitted
        # fields keep the existing completion callers' legacy contract.
        duration = None if raw_duration is None and when is None else _duration(raw_duration)
        signed = [record_id, result, next_step, title, when, duration]
        if next_executor != 'unknown':
            signed.extend(['next-executor-v1', next_executor])
        fingerprint = _signature(signed)
        with self.crm._transaction() as db:
            record = self.crm._require_record(db, owner, record_id)
            if record["hidden"]:
                raise KeyError("未找到你的记录")
            previous = db.execute("SELECT * FROM crm_action_outcomes WHERE owner=? AND request_id=?", (owner, request_id)).fetchone()
            if previous:
                if previous["request_signature"] != fingerprint:
                    raise ValueError("该完成请求已处理，请使用新请求编号")
                return self._outcome_result(db, owner, previous)
            previous = db.execute("SELECT * FROM crm_action_outcomes WHERE owner=? AND record_id=?", (owner, record_id)).fetchone()
            if previous:
                if previous["request_signature"] != fingerprint:
                    raise ValueError("本行动已有完成反馈，请从保留的下一步继续跟进")
                return self._outcome_result(db, owner, previous)
            if record['kind'] != 'action':
                raise ValueError('当前记录不是待办行动，请重新核对；仅完成日程不会结束交流记录')
            if 'expected_completion_snapshot' in data:
                seen = data['expected_completion_snapshot']
                if (not isinstance(seen, str) or re.fullmatch(r'[0-9a-f]{64}', seen) is None
                        or seen != self._completion_snapshot(db, owner, record)):
                    raise ValueError('行动的责任、安排或归属已有变化，请重新打开后核对完成结果')
            if when is not None and (not next_step or not now+5 < when < now+10*366*86400):
                raise ValueError("请提供下一步和有效的未来时间；没有明确时间可只留待办")
            snapshot = analysis_fingerprint(record)
            if data.get("expected_snapshot") is not None and data["expected_snapshot"] != snapshot:
                raise ValueError("行动已更新，请刷新后核对完成结果")
            task_ids = [row[0] for row in db.execute("SELECT DISTINCT COALESCE(p.task_id,p.target_task_id) FROM proposals p "
                "JOIN tasks t ON t.owner=p.owner AND t.id=COALESCE(p.task_id,p.target_task_id) WHERE p.owner=? AND t.status='pending' "
                "AND (p.id=? OR p.id IN (SELECT proposal_id FROM crm_record_proposals WHERE owner=? AND record_id=?))",
                (owner, record["proposal_id"], owner, record_id))]
            for task_id in task_ids:
                self.crm._execute(db, owner, {"action": "complete", "task_id": task_id}, now)
            db.execute("UPDATE proposals SET status='rejected',updated_at=? WHERE owner=? AND status='pending' "
                       "AND (id=? OR id IN (SELECT proposal_id FROM crm_record_proposals WHERE owner=? AND record_id=?))",
                       (now, owner, record["proposal_id"], owner, record_id))
            db.execute("UPDATE crm_records SET status='done',updated_at=? WHERE owner=? AND id=?", (now, owner, record_id))
            if result or next_step:
                content = "完成结果：" + (result or "未补充") + ("\n下一步：" + next_step if next_step else "")
                db.execute("INSERT INTO crm_activities(owner,record_id,content,created_at) VALUES (?,?,?,?)", (owner, record_id, content, now))
            next_id = proposal_id = None
            if next_step:
                next_id = db.execute("INSERT INTO crm_records(owner,source_id,title,content,original_content,source,status,customer_id,classified,kind,parent_record_id,category,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,'web','following',?,1,'action',?,?,?,?)",
                    (owner, "outcome:"+request_id, title, next_step, next_step, record["customer_id"], record_id, record["category"], now, now)).lastrowid
                if next_executor != 'unknown':
                    db.execute('INSERT INTO crm_action_terms VALUES (?,?,?,?)',
                        (owner, next_id, _json({'executor_kind': next_executor,
                            'executor_evidence': '用户在完成反馈中明确核对下一步责任'}), now))
                parent_link = db.execute("SELECT * FROM crm_opportunity_links WHERE owner=? AND entity_type='record' AND entity_id=?", (owner, record_id)).fetchone()
                if (parent_link and parent_link["opportunity_id"] is not None and parent_link["customer_id"] == record["customer_id"]
                        and parent_link["source_snapshot"] == self._entity_snapshot(record)):
                    op = self._require_opportunity(db, owner, record["customer_id"], parent_link["opportunity_id"])
                    if not op["archived"]:
                        child = self.crm._require_record(db, owner, next_id)
                        db.execute("INSERT INTO crm_opportunity_links VALUES (?,?,?,?,?,?,?,?)",
                                   (owner, "record", next_id, record["customer_id"], op["id"], 1, self._entity_snapshot(child), now))
                        db.execute("INSERT INTO crm_opportunity_link_history(owner,entity_type,entity_id,customer_id,opportunity_id,revision,source_snapshot,created_at) "
                                   "VALUES (?,?,?,?,?,?,?,?)", (owner, "record", next_id, record["customer_id"], op["id"], 1, self._entity_snapshot(child), now))
                if when is not None:
                    reply = self.crm._execute(db, owner, {"action": "propose", "title": title, "remind_at": when, "duration_minutes": duration}, now)
                    match = re.match(r"^已整理，待你确认：P([0-9]+)", reply)
                    if not match:
                        raise ValueError("下一步日程提案未建立，请核对时间")
                    proposal_id = int(match[1])
                    db.execute("UPDATE crm_records SET proposal_id=? WHERE owner=? AND id=?", (proposal_id, owner, next_id))
                    self.crm._remember_proposal(db, owner, next_id, proposal_id, now)
            identifier = db.execute("INSERT INTO crm_action_outcomes(owner,record_id,request_id,request_signature,result,next_step,next_record_id,proposal_id,source_snapshot,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)", (owner, record_id, request_id, fingerprint, result, next_step, next_id, proposal_id, snapshot, now)).lastrowid
            save_targets(self.crm, db, owner, 'outcome', identifier,
                         {'record_id': record_id, 'next_record_id': next_id}, now,
                         workspace=self, timeline=getattr(self, 'timeline', None))
            return self._outcome_result(db, owner, db.execute("SELECT * FROM crm_action_outcomes WHERE owner=? AND id=?", (owner, identifier)).fetchone())

    def _priority_items(self, owner):
        db, now, items = self.crm._db, _timestamp(self.clock()), []
        today = datetime.fromtimestamp(now, SHANGHAI).date()
        for customer_row in db.execute("SELECT * FROM crm_customers WHERE owner=? ORDER BY id", (owner,)).fetchall():
            customer = _public(customer_row)
            customer_id = customer["id"]
            contacts = ([_public(row) for row in db.execute("SELECT * FROM crm_contacts WHERE owner=? AND customer_id=? AND archived=0 ORDER BY id", (owner, customer_id))]
                        if self._exists(db, "crm_contacts") else [])
            records = self._records(owner, customer_id)
            open_actions = [row for row in records if row["kind"] == "action" and row["status"] != "done"]
            opportunity_map = {opportunity["id"]: opportunity for opportunity in self.opportunities(owner, customer_id, include_archived=True)["items"]}
            record_map = {record["id"]: record for record in records}
            links = {row["entity_id"]: row["opportunity_id"] for row in db.execute("SELECT * FROM crm_opportunity_links WHERE owner=? AND customer_id=? AND entity_type='record'", (owner, customer_id))
                     if row["entity_id"] in record_map and row["source_snapshot"] == self._entity_snapshot(record_map[row["entity_id"]])}
            for record in open_actions:
                terms = record.get("action_terms", {})
                detail = self.crm.record_detail(owner, record["id"])
                task = detail.get("task") if detail else None
                score, why = 50, ["仍有未完成行动："+record["title"]]
                if task and task["status"] == "pending" and task.get("remind_at") is not None and task["remind_at"] <= now:
                    score = 100; why.append("已确认安排到时仍未完成")
                if terms.get("deadline_at") is not None and terms["deadline_at"] <= now:
                    score = max(score, 95); why.append("原话截止已到，需核对结果")
                if terms.get("check_at") is not None and terms["check_at"] <= now:
                    score = max(score, 90); why.append("约定检查时间已到")
                for field, due_score, label in (("deadline_date", 95, "截止"), ("check_date", 90, "检查")):
                    try:
                        due_date = date.fromisoformat(terms.get(field) or "")
                    except (ValueError, TypeError):
                        continue
                    if due_date <= today:
                        score = max(score, due_score); why.append(f"约定{label}日期已到，具体时刻尚未明确")
                executor = terms.get("executor_kind", "unknown")
                if executor in ("customer", "team"):
                    score = max(score, 65); why.append("正在等待客户反馈" if executor == "customer" else "正在等待内部同事反馈")
                evidence = [{"source_type": "record", "record_id": record["id"], "title": record["title"], "snapshot": record["snapshot"], "terms": terms}]
                if task:
                    evidence.append({"source_type": "task", "task_id": task["id"], "revision": task.get("revision"), "status": task["status"], "remind_at": task.get("remind_at")})
                opportunity_id = links.get(record["id"])
                related_opportunity = opportunity_map.get(opportunity_id)
                related_contacts = (self._project_people(related_opportunity)
                                    if related_opportunity else contacts)
                if related_opportunity:
                    evidence.append({"source_type": "project_people", "opportunity_id": related_opportunity["id"],
                                     "revision": related_opportunity["revision"], "stakeholders": related_opportunity["stakeholders"],
                                     "project_units": related_opportunity["project_units"]})
                items.append(self._priority(customer, related_contacts, f"priority:record:{record['id']}", score, why, evidence,
                                            record_id=record["id"], opportunity_id=opportunity_id, talk=record["title"]))
            if hasattr(self.crm, "profile"):
                profile = self.crm.profile(owner, customer_id)
                for fact in profile.get("fields", []):
                    if fact["key"] == "blockers" and fact["value"]:
                        items.append(self._priority(customer, contacts, f"priority:customer-blocker:{customer_id}", 70,
                            ["客户有已记录阻力："+fact["value"] + ("（个人观察，待核实）" if fact.get("basis") == "observation" else "")],
                            [{"source_type": "customer_fact", "fact_id": fact["id"], "record_id": fact.get("source_record_id"), "value": fact["value"], "basis": fact.get("basis"), "evidence": fact.get("evidence", "")}],
                            talk="核实客户当前阻力及下一步需要的支持"))
            for opportunity in self.opportunities(owner, customer_id)["items"]:
                if opportunity["stage"] not in ("won", "lost") and opportunity["blockers"]:
                    related = self._project_people(opportunity)
                    items.append(self._priority(customer, related, f"priority:opportunity:{opportunity['id']}", 70,
                        ["项目有已记录阻力："+opportunity["blockers"]],
                        [{"source_type": "opportunity", "opportunity_id": opportunity["id"], "title": opportunity["name"], "revision": opportunity["revision"], "blockers": opportunity["blockers"], "scope": opportunity["scope"], "procurement": opportunity["procurement"], "stakeholders": opportunity["stakeholders"], "project_units": opportunity["project_units"]}],
                        opportunity_id=opportunity["id"], talk="核实“"+opportunity["name"]+"”的阻力与所需支持"))
            cycle = customer.get("contact_cycle_days")
            actual = db.execute("SELECT MAX(created_at) FROM crm_activities a WHERE a.owner=? AND a.record_id IN "
                                "(SELECT id FROM crm_records WHERE owner=? AND customer_id=? AND hidden=0)", (owner, owner, customer_id)).fetchone()[0]
            source_times = [row["created_at"] for row in records if row["kind"] == "note"] + ([actual] if actual is not None else [])
            last = max(source_times) if source_times else None
            if cycle and last is not None and now-last >= cycle*86400 and open_actions:
                items.append(self._priority(customer, contacts, f"priority:cycle:{customer_id}", 75,
                    [f"距最近交流记录已超过设定的{cycle}天联系间隔，实际联系情况需核实", "仍有明确下一步待推进"],
                    [{"source_type": "customer", "customer_id": customer_id, "cycle_days": cycle, "last_recorded_contact_at": last},
                     {"source_type": "record", "record_id": open_actions[0]["id"], "snapshot": open_actions[0]["snapshot"]}],
                    record_id=open_actions[0]["id"], talk="核对上次交流后的下一步："+open_actions[0]["title"]))
        return sorted(items, key=lambda item: (-item["score"], item["customer_id"], item["key"]))

    @staticmethod
    def _priority(customer, contacts, key, score, why, evidence, *, record_id=None, opportunity_id=None, talk=""):
        item = {"key": key, "customer_id": customer["id"], "customer_name": customer["name"],
                "record_id": record_id, "opportunity_id": opportunity_id, "score": score,
                "why": why, "whom": [{"contact_id": person["id"], "name": person["name"], "role": person.get("role", ""),
                    **{field: person[field] for field in ("roles", "stance", "influence", "engagement", "concerns", "next_step", "unit_name", "contact_customer_id") if field in person}} for person in contacts],
                "material": [{"source_type": entry["source_type"], "title": entry.get("title", "原记录"),
                              **{key: entry[key] for key in ("record_id", "opportunity_id") if entry.get(key)}}
                             for entry in evidence if entry.get("record_id") or entry.get("opportunity_id")],
                "talk": talk, "progress": "明确当前结果、下一责任人及需要核对的下一步；未明确时间先留待办。",
                "evidence": evidence, "uncertainties": ["记录不代表客户已确认实际进展；沟通前核对最新情况。"]}
        if not contacts:
            item["uncertainties"].append("未明确联系人，请先核对应联系谁。")
        item["signature"] = _signature([key, evidence, item["whom"], opportunity_id, talk])
        return item

    def priorities(self, owner):
        owner, now = _owner(owner), _timestamp(self.clock())
        with self.crm._lock:
            visible = []
            for item in self._priority_items(owner):
                if self.crm._db.execute('SELECT 1 FROM crm_sales_priority_archives WHERE owner=? AND candidate_key=? AND archived=1',
                                        (owner,item['key'])).fetchone():
                    continue
                saved = self.crm._db.execute("SELECT * FROM crm_sales_priority_decisions WHERE owner=? AND candidate_key=?", (owner, item["key"])).fetchone()
                if saved and saved["signature"] == item["signature"]:
                    if saved["decision"] == "dismiss" or (saved["decision"] == "defer" and saved["until_at"] > now):
                        continue
                item["source_changed_after_decision"] = bool(saved and saved["signature"] != item["signature"])
                visible.append(item)
            return {"items": visible, "total": len(visible), "generated_at": now}

    def decide_priority(self, owner, data):
        owner, now = _owner(owner), _timestamp(self.clock())
        if not isinstance(data, dict) or set(data)-{"key", "signature", "decision", "until_at", "note"}:
            raise ValueError("推进建议决定字段无效")
        key = _text(data.get("key"), "推进建议编号", 200, required=True)
        decision = data.get("decision")
        if decision not in ("dismiss", "defer", "reset", "archive"):
            raise ValueError("请选择不适用、稍后处理、归档或恢复")
        until = _timestamp(data["until_at"]) if decision == "defer" and data.get("until_at") is not None else None
        if decision == "defer" and (until is None or until <= now):
            raise ValueError("稍后处理时间应在未来")
        note = _text(data.get("note", ""), "推进决定说明", 1000)
        with self.crm._transaction() as db:
            item = next((item for item in self._priority_items(owner) if item["key"] == key), None)
            if item is None:
                raise KeyError("未找到你的推进建议")
            if data.get("signature") != item["signature"]:
                raise ValueError("推进依据已有变化，请刷新后核对")
            if decision == 'archive':
                db.execute('INSERT INTO crm_sales_priority_archives(owner,candidate_key,item_json,archived,revision,updated_at) VALUES (?,?,?,1,1,?) '
                    'ON CONFLICT(owner,candidate_key) DO UPDATE SET item_json=excluded.item_json,archived=1,revision=crm_sales_priority_archives.revision+1,updated_at=excluded.updated_at',
                    (owner,key,_json(item),now))
                # A later explicit restore should restore this prompt regardless
                # of an earlier signature-level dismiss/defer decision.
                db.execute('UPDATE crm_sales_priority_decisions SET decision=\'reset\',updated_at=? WHERE owner=? AND candidate_key=?',(now,owner,key))
                return {**item,'decision':'archive','decision_note':note}
            db.execute("INSERT INTO crm_sales_priority_decisions VALUES (?,?,?,?,?,?,?) ON CONFLICT(owner,candidate_key) DO UPDATE SET "
                       "signature=excluded.signature,decision=excluded.decision,until_at=excluded.until_at,note=excluded.note,updated_at=excluded.updated_at",
                       (owner, key, item["signature"], decision, until, note, now))
            return {**item, "decision": decision, "until_at": until, "decision_note": note}

    def priority_archives(self,owner,*,q='',page=1,page_size=50):
        owner=_owner(owner); offset=_pagination(page,page_size); search=_search(q)
        with self.crm._lock:
            where="owner=? AND archived=1 AND item_json LIKE ? ESCAPE '\\'"
            total=self.crm._db.execute('SELECT COUNT(*) FROM crm_sales_priority_archives WHERE '+where,(owner,search)).fetchone()[0]
            rows=self.crm._db.execute('SELECT candidate_key,item_json,revision,updated_at FROM crm_sales_priority_archives WHERE '+where+
                ' ORDER BY updated_at DESC,candidate_key LIMIT ? OFFSET ?',(owner,search,page_size,offset)).fetchall()
            return {'items':[{'key':r['candidate_key'],'item':json.loads(r['item_json']),'revision':r['revision'],'archived_at':r['updated_at']} for r in rows],
                'total':total,'page':page,'pages':max(1,(total+page_size-1)//page_size)}

    def restore_priority_archive(self,owner,data):
        owner=_owner(owner); now=_timestamp(self.clock())
        if not isinstance(data,dict) or set(data)!={'key','revision'}:raise ValueError('请核对要恢复的推进提示。')
        key=_text(data['key'],'推进提示编号',200,required=True)
        revision=_identifier(data['revision'])
        with self.crm._transaction() as db:
            row=db.execute('SELECT * FROM crm_sales_priority_archives WHERE owner=? AND candidate_key=?',(owner,key)).fetchone()
            if row is None:raise KeyError('未找到你的归档提示。')
            if not row['archived']:return {'restored':False,'key':key}
            if row['revision']!=revision:raise ValueError('这条提示已有变化，请刷新归档列表后核对。')
            db.execute('UPDATE crm_sales_priority_archives SET archived=0,revision=revision+1,updated_at=? WHERE owner=? AND candidate_key=?',(now,owner,key))
            db.execute("UPDATE crm_sales_priority_decisions SET decision='reset',updated_at=? WHERE owner=? AND candidate_key=?",(now,owner,key))
            return {'restored':True,'key':key}
