"""Owner-bound customer commands shared by voice and the authenticated web app."""
from __future__ import annotations

import asyncio
from datetime import datetime
import inspect
import json
import math
import re
import time

from .parser import ID_TOKEN, SHANGHAI, _identifier_number


_UNSET = object()


CUSTOMER_HELP = (
    '可以这样说：\n'
    '“新建客户华辰科技，联系人王总，关注数据库加密和密评整改。”\n'
    '“补充客户华辰科技，王总希望先微信发技术材料。”\n'
    '“记录客户华辰科技的交流：今天讨论了密钥管理，需补充部署方案。”\n'
    '“查看客户华辰科技”可看拜访前速览。\n'
    '资料变更先生成 C 编号，核对后说“确认客户 C1”；说“客户待确认”查看。\n'
    '日程仍用 P 编号单独确认；没有说时间的事项不会自动排进日程。'
)


def render_draft(draft):
    title = '客户资料待确认' if draft['status'] == 'pending' else '客户资料草稿'
    lines = [f"{title}：C{draft['id']}｜{draft['customer_name']}"]
    for change in draft.get('changes', [])[:35]:
        before = change.get('before')
        after = change.get('after')
        if change.get('key') == 'amount_cents':
            before = f'¥{before / 100:,.2f}' if isinstance(before, (int, float)) else before
            after = f'¥{after / 100:,.2f}' if isinstance(after, (int, float)) else after
        prefix = f"{draft.get('contact_name') or '联系人'} · " if change.get('target') == 'contact' else ''
        basis = '（你的观察）' if change.get('basis') == 'observation' else ''
        old = str(before or '未记录')
        new = str(after)
        old = old[:80] + ('…' if len(old) > 80 else '')
        new = new[:160] + ('…' if len(new) > 160 else '')
        lines.append(f"· {prefix}{change.get('label', change['key'])}：{old} → {new}{basis}")
        if sum(map(len, lines)) > 2600:
            lines.append('完整变更和原话依据请在后台待确认客户变更中核对。')
            break
    if draft.get('status') == 'pending':
        lines.append(f"核对后回复“确认客户 C{draft['id']}”，或“取消客户 C{draft['id']}”。")
    else:
        lines.append('状态：' + {'confirmed': '已确认', 'rejected': '已取消', 'stale': '档案有更新，请重新整理'}.get(draft['status'], draft['status']))
    return '\n'.join(lines)


