"""Customer profiles and reviewable voice changes, isolated from task scheduling."""

import hashlib
import json

from .crm import CRMStore, STAGES, _identifier, _owner, _pagination, _public, _search, _text, analysis_fingerprint
from .customer_schema import ACCOUNT_FIELDS, BASIC_LABELS, CONTACT_FIELDS, CONTACT_LABELS
from .store import _timestamp


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _same_name(first, second):
    return first.strip().casefold() == second.strip().casefold()


class CustomerStore(CRMStore):
    """Facts are append-only. Only explicit confirmation applies a voice draft.

    Old drafts keep their conservative revision guard; new drafts compare the
    exact fields they change and the source content and customer attribution.
    """

    def __init__(self, path):
        super().__init__(path)
        with self._lock:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS crm_customer_revisions (
                    owner TEXT NOT NULL,
                    customer_id INTEGER NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(owner,customer_id),
                    FOREIGN KEY(owner,customer_id) REFERENCES crm_customers(owner,id)
                );
                CREATE TABLE IF NOT EXISTS crm_contacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner TEXT NOT NULL,
                    customer_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT '',
                    phone TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(owner,customer_id,id),
                    FOREIGN KEY(owner,customer_id) REFERENCES crm_customers(owner,id)
                );
                CREATE INDEX IF NOT EXISTS crm_contacts_customer ON crm_contacts(owner,customer_id,id);
                CREATE TABLE IF NOT EXISTS crm_customer_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner TEXT NOT NULL,
                    customer_id INTEGER NOT NULL,
                    contact_id INTEGER,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    basis TEXT NOT NULL CHECK(basis IN ('reported','observation')),
                    evidence TEXT NOT NULL DEFAULT '',
                    source_record_id INTEGER,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(owner,customer_id) REFERENCES crm_customers(owner,id),
                    FOREIGN KEY(owner,customer_id,contact_id) REFERENCES crm_contacts(owner,customer_id,id),
                    FOREIGN KEY(owner,source_record_id) REFERENCES crm_records(owner,id)
                );
                CREATE INDEX IF NOT EXISTS crm_customer_facts_latest
                    ON crm_customer_facts(owner,customer_id,contact_id,key,id);
                CREATE TABLE IF NOT EXISTS crm_customer_drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner TEXT NOT NULL,
                    source_id TEXT,
                    source_record_id INTEGER,
                    customer_id INTEGER,
                    contact_id INTEGER,
                    intent TEXT NOT NULL CHECK(intent IN ('create','update')),
                    customer_name TEXT NOT NULL,
                    contact_name TEXT NOT NULL DEFAULT '',
                    source_text TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    changes_json TEXT NOT NULL,
                    snapshot TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','confirmed','rejected','stale')),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(owner,source_id),
                    FOREIGN KEY(owner,customer_id) REFERENCES crm_customers(owner,id),
                    FOREIGN KEY(owner,source_record_id) REFERENCES crm_records(owner,id)
                );
                CREATE INDEX IF NOT EXISTS crm_customer_drafts_owner
                    ON crm_customer_drafts(owner,status,id);
                CREATE TABLE IF NOT EXISTS crm_customer_contact_migrations (
                    owner TEXT NOT NULL,
                    customer_id INTEGER NOT NULL,
                    PRIMARY KEY(owner,customer_id),
                    FOREIGN KEY(owner,customer_id) REFERENCES crm_customers(owner,id)
                );
                CREATE TRIGGER IF NOT EXISTS crm_customer_revision_insert AFTER INSERT ON crm_customers
                BEGIN
                    INSERT INTO crm_customer_revisions(owner,customer_id,revision) VALUES(NEW.owner,NEW.id,1)
                    ON CONFLICT(owner,customer_id) DO UPDATE SET revision=revision+1;
                END;
                CREATE TRIGGER IF NOT EXISTS crm_customer_revision_update AFTER UPDATE ON crm_customers
                BEGIN
                    INSERT INTO crm_customer_revisions(owner,customer_id,revision) VALUES(NEW.owner,NEW.id,1)
                    ON CONFLICT(owner,customer_id) DO UPDATE SET revision=revision+1;
                END;
                CREATE TRIGGER IF NOT EXISTS crm_contact_revision_insert AFTER INSERT ON crm_contacts
                BEGIN
                    INSERT INTO crm_customer_revisions(owner,customer_id,revision) VALUES(NEW.owner,NEW.customer_id,1)
                    ON CONFLICT(owner,customer_id) DO UPDATE SET revision=revision+1;
                END;
                CREATE TRIGGER IF NOT EXISTS crm_contact_revision_update AFTER UPDATE ON crm_contacts
                BEGIN
                    INSERT INTO crm_customer_revisions(owner,customer_id,revision) VALUES(NEW.owner,NEW.customer_id,1)
                    ON CONFLICT(owner,customer_id) DO UPDATE SET revision=revision+1;
                END;
                CREATE TRIGGER IF NOT EXISTS crm_fact_revision_insert AFTER INSERT ON crm_customer_facts
                BEGIN
                    INSERT INTO crm_customer_revisions(owner,customer_id,revision) VALUES(NEW.owner,NEW.customer_id,1)
                    ON CONFLICT(owner,customer_id) DO UPDATE SET revision=revision+1;
                END;
            """)
        with self._transaction() as db:
            if "archived" not in {row["name"] for row in db.execute("PRAGMA table_info(crm_contacts)")}:
                db.execute("ALTER TABLE crm_contacts ADD COLUMN archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1))")
            if "department" not in {row["name"] for row in db.execute("PRAGMA table_info(crm_contacts)")}:
                db.execute("ALTER TABLE crm_contacts ADD COLUMN department TEXT NOT NULL DEFAULT ''")
            db.execute("INSERT OR IGNORE INTO crm_customer_revisions(owner,customer_id,revision) "
                       "SELECT owner,id,0 FROM crm_customers")
            # One-time import only: later multi-contact edits must survive restart.
            rows = db.execute("SELECT c.* FROM crm_customers c WHERE NOT EXISTS "
                              "(SELECT 1 FROM crm_customer_contact_migrations m "
                              "WHERE m.owner=c.owner AND m.customer_id=c.id)").fetchall()
            for row in rows:
                if row["contact"].strip() and not self._contacts_named(db, row["owner"], row["id"], row["contact"]):
                    self._insert_contact(db, row["owner"], row["id"], {
                        "name": row["contact"].strip(), "role": "", "phone": row["phone"]}, row["updated_at"])
                db.execute("INSERT INTO crm_customer_contact_migrations(owner,customer_id) VALUES (?,?)",
                           (row["owner"], row["id"]))

    @staticmethod
    def _contact_values(data, *, partial=False):
        if not isinstance(data, dict) or set(data) - (set(CONTACT_LABELS) | {"archived"}):
            raise ValueError("联系人字段无效")
        values = dict(data) if partial else {"name": "", "role": "", "phone": "", "department": "", "archived": False, **data}
        if "archived" in values and type(values["archived"]) is not bool:
            raise ValueError("联系人归档标记必须为布尔值")
        for key, value in list(values.items()):
            if key == "archived":
                values[key] = int(value)
                continue
            values[key] = _text(value, CONTACT_LABELS[key], 80 if key == "phone" else 120,
                                required=key == "name").strip()
        return values

    @staticmethod
    def _require_contact(db, owner, customer_id, contact_id):
        row = db.execute("SELECT * FROM crm_contacts WHERE owner=? AND customer_id=? AND id=?",
                         (owner, customer_id, contact_id)).fetchone()
        if row is None:
            raise KeyError("未找到该客户的联系人")
        return row

    @staticmethod
    def _customers_named(db, owner, name):
        return [row for row in db.execute("SELECT * FROM crm_customers WHERE owner=? ORDER BY id", (owner,))
                if _same_name(row["name"], name) or any(_same_name(alias, name) for alias in json.loads(row["aliases_json"]))]

    @staticmethod
    def _contacts_named(db, owner, customer_id, name):
        return [row for row in db.execute("SELECT * FROM crm_contacts WHERE owner=? AND customer_id=? AND archived=0 ORDER BY id",
                                         (owner, customer_id)) if _same_name(row["name"], name)]

    def find_customers_exact(self, owner, name):
        owner, name = _owner(owner), _text(name, "客户名称", 120, required=True)
        with self._lock:
            return [_public(row) for row in self._customers_named(self._db, owner, name)]

    def _customer_public(self, owner, row):
        if row is None:
            return None
        customer = _public(row)
        contacts = self._db.execute("SELECT name,phone FROM crm_contacts WHERE owner=? AND customer_id=? AND archived=0 "
                                    "ORDER BY CASE WHEN id=? THEN 0 ELSE 1 END,id",
                                    (owner, customer["id"], customer.get("primary_contact_id"))).fetchall()
        any_contacts = self._db.execute("SELECT 1 FROM crm_contacts WHERE owner=? AND customer_id=? LIMIT 1",
                                        (owner, customer["id"])).fetchone()
        return {**customer, "primary_contact_name": contacts[0]["name"] if contacts else "" if any_contacts else customer["contact"],
                "primary_contact_phone": contacts[0]["phone"] if contacts else "" if any_contacts else customer["phone"],
                "contact_count": len(contacts)}

    def get_customer(self, owner, customer_id):
        owner, customer_id = _owner(owner), _identifier(customer_id)
        with self._lock:
            row = self._db.execute(
                "SELECT c.*, (SELECT COUNT(*) FROM crm_records r WHERE r.owner=c.owner AND r.customer_id=c.id "
                "AND r.hidden=0) AS record_count FROM crm_customers c WHERE c.owner=? AND c.id=?",
                (owner, customer_id)).fetchone()
            return self._customer_public(owner, row)

    def list_customers(self, owner, q="", stage="", page=1, page_size=50):
        owner, offset, search = _owner(owner), _pagination(page, page_size), _search(q)
        if stage not in ("", *STAGES):
            raise ValueError("销售阶段无效")
        where, params = "c.owner=?", [owner]
        if q.strip():
            where += (" AND (c.name LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM json_each(c.aliases_json) j WHERE j.value LIKE ? ESCAPE '\\') OR "
                      "EXISTS (SELECT 1 FROM crm_contacts p WHERE p.owner=c.owner AND p.customer_id=c.id AND p.archived=0 "
                      "AND (p.name LIKE ? ESCAPE '\\' OR p.phone LIKE ? ESCAPE '\\')) OR "
                      "(NOT EXISTS (SELECT 1 FROM crm_contacts p WHERE p.owner=c.owner AND p.customer_id=c.id) "
                      "AND (c.contact LIKE ? ESCAPE '\\' OR c.phone LIKE ? ESCAPE '\\')))")
            params += [search] * 6
        if stage:
            where += " AND c.stage=?"
            params.append(stage)
        with self._lock:
            total = self._db.execute("SELECT COUNT(*) FROM crm_customers c WHERE " + where, params).fetchone()[0]
            rows = self._db.execute(
                "SELECT c.*, (SELECT COUNT(*) FROM crm_records r WHERE r.owner=c.owner AND r.customer_id=c.id "
                "AND r.hidden=0) AS record_count FROM crm_customers c WHERE " + where
                + " ORDER BY c.updated_at DESC,c.id DESC LIMIT ? OFFSET ?", params + [page_size, offset]).fetchall()
            return {"items": [self._customer_public(owner, row) for row in rows], "total": total, "page": page,
                    "pages": max(1, (total + page_size - 1) // page_size)}

    def find_contacts_exact(self, owner, name, customer_id=None):
        owner, name = _owner(owner), _text(name, "联系人姓名", 120, required=True)
        where, params = "p.owner=? AND p.archived=0", [owner]
        if customer_id is not None:
            where += " AND p.customer_id=?"
            params.append(_identifier(customer_id))
        with self._lock:
            return [_public(row) for row in self._db.execute(
                "SELECT p.*,c.name AS customer_name FROM crm_contacts p JOIN crm_customers c "
                "ON c.owner=p.owner AND c.id=p.customer_id WHERE " + where + " ORDER BY p.id", params)
                    if _same_name(row["name"], name)]

    @staticmethod
    def _insert_contact(db, owner, customer_id, values, now):
        return db.execute("INSERT INTO crm_contacts(owner,customer_id,name,role,phone,department,archived,created_at,updated_at) "
                          "VALUES (?,?,?,?,?,?,?,?,?)", (owner, customer_id, values["name"], values.get("role", ""),
                                                     values.get("phone", ""), values.get("department", ""),
                                                     values.get("archived", 0), now, now)).lastrowid

    @staticmethod
    def _same_contact_identity(first, second):
        """Same names need explicit distinguishing business identity."""
        def phone(value):
            return ''.join(c for c in (value or '') if c.isalnum() or c == '+')
        first_phone, second_phone = phone(first.get('phone')), phone(second.get('phone'))
        if first_phone and second_phone:
            return first_phone == second_phone
        first_department = str(first.get('department') or '').strip().casefold()
        second_department = str(second.get('department') or '').strip().casefold()
        return not (first_department and second_department and first_department != second_department)

    def create_contact(self, owner, customer_id, data, now):
        owner, customer_id, now = _owner(owner), _identifier(customer_id), _timestamp(now)
        values = self._contact_values(data)
        with self._transaction() as db:
            self._require_customer(db, owner, customer_id)
            if not values["archived"] and any(self._same_contact_identity(values, dict(row))
                    for row in self._contacts_named(db, owner, customer_id, values["name"])):
                raise ValueError("该客户已有同名有效联系人，请核对后编辑；不同人请补充不同部门或明确联系电话")
            contact_id = self._insert_contact(db, owner, customer_id, values, now)
            return _public(self._require_contact(db, owner, customer_id, contact_id))

    def create_customer(self, owner, data, now):
        """Keep the original quick form's primary contact usable immediately."""
        owner, now = _owner(owner), _timestamp(now)
        values = self._customer_values(data)
        with self._transaction() as db:
            customer_id = self._insert_customer(db, owner, values, now)
            if values["contact"]:
                self._insert_contact(db, owner, customer_id,
                                     {"name": values["contact"], "phone": values["phone"], "role": ""}, now)
            db.execute("INSERT INTO crm_customer_contact_migrations(owner,customer_id) VALUES (?,?)", (owner, customer_id))
        return self.get_customer(owner, customer_id)

    def update_contact(self, owner, customer_id, contact_id, data, now):
        owner, customer_id, contact_id = _owner(owner), _identifier(customer_id), _identifier(contact_id)
        values, now = self._contact_values(data, partial=True), _timestamp(now)
        with self._transaction() as db:
            current = self._require_contact(db, owner, customer_id, contact_id)
            if not values.get("archived", current["archived"]):
                proposed = {**dict(current), **values}
                if any(row["id"] != contact_id and self._same_contact_identity(proposed, dict(row))
                       for row in self._contacts_named(db, owner, customer_id, proposed['name'])):
                    raise ValueError("该客户已有同名有效联系人，请核对姓名、部门与电话，避免合并不同人")
            values = {key: value for key, value in values.items() if value != current[key]}
            if values:
                keys = list(values)
                db.execute("UPDATE crm_contacts SET " + ",".join(key + "=?" for key in keys)
                           + ",updated_at=? WHERE owner=? AND customer_id=? AND id=?",
                           [values[key] for key in keys] + [now, owner, customer_id, contact_id])
            if values.get("archived"):
                db.execute("UPDATE crm_customers SET primary_contact_id=NULL,updated_at=? WHERE owner=? AND id=? AND primary_contact_id=?",
                           (now, owner, customer_id, contact_id))
            return _public(self._require_contact(db, owner, customer_id, contact_id))

    @staticmethod
    def _fact_values(data, *, voice=False):
        allowed = {"key", "value", "basis", "evidence", "contact_id", "source_record_id"}
        if not isinstance(data, dict) or set(data) - allowed:
            raise ValueError("画像字段无效")
        contact_id = data.get("contact_id")
        if contact_id is not None:
            _identifier(contact_id)
        source_record_id = data.get("source_record_id")
        if source_record_id is not None:
            _identifier(source_record_id)
        schema = CONTACT_FIELDS if contact_id is not None else ACCOUNT_FIELDS
        if not isinstance(data.get("key"), str) or data["key"] not in schema:
            raise ValueError("画像字段与客户或联系人不匹配")
        if data.get("basis") not in ("reported", "observation"):
            raise ValueError("依据类型必须为明确提及或个人观察")
        return {"key": data["key"], "value": _text(data.get("value"), "画像内容", 2000, required=voice).strip(),
                "basis": data["basis"], "evidence": _text(data.get("evidence", ""), "原话依据", 4000).strip(),
                "contact_id": contact_id, "source_record_id": source_record_id}

    def _check_fact_links(self, db, owner, customer_id, values):
        self._require_customer(db, owner, customer_id)
        if values["contact_id"] is not None:
            self._require_contact(db, owner, customer_id, values["contact_id"])
        if values["source_record_id"] is not None:
            record = self._require_record(db, owner, values["source_record_id"])
            if record["customer_id"] not in (None, customer_id):
                raise ValueError("来源记录属于另一客户")
            if not values["evidence"] or values["evidence"] not in record["original_content"]:
                raise ValueError("画像依据必须来自记录原话")

    @staticmethod
    def _insert_fact(db, owner, customer_id, values, now):
        return db.execute("INSERT INTO crm_customer_facts(owner,customer_id,contact_id,key,value,basis,evidence,"
                          "source_record_id,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                          (owner, customer_id, values.get("contact_id"), values["key"], values["value"],
                           values["basis"], values.get("evidence", ""), values.get("source_record_id"), now)).lastrowid

    @staticmethod
    def _fact_public(row):
        result = _public(row)
        schema = CONTACT_FIELDS if result["contact_id"] is not None else ACCOUNT_FIELDS
        spec = schema[result["key"]]
        return {**result, "label": spec["label"], "group": spec["group"],
                "source": "record" if result["source_record_id"] is not None else "manual"}

    def save_fact(self, owner, customer_id, data, now):
        owner, customer_id, now = _owner(owner), _identifier(customer_id), _timestamp(now)
        values = self._fact_values(data)
        with self._transaction() as db:
            self._check_fact_links(db, owner, customer_id, values)
            fact_id = self._insert_fact(db, owner, customer_id, values, now)
            return self._fact_public(db.execute("SELECT * FROM crm_customer_facts WHERE id=?", (fact_id,)).fetchone())

    @staticmethod
    def _snapshot(db, owner, customer_id):
        row = db.execute("SELECT revision FROM crm_customer_revisions WHERE owner=? AND customer_id=?",
                         (owner, customer_id)).fetchone()
        if row is None:
            raise KeyError("未找到客户")
        return hashlib.sha256(_json([owner, customer_id, row["revision"]]).encode()).hexdigest()

    def _field_snapshot(self, db, owner, values, changes):
        customer_id, contact_id = values["customer_id"], values["contact_id"]
        customer = self.get_customer(owner, customer_id)
        if customer is None:
            raise KeyError("未找到客户")
        contact = self._require_contact(db, owner, customer_id, contact_id) if contact_id else None
        state = {"customer_name": customer["name"], "contact_identity":
                 {key: contact[key] for key in ("id", "name", "archived")} if contact is not None else None, "fields": []}
        if values["contact"].get("name"):
            state["contact_name_matches"] = [row["id"] for row in self._contacts_named(db, owner, customer_id, values["contact"]["name"])
                                             if row["id"] != contact_id]
        for change in changes:
            target, key = change["target"], change["key"]
            if target == "basic":
                current = customer[key]
            elif target == "contact" and key in CONTACT_LABELS:
                current = contact[key] if contact is not None else None
            else:
                fact_contact = contact_id if target == "contact" else None
                fact = (db.execute("SELECT id,value,basis,evidence FROM crm_customer_facts WHERE owner=? AND customer_id=? "
                                   "AND contact_id IS ? AND key=? ORDER BY id DESC LIMIT 1", (owner, customer_id, fact_contact, key)).fetchone()
                        if target == "account" or contact_id is not None else None)
                current = dict(fact) if fact else None
            state["fields"].append([target, key, current])
        return "fields:" + _json(state)

    @staticmethod
    def _evidence(data, values, name, source):
        if not isinstance(data, dict) or set(data) - set(values):
            raise ValueError(name + "依据字段无效")
        checked = {}
        for key, value in values.items():
            evidence = data.get(key, value if key == "name" else "")
            evidence = _text(evidence, name + "原话依据", 4000, required=True).strip()
            if evidence not in source:
                raise ValueError(name + "依据必须来自原话")
            checked[key] = evidence
        return checked

    def _draft_values(self, data):
        allowed = {"intent", "customer_id", "customer_name", "contact_id", "contact_name", "basic", "contact",
                   "attributes", "source_text", "basic_evidence", "contact_evidence", "source_content",
                   "source_snapshot", "source_snapshot_version", "selected_customer"}
        if not isinstance(data, dict) or set(data) - allowed or data.get("intent") not in ("create", "update"):
            raise ValueError("客户变更草稿字段无效")
        source = _text(data.get("source_text"), "客户原话", 20000, required=True)
        source_content = data.get("source_content")
        if source_content is not None:
            source_content = _text(source_content, "本次整理内容", 20000, required=True)
        evidence_source = source_content if source_content is not None else source
        selected = data.get("selected_customer", False)
        if type(selected) is not bool or (selected and data["intent"] != "update"):
            raise ValueError("显式选定客户标记无效")
        source_snapshot = data.get("source_snapshot")
        if source_snapshot is not None and (not isinstance(source_snapshot, str) or len(source_snapshot) != 64):
            raise ValueError("来源记录版本无效")
        source_snapshot_version = data.get("source_snapshot_version", 2)
        if type(source_snapshot_version) is not int or source_snapshot_version not in (1, 2):
            raise ValueError("来源记录版本格式无效")
        name = _text(data.get("customer_name"), "客户名称", 120, required=True).strip()
        if name not in evidence_source and not selected:
            raise ValueError("客户名称必须来自原话，请补充公司全名")
        customer_id = data.get("customer_id")
        if data["intent"] == "create" and customer_id is not None:
            raise ValueError("新建客户不能指定已有客户编号")
        if data["intent"] == "update":
            _identifier(customer_id)
        contact_id = data.get("contact_id")
        if contact_id is not None:
            _identifier(contact_id)
            if customer_id is None:
                raise ValueError("新客户不能使用已有联系人编号")
        contact_name = _text(data.get("contact_name") or "", "联系人姓名", 120).strip()
        if contact_name and contact_name not in evidence_source:
            raise ValueError("联系人姓名必须来自原话")
        basic = self._customer_values(data.get("basic", {}), partial=True)
        if set(basic) - set(BASIC_LABELS):
            raise ValueError("语音草稿仅支持已列出的客户基本资料字段")
        if data["intent"] == "create":
            if "name" in basic and basic["name"] != name:
                raise ValueError("新建客户名称不一致")
            basic["name"] = name
        contact = self._contact_values(data.get("contact", {}), partial=True)
        if set(contact) - set(CONTACT_LABELS):
            raise ValueError("请在联系人页面管理归档状态")
        if contact_id is None and (contact or contact_name):
            contact.setdefault("name", contact_name)
            contact = self._contact_values(contact, partial=True)
            contact_name = contact["name"]
        elif contact_id is not None and not contact_name:
            raise ValueError("请明确要修改的联系人姓名")
        if contact and contact_id is None and "name" not in contact:
            raise ValueError("新建联系人需要姓名")
        basic_evidence = self._evidence(data.get("basic_evidence", {}), basic, "客户", evidence_source)
        contact_evidence = self._evidence(data.get("contact_evidence", {}), contact, "联系人", evidence_source)
        attributes = data.get("attributes", [])
        if not isinstance(attributes, list) or len(attributes) > 34:
            raise ValueError("单次画像变更最多 34 项")
        checked, seen = [], set()
        for item in attributes:
            if not isinstance(item, dict) or set(item) - {"key", "value", "basis", "evidence", "target"}:
                raise ValueError("画像变更字段无效")
            target = item.get("target")
            if target not in ("account", "contact"):
                raise ValueError("画像变更对象无效")
            if target == "contact" and not (contact_id is not None or contact.get("name")):
                raise ValueError("联系人画像需要明确联系人")
            key = item.get("key")
            if not isinstance(key, str) or (target, key) in seen:
                raise ValueError("同一画像字段不能在一次草稿中重复")
            values = self._fact_values({key_: item[key_] for key_ in ("key", "value", "basis", "evidence")
                                       if key_ in item} | {"contact_id": 1 if target == "contact" else None}, voice=True)
            if not values["evidence"] or values["evidence"] not in evidence_source:
                raise ValueError("画像依据必须来自原话")
            checked.append({"target": target, **{key_: values[key_] for key_ in ("key", "value", "basis", "evidence")}})
            seen.add((target, key))
        return {"intent": data["intent"], "customer_id": customer_id, "customer_name": name,
                "contact_id": contact_id, "contact_name": contact_name, "basic": basic, "contact": contact,
                "attributes": checked, "source_text": source, "basic_evidence": basic_evidence,
                "contact_evidence": contact_evidence, "source_content": source_content,
                "source_snapshot": source_snapshot, "source_snapshot_version": source_snapshot_version,
                "selected_customer": selected}

    @staticmethod
    def _draft_public(row):
        if row is None:
            return None
        fields = ("id", "intent", "customer_id", "contact_id", "customer_name", "contact_name", "status",
                  "created_at", "updated_at", "source_text", "source_record_id")
        payload = json.loads(row["payload_json"])
        return {**{key: row[key] for key in fields}, "changes": json.loads(row["changes_json"]),
                "source_content": payload.get("source_content"), "selected_customer": payload.get("selected_customer", False)}

    @staticmethod
    def record_snapshot(record, *, version=2):
        # Classification, completion and incidental save timestamps do not
        # change the evidence used for a customer profile. Keep content and
        # attribution guards, including edits made in the same clock tick.
        keys = (("id", "title", "content", "customer_id", "status", "kind", "parent_record_id", "updated_at")
                if version == 1 else
                ("id", "title", "content", "original_content", "customer_id", "parent_record_id"))
        return hashlib.sha256(_json({key: record[key] for key in keys}).encode()).hexdigest()

    def stale_record_drafts(self, owner, record_id, now):
        owner, record_id, now = _owner(owner), _identifier(record_id), _timestamp(now)
        with self._transaction() as db:
            self._require_record(db, owner, record_id)
            db.execute("UPDATE crm_customer_drafts SET status='stale',updated_at=? "
                       "WHERE owner=? AND source_record_id=? AND status='pending'", (now, owner, record_id))

    def _changes(self, db, owner, values):
        customer_id, contact_id = values["customer_id"], values["contact_id"]
        customer = db.execute("SELECT * FROM crm_customers WHERE owner=? AND id=?", (owner, customer_id)).fetchone()
        contact = (self._require_contact(db, owner, customer_id, contact_id) if contact_id is not None else None)
        changes = []
        for target, fields, old, labels in (("basic", values["basic"], customer, BASIC_LABELS),
                                            ("contact", values["contact"], contact, CONTACT_LABELS)):
            evidence = values["basic_evidence" if target == "basic" else "contact_evidence"]
            for key, value in fields.items():
                before = old[key] if old is not None else None
                if target == "basic" and key == "amount_cents" and old is not None and not old["amount_known"]:
                    before = None
                if before != value:
                    changes.append({"target": target, "key": key, "label": labels[key], "before": before,
                                    "after": value, "basis": "reported", "evidence": evidence[key]})
        for item in values["attributes"]:
            fact_contact = contact_id if item["target"] == "contact" else None
            old = (db.execute("SELECT * FROM crm_customer_facts WHERE owner=? AND customer_id=? "
                              "AND contact_id IS ? AND key=? ORDER BY id DESC LIMIT 1",
                              (owner, customer_id, fact_contact, item["key"])).fetchone()
                   if customer_id is not None and (item["target"] == "account" or contact_id is not None) else None)
            schema = CONTACT_FIELDS if item["target"] == "contact" else ACCOUNT_FIELDS
            changes.append({"target": item["target"], "key": item["key"], "label": schema[item["key"]]["label"],
                            "before": old["value"] if old else None, "after": item["value"],
                            "basis": item["basis"], "evidence": item["evidence"]})
        return changes

    def create_customer_draft(self, owner, data, now, *, source_id=None, source_record_id=None):
        owner, now = _owner(owner), _timestamp(now)
        if source_id is not None:
            source_id = _text(source_id, "消息编号", 512, required=True)
        if source_record_id is not None:
            _identifier(source_record_id)
        with self._transaction() as db:
            if source_id is not None:
                previous = db.execute("SELECT * FROM crm_customer_drafts WHERE owner=? AND source_id=?",
                                      (owner, source_id)).fetchone()
                if previous is not None:
                    return self._draft_public(previous)
            values = self._draft_values(data)
            customer_id, contact_id = values["customer_id"], values["contact_id"]
            if customer_id is None:
                if self._customers_named(db, owner, values["customer_name"]):
                    raise ValueError("已有同名客户，请选择已有客户后补充")
                snapshot = "new"
            else:
                self._require_customer(db, owner, customer_id)
                customer = db.execute("SELECT name FROM crm_customers WHERE owner=? AND id=?", (owner, customer_id)).fetchone()
                if not any(row["id"] == customer_id for row in self._customers_named(db, owner, values["customer_name"])):
                    raise ValueError("客户名称与目标档案不一致")
                snapshot = self._snapshot(db, owner, customer_id)
                if contact_id is not None:
                    contact = self._require_contact(db, owner, customer_id, contact_id)
                    if contact["archived"]:
                        raise ValueError("该联系人已归档，请选择有效联系人")
                    if not _same_name(contact["name"], values["contact_name"]):
                        raise ValueError("联系人名称与目标档案不一致")
                    if values["contact"].get("name") and any(row["id"] != contact_id for row in
                            self._contacts_named(db, owner, customer_id, values["contact"]["name"])):
                        raise ValueError("该客户已有同名有效联系人，请核对姓名")
                elif values["contact"].get("name") and self._contacts_named(db, owner, customer_id, values["contact"]["name"]):
                    raise ValueError("已有同名联系人，请明确要补充的联系人")
            if source_record_id is not None:
                record = self._require_record(db, owner, source_record_id)
                if record["original_content"] != values["source_text"]:
                    raise ValueError("客户草稿与来源原话不一致")
                if record["customer_id"] not in (None, customer_id):
                    raise ValueError("来源记录属于另一客户")
                if values["source_content"] is not None and (record["content"] != values["source_content"] or
                        values["source_snapshot"] != self.record_snapshot(record, version=values["source_snapshot_version"])):
                    raise ValueError("来源记录已修改，请按最新内容重新整理")
                if values["selected_customer"] and (values["source_content"] is None or record["customer_id"] != customer_id):
                    raise ValueError("显式选定客户必须与来源记录归属一致")
                # Direct data-layer callers receive the same source-race guard
                # as the service's explicit snapshot path.
                if values["source_content"] is None:
                    values["source_content"] = record["content"]
                    values["source_snapshot"] = self.record_snapshot(record, version=values["source_snapshot_version"])
            elif values["selected_customer"] or values["source_content"] is not None or values["source_snapshot"] is not None:
                raise ValueError("本次整理需要有效的来源记录")
            changes = self._changes(db, owner, values)
            if not changes:
                raise ValueError("没有需要变更的客户信息")
            # Do not replay no-op fields over an intervening manual edit.
            for target, field in (("basic", "basic"), ("contact", "contact")):
                changed_keys = {change["key"] for change in changes if change["target"] == target}
                values[field] = {key: value for key, value in values[field].items() if key in changed_keys}
                evidence_key = field + "_evidence"
                values[evidence_key] = {key: value for key, value in values[evidence_key].items() if key in changed_keys}
            if customer_id is not None:
                snapshot = self._field_snapshot(db, owner, values, changes)
            if source_record_id is not None:
                db.execute("UPDATE crm_customer_drafts SET status='stale',updated_at=? "
                           "WHERE owner=? AND source_record_id=? AND status='pending'", (now, owner, source_record_id))
            draft_id = db.execute("INSERT INTO crm_customer_drafts(owner,source_id,source_record_id,customer_id,"
                                  "contact_id,intent,customer_name,contact_name,source_text,payload_json,changes_json,"
                                  "snapshot,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                  (owner, source_id, source_record_id, customer_id, contact_id, values["intent"],
                                   values["customer_name"], values["contact_name"], values["source_text"], _json(values),
                                   _json(changes), snapshot, now, now)).lastrowid
            return self._draft_public(db.execute("SELECT * FROM crm_customer_drafts WHERE id=?", (draft_id,)).fetchone())

    def get_customer_draft(self, owner, draft_id):
        owner, draft_id = _owner(owner), _identifier(draft_id)
        with self._lock:
            return self._draft_public(self._db.execute("SELECT * FROM crm_customer_drafts WHERE owner=? AND id=?",
                                                     (owner, draft_id)).fetchone())

    def list_customer_drafts(self, owner, status="pending", page=1, page_size=50):
        owner, offset = _owner(owner), _pagination(page, page_size)
        if status not in ("", "pending", "confirmed", "rejected", "stale"):
            raise ValueError("客户草稿状态无效")
        where, params = "owner=?", [owner]
        if status:
            where += " AND status=?"
            params.append(status)
        with self._lock:
            total = self._db.execute("SELECT COUNT(*) FROM crm_customer_drafts WHERE " + where, params).fetchone()[0]
            rows = self._db.execute("SELECT * FROM crm_customer_drafts WHERE " + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
                                    params + [page_size, offset]).fetchall()
            return {"items": [self._draft_public(row) for row in rows], "total": total, "page": page,
                    "pages": max(1, (total + page_size - 1) // page_size)}

    @staticmethod
    def _require_draft(db, owner, draft_id):
        row = db.execute("SELECT * FROM crm_customer_drafts WHERE owner=? AND id=?", (owner, draft_id)).fetchone()
        if row is None:
            raise KeyError("未找到客户变更草稿")
        return row

    def confirm_customer_draft(self, owner, draft_id, now):
        owner, draft_id, now = _owner(owner), _identifier(draft_id), _timestamp(now)
        stale = False
        with self._transaction() as db:
            row = self._require_draft(db, owner, draft_id)
            if row["status"] == "confirmed":
                return self._draft_public(row)
            if row["status"] != "pending":
                raise ValueError("该客户草稿已取消或过期，请重新整理")
            values = json.loads(row["payload_json"])
            customer_id, contact_id = row["customer_id"], row["contact_id"]
            if customer_id is None:
                stale = bool(self._customers_named(db, owner, row["customer_name"]))
            elif row["snapshot"].startswith("fields:"):
                stale = self._field_snapshot(db, owner, values, json.loads(row["changes_json"])) != row["snapshot"]
            else:
                # Old releases stored only a whole-customer revision hash.
                # Keep their conservative guard; new drafts use touched fields.
                stale = self._snapshot(db, owner, customer_id) != row["snapshot"]
            if row["source_record_id"] is not None:
                record = self._require_record(db, owner, row["source_record_id"])
                stale = stale or record["customer_id"] not in (None, customer_id)
                if values.get("source_content") is not None:
                    # Existing drafts without a format marker retain their old,
                    # conservative source revision check across upgrades.
                    source_version = values.get("source_snapshot_version", 1)
                    stale = stale or record["content"] != values["source_content"] or self.record_snapshot(record, version=source_version) != values.get("source_snapshot")
                if values.get("selected_customer"):
                    stale = stale or record["customer_id"] != customer_id
            if stale:
                db.execute("UPDATE crm_customer_drafts SET status='stale',updated_at=? WHERE owner=? AND id=?",
                           (now, owner, draft_id))
            else:
                if customer_id is None:
                    basic = self._customer_values(values["basic"])
                    customer_id = self._insert_customer(db, owner, basic, now)
                    db.execute("INSERT INTO crm_customer_contact_migrations(owner,customer_id) VALUES (?,?)",
                               (owner, customer_id))
                elif values["basic"]:
                    self._update_customer(db, owner, customer_id, values["basic"], now)
                if values["contact"]:
                    if contact_id is None:
                        contact_id = self._insert_contact(db, owner, customer_id, values["contact"], now)
                    else:
                        self._require_contact(db, owner, customer_id, contact_id)
                        keys = list(values["contact"])
                        db.execute("UPDATE crm_contacts SET " + ",".join(key + "=?" for key in keys)
                                   + ",updated_at=? WHERE owner=? AND customer_id=? AND id=?",
                                   [values["contact"][key] for key in keys] + [now, owner, customer_id, contact_id])
                for item in values["attributes"]:
                    self._insert_fact(db, owner, customer_id,
                                      {**item, "contact_id": contact_id if item["target"] == "contact" else None,
                                       "source_record_id": row["source_record_id"]}, now)
                if row["source_record_id"] is not None:
                    old_fingerprint = analysis_fingerprint(record)
                    db.execute("UPDATE crm_records SET customer_id=?,classified=1,updated_at=? WHERE owner=? AND id=?",
                               (customer_id, now, owner, row["source_record_id"]))
                    new_record = self._require_record(db, owner, row["source_record_id"])
                    db.execute("UPDATE crm_analyses SET input_fingerprint=? WHERE owner=? AND record_id=? AND input_fingerprint=?",
                               (analysis_fingerprint(new_record), owner, row["source_record_id"], old_fingerprint))
                    db.execute("UPDATE crm_records SET customer_id=?,updated_at=? WHERE owner=? AND customer_id IS NULL "
                               "AND id IN (SELECT child_record_id FROM crm_analysis_actions WHERE owner=? AND parent_record_id=?)",
                               (customer_id, now, owner, owner, row["source_record_id"]))
                db.execute("UPDATE crm_customer_drafts SET status='confirmed',customer_id=?,contact_id=?,updated_at=? "
                           "WHERE owner=? AND id=?", (customer_id, contact_id, now, owner, draft_id))
            result = self._draft_public(self._require_draft(db, owner, draft_id))
        if stale:
            raise ValueError("客户档案已变化，这份草稿已过期，请重新整理后确认")
        return result

    def reject_customer_draft(self, owner, draft_id, now):
        owner, draft_id, now = _owner(owner), _identifier(draft_id), _timestamp(now)
        with self._transaction() as db:
            row = self._require_draft(db, owner, draft_id)
            if row["status"] == "confirmed":
                raise ValueError("该草稿已确认，请通过新的变更修改客户")
            if row["status"] == "pending":
                db.execute("UPDATE crm_customer_drafts SET status='rejected',updated_at=? WHERE owner=? AND id=?",
                           (now, owner, draft_id))
            return self._draft_public(self._require_draft(db, owner, draft_id))

    def profile(self, owner, customer_id):
        owner, customer_id = _owner(owner), _identifier(customer_id)
        with self._lock:
            customer = self.get_customer(owner, customer_id)
            if customer is None:
                return None
            facts = [self._fact_public(row) for row in self._db.execute(
                "SELECT f.* FROM crm_customer_facts f WHERE f.owner=? AND f.customer_id=? AND NOT EXISTS "
                "(SELECT 1 FROM crm_customer_facts n WHERE n.owner=f.owner AND n.customer_id=f.customer_id "
                "AND n.contact_id IS f.contact_id AND n.key=f.key AND n.id>f.id) ORDER BY f.id",
                (owner, customer_id))]
            fields = [fact for fact in facts if fact["contact_id"] is None]
            contacts = [{**_public(row), "fields": [fact for fact in facts if fact["contact_id"] == row["id"]]}
                        for row in self._db.execute("SELECT * FROM crm_contacts WHERE owner=? AND customer_id=? ORDER BY id",
                                                    (owner, customer_id))]
            history = [self._fact_public(row) for row in self._db.execute(
                "SELECT * FROM crm_customer_facts WHERE owner=? AND customer_id=? ORDER BY id DESC LIMIT 200",
                (owner, customer_id))]
            history_total = self._db.execute("SELECT COUNT(*) FROM crm_customer_facts WHERE owner=? AND customer_id=?",
                                             (owner, customer_id)).fetchone()[0]
            next_tasks = [_public(row) for row in self._db.execute(
                "SELECT DISTINCT t.* FROM tasks t JOIN proposals p ON COALESCE(p.task_id,p.target_task_id)=t.id AND p.owner=t.owner "
                "JOIN crm_record_proposals h ON h.proposal_id=p.id AND h.owner=p.owner "
                "JOIN crm_records r ON r.id=h.record_id AND r.owner=h.owner WHERE t.owner=? AND r.customer_id=? "
                "AND r.hidden=0 AND t.status='pending' ORDER BY t.remind_at,t.id LIMIT 10", (owner, customer_id))]
            open_records = [_public(row) for row in self._db.execute(
                self._RECORD_SELECT + "WHERE r.owner=? AND r.customer_id=? AND r.hidden=0 AND r.kind='action' AND r.status!='done' "
                "ORDER BY r.updated_at DESC,r.id DESC LIMIT 10", (owner, customer_id))]
            recent = self.list_records(owner, customer_id=customer_id, kind="note", page_size=5)["items"]
            recent_activities = [_public(row) for row in self._db.execute(
                "SELECT a.id,a.record_id,a.content,a.created_at,r.title AS record_title "
                "FROM crm_activities a JOIN crm_records r ON r.owner=a.owner AND r.id=a.record_id "
                "WHERE a.owner=? AND r.customer_id=? AND r.hidden=0 "
                "ORDER BY a.created_at DESC,a.id DESC LIMIT 10", (owner, customer_id))]
            present = {field["key"] for field in fields if field["value"]}
            priority_fields = ("pain_points", "requirements", "decision_chain", "budget_notes", "timeline", "next_visit_goal")
            missing = [{"key": key, **ACCOUNT_FIELDS[key]} for key in priority_fields if key not in present]
            drafts = [self._draft_public(row) for row in self._db.execute(
                "SELECT * FROM crm_customer_drafts WHERE owner=? AND customer_id=? AND status='pending' ORDER BY id DESC",
                (owner, customer_id))]
            return {"customer": customer, "fields": fields, "contacts": contacts, "history": history,
                    "history_total": history_total, "brief": {"open_records": open_records, "recent_records": recent,
                    "recent_activities": recent_activities, "next_tasks": next_tasks, "missing_fields": missing}, "drafts": drafts}
