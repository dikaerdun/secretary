"""Conservative, editable scene labels shared by every capture channel."""
import re

CATEGORIES = {
    'idea': '个人想法', 'meeting': '会议纪要', 'conversation': '客户交流',
    'visit_review': '拜访复盘', 'memo': '一般备忘',
}


def infer_category(text, customer_id=None):
    text = str(text or '')
    if any(word in text for word in ('拜访复盘', '交流复盘', '会后复盘', '拜访后的想法')):
        return 'visit_review'
    if any(word in text for word in ('会议纪要', '会议记录', '项目会议', '开会讨论', '参会人员')):
        return 'meeting'
    if any(word in text for word in ('有个想法', '一个想法', '想到一个', '灵感', '个人想法')):
        return 'idea'
    if customer_id is not None or any(word in text for word in ('客户', '拜访', '客户交流')) or re.search(r'[\u4e00-\u9fff]{1,4}总说', text):
        return 'conversation'
    return 'memo'


def resolve_category(value, text='', customer_id=None):
    if value == 'auto':
        return infer_category(text, customer_id)
    if not isinstance(value, str) or value not in CATEGORIES:
        raise ValueError('记录场景无效。')
    return value
