"""Evidence-bound interpretation of coordination, independent of execution.

The model proposes meanings. Current user text grants authority; reference
material, quoted speech and recordings never grant it. Relative dates use the
saved submission timestamp rather than the model completion time.
"""
from __future__ import annotations

import copy
import re

from .arrangement_time import normalize_time
from .secretary_interpreter import is_conditional

FIELDS = {'settle_deadline', 'next_check', 'proposed_execution', 'candidates',
          'decision_mode', 'settlement_scope', 'waiting_for', 'last_progress',
          'agreement', 'application_authority', 'execution_reminder'}

CLOCK_MARKER = r'(?:上午|下午|晚上|早上|中午|凌晨).{0,5}(?:点|时)|\d{1,2}[:：]\d{2}|(?<!\d)(?:1[3-9]|2[0-3])(?:点|时)|零点|午夜'
SETTLEMENT_MARKER = (r'定下来|敲定|确定下来|定下|(?:最晚|必须).{0,15}确定|'
                     r'(?:之前|前).{0,3}定(?!金|价|位|制|义|额)|'
                     r'(?:必须|务必|一定|最晚).{0,3}定(?!金|价|位|制|义|额)')

MODEL_INSTRUCTIONS = """
额外输出arrangement和arrangement_evidence（可为空），区分三个时间：
settle_deadline={strength:target|required,time_text:原话}表示何时把安排确定下来；
next_check={time_text:原话,action:再问/再看}表示下次推进；
proposed_execution={time_text:原话,place:原话可选}表示实际会面/执行，不能混用。
‘本周定下下月拜访’仅确定期限本周，执行月份尚未定；‘本周去拜访’是执行范围，不能变成确定期限。
‘本周约一下’有歧义，先问一次是本周去还是本周定；不编造钟点。
candidates最多6个，每个{time_text:原话,label:简短说明}，不建多条活动。
decision_mode=self（自己打电话/准备）、external（需双方约定）、unknown；
settlement_scope=execution_time或date_only（仅用户明确先定日期）；
waiting_for说明等谁答复；last_progress是本次实际进展；agreement.status仅reported或unknown。
arrangement_evidence逐字段引用本次utterance原文。不要根据附件/历史/推测给执行授权。
application_authority不是模型决定的，不要输出direct_user。无默认提前提醒也能安排日程。
可能改期保持旧有效日程；明确原时间不去才撤销。已定日期不等于对方已同意新钟点。
"""


def _clauses(text):
    return [s.strip() for s in re.split(r'[，,。；;\n]', text) if s.strip()]


def _quote(evidence, key, text):
    value = evidence.get(key)
    return value if isinstance(value, str) and value.strip() and value in text else None


def current_instruction_text(text):
    """Separate current instructions from reported/negated commands."""
    parts = _clauses(text)
    reporting = bool(re.search(r'(?:客户|对方|同事|他|她).{0,4}(?:说|提到|写道)|(?:录音|邮件|材料|附件).{0,8}(?:写|说|提到)|作为(?:沟通)?(?:记录|资料)', text))
    if reporting:
        # Reported agreement may support a subsequent user instruction, but
        # a command inside the report is not itself an instruction to us.
        parts = [part for part in parts if re.match(r'(?:请|帮我|我(?:现在|决定|要)|现在|就按)', part)
                 and re.search(r'安排|加入日程|取消|撤销|暂停|暂缓|恢复|改期|改到|确定', part)]
        if not parts:
            return '', True
    cleaned = []
    for part in parts:
        if re.search(r'(?:不是|并非|不想|不要|不需要|不能|别|不打算).{0,8}(?:取消|撤销|暂停|暂缓|放弃|恢复|改期|改到)', part):
            continue
        cleaned.append(part)
    return '，'.join(cleaned), False


def _time(value, quote, submitted_at, role, base_date):
    # Never trust a model timestamp. Reparse its verbatim evidence instead.
    text = value.get('time_text') if isinstance(value, dict) else value
    if not isinstance(text, str) or text not in quote:
        text = quote
    return normalize_time(text, submitted_at, role=role, base_date=base_date)


