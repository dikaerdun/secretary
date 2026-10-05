"""Apply validated secretary routing inside the existing turn transaction.

Matter grouping creates associations. It never manufactures a calendar task,
confirms an arrangement, or infers that an entire objective has been achieved.
"""
from __future__ import annotations

import json
import re

from .crm import _text
from .matters import MatterConflict
from .secretary_interpreter import day_from_text, clock_from_text
from .store import SHANGHAI
from datetime import datetime


_ACTIVITY_PATTERNS = {
    'meal': r'吃饭|约饭|聚餐|饭局|用餐',
    'visit': r'拜访|见面|会面|约见|会谈|面谈',
    'call': r'打电话|电话(?:联系|沟通|交流|会议)?|通话|致电',
}
_PREPARATION_VERBS = r'准备|整理|补充|完善|编写|撰写|制作|修改|重组|核对|收集|发送|提供|提交|汇总|更新|查阅|打印|演练|彩排|预订|落实'
_PREPARATION_OBJECTS = r'材料|资料|PPT|文档|汇报稿|方案|清单|提纲|场地|餐厅|地点|会议室|酒店|邀请函|邮件|合同|报价|预算|背景|演练|彩排'


def _represented_activity(db, owner, plan_id, title, evidence, now):
    """A plan represents this activity, not every action for the same customer.

Require the actual plan plus a literal event clause, matching participant and
any stated date/clock. Preparation and uncertain/multiple occasions stay as
actions. Historical actions are handled before this check.
"""
    if not plan_id:
        return False
    row = db.execute('SELECT data_json FROM crm_secretary_plans WHERE owner=? AND id=?', (owner, plan_id)).fetchone()
    if row is None:
        return False
    plan = json.loads(row['data_json'])
    pattern = _ACTIVITY_PATTERNS.get(plan.get('activity'))
    if not pattern or plan.get('status') in ('cancelled', 'recapped'):
        return False
    if re.search(r'(?:日|号|今天|明天|后天|今晚)(?:之前|前)|(?:最迟|截至|截止|不晚于).*(?:约|联系|安排)', title):
        # "Arrange it by Friday" is a preparation deadline, not proof that
        # the actual meal/visit takes place on Friday.
        return False
    if re.search(r'(?:会前|饭前|拜访前|电话前).*(?:确认|' + _PREPARATION_VERBS + ')', title, re.I):
        return False
    if re.search(_PREPARATION_VERBS, title, re.I) and re.search(_PREPARATION_OBJECTS, title, re.I):
        return False
    if any(other != plan.get('activity') and re.search(other_pattern, title)
           for other, other_pattern in _ACTIVITY_PATTERNS.items()):
        # A phone call to confirm a meal is a separate action, even though it
        # mentions the meal's participant and date verbatim.
        return False
    if not re.search(pattern + r'|约.{0,20}(?:交流|沟通)|安排.{0,20}(?:交流|沟通)', title):
        return False
    clauses = [part.strip() for part in re.split(r'[，,。；;\n]+', evidence) if re.search(pattern, part)]
    person = str(plan.get('person') or '').strip()
    if person:
        clauses = [part for part in clauses if person in part]
    else:
        # Without a participant, only exact source wording is sufficient.
        normalize = lambda value: re.sub(r'[\s，,。；;：:]+', '', value)
        if normalize(title) != normalize(str(plan.get('title') or '')):
            return False
    if len(clauses) != 1:
        return False
    clause = clauses[0]
    try:
        # A title may identify a second occasion while its broad evidence also
        # contains the first; prefer its explicit date/clock, never collapse it.
        day = day_from_text(title, now, plan.get('date')) or day_from_text(clause, now, plan.get('date'))
        clock = clock_from_text(title) or clock_from_text(clause)
    except (ValueError, TypeError):
        return False
    if day and day != plan.get('date'):
        return False
    if clock:
        if not plan.get('start_at'):
            return False
        start = datetime.fromtimestamp(plan['start_at'], SHANGHAI)
        if (start.hour, start.minute) != clock:
            return False
    return True