class CustomerService:
    def __init__(self, crm, parser, lock, *, clock=time.time, organizer=None, coach=None):
        self.crm, self.parser, self.lock = crm, parser, lock
        self.clock, self.organizer = clock, organizer
        self.coach = coach
        from .sales_workspace import SalesWorkspace
        self.workspace=SalesWorkspace(crm,clock=clock)
        with crm._transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS customer_command_results ('
                       'owner TEXT NOT NULL, source_id TEXT NOT NULL, result TEXT NOT NULL, '
                       'created_at REAL NOT NULL, PRIMARY KEY(owner,source_id))')

    def _cached(self, owner, source_id):
        row = self.crm._db.execute('SELECT result FROM customer_command_results WHERE owner=? AND source_id=?',
                                   (owner, source_id)).fetchone()
        return json.loads(row['result']) if row else None

    def _remember(self, owner, source_id, result):
        with self.crm._transaction() as db:
            db.execute('INSERT INTO customer_command_results VALUES (?,?,?,?) '
                       'ON CONFLICT(owner,source_id) DO UPDATE SET result=excluded.result',
                       (owner, source_id, json.dumps(result, ensure_ascii=False), self.clock()))
        return result

    def prepare_analysis_result(self, owner, record_id, *, prepare_timed=True):
        """Collect one reviewable result; precise promises become pending P only.

        The caller holds the shared application lock. Adoption and proposal
        creation are idempotent independently, so a partially prepared result
        can be resumed without another model call or another reminder.
        """
        from .action_contract import can_schedule, derive_terms
        analysis = self.crm.get_analysis(owner, record_id)
        if analysis is None:
            return {'analysis': None, 'actions': [], 'proposals': []}
        record = self.crm.get_record(owner, record_id)
        actions, proposals = [], []
        for action in analysis['actions']:
            terms = derive_terms(action, record['content'])
            child_id = action.get('adopted_record_id')
            child = self.crm.get_record(owner, child_id) if child_id else None
            when = terms['execution_at']
            precise = (type(when) in (int, float) and math.isfinite(when)
                       and self.clock() + 5 < when < self.clock() + 10 * 366 * 86400)
            if (prepare_timed and not analysis.get('stale') and action['kind'] == 'commitment'
                    and can_schedule(terms) and precise):
                child = self.crm.adopt_action(owner, record_id, action['id'], self.clock())
                if child['proposal_id'] is None:
                    reply = self.crm.execute(owner, f'crm-prepare:{record_id}:{analysis["version"]}:{action["id"]}',
                        {'action': 'propose', 'title': child['title'], 'remind_at': when,
                         'duration_minutes': terms['duration_minutes'] or 30}, self.clock())
                    match = re.match(r'^已整理，待你确认：P([0-9]+)', reply)
                    if match:
                        child = self.crm.link_proposal(owner, child['id'], int(match[1]), self.clock())
            proposal = self.crm.get_proposal(owner, child['proposal_id']) if child and child['proposal_id'] else None
            inheritance=self.workspace.inherit_record_link(owner,record_id,child['id']) if child else None
            actions.append({**action, **terms,
                            **({'project_warning':inheritance['warning']} if inheritance and inheritance.get('warning') else {}),
                            'duration_minutes': proposal['duration_minutes'] if proposal else terms['duration_minutes'],
                            'duration_defaulted': bool(proposal and terms['duration_minutes'] is None),
                            'adopted_record_id': child['id'] if child else None,
                            'record_id': child['id'] if child else None,
                            'proposal_id': proposal['id'] if proposal else None})
            if proposal:
                proposals.append(proposal)
        return {'analysis': self.crm.get_analysis(owner, record_id), 'actions': actions,
                'proposals': proposals}

    @staticmethod
    def _render_actions(result):
        lines = []
        for action in result.get('actions', []):
            kind = '明确待办' if action['kind'] == 'commitment' else '建议'
            line = f"· {kind}：{action['title']}"
            when = action.get('remind_at')
            if type(when) in (int, float) and math.isfinite(when):
                line += '｜' + datetime.fromtimestamp(when, SHANGHAI).strftime('%Y-%m-%d %H:%M')
            else:
                line += '｜时间由你补充'
            if action.get('proposal_id'):
                line += f"｜待确认 P{action['proposal_id']}"
                if action.get('duration_minutes'):
                    line += f"｜预计{action['duration_minutes']}分钟"
                    if action.get('duration_defaulted'):
                        line += '（默认，可修改）'
            lines.append(line)
        if lines:
            lines += ['请核对以上事项；待确认 P 可回复“确认 P编号”后启用提醒。',
                      '未指定时间的事项不会自动排期，建议事项可在后台选择采纳。']
        return '\n'.join(lines)

    def _hide_control(self, owner, source_id):
        self.crm.apply_command(owner, source_id, {'action': 'help'}, '', self.clock())

    def decide(self, owner, identifier, confirm):
        """Caller holds the same mutation lock as task delivery and all web writes."""
        draft = (self.crm.confirm_customer_draft if confirm else self.crm.reject_customer_draft)(
            owner, identifier, self.clock())
        status = draft['status']
        message = (f"客户资料 C{identifier} 已确认：{draft['customer_name']}。"
                   if status == 'confirmed' else f'客户资料 C{identifier} 已取消。' if status == 'rejected'
                   else f'客户资料 C{identifier} 已过期，请重新整理。')
        if status == 'confirmed':
            self._schedule_coach(owner, draft.get('customer_id'))
        return {'draft': draft, 'customer_id': draft.get('customer_id'), 'message': message}

    def _schedule_coach(self, owner, customer_id):
        if self.coach is None or customer_id is None:
            return
        try:
            view = self.coach.view(owner, customer_id)
            recommendation = view.get('recommendation')
            if recommendation is None or recommendation.get('stale'):
                self.coach.schedule(owner, customer_id)
        except Exception:
            # A secondary recommendation must never undo a saved voice note.
            pass

    async def handle(self, owner, source_id, text, *, source='text', force=False,
                     selected_customer_id=_UNSET, record_id=None, category=_UNSET):
        from .customer_parser import MAX_INPUT_LENGTH, should_parse
        from .record_categories import resolve_category
        existing = None
        explicit_clear = record_id is not None and selected_customer_id is None
        async with self.lock:
            if record_id is not None:
                existing = self.crm.get_record(owner, record_id)
                if existing is None:
                    raise KeyError('未找到你的记录')
                text = existing['content']
                if selected_customer_id is _UNSET:
                    selected_customer_id = existing['customer_id']
            elif selected_customer_id is _UNSET:
                selected_customer_id = None
            selected = self.crm.get_customer(owner, selected_customer_id) if selected_customer_id is not None else None
            if selected_customer_id is not None and selected is None:
                raise KeyError('未找到你的客户')
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_INPUT_LENGTH:
            if force or record_id is not None:
                raise ValueError(f'请填写 1 至 {MAX_INPUT_LENGTH} 字的客户记录。')
            return None
        original_text, text = text, text.strip()
        if category is not _UNSET:
            resolve_category(category, text, selected_customer_id)
        # A saved note is source material, not an executable chat command.
        can_control = existing is None and selected is None
        control = re.fullmatch(r'(确认|取消|拒绝)客户\s*(?:[Cc]\s*)?(' + ID_TOKEN + r')[。！!\s]*', text) if can_control else None
        view = re.fullmatch(r'查看客户草稿\s*(?:[Cc]\s*)?(' + ID_TOKEN + r')[。！!\s]*', text) if can_control else None
        local = control or view or (can_control and text in ('客户帮助', '语音客户用法', '客户待确认', '待确认客户'))
        async with self.lock:
            cached = self._cached(owner, source_id)
            if cached is not None:
                return cached
            previous = self.crm._db.execute('SELECT id FROM crm_customer_drafts WHERE owner=? AND source_id=?',
                                            (owner, source_id)).fetchone()
            if previous:
                draft = self.crm.get_customer_draft(owner, previous['id'])
                return self._remember(owner, source_id, {'message': render_draft(draft), 'draft': draft,
                    'customer_id': draft.get('customer_id'), 'record_id': draft.get('source_record_id')})
        if not force and not local and existing is None and selected is None and not should_parse(text):
            return None
        async with self.lock:
            if existing is not None:
                record = self.crm.get_record(owner, existing['id'])
                if record is None or self.crm.record_snapshot(record) != self.crm.record_snapshot(existing):
                    return {'message': '这条记录刚刚被修改，请刷新后按最新内容重整。', 'record_id': existing['id']}
                self.crm.stale_record_drafts(owner, record['id'], self.clock())
                if explicit_clear and record['customer_id'] is not None:
                    record = self.crm.update_record(owner, record['id'], {'customer_id': None}, self.clock())
            else:
                record = self.crm.capture_message(owner, source_id, original_text, source, self.clock())
                if record['original_content'] != original_text:
                    return self._remember(owner, source_id, {'record_id': record['id'],
                        'message': '这条消息编号已有原始记录，已保留先前内容；请使用新的记录重新整理。'})
            capture_fields = {}
            if selected is not None and record['customer_id'] != selected['id']:
                capture_fields['customer_id'] = selected['id']
            if category is not _UNSET:
                capture_fields['category'] = category
            if capture_fields:
                # Finish capture metadata before taking the parser/draft baseline.
                record = self.crm.update_record(owner, record['id'], capture_fields, self.clock())
            captured_snapshot = self.crm.record_snapshot(record)
            base = {'record_id': record['id']}
            if view:
                number = _identifier_number(view[1])
                draft = self.crm.get_customer_draft(owner, number) if number and 0 < number <= 2**63 - 1 else None
                result = {'message': render_draft(draft), 'draft': draft} if draft else {'message': '未找到这个客户资料草稿，请核对 C 编号。'}
                self._hide_control(owner, source_id)
                return self._remember(owner, source_id, {**base, **result})
            if control:
                number = _identifier_number(control[2])
                if number is None or not 0 < number <= 2**63 - 1:
                    result = {'message': '请说明客户资料草稿编号，例如“确认客户 C1”。'}
                else:
                    try:
                        result = self.decide(owner, number, control[1] == '确认')
                    except KeyError:
                        result = {'message': f'未找到你的客户资料草稿 C{number}。'}
                    except ValueError as error:
                        result = {'message': str(error)}
                self._hide_control(owner, source_id)
                return self._remember(owner, source_id, {**base, **result})
            if local:
                message = CUSTOMER_HELP
                if text in ('客户待确认', '待确认客户'):
                    drafts = self.crm.list_customer_drafts(owner)['items']
                    message = '\n'.join(f"C{d['id']}｜{d['customer_name']}｜{len(d['changes'])} 项变更" for d in drafts[:8]) or '当前没有待确认的客户资料。'
                    if drafts:
                        message += '\n请在后台查看具体差异后确认；也可回复“查看客户草稿 C编号”。'
                    if len(drafts) > 8:
                        message += '\n其余草稿可在后台“一句话记录”查看。'
                self._hide_control(owner, source_id)
                return self._remember(owner, source_id, {**base, 'message': message})
            known = [dict(row) for row in self.crm._db.execute(
                'SELECT id,name FROM crm_customers WHERE owner=? ORDER BY updated_at DESC,id DESC LIMIT 1000', (owner,))]
        def candidates(hint=None):
            exact = [item for item in known if item['name'] in text]
            if exact:
                return exact[:10]
            if isinstance(hint, str) and hint.strip():
                partial = [item for item in known if hint.strip().casefold() in item['name'].casefold()]
                if partial:
                    return partial[:10]
            return known[:10]
        context = {'customers': [{'name': item['name']} for item in candidates() + known[:90]]}
        if selected is not None:
            context['customer'] = {'id': selected['id'], 'name': selected['name']}
        if self.parser is None:
            return self._remember(owner, source_id, {**base, 'message': '客户语音整理尚未启用，原话已保留，可先在后台手动添加资料。', 'candidates': candidates()})
        try:
            parameters = inspect.signature(self.parser.parse).parameters
            accepts_context = 'context' in parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values())
            parse = self.parser.parse(text, self.clock(), context=context) if accepts_context else self.parser.parse(text, self.clock())
            parsed = await asyncio.wait_for(parse, timeout=60)
        except Exception:
            return self._remember(owner, source_id, {**base, 'message': '自动理解暂未完成，原话已保存在记录中；可以先核对文字、选定客户，再重新整理。', 'candidates': candidates()})
        intent = parsed.get('intent')
        if explicit_clear and intent in ('create', 'update', 'brief'):
            parsed = {'intent': 'note', 'customer_name': None,
                      'question': '已取消客户归属；请选择客户后再补充画像或查看档案。'}
            intent = 'note'
        if intent == 'none':
            if existing is None and selected is None and not force:
                # A fresh chat message may mention a customer while requesting
                # an ordinary reminder. The durable raw capture stays in place;
                # let the gateway's task parser create its usual P proposal.
                return None
            # Explicit record reprocessing must never execute source text as a
            # fresh task command, even when it contains a reminder request.
            parsed = {'intent': 'note', 'customer_name': selected['name'] if selected else None,
                      'question': '这段内容先作为记录保留，归属和后续事项请核对。'}
            intent = 'note'
        async with self.lock:
            cached = self._cached(owner, source_id)
            if cached is not None:
                return cached
            current = self.crm.get_record(owner, record['id'])
            if current is None or self.crm.record_snapshot(current) != captured_snapshot:
                return self._remember(owner, source_id, {**base, 'message': '这条记录在整理期间已被修改，已保留你的修改。请在后台按最新内容继续整理。'})
            if intent == 'clarify':
                return self._remember(owner, source_id, {**base, 'message': (parsed.get('question') or '请核对本次记录内容。') + '\n原话已保存在记录中。',
                                                       'candidates': candidates(parsed.get('customer_name'))})
            if intent not in ('create', 'update', 'brief', 'note'):
                return self._remember(owner, source_id, {**base, 'message': '原话已保留，本次未生成客户变更；可在后台继续整理。', 'candidates': candidates()})
            name, contact_name = parsed.get('customer_name'), parsed.get('contact_name')
            mentioned = [item for item in known if item['name'] in text]
            if selected is not None:
                if name and name != selected['name'] and any(item['name'] == name for item in known):
                    return self._remember(owner, source_id, {**base, 'message': '原话提到的客户与当前选定客户不同，请核对归属后重整。', 'candidates': candidates(name)})
                customer, name = selected, selected['name']
                if intent == 'create':
                    intent = 'note'
            else:
                matches = self.crm.find_customers_exact(owner, name) if name else []
                # Several full names in the source require an explicit choice,
                # even when a model happened to return only the first one.
                customer = matches[0] if not explicit_clear and len(matches) == 1 and len(mentioned) <= 1 else None
                if intent == 'create' and matches:
                    return self._remember(owner, source_id, {**base, 'message': '已有同名客户，请用“补充客户＋公司全名”更新资料。', 'candidates': matches})
                if intent in ('update', 'brief') and customer is None:
                    wording = '客户名称有歧义' if len(matches) > 1 or len(mentioned) > 1 else '没有找到唯一匹配的客户'
                    return self._remember(owner, source_id, {**base, 'message': wording + '，原话已保存，请在后台选定客户后继续整理；不会自动合并档案。', 'candidates': candidates(name)})
            if intent == 'brief':
                message = self.render_brief(self.crm.profile(owner, customer['id']))
                if self.coach is not None and re.search(r'推进|建议|怎么做|下一步', text):
                    recommendation = self.coach.view(owner, customer['id']).get('recommendation')
                    if recommendation is None or recommendation.get('stale'):
                        self._schedule_coach(owner, customer['id'])
                        message = '正在根据客户记录更新推进建议，请稍后在客户页查看。'
                    else:
                        message = self.coach.render(recommendation)
                if existing is None:
                    self._hide_control(owner, source_id)
                return self._remember(owner, source_id, {**base, 'message': message, 'customer_id': customer['id']})
            if intent == 'note':
                if customer is not None and record['customer_id'] != customer['id']:
                    if record['customer_id'] is not None:
                        return self._remember(owner, source_id, {**base, 'message': '记录已有客户归属，请先核对；没有覆盖已有归属。', 'candidates': candidates(name)})
                    record = self.crm.update_record(owner, record['id'], {'customer_id': customer['id']}, self.clock())
                result = {**base, 'message': ('已保存 ' + customer['name'] + ' 的交流记录。' if customer else '已保存这段交流记录，请在后台选定客户归属。') + '本次整理的后续事项需你核对；新增提醒需要确认后才启用。',
                          'customer_id': customer['id'] if customer else None}
                if customer is None:
                    result['candidates'] = candidates(name)
                if parsed.get('question'):
                    result['message'] += '\n' + parsed['question']
            else:
                if customer is not None and record['customer_id'] != customer['id']:
                    if record['customer_id'] is not None:
                        return self._remember(owner, source_id, {**base, 'message': '记录已有客户归属，请先核对；没有覆盖已有归属。', 'candidates': candidates(name)})
                    fields = {'customer_id': customer['id']}
                    if category is not _UNSET:
                        fields['category'] = category
                    record = self.crm.update_record(owner, record['id'], fields, self.clock())
                contact_id = None
                if customer and contact_name:
                    contacts = self.crm.find_contacts_exact(owner, contact_name, customer['id'])
                    if len(contacts) > 1:
                        return self._remember(owner, source_id, {**base, 'message': '同一客户有多位同名联系人，原话已保留，请先核对联系人。', 'candidates': candidates(name)})
                    if contacts:
                        contact_id = contacts[0]['id']
                data = {key: parsed.get(key, {}) for key in ('basic', 'contact', 'basic_evidence', 'contact_evidence')}
                data.update(intent=intent, customer_id=customer['id'] if customer else None, customer_name=name,
                            contact_id=contact_id, contact_name=contact_name, attributes=parsed.get('attributes', []),
                            source_text=record['original_content'], source_content=record['content'],
                            source_snapshot=self.crm.record_snapshot(record), selected_customer=selected is not None)
                try:
                    draft = self.crm.create_customer_draft(owner, data, self.clock(), source_id=source_id, source_record_id=record['id'])
                except (ValueError, KeyError):
                    return self._remember(owner, source_id, {**base, 'message': '原话已保留，部分客户资料需要核对；没有直接更改档案，请核对后重整。', 'candidates': candidates(name)})
                result = {**base, 'message': render_draft(draft), 'draft': draft, 'customer_id': draft.get('customer_id')}
            detail = self.crm.record_detail(owner, record['id'])
            active_reminders = []
            if detail.get('task') and detail['task']['status'] == 'pending':
                active_reminders.append({**detail['task'], 'record_id': detail['record']['id'],
                                         'customer_name': detail['record'].get('customer_name')})
            old_analysis = self.crm.get_analysis(owner, record['id'])
            for old_action in (old_analysis or {}).get('actions', []):
                child_id = old_action.get('adopted_record_id')
                child_detail = self.crm.record_detail(owner, child_id) if child_id else None
                child_task = child_detail.get('task') if child_detail else None
                if child_task and child_task['status'] == 'pending' and all(
                        item['id'] != child_task['id'] for item in active_reminders):
                    active_reminders.append({**child_task, 'record_id': child_detail['record']['id'],
                                             'customer_name': child_detail['record'].get('customer_name')})
            if active_reminders:
                active = active_reminders[0]
                result['active_reminder'] = active
                result['active_reminders'] = active_reminders
                result['requires_reminder_review'] = True
                result['message'] += '\n这条记录已有生效提醒：'
                for active_item in active_reminders:
                    active_when = active_item.get('remind_at')
                    active_time = (datetime.fromtimestamp(active_when, SHANGHAI).strftime('%Y-%m-%d %H:%M')
                                   if type(active_when) in (int, float) and math.isfinite(active_when) else '未设置时间')
                    result['message'] += '\n· ' + active_item['title'] + '｜' + active_time
                result['message'] += '。本次只整理记录，现存提醒尚未更正；请核对并确认修改或取消提醒。'
            if self.organizer is None:
                if intent == 'note' and customer is not None:
                    self._schedule_coach(owner, customer['id'])
                return self._remember(owner, source_id, result)
            self._remember(owner, source_id, dict(result))
            organize_context = {'customer': customer or {}}
            if customer:
                recent = self.crm.list_records(owner, customer_id=customer['id'], page_size=6)['items']
                organize_context['recent_records'] = [{key: item[key] for key in ('title', 'content', 'status')}
                    for item in recent if item['id'] != record['id']][:5]
        from .crm import analysis_fingerprint
        fingerprint = analysis_fingerprint(record)
        try:
            analysis = await asyncio.wait_for(self.organizer.organize(text, self.clock(), organize_context), timeout=90)
            analysis['input_fingerprint'] = fingerprint
            async with self.lock:
                self.crm.save_analysis(owner, record['id'], analysis, self.clock())
                result.update(self.prepare_analysis_result(owner, record['id'],
                    prepare_timed=not bool(result.get('active_reminder'))))
            result['message'] += '\n' + analysis.get('summary', '')
            rendered = self._render_actions(result)
            if rendered:
                result['message'] += '\n建议跟进：\n' + rendered
        except Exception:
            result['message'] += '\n自动整理暂未完成，可在后台打开记录后重试。'
        async with self.lock:
            if customer is not None:
                self._schedule_coach(owner, customer['id'])
            return self._remember(owner, source_id, result)

    @staticmethod
    def render_brief(profile):
        customer = profile['customer']
        lines = [f"拜访前速览｜{customer['name']}"]
        wanted = {'pain_points', 'requirements', 'crypto_needs', 'next_visit_goal', 'blockers', 'timeline'}
        for fact in profile.get('fields', []):
            if fact['key'] in wanted:
                basis = '（你的观察，待核实）' if fact.get('basis') == 'observation' else ''
                lines.append(f"· {fact['label']}：{fact['value']}{basis}")
        for contact in [item for item in profile.get('contacts', []) if not item.get('archived')][:5]:
            preferences = '；'.join(f"{f['label']}：{f['value']}" + ('（你的观察）' if f.get('basis') == 'observation' else '') for f in contact.get('fields', []) if f['key'] in ('concerns', 'communication_channel', 'contact_hours', 'avoidances'))
            lines.append(f"· 联系人 {contact['name']} {contact.get('role', '')}" + (f'｜{preferences}' if preferences else ''))
        brief = profile.get('brief', {})
        lines.append('未完成跟进：')
        records = brief.get('open_records', [])[:5]
        lines.extend('· ' + item['title'] for item in records)
        if not records:
            lines.append('暂未记录')
        recent = brief.get('recent_records', [])[:2]
        if recent:
            lines.append('最近交流：')
            lines.extend('· ' + item['content'][:180] for item in recent)
        return '\n'.join(lines)[:3500]