def _self_action(instruction):
    for clause in _clauses(instruction):
        if re.match(r'(?:主要(?:想)?(?:聊|谈)|目标|议题|讨论|材料|复盘|例如|比如|举例)', clause):
            continue
        if re.search(r'(?:不要|不用|不想|不打算|别).{0,5}打电话', clause):
            continue
        if re.search(r'打电话|我(?:自己)?(?:给团队|给同事|给大家|来|负责).{0,12}(?:培训|准备|整理|汇报|演示)|我自己(?:安排|决定)', clause):
            return True
    return False


def activity_from_instruction(text):
    """Bind a fallback activity to the current main action, not a topic."""
    instruction, reference_only = current_instruction_text(text)
    if reference_only:
        return 'task'
    kinds = {'打电话': 'call', '致电': 'call', '电话沟通': 'call',
             '拜访': 'visit', '会面': 'visit', '见面': 'visit',
             '吃饭': 'meal', '约饭': 'meal', '聚餐': 'meal', '饭局': 'meal',
             '培训': 'task', '准备': 'task', '整理': 'task', '汇报': 'task', '演示': 'task'}
    for clause in _clauses(instruction):
        if re.match(r'(?:主要(?:想)?(?:聊|谈)|目标|议题|讨论|材料|复盘|例如|比如|举例)', clause):
            continue
        for match in re.finditer('|'.join(kinds), clause):
            if re.search(r'(?:不要|不用|不想|不打算|别).{0,5}$', clause[:match.start()]):
                continue
            return kinds[match.group()]
    return 'task'


