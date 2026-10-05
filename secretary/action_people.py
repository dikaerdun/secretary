"""Explicit, optional people metadata for future actions, separate from advice.

Callers compare the options version (or their enclosing action CAS) before any
business write. A hint, source attendee, or project role never selects a person.
The binding operation uses the timeline's savepoint-aware context writer so a
caller can keep action creation and its about relations in one transaction.
"""
from __future__ import annotations

import hashlib
import json
import re

from .crm import _identifier, _owner


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def normalize_contact_ids(contact_ids):
    if not isinstance(contact_ids, list) or len(contact_ids) > 50:
        raise ValueError("这项待办最多明确关联50位联系人")
    identifiers = [_identifier(value) for value in contact_ids]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("这项待办不能重复选择同一联系人")
    return sorted(identifiers)


def _rows(db, table, owner, where="", parameters=()):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return []
    return [dict(row) for row in db.execute("SELECT * FROM " + table + " WHERE owner=?" + where,
                                          (owner, *parameters))]


def action_contact_options(db, owner, customer_id, opportunity_id, workspace):
    """Return disambiguated active choices and a version of their current scope.

Contacts in the action's unit are eligible. A contact in any other unit must
already be an active, valid member of this exact project. Membership permits a
choice; it does not imply attendance or select that person for the action.
"""
    owner, customer_id = _owner(owner), _identifier(customer_id)
    customer = db.execute("SELECT id,name FROM crm_customers WHERE owner=? AND id=?", (owner, customer_id)).fetchone()
    if customer is None:
        raise KeyError("未找到你的单位")
    project, members, project_units = None, [], []
    if opportunity_id is not None:
        opportunity_id = _identifier(opportunity_id)
        project = dict(workspace._require_opportunity(db, owner, customer_id, opportunity_id))
        members = workspace._stakeholders(db, owner, project, include_archived=True)
        project_units = _rows(db, "crm_opportunity_units", owner, " AND opportunity_id=?", (opportunity_id,))
    member_ids = {item["contact_id"] for item in members if item.get("membership_valid") and not item.get("archived")}
    contacts = _rows(db, "crm_contacts", owner)
    eligible = [item for item in contacts if not item.get("archived") and
                (item["customer_id"] == customer_id or item["id"] in member_ids)]
    eligible.sort(key=lambda item: item["id"])
    if project and project["archived"]:
        eligible = []
    unit_ids = {customer_id, *(item["customer_id"] for item in eligible)}
    units = [dict(row) for row in db.execute("SELECT id,name FROM crm_customers WHERE owner=? ORDER BY id", (owner,))
             if row["id"] in unit_ids]
    names = {unit["id"]: unit["name"] for unit in units}
    choices = []
    for item in eligible:
        public = {key: item.get(key, "") for key in ("id", "customer_id", "name", "department")}
        public.update(contact_id=item["id"], unit_name=names[item["customer_id"]])
        public["label"] = " / ".join(value for value in (public["name"], public["department"], public["unit_name"]) if value)
        choices.append(public)
    # Include revisions and archived memberships as well as the visible choices:
    # a revoked/re-established relationship must invalidate an old selection.
    membership_rows = _rows(db, "crm_opportunity_stakeholders", owner, " AND opportunity_id=?", (opportunity_id,)) if project else []
    unit_relations = sorted(_rows(db, "crm_account_units", owner), key=lambda row: row["customer_id"])
    scope = {"customer": dict(customer), "project": project, "contacts": eligible, "units": units,
             "memberships": sorted(membership_rows, key=lambda row: row["contact_id"]),
             "project_units": sorted(project_units, key=lambda row: row["participant_customer_id"]),
             "unit_relations": unit_relations}
    return {"contacts": choices, "version": hashlib.sha256(_json(scope).encode("utf-8")).hexdigest()}


def validate_action_contacts(db, owner, customer_id, opportunity_id, workspace, contact_ids, expected_version=None):
    """Preflight before adoption; callers may use their enclosing action CAS."""
    identifiers = normalize_contact_ids(contact_ids)
    options = action_contact_options(db, owner, customer_id, opportunity_id, workspace)
    if expected_version is not None:
        if not isinstance(expected_version, str) or not re.fullmatch("[a-f0-9]{64}", expected_version):
            raise ValueError("请重新核对待办联系人选项版本")
        if expected_version != options["version"]:
            raise ValueError("联系人或项目参与关系已有变化，请重新核对这项待办关联谁；尚未采用")
    available = {item["id"] for item in options["contacts"]}
    if any(identifier not in available for identifier in identifiers):
        raise ValueError("所选联系人不属于当前有效范围，或已归档、项目参与关系已失效；请重新核对")
    return identifiers


def bind_action_contacts(timeline, owner, record_id, contact_ids, *, expected_revision=None):
    """Merge explicitly chosen about relations into a future, unoccurred action.

Use only for a new adoption or a guarded unfinished operation. Replaying a
completed adoption must read its current event instead of calling this again.
No source-event people are consulted, and existing explicit people are retained.
"""
    owner, record_id = _owner(owner), _identifier(record_id)
    identifiers = normalize_contact_ids(contact_ids)
    event = timeline.get_event(owner, "record:" + str(record_id))
    if event.get("needs_review"):
        raise ValueError("待办内容、归属或人物关系已有变化，请在待办中重新核对；没有覆盖已有关系")
    with timeline.crm._lock:
        record = timeline.crm._require_record(timeline.crm._db, owner, record_id)
        if record["kind"] != "action":
            raise ValueError("只能为明确采用的待办关联联系人")
        validate_action_contacts(timeline.crm._db, owner, event["customer_id"], event["opportunity_id"],
                                 timeline.workspace, identifiers)
    people = [{"contact_id": item["contact_id"], "relation": item["relation"]}
              for item in event["contact_relations"] if item.get("valid")]
    known = {item["contact_id"] for item in people}
    people.extend({"contact_id": identifier, "relation": "about"} for identifier in identifiers if identifier not in known)
    return timeline.save_context(owner, event["key"], {
        "expected_revision": event["revision"] if expected_revision is None else expected_revision,
        "kind": "reflection", "occurred_at": None,
        "contact_relations": people})


def action_people_receipt(timeline, owner, record_id):
    """Read the current graph for a receipt; an original selection may be stale."""
    owner = _owner(owner)
    key = "record:" + str(_identifier(record_id))
    event = timeline.get_event(owner, key) if timeline is not None else None
    return {"action_event_key": key,
            "action_contact_ids": sorted(item["contact_id"] for item in event["contact_relations"] if item.get("valid")) if event else [],
            "action_contact_needs_review": bool(event and (event.get("needs_review") or
                any(item.get("archived") for item in event["contact_relations"])))}
