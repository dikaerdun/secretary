"""Conservative local interpretation of a change to a selected existing task.

Only a pending proposal is returned. Unsupported/vague language is saved by
the caller and sent back for clarification, never executed or assigned a time.
"""
from datetime import datetime, timedelta
import hashlib
import json
import re

from .crm import _identifier, _owner, _text
from .parser import _identifier_number
from .store import SHANGHAI


CHANGE_INTENT = re.compile(r'改到|改为|改期|推迟到|调整到|延后到|取消(?:原|这|该|此次|本次|任务|提醒|安排|拜访)')
NUM = r'[0-9零〇一二两三四五六七八九十百]+'


def _num(value):
    return _identifier_number(value)


def parse_change(text, now, task):
    if not isinstance(text,str) or not CHANGE_INTENT.search(text): return None
    if not task or task.get('status') != 'pending': raise ValueError('原任务已完成或取消，请核对后新建下一步')
    if re.search(r'(?:不要|不用|不想|不能|别|先不|暂不|暂缓|是否|要不要|能否)\s*(?:把[^，。]{0,20})?(?:取消|改到|改为|改期|推迟|调整|延后)',text):
        raise ValueError('原话包含否定或询问，请明确是否要变更；原安排仍然有效')
    if re.search(r'或者|或是|还是|或|待[^，。]{0,12}确认|等[^，。]{0,12}确认|如果|可能|也许|考虑|打算',text):
        raise ValueError('原话尚有候选或条件，请确定一个安排后再拟变更；原安排仍然有效')
    changed = re.search(r'(?:改到|改为|改期(?:到)?|推迟到|调整到|延后到)(.+)',text)
    if not changed:
        return {'action':'propose_cancel','task_id':task['id'],'title':task['title']}
    value=changed[1]
    current=datetime.fromtimestamp(now,SHANGHAI)
    target_day=None
    iso=re.search(r'(20\d{2})[-/](\d{1,2})[-/](\d{1,2})',value)
    calendar=re.search(r'(?:(' + NUM + r')年)?(' + NUM + r')月(' + NUM + r')(?:日|号)',value)
    relative=re.search(r'今天|明天|后天',value)
    weekday=re.search(r'(下周|本周|这周|周|星期)([一二三四五六日天])',value)
    if sum(len(list(pattern.finditer(value))) for pattern in (
            re.compile(r'20\d{2}[-/]\d{1,2}[-/]\d{1,2}'),
            re.compile(r'(?:'+NUM+r'年)?'+NUM+r'月'+NUM+r'(?:日|号)'),
            re.compile(r'今天|明天|后天'),re.compile(r'(?:下周|本周|这周|周|星期)[一二三四五六日天]'))) != 1:
        raise ValueError('请只指定一个改期日期；原安排仍然有效')
    try:
        if iso:
            target_day=datetime(int(iso[1]),int(iso[2]),int(iso[3]),tzinfo=SHANGHAI); date_end=iso.end()
        elif calendar:
            target_day=datetime(_num(calendar[1]) if calendar[1] else current.year,
                               _num(calendar[2]),_num(calendar[3]),tzinfo=SHANGHAI); date_end=calendar.end()
        elif relative:
            target_day=current.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days={'今天':0,'明天':1,'后天':2}[relative[0]])
            date_end=relative.end()
        elif weekday:
            day='一二三四五六日'.find(weekday[2].replace('天','日'))
            delta=day-current.weekday()
            if weekday[1]=='下周':delta+=7
            elif weekday[1] in ('周','星期') and delta<0:delta+=7
            target_day=current.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=delta)
            date_end=weekday.end()
    except (ValueError,TypeError): raise ValueError('改期日期无效，请补充具体年月日和时间') from None
    if target_day is None: raise ValueError('请补充改到哪一天，以及上午／下午几点；原安排仍然有效')
    value=value[date_end:]
    clock=re.search(r'(?<!\d)([01]?\d|2[0-3])[:：]([0-5]\d)(?!\d)',value)
    spoken=re.search(r'(上午|下午|晚上|早上|凌晨|中午)?\s*('+NUM+r')(?:点|时)(半|(?:'+NUM+r')分)?',value)
    if len(re.findall(r'(?<!\d)(?:[01]?\d|2[0-3])[:：][0-5]\d(?!\d)|(?:上午|下午|晚上|早上|凌晨|中午)?\s*'+NUM+r'(?:点|时)(?:半|(?:'+NUM+r')分)?',value)) != 1:
        raise ValueError('请只指定一个改期时刻；原安排仍然有效')
    if clock: hour,minute=int(clock[1]),int(clock[2])
    elif spoken:
        hour=_num(spoken[2]); period=spoken[1]
        if hour is None or not 0<=hour<=23 or (1<=hour<=12 and not period):
            raise ValueError('请说明上午还是下午；原安排仍然有效')
        if period in ('下午','晚上') and hour<12:hour+=12
        if period=='中午':
            if hour in (1,2):hour+=12
            elif hour not in (11,12,13,14):raise ValueError('中午时刻不明确，请使用24小时制具体时间')
        if period=='凌晨' and hour==12:hour=0
        minute=30 if spoken[3]=='半' else (_num(spoken[3][:-1]) if spoken[3] else 0)
    else: raise ValueError('请补充具体几点；只说下周或某一天不会自动安排')
    try: target=target_day.replace(hour=hour,minute=minute).timestamp()
    except (ValueError,TypeError): raise ValueError('改期时间无效，请核对几点几分') from None
    if target<=now+5: raise ValueError('改期时间已经过去，请指定未来时间')
    duration=task['duration_minutes']
    matches=list(re.finditer(r'('+NUM+r'|半|一个)\s*(分钟|小时)',value))
    durations=set()
    for match in matches:
        number=.5 if match[1]=='半' else 1 if match[1]=='一个' else _num(match[1])
        amount=number*(60 if match[2]=='小时' else 1) if number is not None else None
        if amount is None or amount!=int(amount) or not 5<=amount<=720:
            raise ValueError('预计用时需要为5至720分钟，请核对')
        durations.add(int(amount))
    if len(durations)>1: raise ValueError('发现多个不同用时，请核对原任务要改为多少分钟')
    if durations:duration=durations.pop()
    return {'action':'propose_change','task_id':task['id'],'title':task['title'],
            'remind_at':target,'duration_minutes':duration,'schedule_note':text[:500]}