def checked_arrangement(raw, text, submitted_at, current=None, source_kind='user'):
    current = current or {}
    data = raw.get('arrangement') or {}
    evidence = raw.get('arrangement_evidence') or {}
    if not isinstance(data, dict) or not isinstance(evidence, dict) or set(data) - FIELDS:
        raise ValueError('安排理解的字段无效，原话已保留。')
    result = {'changes': {}, 'issues': [], 'operation': None, 'recognized': False,
              'withdraw_explicit': False, 'check_handled': False, 'reference_only': False,
              'source_authorized': False}
    if source_kind in ('recording', 'recap'):
        return result
    instruction, reference_only = current_instruction_text(text)
    result['reference_only'] = reference_only
    if reference_only:
        return result
    explicit_after_report = bool(re.search(r'(?:客户|对方|他|她).{0,4}说', text)
        and re.match(r'(?:请|帮我|我(?:现在|决定|要)|现在|就按)', instruction))
    if is_conditional(text) and (not explicit_after_report or re.match(r'如果|假如|要是', text)):
        return result
    result['source_authorized'] = True
    changes, issues = result['changes'], result['issues']
    base_date = (current.get('proposed_execution') or {}).get('time_spec', {}).get('date') or current.get('date')
    for key, value in data.items():
        quote = _quote(evidence, key, text)
        if not quote:
            continue
        try:
            if key == 'settle_deadline':
                if value is None and re.search(r'清除|取消|不设|不用', quote):
                    changes[key] = None
                elif re.search(SETTLEMENT_MARKER+r'|确定|落实|约定', quote):
                    changes[key] = {'strength': 'required' if re.search(r'最晚|必须|截止|不得晚|一定', quote) else 'target',
                                    'time_spec': _time(value, quote, submitted_at, 'deadline', base_date), 'evidence': quote}
            elif key == 'next_check':
                if value is None and re.search(r'清除|取消|不设|不用|不再', quote):
                    changes[key] = None
                elif re.search(r'再问|再看|回看|跟进|催|问一下|检查|提醒', quote):
                    changes[key] = {'time_spec': _time(value, quote, submitted_at, 'check', base_date),
                                    'action': (value.get('action') if isinstance(value, dict) else '') or quote,
                                    'origin': 'user'}
            elif key == 'proposed_execution':
                if value is None and re.search(r'未定|没定|待定', quote):
                    changes[key] = None
                elif re.search(SETTLEMENT_MARKER, quote):
                    # A valid quotation can still propose the wrong time role.
                    # Coordination dates must not become actual appointments.
                    continue
                else:
                    changes[key] = {'time_spec': _time(value, quote, submitted_at, 'execution', base_date)}
                    place = value.get('place') if isinstance(value, dict) else None
                    if isinstance(place, str) and place in quote:
                        changes[key]['place'] = place
            elif key == 'candidates' and isinstance(value, list) and 1 <= len(value) <= 6:
                candidates = []
                for item in value:
                    if not isinstance(item, dict) or not isinstance(item.get('time_text'), str) or item['time_text'] not in quote:
                        continue
                    candidates.append({'time_spec': normalize_time(item['time_text'], submitted_at, role='execution', base_date=base_date),
                                       'label': str(item.get('label') or item['time_text'])[:160]})
                if candidates:
                    changes[key] = candidates
            elif key == 'decision_mode' and value in ('self', 'external', 'unknown'):
                if value == 'self' and re.search(r'打电话|准备|自己决定|我自己|整理|写方案', quote):
                    changes[key] = value
                elif value == 'external' and re.search(r'约|会面|吃饭|拜访|对方|答复|同意', quote):
                    changes[key] = value
            elif key == 'settlement_scope' and value == 'date_only' and re.search(r'只定日期|先定日期|日期定下来|这一天即可', quote):
                changes[key] = value
            elif key in ('waiting_for', 'last_progress') and isinstance(value, str):
                changes[key] = {'text': value[:1200], 'evidence': quote}
            elif key == 'agreement' and isinstance(value, dict) and value.get('status') == 'reported' and _agreed(quote):
                changes[key] = {'status': 'reported', 'evidence': quote, 'scope': _agreement_scope(quote)}
            # Authority and reminders are exclusively current-user policy below.
        except ValueError as error:
            issues.append(str(error))

    for clause in _clauses(text):
        if 'settle_deadline' not in changes and re.search(SETTLEMENT_MARKER, clause):
            # Only parse the prefix before the settlement verb. The activity may
            # have a second date after it: 本周定下来，下月去拜访.
            marker = re.search(SETTLEMENT_MARKER, clause)
            verb = re.search(r'定下来|敲定|确定下来|定下|确定|定(?!金|价|位|制|义|额)', marker[0])
            temporal = clause[:marker.start() + verb.start()]
            # 本周把下个月培训时间定下来: the leading period is the
            # settlement deadline; the embedded period belongs to execution.
            leading = re.match(r'^(?:希望|最晚|必须|请|帮我)?\s*(本周|这周|下周|本月|这个月|月底|下月|下个月)(?=把|先把)', temporal)
            if leading:
                temporal = leading[1]
            try:
                spec = normalize_time(temporal, submitted_at, role='deadline', base_date=base_date)
                changes['settle_deadline'] = {'strength': 'required' if re.search(r'最晚|必须|截止|一定', clause) else 'target',
                                            'time_spec': spec, 'evidence': clause}
            except ValueError as error:
                if re.search(r'周|月|天|日|号|\d', temporal):
                    issues.append(str(error))
        if 'next_check' not in changes and re.search(r'再问|再看|回看|再催|再跟进|提醒我(?:问|看)', clause):
            try:
                changes['next_check'] = {'time_spec': normalize_time(clause, submitted_at, role='check', base_date=base_date),
                                        'action': clause, 'origin': 'user'}
            except ValueError as error:
                issues.append(str(error))
        if ('proposed_execution' not in changes and re.search(r'本周|这周|下周|本月|这个月|下月|下个月', clause)
                and re.search(r'去拜访|去见|见面|吃饭|喝茶|打电话|准备|会面', clause)
                and not re.search(SETTLEMENT_MARKER+r'|再问|再看|约一下', clause)):
            try:
                changes['proposed_execution'] = {'time_spec': normalize_time(clause, submitted_at, role='execution', base_date=base_date)}
            except ValueError as error:
                issues.append(str(error))
        if 'proposed_execution' not in changes and re.search(r'把.+(?:培训|会面|拜访|会议|演示|交流).*(?:定下来|敲定|定下)', clause):
            embedded = re.search(r'把(?:下月|下个月|本月|这个月|下周|本周).+?(?=时间|日期|定下来|敲定|定下)', clause)
            if embedded:
                try:
                    changes['proposed_execution'] = {'time_spec': normalize_time(embedded[0][1:], submitted_at, role='execution', base_date=base_date)}
                except ValueError as error:
                    issues.append(str(error))
        # Date and clock updates need not repeat the entire activity. A bare
        # "13号" uses this activity's month, and "下午三点" uses its date.
        if (not changes.get('proposed_execution') and not changes.get('candidates')
                and not re.search(SETTLEMENT_MARKER+r'|再问|再看|回看|再催|再跟进|提醒', clause)
                and re.search(r'\d{1,2}(?:月|日|号)|(?:明天|后天|周[一二三四五六日天])|'+CLOCK_MARKER, clause)
                and (current or re.search(r'安排|培训|打电话|拜访|见面|吃饭|演示|会议', clause))):
            try:
                changes['proposed_execution'] = {'time_spec': normalize_time(clause, submitted_at, role='execution', base_date=base_date)}
            except ValueError as error:
                issues.append(str(error))

    if re.search(r'(?:本周|下周|这个月|下个月)约一下', text) and not changes.get('settle_deadline'):
        issues.insert(0, '你想在这段时间去见面，还是先把见面时间定下来？')
    activity = (raw.get('changes') or {}).get('activity') or current.get('activity')
    if _self_action(instruction):
        changes['decision_mode'] = 'self'
    elif 'decision_mode' not in changes:
        if activity in ('call', 'task') or re.search(r'我(?:自己)?(?:打电话|准备|整理|写)', text):
            changes['decision_mode'] = 'self'
        elif activity in ('meal', 'visit'):
            changes['decision_mode'] = 'external'
        elif explicit_after_report and _agreed(text):
            changes['decision_mode'] = 'external'
    if explicit_after_report and 'proposed_execution' not in changes:
        try:
            changes['proposed_execution'] = {'time_spec': normalize_time(text, submitted_at, role='execution', base_date=base_date)}
        except ValueError as error:
            issues.append(str(error))

    # Explicit operations have precedence. Merely waiting is not evidence that
    # the previous check was actually performed.
    if current:
        if re.search(r'取消(?:这次|本次|这个)?(?:活动|饭局|约饭|会面|拜访)|(?:这次|本次)(?:活动|饭局|约饭|拜访)取消', instruction):
            result['operation'] = 'cancel_activity'
        elif re.search(r'原(?:来|定)?(?:的)?(?:时间|周[一二三四五六日天]|安排).{0,12}(?:不去|取消|不行了)|原时间不去了', instruction):
            result['operation'] = 'withdraw_execution'; result['withdraw_explicit'] = True
        elif re.search(r'暂缓|先暂停|先不跟进|暂停协调', instruction):
            result['operation'] = 'pause'
        elif re.search(r'放弃协调|不再协调|这事不约了', instruction):
            result['operation'] = 'abandon_coordination'
        elif re.search(r'恢复(?:协调|推进|跟进)|继续协调', instruction):
            result['operation'] = 'resume'
        elif re.search(r'继续定(?:钟点|时间)|补充钟点', instruction):
            result['operation'] = 'continue_set_time'
        elif re.search(r'改期|改到|改成|可能改|换个时间', instruction):
            result['operation'] = 'start_reschedule'
        if re.search(r'问过|问了|已(?:经)?(?:问|联系|催)|刚(?:问|联系)|对方(?:回复|答复|说)', text):
            result['check_handled'] = True
            changes['last_progress'] = {'text': text[:1200], 'evidence': text}
        elif re.search(r'还在等|仍在等|还没回|仍没回|等答复', text):
            changes['waiting_for'] = {'text': text[:400], 'evidence': text}

    if _agreed(text):
        changes['agreement'] = {'status': 'reported', 'evidence': text,
                                'scope': 'date_only' if changes.get('settlement_scope') == 'date_only' else _agreement_scope(text)}
    elif current and re.search(r'(?:几点|钟点|具体时间|具体时刻|'+CLOCK_MARKER+r').{0,8}(?:不同意|不行|未确认|没确认|还没定|未定|没定)', instruction):
        changes['agreement'] = {'status':'unknown','evidence':text}
    # No value is auto-applied on a conditional, tentative or quoted request.
    if re.search(r'先给我确认|先核对|让我确认|待我确认|先不要落实|先别落实', text):
        changes['application_authority'] = {'kind': 'none', 'review_requested': True, 'evidence': text}
    elif re.search(r'安排|加入日程|放到日程|就按|确认|已经约好|已约好|已经确定|对方答应|定下来|打电话|拜访|约.{0,20}吃饭', instruction) and not re.search(r'可能|等.{0,12}答复|还没约好|还未确定|时间.{0,3}(?:没定|未定)', instruction):
        changes['application_authority'] = {'kind': 'direct_user', 'evidence': text}
    if re.search(r'先定日期|只定日期|日期定下来', text):
        changes['settlement_scope'] = 'date_only'
        if re.search(r'先定日期|只定日期|(?:先|请|帮我|就把).{0,8}日期定下来', instruction) and not (changes.get('application_authority') or {}).get('review_requested'):
            changes['application_authority'] = {'kind':'direct_user','evidence':text}
    elif (current.get('settlement_scope') == 'date_only'
          and ((changes.get('proposed_execution') or {}).get('time_spec') or {}).get('precision') == 'instant'
          and (changes.get('application_authority') or {}).get('kind') == 'direct_user'
          and re.search(CLOCK_MARKER, instruction)):
        # Continuing a date-only agreement by speaking a fully authorized
        # clock is the same action as the explicit "continue clock" button.
        changes['settlement_scope'] = 'execution_time'
    if re.search(r'不用提醒|不要提醒|不需要提醒', text):
        changes['execution_reminder'] = {'enabled': False, 'evidence': text}
    result['recognized'] = bool(changes.get('settle_deadline') or changes.get('next_check') or changes.get('proposed_execution') or changes.get('candidates') or result['operation'] or raw.get('intent') in ('plan', 'update'))
    result['issues'] = list(dict.fromkeys(issues))[:3]
    return result