def guard_decision(service, owner, decision):
    if decision.get('kind') != 'existing':
        return
    matter = service.get(owner, decision.get('matter_id'))
    if matter['visibility'] != 'active' or matter['status'] == 'ended':
        raise MatterConflict('这件事已经收起或结束，原话已保留；请先恢复或另记一件事。')
    revision = decision.get('base_revision')
    if revision is not None and matter['revision'] != revision:
        raise MatterConflict('这件事已有更新，原话已保留，请核对最新进展后再整理。')


def _action(db, service, owner, matter_id, turn, value, text, now, *, plan_id=None):
    title = _text(value.get('title', ''), '动作标题', 120, required=True).strip()
    evidence = value.get('evidence', '')
    if not isinstance(evidence, str) or not evidence.strip() or evidence not in text:
        return None
    content = _text(value.get('content', evidence), '动作说明', 20000).strip()
    detail = service.get(owner, matter_id)
    existing_id = value.get('existing_record_id')
    known = {r['id']: r for r in detail.get('actions', [])}
    if existing_id is not None:
        if type(existing_id) is not int or existing_id not in known:
            raise ValueError('动作不属于当前事项，请重新核对。')
        existing = known[existing_id]
    else:
        existing = next((r for r in known.values() if r['title'].strip() == title), None)
        existing_id = existing['id'] if existing else None
    requested = value.get('status', 'following')
    if requested not in ('following', 'done'):
        requested = 'following'
    if existing:
        # Store progress separately; do not replace the original task wording.
        db.execute('INSERT INTO crm_activities(owner,record_id,content,created_at) VALUES (?,?,?,?)',
            (owner, existing_id, content, now))
        if requested == 'done' or (requested == 'following' and existing['status'] == 'done' and re.search(r'重新|重做|还没|未完成', evidence)):
            db.execute("UPDATE crm_records SET status=?,updated_at=? WHERE owner=? AND id=?",
                (requested, now, owner, existing_id))
        return {'record_id': existing_id, 'title': existing['title'], 'change': 'completed' if requested == 'done' else 'updated'}
    if requested == 'done':
        # A report about an unknown task is evidence, not a newly completed todo.
        return None
    if _represented_activity(db, owner, plan_id, title, evidence, now):
        # The existing plan is the single place to arrange and review this
        # occurrence. Do not manufacture an additional unscheduled todo.
        return None
    source = service.crm._require_record(db, owner, turn['record_id'])
    identifier = db.execute('''INSERT INTO crm_records(owner,source_id,title,content,original_content,
        source,status,customer_id,classified,kind,parent_record_id,category,created_at,updated_at)
        VALUES (?,?,?,?,?,'web','following',?,1,'action',?,'idea',?,?)''',
        (owner, f"matter-turn:{turn['id']}:{matter_id}:{title}", title, content, evidence,
         detail.get('customer_id') or source['customer_id'], turn['record_id'], now, now)).lastrowid
    if detail.get('opportunity_id'):
        # Only this newly inserted step inherits the canonical goal's project.
        # Existing records keep their explicit historical attribution, even if
        # a later goal or source has a different project.
        flow = getattr(service, 'secretary_flow', None) or getattr(service.crm, 'secretary_flow', None)
        if flow is None:
            raise ValueError('项目归属服务暂未就绪，原话已保留，请稍后重新整理。')
        flow._link_project(db, owner, 'record', identifier, detail['opportunity_id'], now)
    service.attach(owner, matter_id, 'record', identifier, role='action')
    return {'record_id': identifier, 'title': title, 'change': 'created'}


