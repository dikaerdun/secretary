"""Administrative account structure; purchasing power remains project-specific.

Additive metadata preserves every legacy customer/contact/project identifier.
An unconfigured legacy customer is an independent company at revision zero.
"""
from __future__ import annotations

import time

from .crm import _identifier, _owner, _public
from .store import _timestamp


UNIT_TYPES = ("group", "company", "department", "institution", "other")
MAX_DEPTH = 32


def unit_parents(db, owner):
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='crm_account_units'").fetchone()
    return {row["customer_id"]: row["parent_customer_id"] for row in db.execute(
        "SELECT customer_id,parent_customer_id FROM crm_account_units WHERE owner=?", (owner,))} if exists else {}


def root_id(parents, customer_id):
    seen = set()
    while parents.get(customer_id) is not None:
        if customer_id in seen or len(seen) >= MAX_DEPTH-1:
            raise ValueError("单位层级循环或超过32层，请先核对单位结构")
        seen.add(customer_id)
        customer_id = parents[customer_id]
    return customer_id


def same_tree_ids(db, owner, customer_id):
    parents = unit_parents(db, owner)
    root = root_id(parents, customer_id)
    return {row["id"] for row in db.execute("SELECT id FROM crm_customers WHERE owner=?", (owner,))
            if root_id(parents, row["id"]) == root}