def _agreed(text):
    # A confirmed date and an undecided clock are independent facts. Refusing
    # the whole report because its next clause says "钟点还没定" loses the
    # date agreement; refusing a negative agreement clause is still necessary.
    return bool(_positive_agreement_clauses(text))


def _positive_agreement_clauses(text):
    positive = r'已经约好|已约好|已经确定|已确定|对方(?:已经|已)?(?:答应|同意|确认)|都同意|双方同意|约好了'
    negative = r'没约好|未约好|还没|还未|未确定|未确认|不同意|(?:没有|并非|不是).{0,8}(?:确定|确认|约好|同意)'
    return [clause for clause in _clauses(text) if re.search(positive, clause) and not re.search(negative, clause)]


def _agreement_scope(text):
    if re.search(r'只(?:定|确认)日期|先定日期|(?:几点|钟点|具体时间|时间).{0,6}(?:没定|未定|待定|没确认|没同意)', text):
        return 'date_only'
    affirmative = '，'.join(_positive_agreement_clauses(text))
    explicit_date = re.search(r'\d{1,2}(?:号|日)|(?:周|星期)[一二三四五六日天]|明天|后天', affirmative)
    explicit_clock = re.search(CLOCK_MARKER, affirmative)
    return 'date_only' if explicit_date and not explicit_clock else 'execution_time'