class SpokenChangeService:
    def __init__(self,crm,clock):
        self.crm,self.clock=crm,clock
        with crm._transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS crm_spoken_changes (
                owner TEXT NOT NULL,source_id TEXT NOT NULL,record_id INTEGER NOT NULL,
                payload_hash TEXT NOT NULL,proposal_id INTEGER,activity_id INTEGER NOT NULL,
                message TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(owner,source_id),
                FOREIGN KEY(owner,record_id) REFERENCES crm_records(owner,id))''')

    def propose(self,owner,record_id,text,source_id):
        owner,record_id=_owner(owner),_identifier(record_id)
        text=_text(text,'补充原话',6000,required=True)
        source_id=_text(source_id,'提交标识',200,required=True)
        digest=hashlib.sha256(json.dumps([record_id,text],ensure_ascii=False).encode()).hexdigest()
        now=self.clock()
        with self.crm._transaction() as db:
            parent=self.crm._require_record(db,owner,record_id)
            previous=db.execute('SELECT * FROM crm_spoken_changes WHERE owner=? AND source_id=?',(owner,source_id)).fetchone()
            if previous:
                if previous['payload_hash']!=digest:raise ValueError('提交标识已用于其他内容，请刷新后重新提交')
                message=previous['message']; proposal_id=previous['proposal_id']
            else:
                detail=self.crm.record_detail(owner,record_id)
                activity_id=db.execute('INSERT INTO crm_activities(owner,record_id,content,created_at) VALUES (?,?,?,?)',
                                       (owner,record_id,text,now)).lastrowid
                proposal_id=None
                message='补充原话已保存，请明确要修改哪项安排及具体时间。'
                try: command=parse_change(text,now,detail.get('task'))
                except ValueError as exc:command=None; message='补充原话已保存。'+str(exc)
                if command:
                    message=self.crm._execute(db,owner,command,now)
                    match=re.search(r'P([0-9]+)',message)
                    if match:
                        proposal_id=int(match[1])
                        if parent['proposal_id']:
                            self.crm._remember_proposal(db,owner,record_id,parent['proposal_id'],now)
                        self.crm._remember_proposal(db,owner,record_id,proposal_id,now)
                        db.execute("UPDATE crm_records SET proposal_id=?,kind='action',updated_at=? WHERE owner=? AND id=?",
                                   (proposal_id,now,owner,record_id))
                db.execute('INSERT INTO crm_spoken_changes VALUES (?,?,?,?,?,?,?,?)',
                           (owner,source_id,record_id,digest,proposal_id,activity_id,message,now))
        return {**self.crm.record_detail(owner,record_id),'message':message,
                'needs_clarification':proposal_id is None,'original_record_id':record_id}