class AccountNetwork:
    def __init__(self, crm, workspace, clock=None):
        self.crm, self.workspace, self.clock = crm, workspace, clock or workspace.clock or time.time
        with crm._transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS crm_account_units (
                owner TEXT NOT NULL, customer_id INTEGER NOT NULL, parent_customer_id INTEGER,
                unit_type TEXT NOT NULL DEFAULT 'company', revision INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY(owner,customer_id),
                FOREIGN KEY(owner,customer_id) REFERENCES crm_customers(owner,id),
                FOREIGN KEY(owner,parent_customer_id) REFERENCES crm_customers(owner,id),
                CHECK(customer_id<>parent_customer_id), CHECK(revision>0),
                CHECK(unit_type IN ('group','company','department','institution','other')))""")
            db.execute("CREATE INDEX IF NOT EXISTS crm_account_units_parent ON crm_account_units(owner,parent_customer_id)")
            db.execute("""CREATE TABLE IF NOT EXISTS crm_account_unit_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL,
                customer_id INTEGER NOT NULL,parent_customer_id INTEGER,unit_type TEXT NOT NULL,
                revision INTEGER NOT NULL,created_at REAL NOT NULL,
                FOREIGN KEY(owner,customer_id) REFERENCES crm_customers(owner,id))""")

    def _unit(self, db, owner, customer_id, parents=None):
        self.crm._require_customer(db, owner, customer_id)
        customer = db.execute("SELECT * FROM crm_customers WHERE owner=? AND id=?", (owner, customer_id)).fetchone()
        row = db.execute("SELECT * FROM crm_account_units WHERE owner=? AND customer_id=?", (owner, customer_id)).fetchone()
        parents = unit_parents(db, owner) if parents is None else parents
        return {**_public(customer), "customer_id": customer_id,
                "parent_customer_id": row["parent_customer_id"] if row else None,
                "unit_type": row["unit_type"] if row else "company",
                "unit_revision": row["revision"] if row else 0,
                "revision": row["revision"] if row else 0,
                "root_customer_id": root_id(parents, customer_id)}

    def units(self, owner):
        owner = _owner(owner)
        with self.crm._lock:
            parents = unit_parents(self.crm._db, owner)
            items = [self._unit(self.crm._db, owner, row["id"], parents) for row in
                     self.crm._db.execute("SELECT id FROM crm_customers WHERE owner=? ORDER BY id", (owner,)).fetchall()]
            return {"items": items, "total": len(items)}

    def create_unit(self, owner, customer_data, unit_data=None, now=None):
        """Create the legacy customer, primary contact and hierarchy atomically."""
        owner, now = _owner(owner), _timestamp(self.clock() if now is None else now)
        unit_data = {} if unit_data is None else unit_data
        if not isinstance(unit_data, dict) or set(unit_data)-{"parent_customer_id", "unit_type"}:
            raise ValueError("单位层级字段无效")
        values = self.crm._customer_values(customer_data)
        kind, parent = unit_data.get("unit_type", "company"), unit_data.get("parent_customer_id")
        if kind not in UNIT_TYPES:
            raise ValueError("单位类型无效")
        if parent is not None:
            parent = _identifier(parent)
        with self.crm._transaction() as db:
            if parent is not None:
                self.crm._require_customer(db, owner, parent)
            identifier = self.crm._insert_customer(db, owner, values, now)
            if self.workspace._exists(db, "crm_contacts") and values["contact"]:
                self.crm._insert_contact(db, owner, identifier,
                                         {"name": values["contact"], "phone": values["phone"], "role": ""}, now)
            if self.workspace._exists(db, "crm_customer_contact_migrations"):
                db.execute("INSERT INTO crm_customer_contact_migrations(owner,customer_id) VALUES (?,?)", (owner, identifier))
            parents = unit_parents(db, owner)
            parents[identifier] = parent
            root_id(parents, identifier)
            db.execute("INSERT INTO crm_account_units VALUES (?,?,?,?,?,?,?)", (owner, identifier, parent, kind, 1, now, now))
            db.execute("INSERT INTO crm_account_unit_history(owner,customer_id,parent_customer_id,unit_type,revision,created_at) VALUES (?,?,?,?,?,?)",
                       (owner, identifier, parent, kind, 1, now))
            return self._unit(db, owner, identifier, parents)

    def update(self, owner, customer_id, data):
        owner, customer_id, now = _owner(owner), _identifier(customer_id), _timestamp(self.clock())
        if not isinstance(data, dict) or set(data)-{"parent_customer_id", "unit_type", "expected_revision"}:
            raise ValueError("单位层级字段无效")
        with self.crm._transaction() as db:
            old = self._unit(db, owner, customer_id)
            expected = data.get("expected_revision")
            if type(expected) is not int or expected != old["revision"]:
                raise ValueError("单位层级已更新，请刷新后核对再保存")
            parent = data.get("parent_customer_id", old["parent_customer_id"])
            kind = data.get("unit_type", old["unit_type"])
            if kind not in UNIT_TYPES:
                raise ValueError("单位类型无效")
            if parent is not None:
                parent = _identifier(parent)
                self.crm._require_customer(db, owner, parent)
            parents = unit_parents(db, owner)
            previous = dict(parents)
            parents[customer_id] = parent
            # Validate the entire owner graph: moving a subtree can exceed depth.
            ids = [row["id"] for row in db.execute("SELECT id FROM crm_customers WHERE owner=?", (owner,))]
            for identifier in ids:
                root_id(parents, identifier)
            if (parent, kind) == (old["parent_customer_id"], old["unit_type"]):
                return old
            revision = expected+1
            db.execute("INSERT INTO crm_account_units VALUES (?,?,?,?,?,?,?) ON CONFLICT(owner,customer_id) DO UPDATE SET "
                       "parent_customer_id=excluded.parent_customer_id,unit_type=excluded.unit_type,revision=excluded.revision,updated_at=excluded.updated_at",
                       (owner, customer_id, parent, kind, revision, now, now))
            db.execute("INSERT INTO crm_account_unit_history(owner,customer_id,parent_customer_id,unit_type,revision,created_at) VALUES (?,?,?,?,?,?)",
                       (owner, customer_id, parent, kind, revision, now))
            # Invalidate only projects whose permissible unit tree changed.
            affected = {customer_id}
            if parent != old["parent_customer_id"]:
                roots = {root_id(previous, customer_id), root_id(parents, customer_id)}
                affected.update(identifier for identifier in ids if root_id(previous, identifier) in roots or root_id(parents, identifier) in roots)
            for identifier in affected:
                db.execute("UPDATE crm_opportunities SET revision=revision+1,updated_at=? WHERE owner=? AND customer_id=?",
                           (now, owner, identifier))
                if self.workspace._exists(db, "crm_customer_revisions"):
                    db.execute("UPDATE crm_customer_revisions SET revision=revision+1 WHERE owner=? AND customer_id=?", (owner, identifier))
            return self._unit(db, owner, customer_id, parents)

    def view(self, owner, customer_id, include_archived=False):
        owner, customer_id = _owner(owner), _identifier(customer_id)
        if type(include_archived) is not bool:
            raise ValueError("单位归档筛选无效")
        with self.crm._lock:
            db = self.crm._db
            parents = unit_parents(db, owner)
            unit = self._unit(db, owner, customer_id, parents)
            all_units = self.units(owner)["items"]
            descendants = {customer_id}
            while True:
                expanded = descendants | {item["id"] for item in all_units if item["parent_customer_id"] in descendants}
                if expanded == descendants:
                    break
                descendants = expanded
            ancestors, ancestor = [], parents.get(customer_id)
            while ancestor is not None:
                ancestors.append(self._unit(db, owner, ancestor, parents))
                ancestor = parents.get(ancestor)
            names = {item["id"]: item["name"] for item in all_units}
            contacts, projects = [], []
            for identifier in sorted(descendants):
                if self.workspace._exists(db, "crm_contacts"):
                    contacts.extend({**_public(row), "unit_name": names[identifier], "unit_customer_id": identifier}
                                    for row in db.execute("SELECT * FROM crm_contacts WHERE owner=? AND customer_id=?"+
                                      ("" if include_archived else " AND archived=0")+" ORDER BY id", (owner, identifier)))
                projects.extend({**item, "unit_name": names[identifier], "unit_customer_id": identifier}
                                for item in self.workspace.opportunities(owner, identifier, include_archived)["items"])
            return {"unit": unit, "ancestors": ancestors,
                    "units": [item for item in all_units if item["id"] in descendants],
                    "contacts": contacts, "projects": projects,
                    "contact_count": len(contacts), "project_count": len(projects),
                    "include_archived": include_archived}
