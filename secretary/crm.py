"""Owner-isolated customer notes alongside the existing confirmation workflow.

Capturing or editing a note never creates tasks or notifications. The web and
gateway callers use Store.execute for explicit scheduling/confirmation and hold
their shared application lock across that operation and the CRM link.
"""

from datetime import datetime
import hashlib
import json
import re
import time

from .agenda import _window
from .record_categories import CATEGORIES, infer_category, resolve_category
from .store import SHANGHAI, Store, _timestamp


STAGES = ("lead", "contact", "qualified", "proposal", "negotiation", "won", "lost")
RECORD_STATUSES = ("unfiled", "following", "done")
MAX_AMOUNT_CENTS = 999_999_999_999
_CUSTOMER_FIELDS = {"name", "contact", "phone", "stage", "amount_cents", "notes", "aliases", "contact_cycle_days", "primary_contact_id"}
_RECORD_FIELDS = {"title", "content", "customer_id", "status", "kind", "parent_record_id", "category"}
_COMMAND_ACTIONS = {"help", "list", "proposals", "agenda", "confirm", "reject",
                    "reschedule_proposal", "complete", "cancel", "snooze"}


class RecordConflict(ValueError):
    """A record edit targets a revision that another writer already changed."""


def _text(value, name, maximum, *, required=False):
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{name}必须是最多 {maximum} 字的文本")
    if required and not value.strip():
        raise ValueError(f"{name}不能为空")
    return value


def _owner(value):
    return _text(value, "成员账号", 512, required=True)


def _identifier(value):
    if type(value) is not int or not 0 < value <= 2**63 - 1:
        raise ValueError("编号必须为正整数")
    return value


def _pagination(page, page_size):
    if type(page) is not int or not 1 <= page <= 1_000_000:
        raise ValueError("页码必须为 1 至 1000000 的整数")
    if type(page_size) is not int or not 1 <= page_size <= 200:
        raise ValueError("每页数量必须为 1 至 200 的整数")
    return (page - 1) * page_size


def _public(row):
    if row is None:
        return None
    result = {key: value for key, value in dict(row).items()
              if key not in {"owner", "source_id", "hidden", "classified"}}
    if "aliases_json" in result:
        result["aliases"] = json.loads(result.pop("aliases_json"))
    if "amount_known" in result:
        result["amount_known"] = bool(result["amount_known"])
        if not result["amount_known"]:
            result["amount_cents"] = None
    if "archived" in result:
        result["archived"] = bool(result["archived"])
    return result