def add_execution_evidence(semantic, applied, plan, text, submitted_at, *, source_kind='user'):
    """Bridge checked legacy fields without guessing the clock on a new day."""
    value = copy.deepcopy(semantic)
    changes = value['changes']
    if source_kind != 'user' or not value.get('source_authorized'):
        return value
    if 'proposed_execution' not in changes and not changes.get('candidates'):
        if applied.get('start_at') is not None:
            changes['proposed_execution'] = {'time_spec': normalize_time(applied['start_at'], submitted_at, role='execution'),
                'place': plan.get('place', ''), 'duration_minutes': plan.get('duration_minutes', 30)}
        elif 'date' in applied and applied['date']:
            changes['proposed_execution'] = {'time_spec': normalize_time(applied['date'], submitted_at, role='execution'),
                'place': plan.get('place', ''), 'duration_minutes': plan.get('duration_minutes', 30)}
        elif 'start_at' in applied and applied['start_at'] is None:
            changes['proposed_execution'] = None
    if 'proposed_execution' not in changes and ('place' in applied or 'duration_minutes' in applied):
        current = plan.get('proposed_execution')
        if isinstance(current, dict):
            changes['proposed_execution'] = {**current, 'place': plan.get('place', ''),
                                           'duration_minutes': plan.get('duration_minutes', 30)}
    if 'remind_minutes' in applied:
        changes['execution_reminder'] = {'enabled': True, 'minutes': applied['remind_minutes'], 'evidence': text}
    if 'reminder_at' in applied:
        changes['execution_reminder'] = {'enabled': True, 'at': applied['reminder_at'], 'evidence': text}
    return value
