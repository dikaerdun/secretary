"""Source-backed action terms shared by notes, materials and exchanges.

``owner`` remains a storage/security boundary. It is never an executor hint.
Missing terms in an old analysis stay unknown; reading them never edits tasks.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import re

from .parser import _explicit_time
from .store import SHANGHAI, _timestamp


EXECUTOR_KINDS = ('self', 'customer', 'team', 'unknown')
TERM_FIELDS = frozenset({
    'executor_kind', 'executor_evidence', 'duration_minutes', 'duration_evidence',
    'execution_at', 'execution_evidence', 'deadline_at', 'deadline_evidence',
    'check_at', 'check_evidence', 'deadline_date', 'check_date',
})
_QUOTE = re.compile(r'原话：[“"](.+?)[”"]', re.S)
_DURATION = re.compile(r'(\d{1,4}|[一二两三四五六七八九十百]+|半|一个)\s*(小时|分钟|分)')
_SELF = re.compile(r'我(?:们)?(?:答应|承诺|会|将|要|得|计划|负责|准备|'
                   r'(?:今天|明天|后天|下周|本周|这周|周[一二三四五六日天]))')
_CLOSED = re.compile(r'取消|不用|不要|无需|暂不|先别|已(?:经)?(?:完成|发送|提交|交付)')
_DEADLINE = re.compile(r'截止|截至|之前|以前|最迟|前(?:完成|提交|交付|发|给|$)|以内|之内|(?:点|时|日|号|天|周[一二三四五六日天])前')
_CHECK = re.compile(r'检查|核实|查看|催|回访|确认(?:是否|进度|收到)')
_NEXT_ACTION = re.compile(r'^(?:然后|另外|并且|再|接着)?(?:(?:今天|明天|后天|(?:本|这|下)?周[一二三四五六日天])[^，,]{0,20})?'
                          r'(?:会议|会谈|会面|开会|拜访|准备|发送|提交|整理|客户|对方|同事|我(?:们)?)')
_DATE_WORDS = re.compile(r'\d{4}-\d{1,2}-\d{1,2}|(?:\d{4}年)?\d{1,2}月\d{1,2}[日号]|'
                         r'今天|今日|明天|后天|(?:下|本|这)?(?:周|星期)[一二三四五六日天]')


def _number(text):
    if text in ('半', '一个'):
        return .5 if text == '半' else 1
    if text.isdigit():
        return int(text)
    digits = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
              '六': 6, '七': 7, '八': 8, '九': 9}
    value, current = 0, 0
    for char in text:
        if char in digits:
            current = digits[char]
        elif char in ('十', '百'):
            value += (current or 1) * (10 if char == '十' else 100)
            current = 0
        else:
            return None
    return value + current


def _at(value):
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                return None
            value = parsed.timestamp()
        except (ValueError, OverflowError):
            return None
    try:
        return _timestamp(value)
    except (ValueError, OverflowError, TypeError):
        return None


def _quote(action, content):
    quote = action.get('evidence')
    if not isinstance(quote, str) or not quote.strip():
        matched = _QUOTE.search(str(action.get('reason', '')))
        quote = matched[1] if matched else ''
    quote = quote.strip()
    title = action.get('title', '')
    if not quote or not isinstance(title, str) or not title or title not in quote:
        return ''
    if content is not None and (not isinstance(content, str) or quote not in content):
        return ''
    return quote


def _date_term(evidence, claimed, now):
    """Resolve an explicit source date without assigning a clock time."""
    supplied = None
    if claimed is not None:
        try:
            supplied = date.fromisoformat(claimed) if isinstance(claimed, str) else None
            if supplied is None or supplied.isoformat() != claimed:
                return None
        except ValueError:
            return None
    anchor = None
    if now is not None:
        try:
            anchor = datetime.fromtimestamp(_timestamp(now), SHANGHAI).date()
        except (ValueError, TypeError, OverflowError):
            return None
    words = list(_DATE_WORDS.finditer(evidence))
    if len(words) != 1:
        return None
    token = words[0].group()
    target = None
    try:
        if re.fullmatch(r'\d{4}-\d{1,2}-\d{1,2}', token):
            year, month, day = map(int, token.split('-'))
            target = date(year, month, day)
        elif '月' in token:
            matched = re.fullmatch(r'(?:(\d{4})年)?(\d{1,2})月(\d{1,2})[日号]', token)
            year = int(matched[1]) if matched[1] else anchor.year if anchor else supplied.year if supplied else None
            if year is not None:
                target = date(year, int(matched[2]), int(matched[3]))
        elif anchor:
            if token in ('今天', '今日', '明天', '后天'):
                target = anchor + timedelta(days={'今天': 0, '今日': 0, '明天': 1, '后天': 2}[token])
            else:
                weekday = '一二三四五六日'.index(token[-1].replace('天', '日'))
                if token.startswith('下'):
                    target = anchor + timedelta(days=7 - anchor.weekday() + weekday)
                elif token.startswith(('本', '这')):
                    target = anchor + timedelta(days=weekday - anchor.weekday())
                else:
                    target = anchor + timedelta(days=(weekday - anchor.weekday()) % 7)
        elif supplied:
            # Already verified relative dates are preserved when replaying a
            # persisted action. Reading on a later day must not move them.
            target = supplied
    except (ValueError, OverflowError):
        return None
    if target is None or (supplied is not None and supplied != target):
        return None
    return target.isoformat()


def derive_terms(action, content=None, now=None):
    """Normalize only terms supported by this action's own source quote.

    Existing validated ``remind_at`` is the compatibility execution field.
    New terms require their own quote; deadline/check evidence cannot become a
    personal execution appointment. Invalid optional model fields are unset.
    """
    result = {'executor_kind': 'unknown', 'executor_evidence': '',
              'duration_minutes': None, 'duration_evidence': '',
              'execution_at': None, 'execution_evidence': '',
              'deadline_at': None, 'deadline_evidence': '',
              'check_at': None, 'check_evidence': '', 'deadline_date': None, 'check_date': None}
    if not isinstance(action, dict):
        return result
    quote = _quote(action, content)
    title = action.get('title', '')
    clauses = [part.strip() for part in re.split(r'[。！？;；\n]', quote) if title in part]
    clause = clauses[0] if len(clauses) == 1 else ''
    if clause and not _CLOSED.search(clause):
        prefix = clause[:clause.find(title)]
        claimed = action.get('executor_kind')
        supplied = action.get('executor_evidence')
        executor_quote = supplied if isinstance(supplied, str) and supplied and supplied in clause else clause
        # Named people have no inferred company relationship. Explicit source
        # words must identify a customer or an internal colleague.
        if re.search(r'(?:让|由|请)(?:客户|对方)', prefix):
            executor = 'customer'
        elif re.search(r'(?:让|由|请)(?:内部同事|同事|我方团队)', prefix):
            executor = 'team'
        elif re.search(r'(?:让|由|请)[\u4e00-\u9fff]{1,8}$', prefix) and not re.search(r'我(?:们)?(?:来)?$', prefix):
            executor = 'unknown'
        elif _SELF.search(prefix) or re.search(r'(?:请|要求|需要|由)我(?:们)?(?:来)?$', prefix):
            executor = 'self'
        elif re.search(r'同事|我方(?:团队|人员)|内部(?:团队|人员)', prefix):
            executor = 'team'
        elif re.search(r'客户|对方', prefix):
            executor = 'customer'
        else:
            executor = 'unknown'
        if claimed in EXECUTOR_KINDS and claimed != 'unknown' and claimed != executor:
            executor = 'unknown'
        if executor != 'unknown':
            pattern = _SELF if executor == 'self' else re.compile(r'同事|我方|内部') if executor == 'team' else re.compile(r'客户|对方')
            if not pattern.search(executor_quote):
                executor_quote = clause
            result.update(executor_kind=executor, executor_evidence=executor_quote)

        # Comma-separated meeting/preparation actions have their own duration.
        # A relative reminder such as "two hours later" is never work duration.
        scope = clause
        after = clause[clause.find(title) + len(title):]
        kept = []
        for index, part in enumerate(re.split(r'[，,]', after)):
            if index and _NEXT_ACTION.search(part.strip()):
                break
            kept.append(part)
        scope = clause[:clause.find(title) + len(title)] + '，'.join(kept)
        duration_quote = action.get('duration_evidence')
        duration_quote = duration_quote if isinstance(duration_quote, str) and duration_quote and duration_quote in scope else ''
        duration_source = duration_quote or scope
        matches = [match for match in _DURATION.finditer(duration_source)
                   if not re.match(r'\s*(?:之?后|前|内|以内|之内)', duration_source[match.end():])]
        minutes = set()
        for match in matches:
            number = _number(match[1])
            value = number * (60 if match[2] == '小时' else 1) if number is not None else None
            if value is not None and value == int(value) and 5 <= value <= 720:
                minutes.add(int(value))
        requested_duration = action.get('duration_minutes')
        if len(minutes) == 1:
            duration = next(iter(minutes))
            if requested_duration is None or (type(requested_duration) is int and requested_duration == duration):
                result.update(duration_minutes=duration, duration_evidence=duration_quote or matches[0].group())

    if action.get('kind') != 'commitment' or not clause or _CLOSED.search(clause):
        return result
    for field, evidence_field, marker in (
        ('execution_at', 'execution_evidence', None),
        ('deadline_at', 'deadline_evidence', _DEADLINE),
        ('check_at', 'check_evidence', _CHECK),
    ):
        evidence = action.get(evidence_field)
        value = action.get(field)
        if field == 'execution_at' and value is None:
            # The original organizer has already validated legacy remind_at.
            value = action.get('remind_at')
            evidence = action.get('time_evidence') or evidence
            if (value is not None and not evidence and not _DEADLINE.search(scope)
                    and action.get('check_at') is None and action.get('check_date') is None
                    and (_DATE_WORDS.search(scope) or _explicit_time(scope, scope))):
                result[field] = _at(value)
                continue
        if (field == 'execution_at' and not evidence and value is not None
                and value == action.get('remind_at') and not _DEADLINE.search(clause)
                and action.get('check_at') is None and action.get('check_date') is None
                and (_DATE_WORDS.search(scope) or _explicit_time(scope, scope))):
            result[field] = _at(value)
            continue
        if not isinstance(evidence, str) or not evidence or evidence not in scope:
            continue
        if marker is not None and not marker.search(scope):
            continue
        if marker is not None:
            # A date-only cutoff/check is still useful source text, but it
            # cannot acquire an invented clock or allocate personal capacity.
            result[evidence_field] = evidence
        if field == 'execution_at' and (re.search(re.escape(evidence) + r'\s*(?:之?前|以前|以内|之内)', scope)
                or '最迟' in evidence or re.search(r'最迟[^，,]{0,12}' + re.escape(evidence), scope)
                or re.search(re.escape(evidence) + r'\s*最迟', scope)
                or evidence == action.get('deadline_evidence') or evidence == action.get('check_evidence')):
            continue
        try:
            precise = _explicit_time(evidence, scope)
        except (ValueError, TypeError):
            precise = False
        if precise:
            result[field] = _at(value)
            if result[field] is not None:
                result[evidence_field] = evidence
    check_action = bool(re.match(r'^(?:检查|核实|查看|确认(?:是否|进度|收到)|核对(?:是否|进度|收到))', title))
    if check_action and result['check_at'] is None and action.get('check_date') is None:
        legacy_check = _at(action.get('remind_at'))
        if legacy_check is not None:
            result['check_at'] = legacy_check
            result['check_evidence'] = action.get('time_evidence') if isinstance(action.get('time_evidence'), str) and action['time_evidence'] in clause else clause
    if check_action and (result['check_at'] is not None or action.get('check_date') is not None):
        result['execution_at'] = None
        result['execution_evidence'] = ''
    for field, evidence_field, marker in (
        ('deadline_date', 'deadline_evidence', _DEADLINE), ('check_date', 'check_evidence', _CHECK),
    ):
        evidence = action.get(evidence_field)
        if not isinstance(evidence, str) or not evidence or evidence not in scope:
            if marker.search(scope) and len(list(_DATE_WORDS.finditer(scope))) == 1:
                evidence = scope
            else:
                continue
        if not marker.search(scope):
            continue
        result[evidence_field] = evidence
        if result[field.replace('_date', '_at')] is None or action.get(field) is not None:
            result[field] = _date_term(evidence, action.get(field), now)
    return result


def can_schedule(action):
    """Only my explicit execution appointment can allocate my capacity."""
    return isinstance(action, dict) and action.get('executor_kind') == 'self' and action.get('execution_at') is not None