def _paged(rows, total, page, page_size):
    return {"items": [_public(row) for row in rows], "total": total,
            "page": page, "pages": max(1, (total + page_size - 1) // page_size)}


def _search(value):
    value = _text(value, "关键词", 200).strip()
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def analysis_fingerprint(record):
    """Bind model analysis to the exact source snapshot supplied by the caller."""
    source = {key: record[key] for key in ("title", "content", "original_content", "customer_id")}
    return hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


class CRMStore(Store):
    """Shares Store's schema/transactions; CRM edits never create reminders.

    Editing a note's title also updates its still-pending proposal atomically.
    Confirmed proposals and tasks retain their historical titles.

    Getters return None for missing/foreign resources. Mutations raise KeyError
    for missing/foreign resources, ValueError for invalid input. Public rows
    deliberately exclude owner, callback identifiers and internal flags.
    """

    def __init__(self, path):
        super().__init__(path)
        with self._lock:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS crm_customers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner TEXT NOT NULL,
                    name TEXT NOT NULL,
                    contact TEXT NOT NULL DEFAULT '',
                    phone TEXT NOT NULL DEFAULT '',
                    stage TEXT NOT NULL DEFAULT 'lead'
                        CHECK(stage IN ('lead','contact','qualified','proposal','negotiation','won','lost')),
                    amount_cents INTEGER NOT NULL DEFAULT 0
                        CHECK(amount_cents >= 0 AND amount_cents <= 999999999999),
                    notes TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(owner, id)
                );
                CREATE INDEX IF NOT EXISTS crm_customers_owner ON crm_customers(owner, stage, updated_at);
                CREATE TABLE IF NOT EXISTS crm_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner TEXT NOT NULL,
                    source_id TEXT,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    original_content TEXT NOT NULL,
                    source TEXT NOT NULL CHECK(source IN ('voice','text','web','legacy')),
                    status TEXT NOT NULL DEFAULT 'unfiled' CHECK(status IN ('unfiled','following','done')),
                    customer_id INTEGER,
                    proposal_id INTEGER REFERENCES proposals(id),
                    hidden INTEGER NOT NULL DEFAULT 0 CHECK(hidden IN (0,1)),
                    classified INTEGER NOT NULL DEFAULT 0 CHECK(classified IN (0,1)),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(owner, source_id),
                    UNIQUE(owner, id),
                    FOREIGN KEY(owner, customer_id) REFERENCES crm_customers(owner, id)
                );
                CREATE INDEX IF NOT EXISTS crm_records_owner ON crm_records(owner, hidden, status, updated_at);
                CREATE UNIQUE INDEX IF NOT EXISTS crm_records_proposal ON crm_records(proposal_id)
                    WHERE proposal_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS crm_activities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner TEXT NOT NULL,
                    record_id INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(owner, record_id) REFERENCES crm_records(owner, id)
                );
                CREATE INDEX IF NOT EXISTS crm_activities_record ON crm_activities(owner, record_id, id);
                CREATE TABLE IF NOT EXISTS crm_record_proposals (
                    proposal_id INTEGER PRIMARY KEY REFERENCES proposals(id),
                    owner TEXT NOT NULL,
                    record_id INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(owner, record_id) REFERENCES crm_records(owner, id)
                );
                CREATE INDEX IF NOT EXISTS crm_record_proposals_record
                    ON crm_record_proposals(owner, record_id, proposal_id);
                CREATE TABLE IF NOT EXISTS crm_analyses (
                    owner TEXT NOT NULL,
                    record_id INTEGER NOT NULL,
                    data_json TEXT NOT NULL,
                    input_fingerprint TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(owner, record_id),
                    FOREIGN KEY(owner, record_id) REFERENCES crm_records(owner, id)
                );
                CREATE TABLE IF NOT EXISTS crm_action_terms (
                    owner TEXT NOT NULL, record_id INTEGER NOT NULL, terms_json TEXT NOT NULL,
                    updated_at REAL NOT NULL, PRIMARY KEY(owner,record_id),
                    FOREIGN KEY(owner,record_id) REFERENCES crm_records(owner,id)
                );
                CREATE TABLE IF NOT EXISTS crm_record_transcripts (
                    owner TEXT NOT NULL,record_id INTEGER NOT NULL,original_text TEXT NOT NULL,
                    corrected_text TEXT NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL,
                    PRIMARY KEY(owner,record_id),FOREIGN KEY(owner,record_id) REFERENCES crm_records(owner,id)
                );
                CREATE TABLE IF NOT EXISTS crm_analysis_actions (
                    owner TEXT NOT NULL,
                    parent_record_id INTEGER NOT NULL,
                    action_id INTEGER NOT NULL CHECK(action_id BETWEEN 1 AND 6),
                    child_record_id INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(owner, parent_record_id, action_id),
                    UNIQUE(owner, child_record_id),
                    CHECK(parent_record_id != child_record_id),
                    FOREIGN KEY(owner, parent_record_id) REFERENCES crm_analyses(owner, record_id),
                    FOREIGN KEY(owner, child_record_id) REFERENCES crm_records(owner, id)
                );
            """)
        with self._transaction() as db:
            customer_columns = {row["name"] for row in db.execute("PRAGMA table_info(crm_customers)")}
            for key, declaration in (("amount_known", "INTEGER NOT NULL DEFAULT 1 CHECK(amount_known IN (0,1))"),
                                     ("aliases_json", "TEXT NOT NULL DEFAULT '[]'"),
                                     ("contact_cycle_days", "INTEGER"), ("primary_contact_id", "INTEGER")):
                if key not in customer_columns:
                    db.execute("ALTER TABLE crm_customers ADD COLUMN " + key + " " + declaration)
            record_columns = {row["name"] for row in db.execute("PRAGMA table_info(crm_records)")}
            if "category" not in record_columns:
                db.execute("ALTER TABLE crm_records ADD COLUMN category TEXT NOT NULL DEFAULT 'memo' "
                           "CHECK(category IN ('idea','meeting','conversation','visit_review','memo'))")
                for row in db.execute("SELECT id,title,content,customer_id FROM crm_records").fetchall():
                    db.execute("UPDATE crm_records SET category=? WHERE id=?",
                               (infer_category(row['title'] + '\n' + row['content'], row['customer_id']), row['id']))
            added_kind = "kind" not in record_columns
            if added_kind:
                db.execute("ALTER TABLE crm_records ADD COLUMN kind TEXT NOT NULL DEFAULT 'note' CHECK(kind IN ('note','action'))")
            if "parent_record_id" not in record_columns:
                db.execute("ALTER TABLE crm_records ADD COLUMN parent_record_id INTEGER")
                db.execute("UPDATE crm_records SET parent_record_id=(SELECT parent_record_id FROM crm_analysis_actions a "
                           "WHERE a.owner=crm_records.owner AND a.child_record_id=crm_records.id)")
            if "superseded_by_analysis_version" not in record_columns:
                db.execute("ALTER TABLE crm_records ADD COLUMN superseded_by_analysis_version INTEGER")
            action_sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='crm_analysis_actions'").fetchone()[0]
            if "BETWEEN 1 AND 6" in action_sql:
                db.execute("CREATE TABLE crm_analysis_actions_versioned (owner TEXT NOT NULL,parent_record_id INTEGER NOT NULL,"
                           "action_id INTEGER NOT NULL CHECK(action_id>=1),child_record_id INTEGER NOT NULL,created_at REAL NOT NULL,"
                           "PRIMARY KEY(owner,parent_record_id,action_id),UNIQUE(owner,child_record_id),CHECK(parent_record_id!=child_record_id),"
                           "FOREIGN KEY(owner,parent_record_id) REFERENCES crm_analyses(owner,record_id),"
                           "FOREIGN KEY(owner,child_record_id) REFERENCES crm_records(owner,id))")
                db.execute("INSERT INTO crm_analysis_actions_versioned SELECT a.owner,a.parent_record_id,"
                           "(n.version-1)*6+a.action_id,a.child_record_id,a.created_at FROM crm_analysis_actions a "
                           "JOIN crm_analyses n ON n.owner=a.owner AND n.record_id=a.parent_record_id")
                db.execute("DROP TABLE crm_analysis_actions")
                db.execute("ALTER TABLE crm_analysis_actions_versioned RENAME TO crm_analysis_actions")
            if added_kind:
                db.execute("UPDATE crm_records SET kind='action' WHERE proposal_id IS NOT NULL OR parent_record_id IS NOT NULL")
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='crm_coaching_adoptions'").fetchone():
                    db.execute("UPDATE crm_records SET kind='action' WHERE EXISTS (SELECT 1 FROM crm_coaching_adoptions a "
                               "WHERE a.owner=crm_records.owner AND a.record_id=crm_records.id)")
            # Upgrade existing CRM links before any legacy import. A note can
            # have multiple successive follow-ups, while proposal_id is merely
            # the currently displayed one.
            db.execute(
                "INSERT OR IGNORE INTO crm_record_proposals(proposal_id,owner,record_id,created_at) "
                "SELECT p.id,r.owner,r.id,r.updated_at FROM crm_records r JOIN proposals p "
                "ON p.id=r.proposal_id AND p.owner=r.owner")

    @staticmethod
    def _customer_values(data, *, partial=False):
        if not isinstance(data, dict) or set(data) - _CUSTOMER_FIELDS:
            raise ValueError("客户字段无效")
        values = dict(data) if partial else {
            "name": "", "contact": "", "phone": "", "stage": "lead", "amount_cents": None, "notes": "",
            "aliases": [], "contact_cycle_days": None, "primary_contact_id": None, **data}
        for key, limit in (("name", 120), ("contact", 120), ("phone", 80), ("notes", 10000)):
            if key in values:
                values[key] = _text(values[key], key, limit, required=key == "name").strip()
        if "stage" in values and values["stage"] not in STAGES:
            raise ValueError("销售阶段无效")
        if "amount_cents" in values:
            amount = values["amount_cents"]
            if amount is not None and (type(amount) is not int or not 0 <= amount <= MAX_AMOUNT_CENTS):
                raise ValueError(f"商机金额必须为 0 至 {MAX_AMOUNT_CENTS} 的整数分")
        if "aliases" in values:
            aliases = values["aliases"]
            if not isinstance(aliases, list) or len(aliases) > 20:
                raise ValueError("客户简称最多 20 个")
            aliases = [_text(value, "客户简称", 120, required=True).strip() for value in aliases]
            if len({value.casefold() for value in aliases}) != len(aliases):
                raise ValueError("客户简称不能重复")
            values["aliases"] = aliases
        if "contact_cycle_days" in values and values["contact_cycle_days"] is not None:
            days = values["contact_cycle_days"]
            if type(days) is not int or not 1 <= days <= 3650:
                raise ValueError("建议联系周期需要为 1 至 3650 天，或留空")
        if values.get("primary_contact_id") is not None:
            _identifier(values["primary_contact_id"])
        return values

    @staticmethod
    def _customer_sql_values(values):
        result = dict(values)
        if "amount_cents" in result:
            result["amount_known"] = int(result["amount_cents"] is not None)
            result["amount_cents"] = result["amount_cents"] if result["amount_known"] else 0
        if "aliases" in result:
            result["aliases_json"] = json.dumps(result.pop("aliases"), ensure_ascii=False)
        return result

    @staticmethod
    def _check_customer_names(db, owner, values, customer_id=None):
        current = db.execute("SELECT * FROM crm_customers WHERE owner=? AND id=?", (owner, customer_id)).fetchone() if customer_id else None
        name = values.get("name", current["name"] if current else "")
        aliases = values.get("aliases", json.loads(current["aliases_json"]) if current else [])
        normalized = {name.casefold(), *(alias.casefold() for alias in aliases)}
        if len(normalized) != len(aliases) + 1:
            raise ValueError("客户简称不能与客户名称重复")
        if "name" in values or "aliases" in values or current is None:
            for other in db.execute("SELECT id,name,aliases_json FROM crm_customers WHERE owner=?", (owner,)):
                if other["id"] == customer_id:
                    continue
                if normalized & {other["name"].casefold(), *(alias.casefold() for alias in json.loads(other["aliases_json"]))}:
                    raise ValueError("客户名称或简称与已有客户重复，请核对已有档案")
        contact_id = values.get("primary_contact_id")
        if contact_id is not None:
            if customer_id is None or not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='crm_contacts'").fetchone():
                raise ValueError("请先创建客户和联系人，再选择主联系人")
            if not db.execute("SELECT 1 FROM crm_contacts WHERE owner=? AND customer_id=? AND id=? AND archived=0",
                              (owner, customer_id, contact_id)).fetchone():
                raise ValueError("主联系人必须是该客户的有效联系人")

    def _insert_customer(self, db, owner, values, now):
        self._check_customer_names(db, owner, values)
        converted = self._customer_sql_values(values)
        keys = list(converted)
        return db.execute("INSERT INTO crm_customers(owner," + ",".join(keys) + ",created_at,updated_at) VALUES ("
                          + ",".join("?" for _ in range(len(keys) + 3)) + ")",
                          [owner] + [converted[key] for key in keys] + [now, now]).lastrowid

    def _update_customer(self, db, owner, customer_id, values, now):
        self._require_customer(db, owner, customer_id)
        self._check_customer_names(db, owner, values, customer_id)
        converted = self._customer_sql_values(values)
        current = db.execute("SELECT * FROM crm_customers WHERE owner=? AND id=?", (owner, customer_id)).fetchone()
        converted = {key: value for key, value in converted.items()
                     if ((json.loads(value) != json.loads(current[key])) if key == "aliases_json"
                         else value != current[key])}
        if converted:
            keys = list(converted)
            db.execute("UPDATE crm_customers SET " + ",".join(key + "=?" for key in keys) + ",updated_at=? WHERE owner=? AND id=?",
                       [converted[key] for key in keys] + [now, owner, customer_id])

    @staticmethod
    def _record_values(data, *, partial=False):
        if not isinstance(data, dict) or set(data) - _RECORD_FIELDS:
            raise ValueError("记录字段无效")
        values = dict(data) if partial else {"title": "", "content": "", "customer_id": None,
                                           "status": "unfiled", "kind": "note", "parent_record_id": None, **data}
        if "title" in values:
            values["title"] = _text(values["title"], "事项标题", 120, required=True).strip()
        if "content" in values:
            values["content"] = _text(values["content"], "记录内容", 20000)
        if "status" in values and values["status"] not in RECORD_STATUSES:
            raise ValueError("记录状态无效")
        if values.get("customer_id") is not None:
            _identifier(values["customer_id"])
        if "kind" in values and values["kind"] not in ("note", "action"):
            raise ValueError("记录类型必须为交流记录或待办")
        if "category" in values and (not isinstance(values['category'], str) or values['category'] not in ('auto', *CATEGORIES)):
            raise ValueError("记录场景无效。")
        if values.get("parent_record_id") is not None:
            _identifier(values["parent_record_id"])
        return values

    @staticmethod
    def _require_customer(db, owner, customer_id):
        if customer_id is not None and not db.execute(
                "SELECT 1 FROM crm_customers WHERE owner=? AND id=?", (owner, customer_id)).fetchone():
            raise KeyError("未找到客户")

    @staticmethod
    def _require_record(db, owner, record_id):
        row = db.execute("SELECT * FROM crm_records WHERE owner=? AND id=? AND hidden=0",
                         (owner, record_id)).fetchone()
        if row is None:
            raise KeyError("未找到记录")
        return row

    def create_customer(self, owner, data, now):
        owner, now = _owner(owner), _timestamp(now)
        values = self._customer_values(data)
        with self._transaction() as db:
            customer_id = self._insert_customer(db, owner, values, now)
        return self.get_customer(owner, customer_id)

    def update_customer(self, owner, customer_id, data, now):
        owner, customer_id, now = _owner(owner), _identifier(customer_id), _timestamp(now)
        values = self._customer_values(data, partial=True)
        with self._transaction() as db:
            self._update_customer(db, owner, customer_id, values, now)
        return self.get_customer(owner, customer_id)

    def get_customer(self, owner, customer_id):
        owner, customer_id = _owner(owner), _identifier(customer_id)
        with self._lock:
            return _public(self._db.execute(
                "SELECT c.*, (SELECT COUNT(*) FROM crm_records r WHERE r.owner=c.owner AND r.customer_id=c.id "
                "AND r.hidden=0) AS record_count FROM crm_customers c WHERE c.owner=? AND c.id=?",
                (owner, customer_id)).fetchone())

    def list_customers(self, owner, q="", stage="", page=1, page_size=50):
        owner, offset, search = _owner(owner), _pagination(page, page_size), _search(q)
        if stage not in ("", *STAGES):
            raise ValueError("销售阶段无效")
        where, params = "c.owner=?", [owner]
        if q.strip():
            where += " AND (c.name LIKE ? ESCAPE '\\' OR c.contact LIKE ? ESCAPE '\\' OR c.phone LIKE ? ESCAPE '\\' "
            where += "OR EXISTS (SELECT 1 FROM json_each(c.aliases_json) j WHERE j.value LIKE ? ESCAPE '\\'))"
            params += [search] * 4
        if stage:
            where += " AND c.stage=?"
            params.append(stage)
        with self._lock:
            total = self._db.execute("SELECT COUNT(*) FROM crm_customers c WHERE " + where, params).fetchone()[0]
            rows = self._db.execute(
                "SELECT c.*, (SELECT COUNT(*) FROM crm_records r WHERE r.owner=c.owner AND r.customer_id=c.id "
                "AND r.hidden=0) AS record_count FROM crm_customers c WHERE " + where
                + " ORDER BY c.updated_at DESC,c.id DESC LIMIT ? OFFSET ?", params + [page_size, offset]).fetchall()
            return _paged(rows, total, page, page_size)

    def create_record(self, owner, data, now):
        owner, now = _owner(owner), _timestamp(now)
        values = self._record_values(data)
        with self._transaction() as db:
            self._require_customer(db, owner, values["customer_id"])
            if values["parent_record_id"] is not None:
                parent = self._require_record(db, owner, values["parent_record_id"])
                if values["customer_id"] is None:
                    values["customer_id"] = parent["customer_id"]
                if parent["customer_id"] != values["customer_id"]:
                    raise ValueError("后续交流必须与来源记录属于同一客户")
            category = resolve_category(values.get('category', 'auto'), values['title'] + '\n' + values['content'], values['customer_id'])
            if values['parent_record_id'] is not None and 'category' not in data:
                category = parent['category']
            record_id = db.execute(
                "INSERT INTO crm_records(owner,title,content,original_content,source,status,customer_id,classified,"
                "kind,parent_record_id,category,created_at,updated_at) VALUES (?,?,?,?,'web',?,?,1,?,?,?,?,?)",
                (owner, values["title"], values["content"], values["content"], values["status"],
                 values["customer_id"], values["kind"], values["parent_record_id"], category, now, now)).lastrowid
        return self.get_record(owner, record_id)

    def update_record(self, owner, record_id, data, now, *, expected_updated_at=None):
        with self._transaction() as db:
            self._update_record(db, owner, record_id, data, now, expected_updated_at=expected_updated_at)
        return self.get_record(owner, record_id)

    def _update_record(self, db, owner, record_id, data, now, *, expected_updated_at=None):
        """Reuse record edit invariants inside an existing atomic business write."""
        owner, record_id, now = _owner(owner), _identifier(record_id), _timestamp(now)
        values = self._record_values(data, partial=True)
        expected = _timestamp(expected_updated_at) if expected_updated_at is not None else None
        record = self._require_record(db, owner, record_id)
        if expected is not None and expected != record['updated_at']:
            raise RecordConflict('这条记录已被其他页面更新，请刷新核对；你的输入没有覆盖已保存内容。')
        if 'category' in values:
            values['category'] = resolve_category(values['category'],
                values.get('title', record['title']) + '\n' + values.get('content', record['content']),
                values.get('customer_id', record['customer_id']))
        self._require_customer(db, owner, values.get("customer_id"))
        if "parent_record_id" in values and values["parent_record_id"] != record["parent_record_id"]:
            raise ValueError("来源交流建立后不能改换，请新建一条后续交流")
        values = {key: value for key, value in values.items() if value != record[key]}
        if values:
            guard = getattr(self, '_record_title_relation_guard', None)
            title_plan = (guard.prepare_record_title_rebase(db, owner, record)
                          if guard and set(values) == {'title'} and record['kind'] == 'note'
                          and record['content'].strip() and not record['hidden'] else None)
            now = max(now, record['updated_at'] + 0.000001)
            keys = list(values)
            db.execute("UPDATE crm_records SET " + ",".join(key + "=?" for key in keys)
                       + ",updated_at=? WHERE owner=? AND id=?",
                       [values[key] for key in keys] + [now, owner, record_id])
            if 'content' in values:
                db.execute('UPDATE crm_record_transcripts SET corrected_text=?,updated_at=? '
                           'WHERE owner=? AND record_id=?',(values['content'],now,owner,record_id))
            if "title" in values and record["proposal_id"] is not None:
                db.execute("UPDATE proposals SET title=?,updated_at=? WHERE owner=? AND id=? AND status='pending' AND target_task_id IS NULL",
                           (values["title"], now, owner, record["proposal_id"]))
            if values.get("status") == "done":
                db.execute("UPDATE proposals SET status='rejected',updated_at=? WHERE owner=? AND status='pending' "
                           "AND (id=? OR id IN (SELECT proposal_id FROM crm_record_proposals WHERE owner=? AND record_id=?))",
                           (now, owner, record["proposal_id"], owner, record_id))
            if title_plan:
                guard.apply_record_title_rebase(db, owner, record,
                    self._require_record(db, owner, record_id), title_plan, now)
        return self._require_record(db, owner, record_id)

    _RECORD_SELECT = (
        "SELECT r.*,c.name AS customer_name,COALESCE(t.remind_at,p.remind_at) AS remind_at,"
        "t.status AS task_status,p.status AS proposal_status,p.remind_at AS proposal_remind_at,"
        "p.updated_at AS proposal_updated_at,t.id AS task_id FROM crm_records r "
        "LEFT JOIN crm_customers c ON c.id=r.customer_id AND c.owner=r.owner "
        "LEFT JOIN proposals p ON p.id=r.proposal_id AND p.owner=r.owner "
        "LEFT JOIN tasks t ON t.id=COALESCE(p.task_id,p.target_task_id) AND t.owner=r.owner ")

    def get_record(self, owner, record_id):
        owner, record_id = _owner(owner), _identifier(record_id)
        with self._lock:
            record = _public(self._db.execute(self._RECORD_SELECT + "WHERE r.owner=? AND r.id=? AND r.hidden=0",
                                           (owner, record_id)).fetchone())
            if record is not None:
                row = self._db.execute('SELECT terms_json FROM crm_action_terms WHERE owner=? AND record_id=?',
                                       (owner, record_id)).fetchone()
                record['action_terms'] = json.loads(row['terms_json']) if row else {}
                stamp = self._db.execute('SELECT updated_at FROM crm_action_terms WHERE owner=? AND record_id=?',
                                         (owner,record_id)).fetchone()
                record['terms_updated_at'] = stamp['updated_at'] if stamp else None
            return record

    def save_action_terms(self, owner, record_id, terms, now, *, expected_updated_at=None, explicit=False):
        from .action_contract import TERM_FIELDS
        owner, record_id, now = _owner(owner), _identifier(record_id), _timestamp(now)
        if not isinstance(terms, dict) or set(terms)-set(TERM_FIELDS):
            raise ValueError('行动时间与责任字段无效')
        if terms.get('executor_kind', 'unknown') not in ('self', 'customer', 'team', 'unknown'):
            raise ValueError('执行主体无效')
        for key in ('execution_at', 'deadline_at', 'check_at'):
            if terms.get(key) is not None: _timestamp(terms[key])
        for key in ('deadline_date','check_date'):
            if terms.get(key) is not None:
                from datetime import date
                value=terms[key]
                if not isinstance(value,str) or date.fromisoformat(value).isoformat()!=value:
                    raise ValueError('截止／检查日期需要为YYYY-MM-DD')
        duration = terms.get('duration_minutes')
        if duration is not None and (type(duration) is not int or not 5 <= duration <= 720):
            raise ValueError('预计用时需要为5至720分钟')
        for key in TERM_FIELDS:
            if key.endswith('_evidence') and key in terms:
                _text(terms[key] or '', '行动依据', 2000)
        with self._transaction() as db:
            self._require_record(db, owner, record_id)
            prior=db.execute('SELECT updated_at FROM crm_action_terms WHERE owner=? AND record_id=?',
                             (owner,record_id)).fetchone()
            if explicit:
                if expected_updated_at!=(prior['updated_at'] if prior else None):
                    raise ValueError('行动资料已有变化，请刷新后核对')
                if prior:now=max(now,prior['updated_at']+0.000001)
            elif prior:
                return self.get_record(owner,record_id)
            db.execute('INSERT INTO crm_action_terms VALUES (?,?,?,?) ON CONFLICT(owner,record_id) '
                       'DO UPDATE SET terms_json=excluded.terms_json,updated_at=excluded.updated_at',
                       (owner,record_id,json.dumps(terms,ensure_ascii=False,allow_nan=False),now))
        return self.get_record(owner, record_id)

    def save_transcript(self, owner, record_id, original, corrected, now):
        owner,record_id,now = _owner(owner),_identifier(record_id),_timestamp(now)
        original = _text(original,'原始转写',20000)
        corrected = _text(corrected,'校对转写',20000)
        with self._transaction() as db:
            self._require_record(db,owner,record_id)
            prior = db.execute('SELECT original_text FROM crm_record_transcripts WHERE owner=? AND record_id=?',
                               (owner,record_id)).fetchone()
            if prior and prior['original_text'] != original:
                raise ValueError('原始转写已保存，不能覆盖；请保留原文并修改校对版本')
            db.execute('INSERT INTO crm_record_transcripts VALUES (?,?,?,?,?,?) '
                       'ON CONFLICT(owner,record_id) DO UPDATE SET corrected_text=excluded.corrected_text,updated_at=excluded.updated_at',
                       (owner,record_id,original,corrected,now,now))

    def _historical_material_note_ids(self, db, owner):
        """Project untouched generated history out of ordinary record lists."""
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"crm_materials", "crm_material_jobs"} <= tables:
            return []
        materials = {row["id"]: row["record_id"] for row in db.execute(
            "SELECT m.id,m.record_id FROM crm_materials m JOIN crm_records r "
            "ON r.owner=m.owner AND r.id=m.record_id WHERE m.owner=? "
            "AND m.duplicate_of IS NULL AND r.hidden=0", (owner,))}
        generated = set()
        for job in db.execute("SELECT material_id,input_key,analysis_json FROM crm_material_jobs "
                              "WHERE owner=? AND analysis_json IS NOT NULL", (owner,)):
            try:
                analysis = json.loads(job["analysis_json"])
            except (ValueError, TypeError):
                continue
            if isinstance(analysis, dict) and type(analysis.get("_record_id")) is int:
                generated.add((job["material_id"], job["input_key"], analysis["_record_id"]))
        protected = {row[0] for row in db.execute(
            "SELECT parent_record_id FROM crm_records WHERE owner=? AND parent_record_id IS NOT NULL", (owner,))}
        for table in ("crm_activities", "crm_analyses", "crm_record_transcripts"):
            if table in tables:
                protected.update(row[0] for row in db.execute("SELECT record_id FROM " + table + " WHERE owner=?", (owner,)))
        if "crm_opportunity_links" in tables:
            protected.update(row[0] for row in db.execute(
                "SELECT entity_id FROM crm_opportunity_links WHERE owner=? AND entity_type='record'", (owner,)))
        if "crm_timeline_contexts" in tables:
            for row in db.execute("SELECT event_key FROM crm_timeline_contexts WHERE owner=?", (owner,)):
                key = re.fullmatch(r"record:([1-9][0-9]*)", row[0])
                if key:
                    protected.add(int(key[1]))
        excluded = []
        for row in db.execute("SELECT id,source_id FROM crm_records WHERE owner=? AND hidden=0 "
                              "AND kind='note' AND status='unfiled' AND updated_at=created_at "
                              "AND parent_record_id IS NULL AND proposal_id IS NULL", (owner,)):
            source = re.fullmatch(r"material:([1-9][0-9]*):([0-9a-f]{64}):note", row["source_id"] or "")
            if not source or row["id"] in protected:
                continue
            identifier = int(source[1])
            if (identifier in materials and materials[identifier] != row["id"]
                    and (identifier, source[2], row["id"]) in generated):
                excluded.append(row["id"])
        return excluded

    def list_records(self, owner, q="", status="", customer_id=None, page=1, page_size=50, kind="", queue="", now=None, category=""):
        owner, offset, search = _owner(owner), _pagination(page, page_size), _search(q)
        if status not in ("", *RECORD_STATUSES):
            raise ValueError("记录状态无效")
        where, params = "r.owner=? AND r.hidden=0", [owner]
        if category:
            resolve_category(category)
            where += " AND r.category=?"
            params.append(category)
        if q.strip():
            where += " AND (r.title LIKE ? ESCAPE '\\' OR r.content LIKE ? ESCAPE '\\' OR c.name LIKE ? ESCAPE '\\')"
            params += [search] * 3
        if status:
            where += " AND r.status=?"
            params.append(status)
        if kind not in ("", "note", "action"):
            raise ValueError("记录类型无效")
        if kind:
            where += " AND r.kind=?"
            params.append(kind)
        if queue not in ("", "needs_time", "pending_schedule"):
            raise ValueError("记录队列无效")
        if queue:
            where += " AND r.kind='action' AND r.status!='done'"
            if queue == "needs_time":
                # A customer's or colleague's promise is a waiting item, rather
                # than a request to allocate the current user's execution time.
                # Missing/unknown responsibility remains available for review.
                where += " AND NOT EXISTS (SELECT 1 FROM crm_action_terms qa WHERE qa.owner=r.owner "
                where += "AND qa.record_id=r.id AND json_extract(qa.terms_json,'$.executor_kind') IN ('customer','team'))"
                where += " AND NOT EXISTS (SELECT 1 FROM proposals qp WHERE qp.owner=r.owner AND qp.id=r.proposal_id "
                where += "AND ((qp.status='pending' AND qp.remind_at IS NOT NULL) OR EXISTS (SELECT 1 FROM tasks qt "
                where += "WHERE qt.owner=qp.owner AND qt.id=COALESCE(qp.task_id,qp.target_task_id) AND qt.status='pending')))"
            else:
                where += " AND EXISTS (SELECT 1 FROM proposals qp WHERE qp.owner=r.owner AND qp.id=r.proposal_id "
                where += "AND qp.status='pending' AND qp.remind_at IS NOT NULL)"
        if customer_id is not None:
            where += " AND r.customer_id=?"
            params.append(_identifier(customer_id))
        with self._lock:
            historical = self._historical_material_note_ids(self._db, owner)
            if historical:
                where += " AND r.id NOT IN (SELECT value FROM json_each(?))"
                params.append(json.dumps(historical))
            total = self._db.execute(
                "SELECT COUNT(*) FROM crm_records r LEFT JOIN crm_customers c ON c.id=r.customer_id "
                "AND c.owner=r.owner WHERE " + where, params).fetchone()[0]
            rows = self._db.execute(self._RECORD_SELECT + "WHERE " + where
                                   + " ORDER BY r.updated_at DESC,r.id DESC LIMIT ? OFFSET ?",
                                   params + [page_size, offset]).fetchall()
            result=_paged(rows,total,page,page_size)
            for item in result['items']:
                metadata=self._db.execute('SELECT terms_json,updated_at FROM crm_action_terms WHERE owner=? AND record_id=?',
                                          (owner,item['id'])).fetchone()
                item['action_terms']=json.loads(metadata['terms_json']) if metadata else {}
                item['terms_updated_at']=metadata['updated_at'] if metadata else None
            return result

    def add_activity(self, owner, record_id, content, now):
        owner, record_id, now = _owner(owner), _identifier(record_id), _timestamp(now)
        content = _text(content, "跟进内容", 20000, required=True).strip()
        with self._transaction() as db:
            self._require_record(db, owner, record_id)
            activity_id = db.execute(
                "INSERT INTO crm_activities(owner,record_id,content,created_at) VALUES (?,?,?,?)",
                (owner, record_id, content, now)).lastrowid
            db.execute("UPDATE crm_records SET updated_at=? WHERE owner=? AND id=?", (now, owner, record_id))
            return _public(db.execute("SELECT * FROM crm_activities WHERE id=? AND owner=?",
                                      (activity_id, owner)).fetchone())

    def record_detail(self, owner, record_id):
        with self._lock:
            record = self.get_record(owner, record_id)
            if record is None:
                return None
            activities = self._db.execute(
                "SELECT * FROM crm_activities WHERE owner=? AND record_id=? ORDER BY id DESC LIMIT 200",
                (owner, record_id)).fetchall()
            total = self._db.execute("SELECT COUNT(*) FROM crm_activities WHERE owner=? AND record_id=?",
                                     (owner, record_id)).fetchone()[0]
            proposal = self.get_proposal(owner, record["proposal_id"]) if record["proposal_id"] else None
            task_id = (proposal["task_id"] or proposal.get("target_task_id")) if proposal else None
            task = self.get_task(owner, task_id) if task_id else None
            transcript = self._db.execute('SELECT original_text,corrected_text,created_at,updated_at '
                'FROM crm_record_transcripts WHERE owner=? AND record_id=?',(owner,record_id)).fetchone()
            return {"record": record, "activities": [_public(row) for row in activities],
                    "activities_total": total, "proposal": proposal, "task": task,
                    "transcript": dict(transcript) if transcript else None,
                    "analysis": self.get_analysis(owner, record_id)}

    @staticmethod
    def _analysis_values(data):
        from .action_contract import TERM_FIELDS
        fields = {"summary", "key_points", "open_questions", "actions", "input_fingerprint"}
        if not isinstance(data, dict) or set(data) - fields:
            raise ValueError("交流整理字段无效")
        result = {"summary": _text(data.get("summary"), "交流摘要", 4000, required=True).strip()}
        for key in ("key_points", "open_questions"):
            items = data.get(key, [])
            if not isinstance(items, list) or len(items) > 12:
                raise ValueError("要点及待澄清问题各最多 12 条")
            result[key] = [_text(item, "交流要点", 1000, required=True).strip() for item in items]
        fingerprint = data.get("input_fingerprint")
        if not isinstance(fingerprint, str) or not re.fullmatch("[0-9a-f]{64}", fingerprint):
            raise ValueError("缺少有效的交流原文版本，请重新整理")
        result["input_fingerprint"] = fingerprint
        actions = data.get("actions", [])
        if not isinstance(actions, list) or len(actions) > 6:
            raise ValueError("每次交流最多整理 6 项待办或建议")
        result["actions"] = []
        for number, action in enumerate(actions, start=1):
            allowed = {"title", "kind", "reason", "owner_hint", "remind_at", "evidence", "time_evidence"} | set(TERM_FIELDS)
            if not isinstance(action, dict) or set(action) - allowed:
                raise ValueError("待办或建议字段无效")
            if action.get("kind") not in ("commitment", "suggestion"):
                raise ValueError("事项类型必须为明确待办或 AI 建议")
            remind_at = action.get("remind_at")
            if remind_at is not None:
                remind_at = _timestamp(remind_at)
            result["actions"].append({
                "id": number,
                "title": _text(action.get("title"), "待办标题", 120, required=True).strip(),
                "kind": action["kind"],
                "reason": _text(action.get("reason", ""), "建议原因", 2000).strip(),
                "owner_hint": _text(action.get("owner_hint", ""), "负责人提示", 120).strip(),
                "remind_at": remind_at,
                **{key: action[key] for key in ('evidence','time_evidence',*TERM_FIELDS) if key in action},
            })
        return result

    def save_analysis(self, owner, record_id, data, now):
        """Replace unconfirmed drafts without discarding adopted work or history."""
        owner, record_id, now = _owner(owner), _identifier(record_id), _timestamp(now)
        analysis = self._analysis_values(data)
        fingerprint = analysis.pop("input_fingerprint")
        with self._transaction() as db:
            record = self._require_record(db, owner, record_id)
            if analysis_fingerprint(record) != fingerprint:
                raise ValueError("交流记录已修改，请根据最新内容重新整理")
            from .action_contract import derive_terms
            for action in analysis['actions']:
                action.update(derive_terms(action, record['content'],now=now))
            previous = db.execute("SELECT version FROM crm_analyses WHERE owner=? AND record_id=?",
                                  (owner, record_id)).fetchone()
            previous_version = previous["version"] if previous else 0
            version = previous_version + 1
            if db.execute("SELECT 1 FROM crm_analysis_actions a JOIN crm_records r ON r.owner=a.owner AND r.id=a.child_record_id "
                          "JOIN proposals p ON p.owner=r.owner AND (p.id=r.proposal_id OR p.id IN "
                          "(SELECT proposal_id FROM crm_record_proposals h WHERE h.owner=r.owner AND h.record_id=r.id)) "
                          "JOIN tasks t ON t.owner=p.owner AND t.id=COALESCE(p.task_id,p.target_task_id) "
                          "WHERE a.owner=? AND a.parent_record_id=? AND t.status='pending' LIMIT 1",
                          (owner, record_id)).fetchone():
                raise ValueError("本次整理已有已确认的活动提醒，请先完成或取消提醒，再新建后续交流记录")
            children = db.execute("SELECT r.* FROM crm_analysis_actions a JOIN crm_records r "
                                  "ON r.owner=a.owner AND r.id=a.child_record_id "
                                  "WHERE a.owner=? AND a.parent_record_id=? AND a.action_id>?",
                                  (owner, record_id, (previous_version - 1) * 6)).fetchall()
            for child in children:
                proposals = db.execute("SELECT p.*,t.status AS task_status FROM proposals p "
                                       "LEFT JOIN tasks t ON t.owner=p.owner AND t.id=COALESCE(p.task_id,p.target_task_id) "
                                       "WHERE p.owner=? AND (p.id=? OR p.id IN (SELECT proposal_id FROM crm_record_proposals "
                                       "WHERE owner=? AND record_id=?))",
                                       (owner, child["proposal_id"], owner, child["id"])).fetchall()
                if any(p["task_status"] == "pending" for p in proposals):
                    raise ValueError("本次整理已有已确认的活动提醒，请先完成或取消提醒，再新建后续交流记录")
                if not any(p["status"] == "pending" and p["task_id"] is None and p["target_task_id"] is None for p in proposals):
                    raise ValueError("本次整理已有事项被采纳，请新建后续交流记录，避免覆盖已采纳的待办")
            for child in children:
                db.execute("UPDATE proposals SET status='rejected',updated_at=? WHERE owner=? AND status='pending' "
                           "AND (id=? OR id IN (SELECT proposal_id FROM crm_record_proposals WHERE owner=? AND record_id=?))",
                           (now, owner, child["proposal_id"], owner, child["id"]))
                db.execute("UPDATE crm_records SET status='done',superseded_by_analysis_version=?,updated_at=? "
                           "WHERE owner=? AND id=?", (version, now, owner, child["id"]))
            db.execute(
                "INSERT INTO crm_analyses(owner,record_id,data_json,input_fingerprint,version,created_at,updated_at) "
                "VALUES (?,?,?,?,1,?,?) ON CONFLICT(owner,record_id) DO UPDATE SET "
                "data_json=excluded.data_json,input_fingerprint=excluded.input_fingerprint,"
                "version=crm_analyses.version+1,updated_at=excluded.updated_at",
                (owner, record_id, json.dumps(analysis, ensure_ascii=False, separators=(",", ":")), fingerprint, now, now))
        return self.get_analysis(owner, record_id)

    def get_analysis(self, owner, record_id):
        owner, record_id = _owner(owner), _identifier(record_id)
        with self._lock:
            record = self.get_record(owner, record_id)
            if record is None:
                return None
            row = self._db.execute("SELECT * FROM crm_analyses WHERE owner=? AND record_id=?",
                                   (owner, record_id)).fetchone()
            if row is None:
                return None
            result = json.loads(row["data_json"])
            adopted = dict(self._db.execute(
                "SELECT action_id,child_record_id FROM crm_analysis_actions WHERE owner=? AND parent_record_id=?",
                (owner, record_id)).fetchall())
            offset = (row["version"] - 1) * 6
            for action in result["actions"]:
                action["id"] += offset
                action["adopted_record_id"] = adopted.get(action["id"])
                action["record_id"] = action["adopted_record_id"]
            previous_actions = [_public(item) for item in self._db.execute(
                "SELECT a.action_id,a.child_record_id AS record_id,r.title,r.status,r.superseded_by_analysis_version,"
                "((a.action_id-1)/6)+1 AS analysis_version FROM crm_analysis_actions a JOIN crm_records r "
                "ON r.owner=a.owner AND r.id=a.child_record_id WHERE a.owner=? AND a.parent_record_id=? "
                "AND a.action_id<=? ORDER BY a.action_id DESC LIMIT 100", (owner, record_id, offset))]
            return {**result, "input_fingerprint": row["input_fingerprint"],
                    "previous_actions": previous_actions,
                    "stale": row["input_fingerprint"] != analysis_fingerprint(record),
                    "version": row["version"], "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def adopt_action(self, owner, record_id, action_id, now, *, expected_visit_revision=None):
        """Explicitly adopt one suggestion as an action, never as an active reminder."""
        owner, record_id, now = _owner(owner), _identifier(record_id), _timestamp(now)
        action_id = _identifier(action_id)
        with self._lock:
            preview = self.get_analysis(owner, record_id)
            selected = next((item for item in (preview or {}).get('actions', []) if item['id'] == action_id), None)
            visits = getattr(self, 'visit_service', None)
            context = visits.record_action_context(owner, record_id, selected) if visits else None
        with self._transaction() as db:
            parent = self._require_record(db, owner, record_id)
            existing = db.execute(
                "SELECT child_record_id FROM crm_analysis_actions WHERE owner=? AND parent_record_id=? AND action_id=?",
                (owner, record_id, action_id)).fetchone()
            if existing:
                return self.get_record(owner, existing["child_record_id"])
            if context:
                latest = visits._fingerprint(db, visits._require(db, owner, context['visit_id']))
                if (latest != context['revision'] or
                        (expected_visit_revision is not None and expected_visit_revision != latest)):
                    raise ValueError('关联交流已变化，请刷新后核对完整来源')
                if context['blocked']:
                    raise ValueError('；'.join(context['reasons']))
            analysis = self.get_analysis(owner, record_id)
            if analysis is None:
                raise KeyError("尚未整理本次交流")
            if analysis["stale"]:
                raise ValueError("交流记录已修改，这项建议已过期，请重新整理或新建后续交流记录")
            action = next((item for item in analysis["actions"] if item["id"] == action_id), None)
            if action is None:
                raise KeyError("未找到本次交流中的待办或建议")
            kind = "明确待办" if action["kind"] == "commitment" else "AI 建议（用户已采纳）"
            content = (f"来源交流：#{record_id} {parent['title']}\n"
                       f"整理类型：{kind}\n"
                       f"事项：{action['title']}\n"
                       f"整理依据：{action['reason']}\n"
                       f"负责人提示：{action['owner_hint'] or '待明确'}")
            child_id = db.execute(
                "INSERT INTO crm_records(owner,title,content,original_content,source,status,customer_id,classified,"
                "kind,parent_record_id,category,created_at,updated_at) VALUES (?,?,?,?,'web','following',?,1,'action',?,?,?,?)",
                (owner, action["title"], content, content, parent["customer_id"], record_id, parent['category'], now, now)).lastrowid
            db.execute("INSERT INTO crm_analysis_actions(owner,parent_record_id,action_id,child_record_id,created_at) "
                       "VALUES (?,?,?,?,?)", (owner, record_id, action_id, child_id, now))
            from .action_contract import derive_terms
            terms = derive_terms(action, parent['content'])
            db.execute('INSERT INTO crm_action_terms VALUES (?,?,?,?)',
                       (owner,child_id,json.dumps(terms,ensure_ascii=False),now))
        return self.get_record(owner, child_id)

    def get_proposal(self, owner, proposal_id):
        owner, proposal_id = _owner(owner), _identifier(proposal_id)
        with self._lock:
            return _public(self._db.execute("SELECT * FROM proposals WHERE owner=? AND id=?",
                                           (owner, proposal_id)).fetchone())

    def get_task(self, owner, task_id):
        owner, task_id = _owner(owner), _identifier(task_id)
        with self._lock:
            return _public(self._db.execute("SELECT * FROM tasks WHERE owner=? AND id=?",
                                           (owner, task_id)).fetchone())

    @staticmethod
    def _remember_proposal(db, owner, record_id, proposal_id, now):
        existing = db.execute("SELECT owner,record_id FROM crm_record_proposals WHERE proposal_id=?",
                              (proposal_id,)).fetchone()
        if existing:
            if existing["owner"] != owner or existing["record_id"] != record_id:
                raise ValueError("提案已关联另一条记录")
            return
        db.execute("INSERT INTO crm_record_proposals(proposal_id,owner,record_id,created_at) VALUES (?,?,?,?)",
                   (proposal_id, owner, record_id, now))

    def link_proposal(self, owner, record_id, proposal_id, now):
        owner, record_id, proposal_id = _owner(owner), _identifier(record_id), _identifier(proposal_id)
        now = _timestamp(now)
        with self._transaction() as db:
            record = self._require_record(db, owner, record_id)
            proposal = db.execute("SELECT * FROM proposals WHERE owner=? AND id=?", (owner, proposal_id)).fetchone()
            if proposal is None:
                raise KeyError("未找到提案")
            if proposal["title"] != record["title"] and proposal["target_task_id"] is None:
                raise ValueError("提案与记录标题不一致")
            if proposal["target_task_id"] is not None and not db.execute(
                    "SELECT 1 FROM proposals p WHERE p.owner=? AND p.task_id=? AND (p.id=? OR p.id IN "
                    "(SELECT proposal_id FROM crm_record_proposals WHERE owner=? AND record_id=?))",
                    (owner, proposal["target_task_id"], record["proposal_id"], owner, record_id)).fetchone():
                raise ValueError("变更提案的原提醒不属于本记录")
            if record["proposal_id"] is not None:
                self._remember_proposal(db, owner, record_id, record["proposal_id"], now)
            self._remember_proposal(db, owner, record_id, proposal_id, now)
            db.execute("UPDATE crm_records SET proposal_id=?,kind='action',updated_at=? WHERE owner=? AND id=?",
                       (proposal_id, now, owner, record_id))
        return self.get_record(owner, record_id)

    def capture_message(self, owner, source_id, text, source, now):
        owner, now = _owner(owner), _timestamp(now)
        source_id = _text(source_id, "消息编号", 512, required=True)
        text = _text(text, "原始内容", 20000, required=True)
        if source not in ("voice", "text", "web"):
            raise ValueError("消息来源无效")
        with self._transaction() as db:
            previous = db.execute("SELECT * FROM crm_records WHERE owner=? AND source_id=?",
                                  (owner, source_id)).fetchone()
            if previous:
                return _public(previous)
            record_id = db.execute(
                "INSERT INTO crm_records(owner,source_id,title,content,original_content,source,category,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)", (owner, source_id, text.strip()[:120], text, text, source, infer_category(text), now, now)).lastrowid
        return self.get_record(owner, record_id)

    def apply_command(self, owner, source_id, command, reply, now):
        """Classify after parsing. A failed/unknown parse remains a visible note.

        A proposed title is associated only with an existing, same-owner proposal
        returned in Store's success prefix and with the same validated title.
        Replays never replace subsequent human edits.
        """
        owner, now = _owner(owner), _timestamp(now)
        source_id = _text(source_id, "消息编号", 512, required=True)
        with self._transaction() as db:
            record = db.execute("SELECT * FROM crm_records WHERE owner=? AND source_id=?",
                                (owner, source_id)).fetchone()
            if record is None:
                raise KeyError("未找到消息记录")
            if record["classified"]:
                return self.get_record(owner, record["id"])
            action = command.get("action") if isinstance(command, dict) else None
            if isinstance(action, str) and action in _COMMAND_ACTIONS:
                db.execute("UPDATE crm_records SET hidden=1,classified=1 WHERE owner=? AND id=?",
                           (owner, record["id"]))
                return None
            if action != "propose":
                return self.get_record(owner, record["id"])
            title = command.get("title")
            if not isinstance(title, str) or not title.strip() or len(title.strip()) > 120:
                return self.get_record(owner, record["id"])
            match = re.match(r"^已整理，待你确认：P([1-9][0-9]*)\s", reply) if isinstance(reply, str) else None
            proposal_id = int(match[1]) if match and len(match[1]) <= 19 else None
            if proposal_id is not None and proposal_id > 2**63 - 1:
                proposal_id = None
            proposal = db.execute("SELECT * FROM proposals WHERE owner=? AND id=? AND title=?",
                                  (owner, proposal_id, title.strip())).fetchone() if proposal_id else None
            if proposal:
                linked = db.execute(
                    "SELECT r.* FROM crm_record_proposals h JOIN crm_records r ON r.id=h.record_id AND r.owner=h.owner "
                    "WHERE h.owner=? AND h.proposal_id=?", (owner, proposal_id)).fetchone()
                if linked and linked["id"] != record["id"]:
                    # Recovery after Store committed but before capture classification:
                    # startup may already have imported this proposal. Enrich that row
                    # with the recovered original, preserving later human edits.
                    if linked["source"] == "legacy":
                        content = record["original_content"] if linked["content"] == proposal["title"] else linked["content"]
                        db.execute("UPDATE crm_records SET original_content=?,content=?,source=?,updated_at=? "
                                   "WHERE owner=? AND id=?", (record["original_content"], content, record["source"],
                                                              now, owner, linked["id"]))
                    db.execute("UPDATE crm_records SET hidden=1,classified=1 WHERE owner=? AND id=?",
                               (owner, record["id"]))
                    return self.get_record(owner, linked["id"])
                self._remember_proposal(db, owner, record["id"], proposal["id"], now)
            db.execute("UPDATE crm_records SET title=?,proposal_id=?,kind='action',classified=1,updated_at=? WHERE owner=? AND id=?",
                       (title.strip(), proposal["id"] if proposal else None, now, owner, record["id"]))
            return self.get_record(owner, record["id"])

    def recover_message(self, owner, source_id, reply, now):
        """Repair classification after Store committed but the process stopped.

        Only an exact, owner/source-bound cached result can classify the capture.
        No model or business command is re-run. Unknown results stay visible so
        a changed response format cannot silently discard a customer's note.
        """
        owner, now = _owner(owner), _timestamp(now)
        source_id = _text(source_id, "消息编号", 512, required=True)
        with self._lock:
            record = self._db.execute("SELECT * FROM crm_records WHERE owner=? AND source_id=?",
                                      (owner, source_id)).fetchone()
            if record is None:
                raise KeyError("未找到消息记录")
            if record["classified"]:
                return self.get_record(owner, record["id"])
            cached = self._db.execute("SELECT reply,created_at FROM command_results WHERE owner=? AND source_id=?",
                                      (owner, source_id)).fetchone()
            if not isinstance(reply, str) or cached is None or cached["reply"] != reply:
                return self.get_record(owner, record["id"])
            match = re.match(r"^已整理，待你确认：P([1-9][0-9]{0,18})\s", reply)
            if match:
                proposal_id = int(match[1])
                proposal = self.get_proposal(owner, proposal_id) if proposal_id <= 2**63 - 1 else None
                if proposal is None:
                    return self.get_record(owner, record["id"])
                # Creation and rescheduling use the same review prefix. Store
                # timestamps both the proposal and cached creation result with
                # the same `now`; a later result must be a reschedule command.
                command = ({"action": "propose", "title": proposal["title"]}
                           if proposal["created_at"] == cached["created_at"]
                           else {"action": "reschedule_proposal"})
                return self.apply_command(owner, source_id, command, reply, now)
            command_prefixes = (
                r"^随口说一件事，我会整理为待确认提案；",
                r"^当前没有(?:未完成事项|待确认提案)。$",
                r"^(?:未完成事项|待确认提案)（第 [0-9]+/[0-9]+ 页",
                r"^待办清单共 [0-9]+ 页，",
                r"^(?:今天|本周|本月)(?:安排｜|暂没有已确认安排。|安排只有 [0-9]+ 页，)",
                r"^提案 P[1-9][0-9]* (?:已确认，提醒已启用：|已确认，不会重复建立任务：|尚未确认：|未修改：)",
                r"^提案已取消(?:，未启用提醒)?：P[1-9][0-9]* ",
                r"^未找到你的(?:提案 P|任务 #)[1-9][0-9]*，",
                r"^(?:已完成|已取消|已调整提醒|该事项已完成|该事项已取消)：#[1-9][0-9]* ",
            )
            if any(re.match(prefix, reply) for prefix in command_prefixes):
                return self.apply_command(owner, source_id, {"action": "help"}, reply, now)
            return self.get_record(owner, record["id"])

    def import_legacy(self, owner, now):
        owner, now = _owner(owner), _timestamp(now)
        with self._transaction() as db:
            rows = db.execute(
                "SELECT p.* FROM proposals p WHERE p.owner=? AND p.status IN ('pending','confirmed') "
                "AND NOT EXISTS (SELECT 1 FROM crm_record_proposals h WHERE h.owner=p.owner AND h.proposal_id=p.id)",
                (owner,)).fetchall()
            for row in rows:
                record_id = db.execute(
                    "INSERT INTO crm_records(owner,title,content,original_content,source,proposal_id,classified,"
                    "kind,category,created_at,updated_at) VALUES (?,?,?,?,'legacy',?,1,'action',?,?,?)",
                    (owner, row["title"], row["title"], row["title"], row["id"], infer_category(row['title']), row["created_at"], now)).lastrowid
                self._remember_proposal(db, owner, record_id, row["id"], now)
            return len(rows)

    def dashboard(self, owner, now):
        owner, now = _owner(owner), _timestamp(now)
        start, end = (value.timestamp() for value in _window("day", datetime.fromtimestamp(now, SHANGHAI)))
        with self._lock:
            customers = self._db.execute("SELECT COUNT(*) FROM crm_customers WHERE owner=?", (owner,)).fetchone()[0]
            # Sum Python integers: many individual valid opportunities can exceed
            # SQLite's signed 64-bit SUM range without damaging the dashboard.
            pipeline = sum(row[0] for row in self._db.execute(
                "SELECT amount_cents FROM crm_customers WHERE owner=? AND amount_known=1 AND stage NOT IN ('won','lost')", (owner,)))
            unknown_amounts = self._db.execute("SELECT COUNT(*) FROM crm_customers WHERE owner=? AND amount_known=0 "
                                              "AND stage NOT IN ('won','lost')", (owner,)).fetchone()[0]
            historical = self._historical_material_note_ids(self._db, owner)
            counts = dict(self._db.execute("SELECT status,COUNT(*) FROM crm_records WHERE owner=? AND hidden=0 "
                                          "AND id NOT IN (SELECT value FROM json_each(?)) GROUP BY status",
                                          (owner, json.dumps(historical))).fetchall())
            overdue = self._db.execute("SELECT COUNT(*) FROM tasks WHERE owner=? AND status='pending' "
                                       "AND remind_at < ?", (owner, now)).fetchone()[0]
            today = self._db.execute("SELECT COUNT(*) FROM tasks WHERE owner=? AND status='pending' AND remind_at>=? AND remind_at<?",
                                     (owner, start, end)).fetchone()[0]
            overdue_items = [_public(row) for row in self._db.execute("SELECT * FROM tasks WHERE owner=? AND status='pending' "
                             "AND remind_at<? ORDER BY remind_at,id LIMIT 10", (owner, now))]
            today_items = [_public(row) for row in self._db.execute("SELECT * FROM tasks WHERE owner=? AND status='pending' "
                           "AND remind_at>=? AND remind_at<? ORDER BY remind_at,id LIMIT 10", (owner, start, end))]
            upcoming = [_public(row) for row in self._db.execute("SELECT * FROM tasks WHERE owner=? AND status='pending' AND remind_at>=? "
                                         "ORDER BY remind_at,id LIMIT 10", (owner, now))]
            needs_time = self.list_records(owner, queue="needs_time", page_size=10)
            pending_schedule = self.list_records(owner, queue="pending_schedule", page_size=10)
            return {"stats": {"customers": customers, "unfiled": counts.get("unfiled", 0),
                              "following": counts.get("following", 0), "overdue": overdue, "today": today,
                              "needs_time": needs_time["total"], "pending_schedule": pending_schedule["total"],
                              "pipeline_cents": pipeline, "unknown_amounts": unknown_amounts},
                    "recent": self.list_records(owner, page_size=10)["items"],
                    "upcoming": self._task_context(owner, upcoming),
                    "queues": {"overdue": {"total": overdue, "items": self._task_context(owner, overdue_items)},
                               "today": {"total": today, "items": self._task_context(owner, today_items)},
                               "needs_time": needs_time, "pending_schedule": pending_schedule}}

    def _task_context(self, owner, tasks):
        results = []
        for task in tasks:
            row = self._db.execute("SELECT r.id AS record_id,r.customer_id,r.kind AS record_kind,r.status AS record_status FROM proposals p "
                                  "JOIN crm_record_proposals h ON h.owner=p.owner AND h.proposal_id=p.id "
                                  "JOIN crm_records r ON r.owner=h.owner AND r.id=h.record_id "
                                  "WHERE p.owner=? AND COALESCE(p.task_id,p.target_task_id)=? AND r.hidden=0 ORDER BY h.created_at DESC LIMIT 1",
                                  (owner, task["id"])).fetchone()
            customer = self.get_customer(owner, row["customer_id"]) if row and row["customer_id"] else None
            results.append({**task, "record_id": row["record_id"] if row else None,
                            "record_kind": row["record_kind"] if row else None,
                            "record_status": row["record_status"] if row else None,
                            "customer_id": row["customer_id"] if row else None,
                            "customer_name": customer["name"] if customer else None,
                            "contact_hint": (customer.get("primary_contact_name", customer.get("contact", ""))) if customer else ""})
        return results

    def agenda(self, owner, period="day", date=None, page=1, page_size=200, now=None):
        owner, offset = _owner(owner), _pagination(page, page_size)
        if period not in ("day", "week", "month"):
            raise ValueError("日程范围必须为 day、week 或 month")
        if date is not None:
            if not isinstance(date, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", date):
                raise ValueError("日期格式应为 YYYY-MM-DD")
            try:
                current = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=SHANGHAI)
            except ValueError:
                raise ValueError("日期无效") from None
        else:
            current = datetime.fromtimestamp(_timestamp(time.time() if now is None else now), SHANGHAI)
        start, end = (value.timestamp() for value in _window(period, current))
        where = "owner=? AND status IN ('pending','completed') AND remind_at<? AND remind_at+duration_minutes*60>?"
        params = [owner, end, start]
        with self._lock:
            total = self._db.execute("SELECT COUNT(*) FROM tasks WHERE " + where, params).fetchone()[0]
            rows = self._db.execute("SELECT * FROM tasks WHERE " + where + " ORDER BY remind_at,id LIMIT ? OFFSET ?",
                                    params + [page_size, offset]).fetchall()
            result = _paged(rows, total, page, page_size)
            result["items"] = self._task_context(owner, result["items"])
            return {**result, "start": start, "end": end}