def apply_decision(service, db, turn, decision, *, text, now, plan_id=None, scope=None):
    owner = turn['owner']
    scope = scope or {}
    kind = decision.get('kind', 'source')
    if kind == 'source' and decision.get('matter_id'):
        decision = {**decision, 'kind':'existing', 'actions':[], 'updates':{}}
        kind = 'existing'
    if plan_id and kind in ('source', 'new'):
        associated = service.resolve(owner, 'plan', plan_id)['items']
        if len(associated) == 1:
            existing = associated[0]
            if existing['visibility'] != 'active' or existing['status'] == 'ended':
                return {'kind': 'source', 'matter_id': existing['id'], 'matter': existing,
                    'items': associated, 'changes': [], 'reason': '活动更新已保留，事项目标状态不变。'}
            decision = {**decision, 'kind': 'existing', 'matter_id': existing['id'], 'base_revision': existing['revision']}
            kind = 'existing'
        elif len(associated) > 1:
            return {'kind': 'ambiguous', 'candidates': associated, 'changes': [], 'reason': '这次活动关联多件事，请选择这句话补充的目标。'}
    if kind == 'ambiguous':
        return {'kind': kind, 'candidates': decision.get('candidates', [])[:3],
            'reason': decision.get('reason') or '请核对这句话补充哪件事。', 'changes': []}
    if kind == 'source' and not plan_id:
        return {'kind': 'source', 'reason': decision.get('reason', ''), 'changes': []}
    groups = decision.get('groups') if kind == 'new' else None
    if not isinstance(groups, list) or not groups:
        groups = [decision]
    summaries, changes = [], []
    for index, group in enumerate(groups[:5]):
        if kind == 'existing':
            guard_decision(service, owner, decision)
            matter_id = decision['matter_id']
            detail = service.get(owner, matter_id)
            missing = {key:scope[key] for key in ('customer_id','opportunity_id') if scope.get(key) and not detail.get(key)}
            if missing:
                service.update(owner, matter_id, {**missing, 'expected_revision':detail['revision'],
                    'request_id':f"matter-scope:{turn['id']}:{index}"})
        else:
            plan = None
            if plan_id:
                row = db.execute('SELECT data_json FROM crm_secretary_plans WHERE owner=? AND id=?', (owner, plan_id)).fetchone()
                if row:
                    plan = json.loads(row['data_json'])
            title = group.get('title') or (plan or {}).get('title') or text[:80]
            objective = group.get('objective') or (plan or {}).get('goal') or ''
            data = {'title': title[:120], 'objective': objective[:4000],
                'source_record_ids': [turn['record_id']], 'request_id': f"matter-turn:{turn['id']}:{index}"}
            for field in ('customer_id', 'opportunity_id'):
                selected = scope.get(field) or (plan or {}).get(field)
                if selected:
                    data[field] = selected
            result = service.create(owner, data)
            matter_id = result['matter']['id']
        service.attach(owner, matter_id, 'record', turn['record_id'], role='source')
        if plan_id and index == 0:
            service.attach(owner, matter_id, 'plan', plan_id, role='plan')
        attachments = db.execute('SELECT material_id FROM crm_secretary_turn_attachments WHERE owner=? AND turn_id=?',
            (owner, turn['id'])).fetchall() if service._exists(db, 'crm_secretary_turn_attachments') else []
        for attachment in attachments:
            service.attach(owner, matter_id, 'material', attachment['material_id'], role='source')
        actions = group.get('actions') if groups != [decision] else decision.get('actions')
        if turn['source_kind'] == 'user':
            for action in (actions or [])[:12]:
                change = _action(db, service, owner, matter_id, turn, action, text, now,
                    plan_id=plan_id if index == 0 else None)
                if change:
                    changes.append(change)
        detail = service.get(owner, matter_id)
        updates = decision.get('updates') or {}
        # Waiting/paused may be user reports. Ending requires the explicit UI.
        supported = {key:updates[key] for key in ('title','objective') if isinstance(updates.get(key),str) and updates[key].strip()}
        new_status = updates.get('status')
        if new_status in ('waiting', 'paused'): supported['status'] = new_status
        evidence = updates.get('evidence') or (text if re.search(r'(?:目标|标题|名字|名称).{0,6}(?:改为|改成|改一下|调整为|修改为)', text) else '')
        if turn['source_kind'] == 'user' and supported and evidence and evidence in text:
            service.update(owner, matter_id, {**supported, 'expected_revision': detail['revision'],
                'request_id': f"matter-status:{turn['id']}:{index}"})
            detail = service.get(owner, matter_id)
        summaries.append(service._summary(detail))
    return {'kind': kind, 'matter_id': summaries[0]['id'], 'matter': summaries[0],
        'items': summaries, 'changes': changes, 'reason': decision.get('reason', '')}
