"""Durable, owner-scoped secretary conversations over existing CRM sources.

A direct instruction may apply an arrangement. Model-only suggestions never do.
Appointment time and reminder time stay separate, using the legacy task start
for conflict checks and its notification due time for the actual alert.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import json
import re
import secrets
import time

from .crm import _identifier, _owner, _text
from .store import SHANGHAI, _timestamp
from .secretary_interpreter import (SecretaryInterpreter, FIELDS, INTENTS,
    day_from_text, timestamp_from_text, reminder_minutes, is_conditional)
from .conversation_attachments import ConversationAttachments


def encoded(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False)


class FlowConflict(ValueError):
    pass


class SecretaryFlow:
    def __init__(self,crm,workspace,lock,*,interpreter=None,visits=None,timeline=None,clock=time.time):
        self.crm,self.workspace,self.lock=crm,workspace,lock
        self.interpreter=interpreter or SecretaryInterpreter()
        self.visits,self.timeline,self.clock=visits,timeline,clock
        self.closed=False
        with crm._transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS crm_secretary_plans(
                    id INTEGER PRIMARY KEY,owner TEXT NOT NULL,record_id INTEGER NOT NULL,
                    visit_id INTEGER,data_json TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,updated_at REAL NOT NULL,
                    UNIQUE(owner,id),FOREIGN KEY(owner,record_id) REFERENCES crm_records(owner,id));
                CREATE TABLE IF NOT EXISTS crm_secretary_turns(
                    id INTEGER PRIMARY KEY,owner TEXT NOT NULL,request_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,plan_id INTEGER,record_id INTEGER NOT NULL,
                    base_revision INTEGER,scope_json TEXT NOT NULL,source_kind TEXT NOT NULL,
                    status TEXT NOT NULL,reply TEXT NOT NULL DEFAULT '',question TEXT NOT NULL DEFAULT '',
                    data_json TEXT NOT NULL DEFAULT '{}',error TEXT NOT NULL DEFAULT '',
                    lease TEXT,claimed_at REAL,created_at REAL NOT NULL,updated_at REAL NOT NULL,
                    UNIQUE(owner,request_id),FOREIGN KEY(owner,record_id) REFERENCES crm_records(owner,id),
                    FOREIGN KEY(owner,plan_id) REFERENCES crm_secretary_plans(owner,id));
                CREATE INDEX IF NOT EXISTS secretary_turn_queue ON crm_secretary_turns(status,claimed_at,id);
                CREATE TABLE IF NOT EXISTS crm_secretary_plan_history(
                    id INTEGER PRIMARY KEY,owner TEXT NOT NULL,plan_id INTEGER NOT NULL,
                    turn_id INTEGER NOT NULL,data_json TEXT NOT NULL,revision INTEGER NOT NULL,created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS crm_secretary_settings(
                    owner TEXT PRIMARY KEY,business_context TEXT NOT NULL DEFAULT '',
                    remind_minutes INTEGER,revision INTEGER NOT NULL DEFAULT 1,updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS crm_secretary_prospects(
                    id INTEGER PRIMARY KEY,owner TEXT NOT NULL,name TEXT NOT NULL,
                    phone TEXT NOT NULL DEFAULT '',wechat TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',source_record_id INTEGER NOT NULL,
                    customer_id INTEGER,contact_id INTEGER,status TEXT NOT NULL DEFAULT 'new',
                    created_at REAL NOT NULL,updated_at REAL NOT NULL,UNIQUE(owner,id));
                CREATE TABLE IF NOT EXISTS crm_secretary_adoptions(
                    owner TEXT NOT NULL,turn_id INTEGER NOT NULL,action_index INTEGER NOT NULL,
                    record_id INTEGER NOT NULL,created_at REAL NOT NULL,
                    PRIMARY KEY(owner,turn_id,action_index));
                CREATE TABLE IF NOT EXISTS crm_secretary_source_history(
                    id INTEGER PRIMARY KEY,owner TEXT NOT NULL,record_id INTEGER NOT NULL,
                    content TEXT NOT NULL,created_at REAL NOT NULL,change_kind TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS crm_secretary_trash(
                    owner TEXT NOT NULL,record_id INTEGER NOT NULL,trashed_at REAL NOT NULL,
                    PRIMARY KEY(owner,record_id));
            """)
        self.attachments=ConversationAttachments(crm)
        self.matters = getattr(crm, 'matter_service', None)
        self.matter_router = None
        with crm._transaction() as db:
            columns = {r['name'] for r in db.execute('PRAGMA table_info(crm_secretary_turns)')}
            for name, definition in (('matter_id', 'INTEGER'), ('matter_revision', 'INTEGER'),
                                     ('matter_mode', "TEXT NOT NULL DEFAULT 'auto'")):
                if name not in columns:
                    db.execute('ALTER TABLE crm_secretary_turns ADD COLUMN ' + name + ' ' + definition)
        from .arrangement_queue import ArrangementQueue
        self.arrangements = ArrangementQueue(crm, workspace, lock, clock=clock, flow=self)
        with crm._transaction() as db:
            if 'arrangement_auto_check' not in {r['name'] for r in db.execute('PRAGMA table_info(crm_secretary_settings)')}:
                db.execute('ALTER TABLE crm_secretary_settings ADD COLUMN arrangement_auto_check INTEGER NOT NULL DEFAULT 0 CHECK(arrangement_auto_check IN (0,1))')

    def _require_plan(self,db,owner,identifier):
        row=db.execute("SELECT * FROM crm_secretary_plans WHERE owner=? AND id=?",(owner,_identifier(identifier))).fetchone()
        if row is None: raise KeyError("未找到你的交流计划。")
        if not db.execute('SELECT 1 FROM crm_records WHERE owner=? AND id=? AND hidden=0',(owner,row['record_id'])).fetchone():
            raise FlowConflict('这次交流已归档或移入回收站，请先恢复记录。')
        return row

    def _scope(self,db,owner,data):
        scope={}
        for key,table in (("customer_id","crm_customers"),("contact_id","crm_contacts"),("opportunity_id","crm_opportunities")):
            if data.get(key) is None: continue
            value=_identifier(data[key])
            row=db.execute("SELECT * FROM "+table+" WHERE owner=? AND id=?",(owner,value)).fetchone()
            if row is None: raise KeyError("未找到当前对象。")
            if "archived" in row.keys() and row["archived"]: raise ValueError("当前对象已归档。")
            scope[key]=value
            if key=="contact_id": scope.setdefault("customer_id",row["customer_id"])
            if key=="opportunity_id" and row["customer_id"] is not None:
                if scope.get("customer_id") not in (None,row["customer_id"]):
                    raise ValueError("项目与当前单位不一致。")
                scope["customer_id"]=row["customer_id"]
        if scope.get("contact_id") and scope.get("customer_id"):
            person=db.execute("SELECT * FROM crm_contacts WHERE owner=? AND id=?",(owner,scope["contact_id"])).fetchone()
            if person["customer_id"]!=scope["customer_id"]:
                if not scope.get("opportunity_id") or not any(p["contact_id"]==person["id"] and p.get("membership_valid")
                        for p in self.workspace.stakeholders(owner,scope["customer_id"],scope["opportunity_id"])["items"]):
                    raise ValueError("联系人未明确参与当前项目。")
        return scope

    def submit(self,owner,data):
        owner,now=_owner(owner),_timestamp(self.clock())
        if not isinstance(data,dict) or set(data)-{"text","request_id","plan_id","expected_revision","customer_id","contact_id","opportunity_id","original_transcript","source_kind","material_ids","matter_id","matter_revision","matter_mode"}:
            raise ValueError("秘书输入字段无效。")
        material_ids=self.attachments.identifiers(data.get("material_ids",[]))
        utterance=_text(data.get("text",""),"原话",20000,required=not material_ids)
        text=utterance.strip() or "补充交流材料"
        request_id=_text(data.get("request_id"),"提交标识",200,required=True).strip()
        original=_text(data.get("original_transcript",utterance),"原始转写",20000,required=not material_ids)
        source_kind=data.get("source_kind","user")
        if source_kind not in ("user","recording","recap"): raise ValueError("来源类型无效。")
        matter_mode = data.get('matter_mode', 'auto')
        if matter_mode not in ('auto', 'new', 'fresh', 'source', 'existing'):
            raise ValueError('请选择继续原事项或另记一件事。')
        digest=hashlib.sha256(encoded(data).encode()).hexdigest()
        with self.crm._transaction() as db:
            prior=db.execute("SELECT * FROM crm_secretary_turns WHERE owner=? AND request_id=?",(owner,request_id)).fetchone()
            if prior:
                if prior["payload_hash"]!=digest: raise ValueError("这次提交已经保存，请用新的提交标识记录修改。")
                return self._turn(db,prior)
            scope=self._scope(db,owner,data)
            matter = None
            if data.get('matter_id') is not None:
                if self.matters is None:
                    raise ValueError('事项服务暂未配置。')
                matter = self.matters.get(owner, data['matter_id'])
                if matter['visibility'] != 'active' or matter['status'] == 'ended':
                    raise FlowConflict('这件事已收起或结束，请先恢复；当前输入保留。')
                if type(data.get('matter_revision')) is not int or data['matter_revision'] != matter['revision']:
                    raise FlowConflict('这件事已有更新，请刷新后再补充；当前输入保留。')
                if db.execute("SELECT 1 FROM crm_secretary_turns WHERE owner=? AND matter_id=? AND status IN ('queued','processing')",
                    (owner, matter['id'])).fetchone():
                    raise FlowConflict('秘书正在整理这件事的上次输入，请等回执后继续。')
                for field in ('customer_id', 'opportunity_id'):
                    if scope.get(field) and matter.get(field) and scope[field] != matter[field]:
                        raise FlowConflict('当前事项归属不同，请另记一件事。')
                    if not scope.get(field) and matter.get(field):
                        scope[field] = matter[field]
            plan=None
            if data.get("plan_id") is not None:
                plan=self._require_plan(db,owner,data["plan_id"])
                if type(data.get("expected_revision")) is not int or data["expected_revision"]!=plan["revision"]:
                    raise FlowConflict("这次交流已有更新，原安排保留，请刷新后再补充。")
                if db.execute("SELECT 1 FROM crm_secretary_turns WHERE owner=? AND plan_id=? AND status IN ('queued','processing')",(owner,plan["id"])).fetchone():
                    raise FlowConflict("秘书正在整理刚才的补充，请等回执后继续；当前输入保留。")
                known=json.loads(plan["data_json"])
                if scope.get('customer_id') and known.get('customer_id') and scope['customer_id']!=known['customer_id']:
                    raise FlowConflict('这次交流已有客户归属；请另建交流，保留原客户历史。')
                if scope.get('opportunity_id') and known.get('opportunity_id') and scope['opportunity_id']!=known['opportunity_id']:
                    raise FlowConflict('这次交流属于另一个项目，请另记一件事；原安排保留。')
                for key in ("customer_id","contact_id","opportunity_id"):
                    if key not in scope and known.get(key) is not None: scope[key]=known[key]
                self._scope(db,owner,scope)
                if self.matters and matter is None:
                    associated = self.matters.resolve(owner, 'plan', plan['id'])['items']
                    if len(associated) == 1 and associated[0]['visibility'] == 'active' and associated[0]['status'] != 'ended':
                        matter = self.matters.get(owner, associated[0]['id'])
            elif matter and matter_mode not in ('new', 'fresh', 'source') and re.search(r'改到|改成|改期|取消.*(?:约|会|安排)|提前.*提醒|已经约好|主要聊|地点[是改]', text):
                active = [p for p in matter.get('plans', []) if p.get('status') not in ('recapped','cancelled')]
                if len(active) == 1:
                    plan = self._require_plan(db, owner, active[0]['id'])
            material_ids=self.attachments.validate(db,owner,material_ids,scope)
            record_id=db.execute("""INSERT INTO crm_records
                (owner,source_id,title,content,original_content,source,category,customer_id,status,classified,created_at,updated_at)
                VALUES (?,?,?,?,?,'web','idea',?,'following',1,?,?)""",
                (owner,"secretary:"+request_id,text[:120],text,original,scope.get("customer_id"),now,now)).lastrowid
            if "original_transcript" in data:
                db.execute("INSERT INTO crm_record_transcripts VALUES (?,?,?,?,?,?)",(owner,record_id,original,text,now,now))
            turn_id=db.execute("""INSERT INTO crm_secretary_turns
                (owner,request_id,payload_hash,plan_id,record_id,base_revision,scope_json,source_kind,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,'queued',?,?)""",
                (owner,request_id,digest,plan["id"] if plan else None,record_id,plan["revision"] if plan else None,encoded(scope),source_kind,now,now)).lastrowid
            self.attachments.save(db,owner,turn_id,material_ids,now)
            db.execute('UPDATE crm_secretary_turns SET matter_id=?,matter_revision=?,matter_mode=? WHERE owner=? AND id=?',
                (matter['id'] if matter else None, matter['revision'] if matter else None, matter_mode, owner, turn_id))
            self.attachments.link(db,owner,material_ids,scope,now,plan_id=plan["id"] if plan else None,
                visit_id=plan["visit_id"] if plan else None,link_project=self._link_project)
            db.execute("INSERT INTO crm_secretary_source_history(owner,record_id,content,created_at,change_kind) VALUES (?,?,?,?,'original')",(owner,record_id,original,now))
            if plan:
                generation = self.arrangements.begin_hold(db, owner, plan['id'], turn_id, now)
                db.execute('UPDATE crm_secretary_turns SET data_json=? WHERE owner=? AND id=?',
                    (encoded({'arrangement_hold_generation': generation}), owner, turn_id))
        return self.turn(owner,turn_id)

    def _release_arrangement_hold(self, db, row):
        if not row['plan_id']:
            return
        generation = json.loads(row['data_json']).get('arrangement_hold_generation')
        if generation is not None:
            self.arrangements.release_hold(db, row['owner'], row['plan_id'], row['id'], generation, self.clock())

    def _turn(self,db,row,*,with_plan=True):
        record=self.crm.get_record(row["owner"],row["record_id"])
        result={key:row[key] for key in ("id","plan_id","record_id","status","reply","question","error","created_at","updated_at")}
        result["text"]=record["content"] if record else ""
        result["result"]=json.loads(row["data_json"])
        route = result['result'].get('matter_route') or {}
        if self.matters:
            from .matter_correction import effective_route
            route = effective_route(self, row['owner'], row['id'], route)
            result['result']['matter_route'] = route
        result['matter_route'] = route
        result['matter_id'] = route['matter_id'] if 'matter_id' in route else row['matter_id']
        if result['matter_id'] and self.matters:
            result['matter'] = self.matters.get(row['owner'], result['matter_id'])
        result["attachments"]=self.attachments.read(db,row["owner"],self.attachments.ids(db,row["owner"],turn_id=row["id"],plan_id=row["plan_id"]))
        result["attachment_status"]=self.attachments.state(result["attachments"])
        result["attachment_message"]=self.attachments.message(result["attachments"])
        if not result["reply"] and result["attachment_message"]:result["reply"]=result["attachment_message"]
        result["adoptions"]={str(r["action_index"]):r["record_id"] for r in db.execute(
            "SELECT action_index,record_id FROM crm_secretary_adoptions WHERE owner=? AND turn_id=?",(row["owner"],row["id"]))}
        if with_plan and row["plan_id"]:
            result["plan"]=self._public_plan(db,self._require_plan(db,row["owner"],row["plan_id"]))
        return result

    def turn(self,owner,identifier):
        owner,identifier=_owner(owner),_identifier(identifier)
        with self.crm._lock:
            row=self.crm._db.execute("SELECT * FROM crm_secretary_turns WHERE owner=? AND id=?",(owner,identifier)).fetchone()
            if row is None: raise KeyError("未找到这条原话。")
            return self._turn(self.crm._db,row)

    def _public_plan(self,db,row,*,turns=False):
        data={**self._default_plan(),**json.loads(row["data_json"])}
        result={**data,"id":row["id"],"record_id":row["record_id"],"visit_id":row["visit_id"],
            "revision":row["revision"],"created_at":row["created_at"],"updated_at":row["updated_at"]}
        result["attachments"]=self.attachments.read(db,row["owner"],self.attachments.ids(db,row["owner"],plan_id=row["id"]))
        customer=db.execute("SELECT name FROM crm_customers WHERE owner=? AND id=?",(row["owner"],data.get("customer_id"))).fetchone()
        result["customer_name"]=customer["name"] if customer else ""
        active=db.execute('SELECT id,title,remind_at,status FROM tasks WHERE owner=? AND id=?',(row['owner'],data.get('task_id'))).fetchone()
        result['active_schedule']=dict(active) if active and active['status']=='pending' else None
        result['arrangement'] = self.arrangements.projection(db, row)
        result['active_schedule'] = result['arrangement']['active_schedule']
        result['blocking_reasons'] = result['arrangement']['blocking_reasons']
        result['can_apply'] = result['arrangement']['can_apply']
        if turns:
            result["turns"]=[self._turn(db,r,with_plan=False) for r in db.execute(
                "SELECT * FROM crm_secretary_turns WHERE owner=? AND plan_id=? ORDER BY id",(row["owner"],row["id"]))]
            result["history"]=[{"revision":r["revision"],"created_at":r["created_at"],"data":json.loads(r["data_json"])}
                for r in db.execute("SELECT * FROM crm_secretary_plan_history WHERE owner=? AND plan_id=? ORDER BY id",(row["owner"],row["id"]))]
            result['arrangement_history'] = [
                {'operation': r['operation'], 'revision': r['result_revision'], 'created_at': r['created_at'],
                 'receipt': json.loads(r['response_json']).get('receipt', '')}
                for r in db.execute('SELECT * FROM crm_arrangement_operations WHERE owner=? AND plan_id=? ORDER BY id', (row['owner'], row['id']))]
        return result

    def plan(self,owner,identifier):
        owner=_owner(owner)
        with self.crm._lock: return self._public_plan(self.crm._db,self._require_plan(self.crm._db,owner,identifier),turns=True)

    def for_record(self,owner,identifier):
        owner,identifier=_owner(owner),_identifier(identifier)
        with self.crm._lock:
            row=self.crm._db.execute('''SELECT p.* FROM crm_secretary_plans p
                JOIN crm_secretary_turns t ON t.owner=p.owner AND t.plan_id=p.id
                JOIN crm_records r ON r.owner=t.owner AND r.id=t.record_id AND r.hidden=0
                JOIN crm_records root ON root.owner=p.owner AND root.id=p.record_id AND root.hidden=0
                WHERE t.owner=? AND t.record_id=? ORDER BY p.id DESC LIMIT 1''',(owner,identifier)).fetchone()
            return self._public_plan(self.crm._db,row) if row else None

    def list_plans(self,owner,*,customer_id=None,contact_id=None,opportunity_id=None,visit_id=None,limit=30):
        owner=_owner(owner)
        if type(limit) is not int or not 1<=limit<=200: raise ValueError("数量无效。")
        with self.crm._lock:
            rows=self.crm._db.execute("SELECT p.* FROM crm_secretary_plans p JOIN crm_records r ON r.owner=p.owner AND r.id=p.record_id AND r.hidden=0 WHERE p.owner=? ORDER BY p.updated_at DESC,p.id DESC LIMIT 200",(owner,)).fetchall()
            items=[self._public_plan(self.crm._db,r) for r in rows]
            for key,value in (("customer_id",customer_id),("contact_id",contact_id),("opportunity_id",opportunity_id),("visit_id",visit_id)):
                if value is not None:
                    value=_identifier(value);items=[p for p in items if p.get(key)==value]
            return {"items":items[:limit],"total":len(items),"truncated":len(rows)==200}

    def recent_turns(self,owner,limit=10):
        owner=_owner(owner)
        with self.crm._lock:
            rows=self.crm._db.execute("""SELECT t.* FROM crm_secretary_turns t
                JOIN crm_records r ON r.owner=t.owner AND r.id=t.record_id AND r.hidden=0
                LEFT JOIN crm_secretary_plans p ON p.owner=t.owner AND p.id=t.plan_id
                LEFT JOIN crm_records root ON root.owner=p.owner AND root.id=p.record_id
                LEFT JOIN crm_secretary_trash x ON x.owner=t.owner AND x.record_id=t.record_id
                WHERE t.owner=? AND x.record_id IS NULL AND (t.plan_id IS NULL OR root.hidden=0)
                ORDER BY t.id DESC LIMIT ?""",(owner,limit)).fetchall()
            return {"items":[self._turn(self.crm._db,r) for r in rows]}

    def settings(self,owner):
        owner=_owner(owner)
        with self.crm._lock:
            row=self.crm._db.execute("SELECT * FROM crm_secretary_settings WHERE owner=?",(owner,)).fetchone()
        return {"business_context":row["business_context"] if row else "",
            "remind_minutes":row["remind_minutes"] if row else None,"revision":row["revision"] if row else 0,
            "arrangement_auto_check": bool(row['arrangement_auto_check']) if row else False}

    def update_settings(self,owner,data):
        owner=_owner(owner)
        if not isinstance(data,dict) or set(data)-{"business_context","remind_minutes","expected_revision","arrangement_auto_check"}:
            raise ValueError("秘书偏好字段无效。")
        existing=self.settings(owner)
        if data.get("expected_revision")!=existing["revision"]: raise FlowConflict("秘书偏好已有更新，请刷新。")
        text=_text(data.get("business_context",existing["business_context"]),"我方资料",12000)
        minutes=data.get("remind_minutes",existing["remind_minutes"])
        if minutes is not None and (type(minutes) is not int or not 0<=minutes<=10080):
            raise ValueError("提前提醒需要为0至10080分钟。")
        auto_check = data.get('arrangement_auto_check', existing['arrangement_auto_check'])
        if type(auto_check) is not bool:
            raise ValueError('自动推进点偏好需要为明确的开或关。')
        with self.crm._transaction() as db:
            actual=db.execute("SELECT revision FROM crm_secretary_settings WHERE owner=?",(owner,)).fetchone()
            if (actual["revision"] if actual else 0)!=existing["revision"]: raise FlowConflict("秘书偏好已有更新，请刷新。")
            db.execute("""INSERT INTO crm_secretary_settings(owner,business_context,remind_minutes,revision,updated_at) VALUES (?,?,?,?,?)
                ON CONFLICT(owner) DO UPDATE SET business_context=excluded.business_context,
                remind_minutes=excluded.remind_minutes,revision=excluded.revision,updated_at=excluded.updated_at""",
                (owner,text,minutes,existing["revision"]+1,self.clock()))
            db.execute('UPDATE crm_secretary_settings SET arrangement_auto_check=? WHERE owner=?', (int(auto_check), owner))
        return self.settings(owner)

    def _context(self,db,row):
        scope=json.loads(row["scope_json"])
        plan=self._public_plan(db,self._require_plan(db,row["owner"],row["plan_id"])) if row["plan_id"] else None
        customers=[{"id":r["id"],"name":r["name"],"aliases":json.loads(r["aliases_json"]) if "aliases_json" in r.keys() else []}
            for r in db.execute("SELECT * FROM crm_customers WHERE owner=? ORDER BY updated_at DESC LIMIT 100",(row["owner"],))]
        contacts=[dict(r) for r in db.execute("SELECT id,customer_id,name,role,department FROM crm_contacts WHERE owner=? AND archived=0 LIMIT 200",(row["owner"],))]
        projects=[{"id":r["id"],"customer_id":r["customer_id"],"name":r["name"]} for r in db.execute(
            "SELECT * FROM crm_opportunities WHERE owner=? AND archived=0 LIMIT 100",(row["owner"],))]
        # A bounded catalogue must always contain the explicitly selected scope.
        selected={**{k:(plan or {}).get(k) for k in ('customer_id','contact_id','opportunity_id')},**scope}
        for key,items,table,fields in (
            ('customer_id',customers,'crm_customers',('id','name','aliases_json')),
            ('contact_id',contacts,'crm_contacts',('id','customer_id','name','role','department')),
            ('opportunity_id',projects,'crm_opportunities',('id','customer_id','name'))):
            identifier=selected.get(key)
            if identifier and not any(item['id']==identifier for item in items):
                entity=db.execute('SELECT * FROM '+table+' WHERE owner=? AND id=?',(row['owner'],identifier)).fetchone()
                if entity:
                    value={k:entity[k] for k in fields}
                    if 'aliases_json' in value:value['aliases']=json.loads(value.pop('aliases_json'))
                    items.append(value)
        history=[]
        facts=[];project_context=None
        target=scope.get("customer_id") or (plan or {}).get("customer_id")
        if target:
            history=[{"id":r["id"],"title":r["title"],"content":r["content"][:1800],"created_at":r["created_at"]}
                for r in db.execute("SELECT * FROM crm_records WHERE owner=? AND customer_id=? AND hidden=0 ORDER BY id DESC LIMIT 8",(row["owner"],target))]
            if hasattr(self.crm,'profile'):
                facts=[{k:r[k] for k in ('contact_id','key','value','basis','evidence')} for r in db.execute(
                    'SELECT * FROM crm_customer_facts WHERE owner=? AND customer_id=? ORDER BY id DESC LIMIT 12',(row['owner'],target))]
            project_id=scope.get('opportunity_id') or (plan or {}).get('opportunity_id')
            if project_id:
                project=db.execute('SELECT * FROM crm_opportunities WHERE owner=? AND id=?',(row['owner'],project_id)).fetchone()
                if project:project_context={k:project[k] for k in ('name','stage','amount_cents','amount_type','approval','scope','decision_chain','blockers','milestones','notes')}
        matter = self.matters.get(row['owner'], row['matter_id']) if self.matters and row['matter_id'] else None
        return {"plan":plan,"scope":scope,"matter":matter,"source_kind":row["source_kind"],"customers":customers,
            "contacts":contacts,"projects":projects,"recent_records":history,"customer_facts":facts,
            "project_context":project_context,"preferences":self.settings(row["owner"]),
            "attachments":self.attachments.read(db,row["owner"],self.attachments.ids(db,row["owner"],turn_id=row["id"],plan_id=row["plan_id"]),context=True)}

    @staticmethod
    def _default_plan():
        return {"title":"","person":"","date":None,"start_at":None,"activity":"meal","goal":"","topics":[],
            "place":"","booking":"unknown","status":"preparing","remind_minutes":None,"reminder_at":None,
            "duration_minutes":30,"duration_estimated":True,"customer_id":None,"contact_id":None,
            "opportunity_id":None,"task_id":None,"question":"","preparation":{},"suggestions":[],"identity_candidates":[]}

    def _checked_changes(self,raw,text,now,current,context):
        changes,evidence=raw.get("changes") or {},raw.get("evidence") or {}
        if not isinstance(changes,dict) or not isinstance(evidence,dict) or set(changes)-FIELDS:
            raise ValueError("秘书返回了无法识别的字段，原话已保留。")
        result,issues={},[]
        for key,value in changes.items():
            if key=="title":
                if isinstance(value,str) and value.strip(): result[key]=value.strip()[:120]
                continue
            quote=evidence.get(key)
            scope=context["scope"]
            if key in ("customer_id","contact_id","opportunity_id"):
                if value is None: continue
                table={"customer_id":"customers","contact_id":"contacts","opportunity_id":"projects"}[key]
                if type(value) is not int or not any(item["id"]==value for item in context[table]):
                    issues.append("识别到的对象不在你的资料中，请核对。");continue
                if value==scope.get(key) or value==current.get(key):
                    result[key]=value;continue
                chosen=next(item for item in context[table] if item["id"]==value)
                tokens=[chosen["name"],*chosen.get("aliases",[])]
                if isinstance(quote,str) and quote and quote in text and any(token in text for token in tokens):
                    matches=[p for p in context[table] if any(token and token in text for token in [p["name"],*p.get("aliases",[])])]
                    if len(matches)==1:result[key]=value
                    else:issues.append("有同名或相近对象，请明确这次是哪一位。")
                else:
                    if isinstance(quote,str) and quote and quote in text:
                        context.setdefault('identity_candidates',[]).append({'field':key,'id':value,'name':chosen['name'],
                            'customer_id':chosen.get('customer_id',value if key=='customer_id' else None)})
                    issues.append("这个称呼可能对应已有资料，请点选秘书识别的对象，或先保留待核对。")
                continue
            if not isinstance(quote,str) or not quote or quote not in text:
                if value is not None: issues.append("有信息缺少这次原话依据，请补充。")
                continue
            try:
                if key=="date":
                    day=day_from_text(quote,now,current.get("date"))
                    if day: result[key]=day
                elif key in ("start_at","reminder_at"):
                    if key=='start_at' and value is None and re.search(r'时间(?:还|尚)?(?:没定|未定|待定)|钟点(?:还)?没定',quote):
                        result[key]=None;result['booking']='tentative';continue
                    point=timestamp_from_text(quote,now,result.get("date") or current.get("date"))
                    if point is None: continue
                    if point<=now+5: issues.append("这个时刻已经过去，请补充未来时间。");continue
                    if point>now+10*366*86400: issues.append("请核对日期，安排需要在未来十年内。");continue
                    result[key]=point
                    if key=="start_at":result["date"]=datetime.fromtimestamp(point,SHANGHAI).date().isoformat()
                elif key=="remind_minutes":
                    minutes=reminder_minutes(quote)
                    if minutes is not None and 0<=minutes<=10080:result[key]=minutes
                elif key=="booking":
                    if value=="confirmed" and re.search(r"约好|确定(?:了|时间)|对方答应|已经定",quote) and not re.search(r"没|未|不|待|准备",quote): result[key]=value
                    elif value=="cancelled" and re.search(r"取消|不去|不约|不见",quote) and not is_conditional(text):result[key]=value
                    elif value in ("unknown","tentative"):result[key]=value
                elif key=="activity":
                    if value in ("meal","visit","call","task"):result[key]=value
                elif key=="topics":
                    if isinstance(value,list) and len(value)<=12 and all(isinstance(v,str) and len(v)<=300 for v in value):result[key]=value
                elif key=="duration_minutes":
                    if type(value) is int and 5<=value<=720 and re.search(r"用时|持续|大约|预计|聊.{0,3}(?:分钟|小时)",quote):
                        result[key]=value;result["duration_estimated"]=False
                elif key in ("person","goal","place","phone","wechat","business_context"):
                    if isinstance(value,str) and len(value)<= (12000 if key=="business_context" else 2000):result[key]=value
            except ValueError as exc:issues.append(str(exc))
        if result.get("contact_id"):
            person=next(p for p in context["contacts"] if p["id"]==result["contact_id"])
            result.setdefault("customer_id",person["customer_id"])
            result.setdefault("person",person["name"])
        if 'goal' not in result and not current.get('goal'):
            goal=re.search(r'(?:主要(?:想)?(?:聊|谈)|目标是|目的是|希望达成)([^，。；,.\n]+)',text)
            if goal:result['goal']=goal[1].strip()[:2000]
        if is_conditional(text):
            for key in ("start_at","reminder_at","remind_minutes","booking"):result.pop(key,None)
        if context["source_kind"]=="recording":
            for key in ("start_at","reminder_at","remind_minutes","booking"):result.pop(key,None)
        return result,issues

    @staticmethod
    def _bounded_suggestions(raw,text):
        result=[]
        for item in (raw.get("suggestions") or [])[:3]:
            if not isinstance(item,dict):continue
            title,reason=item.get("title"),item.get("reason","")
            if isinstance(title,str) and 0<len(title)<=120 and isinstance(reason,str) and len(reason)<=1000:
                evidence=item.get("evidence","")
                result.append({"title":title,"reason":reason,"evidence":evidence if isinstance(evidence,str) and evidence in text else ""})
        return result

    @staticmethod
    def _preparation(raw,attachments=None):
        value=raw.get("preparation") or {}
        if not isinstance(value,dict):return {}
        result={"objective":str(value.get("objective",""))[:600],
            "questions":[v[:500] for v in value.get("questions",[])[:3] if isinstance(v,str)],
            "materials":[v[:500] for v in value.get("materials",[])[:3] if isinstance(v,str)]}
        sources={item['material_id']:item for item in (attachments or []) if item.get('readable')}
        references=value.get('references') or []
        result['references']=[]
        if isinstance(references,list):
            for item in references[:6]:
                if not isinstance(item,dict):continue
                identifier,quote=item.get('material_id'),item.get('quote')
                source=sources.get(identifier) if type(identifier) is int else None
                if source and isinstance(quote,str) and 0<len(quote)<=1000 and quote in source.get('text','') and (
                        'version_id' not in item or item['version_id']==source['version_id']):
                    reference={'material_id':identifier,'version_id':source['version_id'],'quote':quote}
                    if reference not in result['references']:result['references'].append(reference)
        return result

    def _question(self,plan,issues,preference=None):
        if issues:return issues[0]
        if plan.get("identity_question"):return plan["identity_question"]
        if not plan.get("goal"):return "这次主要想谈什么，希望推进到什么结果？想到的几件事直接告诉我就行。"
        if not plan.get("date"):return "准备安排在哪一天？还没定也可以先保留。"
        if plan.get("start_at") is None:return "这次准备几点？还没约定可以先留在当天的时间待定计划。"
        if plan["activity"] in ("meal","visit") and plan["booking"]=="unknown":return "这次已经约好，还是准备去约？"
        return issues[0] if issues else ""

    def _reply(self,plan,question):
        if plan["status"]=="cancelled":return "已取消这次安排，相关待发送提醒已停止，历史记录保留。"
        day=plan.get("date") or "日期待定"
        at=datetime.fromtimestamp(plan["start_at"],SHANGHAI).strftime("%H:%M") if plan.get("start_at") else "时间待定"
        text=f"已记下：{day} {at}，{plan['title']}。"
        if plan.get("goal"):text+="目标："+plan["goal"]+"。"
        if plan["status"]=="scheduled" and plan.get("reminder_at"):
            text+="提醒："+datetime.fromtimestamp(plan["reminder_at"],SHANGHAI).strftime("%m月%d日 %H:%M")+"。"
        if question:text+=question
        return text

    def _link_timeline(self,owner,record_id,plan,*,recap=False):
        if not self.timeline or not plan.get("contact_id"):return
        event=self.timeline.get_event(owner,"record:"+str(record_id))
        data={"expected_revision":event["revision"],"kind":"communication" if recap else "reflection",
            "contact_relations":[{"contact_id":plan["contact_id"],"relation":"direct" if recap else "about"}]}
        self.timeline.save_context(owner,event["key"],data)

    def _link_project(self,db,owner,kind,identifier,project_id,now):
        entity=self.workspace._entity(db,owner,kind,identifier)
        self.workspace._require_opportunity(db,owner,entity['customer_id'],project_id)
        snapshot=self.workspace._link_snapshot(db,owner,kind,entity)
        previous=db.execute('SELECT * FROM crm_opportunity_links WHERE owner=? AND entity_type=? AND entity_id=?',(owner,kind,identifier)).fetchone()
        if previous and previous['source_snapshot']==snapshot and previous['opportunity_id']==project_id:return
        revision=previous['revision']+1 if previous else 1
        db.execute('INSERT INTO crm_opportunity_links VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(owner,entity_type,entity_id) DO UPDATE SET customer_id=excluded.customer_id,opportunity_id=excluded.opportunity_id,revision=excluded.revision,source_snapshot=excluded.source_snapshot,updated_at=excluded.updated_at',
            (owner,kind,identifier,entity['customer_id'],project_id,revision,snapshot,now))
        db.execute('INSERT INTO crm_opportunity_link_history(owner,entity_type,entity_id,customer_id,opportunity_id,revision,source_snapshot,created_at) VALUES (?,?,?,?,?,?,?,?)',
            (owner,kind,identifier,entity['customer_id'],project_id,revision,snapshot,now))

    async def process_one(self):
        if self.closed:return False
        now,lease=_timestamp(self.clock()),secrets.token_hex(16)
        async with self.lock:
            with self.crm._transaction() as db:
                row=None
                for candidate in db.execute("""SELECT t.* FROM crm_secretary_turns t
                        JOIN crm_records r ON r.owner=t.owner AND r.id=t.record_id AND r.hidden=0
                        LEFT JOIN crm_secretary_plans p ON p.owner=t.owner AND p.id=t.plan_id
                        LEFT JOIN crm_records root ON root.owner=p.owner AND root.id=p.record_id
                        WHERE (t.status='queued' OR (t.status='processing' AND t.claimed_at<?))
                        AND (t.plan_id IS NULL OR root.hidden=0) ORDER BY t.id""",(now-240,)):
                    sources=self.attachments.read(db,candidate['owner'],self.attachments.ids(db,candidate['owner'],turn_id=candidate['id'],plan_id=candidate['plan_id']))
                    if self.attachments.state(sources)!='waiting':row=candidate;break
                if row is None:return False
                db.execute("UPDATE crm_secretary_turns SET status='processing',lease=?,claimed_at=?,updated_at=? WHERE id=?",(lease,now,now,row["id"]))
                record=self.crm._require_record(db,row["owner"],row["record_id"])
                text=record["content"]
                context=self._context(db,row)
        try:
            raw=await asyncio.wait_for(self.interpreter.interpret(text,row['created_at'],context),75)
            if not isinstance(raw,dict) or raw.get("intent") not in INTENTS:raise ValueError("这次理解未完成，原话已保留。")
            if self.matter_router:
                mode = 'new' if row['matter_mode'] == 'fresh' else row['matter_mode']
                if row['source_kind'] == 'recording' or is_conditional(text):
                    mode = 'source'
                if raw['intent'] in ('settings', 'contact', 'work_plan'):
                    raw['_matter_route'] = {'kind': 'source', 'reason': '保存资料或查询，不另建跟进事项。'}
                else:
                    raw['_matter_route'] = await asyncio.wait_for(self.matter_router.route(row['owner'], text,
                        scope=context['scope'], matter_id=row['matter_id'], mode=mode), 75)
        except asyncio.CancelledError:
            with self.crm._transaction() as db:db.execute("UPDATE crm_secretary_turns SET status='queued',lease=NULL,claimed_at=NULL WHERE id=? AND lease=?",(row["id"],lease))
            raise
        except Exception:
            with self.crm._transaction() as db:
                db.execute("UPDATE crm_secretary_turns SET status='failed',error=?,lease=NULL,claimed_at=NULL,updated_at=? WHERE id=? AND lease=?",
                    ("原话已保存，秘书暂未整理完成。可以继续补充或重试。",self.clock(),row["id"],lease))
                self._release_arrangement_hold(db,row)
            return True
        async with self.lock:
            try:
                with self.crm._transaction() as db:
                    self._apply_turn(db,row,lease,text,now,context,raw)
            except Exception as error:
                with self.crm._transaction() as db:
                    db.execute("UPDATE crm_secretary_turns SET status='failed',error=?,lease=NULL,claimed_at=NULL,updated_at=? WHERE id=? AND lease=?",
                        ("原话已保留，这次整理未完成。请重试；原来的安排保持不变。",self.clock(),row["id"],lease))
                    from .matters import MatterConflict
                    if isinstance(error, MatterConflict):
                        db.execute("UPDATE crm_secretary_turns SET status='needs_attention',error=? WHERE id=?", (str(error), row['id']))
                    self._release_arrangement_hold(db,row)
        return True

    def _apply_turn(self,db,row,lease,text,now,context,raw):
        fresh=db.execute("SELECT * FROM crm_secretary_turns WHERE id=? AND lease=? AND status='processing'",(row["id"],lease)).fetchone()
        if fresh is None:return True
        generation = json.loads(row['data_json']).get('arrangement_hold_generation')
        if row['plan_id'] and generation is not None:
            hold = db.execute('SELECT hold_turn_id,hold_generation,followup_hold_until FROM crm_secretary_plans WHERE owner=? AND id=?', (row['owner'],row['plan_id'])).fetchone()
            if not hold or hold['hold_turn_id'] != row['id'] or hold['hold_generation'] != generation or (hold['followup_hold_until'] or 0) <= self.clock():
                db.execute("UPDATE crm_secretary_turns SET status='failed',error=?,lease=NULL,claimed_at=NULL WHERE id=? AND lease=?",
                    ('这次整理已超时或被更新，原话已保留；请明确重试。',row['id'],lease))
                self._release_arrangement_hold(db,row)
                return True
        decision = raw.get('_matter_route')
        if self.matters and decision:
            from .matter_flow_integration import guard_decision
            guard_decision(self.matters, row['owner'], decision)
        if self.matters and row['matter_id']:
            current_matter = self.matters.get(row['owner'], row['matter_id'])
            if current_matter['visibility'] != 'active' or current_matter['status'] == 'ended' or current_matter['revision'] != row['matter_revision']:
                from .matters import MatterConflict
                raise MatterConflict('这件事已有更新或已收起，原话已保留，请按最新进展重新核对。')
        visible=db.execute('SELECT 1 FROM crm_records WHERE owner=? AND id=? AND hidden=0',(row['owner'],row['record_id'])).fetchone()
        if visible and row['plan_id']:
            visible=db.execute('''SELECT 1 FROM crm_secretary_plans p JOIN crm_records r
                ON r.owner=p.owner AND r.id=p.record_id WHERE p.owner=? AND p.id=? AND r.hidden=0''',
                (row['owner'],row['plan_id'])).fetchone()
        if not visible:
            db.execute("""UPDATE crm_secretary_turns SET status='needs_attention',error=?,lease=NULL,
                claimed_at=NULL,updated_at=? WHERE id=? AND lease=?""",
                ('这条记录已收起，已停止整理；恢复后可明确重试。',now,row['id'],lease))
            self._release_arrangement_hold(db,row)
            return True
        attachments=self.attachments.read(db,row['owner'],self.attachments.ids(db,row['owner'],turn_id=row['id'],plan_id=row['plan_id']))
        if self.attachments.stamp(attachments)!=self.attachments.stamp(context.get('attachments',[])):
            db.execute("UPDATE crm_secretary_turns SET status='queued',reply=?,lease=NULL,claimed_at=NULL,updated_at=? WHERE id=? AND lease=?",
                ('附件内容已更新，原话已保留，秘书将依据最新材料重新整理。',now,row['id'],lease))
            return True
        separate = bool(decision and decision.get('kind') in ('new', 'ambiguous') and row['plan_id'])
        planrow=self._require_plan(db,row["owner"],row["plan_id"]) if row["plan_id"] else None
        if planrow and planrow["revision"]!=row["base_revision"]:
            db.execute("UPDATE crm_secretary_turns SET status='needs_attention',error=?,lease=NULL,claimed_at=NULL WHERE id=?",
                ("这次交流已有更新，补充原话已保留，请按最新内容继续。",row["id"]))
            self._release_arrangement_hold(db,row)
            return True
        if separate: planrow = None
        current={**self._default_plan(),**json.loads(planrow["data_json"])} if planrow else self._default_plan()
        from .arrangement_semantics import checked_arrangement, add_execution_evidence, activity_from_instruction, SETTLEMENT_MARKER
        semantic = checked_arrangement(raw, text, row['created_at'], current, row['source_kind'])
        checked_raw = dict(raw)
        checked_raw['changes'] = dict(raw.get('changes') or {})
        for key in ('date', 'start_at'):
            quote = (raw.get('evidence') or {}).get(key, '')
            if isinstance(quote, str) and re.search(SETTLEMENT_MARKER+r'|再问|再看|回看|再催|再跟进', quote):
                checked_raw['changes'].pop(key, None)
        changes,issues=self._checked_changes(checked_raw,text,row['created_at'],current,context)
        issues = semantic['issues'] + issues
        suggestions=self._bounded_suggestions(raw,text)
        preparation=self._preparation(raw,context.get('attachments'))
        intent=raw["intent"]
        if semantic['source_authorized'] and semantic['changes'].get('proposed_execution') and not planrow and intent=='note':
            intent='plan'
            # The guarded date fallback creates an activity even if the model
            # returned note. Do not inherit the legacy default meal type for a
            # phone call or visit, nor suppress a distinct preparation step.
            changes['activity'] = activity_from_instruction(text)
        record=self.crm._require_record(db,row['owner'],row['record_id'])
        if attachments and not record['original_content'].strip():intent='note';changes={};issues=[]
        if not semantic['source_authorized'] and intent=="plan":intent="note"
        if row["source_kind"]=="recording":intent="note"
        if intent=="recap" and not (row["source_kind"]=="recap" or re.search(r"复盘|聊完|谈完|吃完|拜访完|会后|结束了",text)):
            intent="note"
        if not semantic['source_authorized'] and intent in ("update","settings"):intent="note"
        if is_conditional(text) and intent=='recap':intent='note'
        if decision and decision.get('kind') == 'ambiguous':
            intent='note'; changes={}; issues=[]; suggestions=[]
        elif separate and intent in ('update','recap'):
            intent='plan' if changes.get('date') else 'note'
        summary=str(raw.get("summary") or "")[:1800]
        result={"intent":intent,"summary":summary,"suggestions":suggestions,"preparation":preparation,
                "mode":"model" if getattr(self.interpreter,"available",True) else "basic","attachments":attachments}
        if decision: result['pending_matter_decision'] = decision
        reply,question="原话已保存，可以随时继续补充。",""
        plan_id=None if separate else row["plan_id"]
        if intent=="settings":
            preference=self.settings(row["owner"])
            business=changes.get("business_context",preference["business_context"])
            self.update_settings_in_transaction(db,row["owner"],business,changes.get("remind_minutes",preference["remind_minutes"]),now)
            reply="秘书偏好已记住，后续会沿用；这次原话已保留。"
        elif intent=="contact":
            name=changes.get("person") or "新认识的联系人"
            prospect_id=db.execute("""INSERT INTO crm_secretary_prospects
                (owner,name,phone,wechat,notes,source_record_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)""",
                (row["owner"],name,changes.get("phone",""),changes.get("wechat",""),text,row["record_id"],now,now)).lastrowid
            result["prospect_id"]=prospect_id
            reply="已留下"+name+"和认识背景。单位还不知道也可以先保存，后面继续告诉我。"
            customer_id=changes.get('customer_id') or context['scope'].get('customer_id')
            if customer_id:
                self.crm._require_customer(db,row['owner'],customer_id)
                db.execute('UPDATE crm_secretary_prospects SET customer_id=? WHERE owner=? AND id=?',(customer_id,row['owner'],prospect_id))
                db.execute('UPDATE crm_records SET customer_id=? WHERE owner=? AND id=?',(customer_id,row['owner'],row['record_id']))
                result['scope']={'customer_id':customer_id}
                contact_id=changes.get('contact_id') or context['scope'].get('contact_id')
                if contact_id or not self.crm._contacts_named(db,row['owner'],customer_id,name):
                    prospect=db.execute('SELECT * FROM crm_secretary_prospects WHERE owner=? AND id=?',(row['owner'],prospect_id)).fetchone()
                    linked=self._finalize_prospect_link(db,row['owner'],prospect,customer_id,contact_id,now)
                    result['scope']['contact_id']=linked['contact_id']
                    reply='已保存'+name+'，并归入对应客户的联系人档案。认识背景和原话保留。'
                else:reply='已识别联系人所属单位；有同名联系人，先保留这次原话，请在“新认识的人”选择已有档案。'
        elif intent in ("plan","update","recap","discussion","note") and (intent=="plan" or planrow) and not (decision and decision.get('kind') == 'ambiguous'):
            applied=changes if intent in ('plan','update') else {}
            plan={**current,**{k:v for k,v in applied.items() if k in current}}
            plan.update({k:v for k,v in context["scope"].items() if k not in changes})
            if context.get('identity_candidates'):plan['identity_candidates']=context['identity_candidates'][:3]
            if context['scope'].get('contact_id') or 'contact_id' in changes:
                plan['identity_candidates']=[]
                person=next(p for p in context['contacts'] if p['id']==plan['contact_id'])
                plan['person']=person['name']
            if plan['identity_candidates']:plan['identity_question']='这个称呼可能对应下面的资料，是哪一位？也可以暂时保留。'
            self._scope(db,row['owner'],{k:plan[k] for k in ('customer_id','contact_id','opportunity_id') if plan.get(k)})
            if not plan["title"]:plan["title"]=("与"+plan["person"]+"交流") if plan["person"] else text[:120]
            if plan['activity']=='task' and not plan['goal']:plan['goal']=plan['title']
            if plan.get("start_at") and "date" in applied and "start_at" not in applied and re.search(r'钟点不变|时间不变', text):
                # Changing only the day carries the explicitly known clock.
                old_at=datetime.fromtimestamp(plan["start_at"],SHANGHAI)
                plan["start_at"]=datetime.fromisoformat(plan["date"]).replace(hour=old_at.hour,minute=old_at.minute,tzinfo=SHANGHAI).timestamp()
            if "start_at" in applied or "date" in applied or "remind_minutes" in applied:
                if "reminder_at" not in applied:plan["reminder_at"]=None
            if raw.get("identity_question") and (plan['identity_candidates'] or any('同名' in item for item in issues)):
                plan["identity_question"]=str(raw["identity_question"])[:400]
            elif context["scope"].get("contact_id") or "contact_id" in changes:plan["identity_question"]=""
            if preparation.get("objective") or preparation.get("questions"):plan["preparation"]=preparation
            if suggestions:plan["suggestions"]=suggestions
            result['scope']={k:plan[k] for k in ('customer_id','contact_id','opportunity_id') if plan.get(k)}
            if intent=="recap":
                plan["status"]="recapped"
                result["recap"]=summary or text
                reply="复盘原话已保存到这次交流，可以在客户历程和这次计划中回看。"
                if plan.get("task_id"):self.crm._execute(db,row["owner"],{"action":"complete","task_id":plan["task_id"]},now)
            preference=self.settings(row["owner"])["remind_minutes"]
            question="" if intent=="recap" else self._question(plan,issues,preference)
            plan["question"]=question
            if not planrow:
                visit_id=None
                if self.visits is not None:
                    visit_id=db.execute("INSERT INTO crm_visits(owner,title,customer_id,occurred_at,created_at,updated_at) VALUES (?,?,?,NULL,?,?)",
                        (row["owner"],plan["title"],plan.get("customer_id"),now,now)).lastrowid
                plan_id=db.execute("INSERT INTO crm_secretary_plans(owner,record_id,visit_id,data_json,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                    (row["owner"],row["record_id"],visit_id,encoded(plan),now,now)).lastrowid
                revision=1
            else:
                revision=planrow["revision"]+1
                db.execute("UPDATE crm_secretary_plans SET data_json=?,revision=?,updated_at=? WHERE owner=? AND id=?",
                    (encoded(plan),revision,now,row["owner"],plan_id))
                if planrow["visit_id"]:
                    db.execute("UPDATE crm_visits SET title=?,customer_id=?,revision=revision+1,updated_at=? WHERE owner=? AND id=?",
                        (plan["title"],plan.get("customer_id"),now,row["owner"],planrow["visit_id"]))
            semantic = add_execution_evidence(semantic, applied, plan, text, row['created_at'], source_kind=row['source_kind'])
            semantic.update(new_plan=not bool(planrow), intent=intent,
                source_authorized=semantic['source_authorized'],
                expected_task_revision=((context.get('plan') or {}).get('active_schedule') or {}).get('revision'))
            synced = self.arrangements.sync_from_turn(db, row['owner'], plan_id, semantic,
                row['id'], row['created_at'], now)
            plan = {**self._default_plan(), **json.loads(self._require_plan(db,row['owner'],plan_id)['data_json'])}
            if 'schedule_conflict' in synced.get('arrangement', {}).get('blocking_reasons', []):
                issues.insert(0, '与已有日程存在冲突，原安排保留，请核对时间。')
                plan['status'] = 'needs_attention'
            if synced.get('arrangement', {}).get('settling_state') in ('settled','paused','abandoned'):
                question = ''
            else:
                question = '' if intent=='recap' else self._question(plan,issues,preference)
            plan['question'] = question
            db.execute('UPDATE crm_secretary_plans SET data_json=? WHERE owner=? AND id=?', (encoded(plan),row['owner'],plan_id))
            result['arrangement_receipt'] = synced
            db.execute("INSERT INTO crm_secretary_plan_history(owner,plan_id,turn_id,data_json,revision,created_at) VALUES (?,?,?,?,?,?)",
                (row["owner"],plan_id,row["id"],encoded(plan),revision,now))
            db.execute("UPDATE crm_records SET customer_id=?,kind=?,category=?,status=?,updated_at=? WHERE owner=? AND id=?",
                (plan.get("customer_id"),"action" if not planrow else "note","visit_review" if intent=="recap" else "idea",
                 "following" if not planrow else "done",now,row["owner"],row["record_id"]))
            canonical=planrow["record_id"] if planrow else row["record_id"]
            db.execute("UPDATE crm_records SET customer_id=?,title=?,updated_at=? WHERE owner=? AND id=?",
                (plan.get("customer_id"),plan["title"],now,row["owner"],canonical))
            if plan['status'] in ('recapped','cancelled'):
                db.execute("UPDATE crm_records SET status='done' WHERE owner=? AND id=?",(row['owner'],canonical))
            visit_id=planrow['visit_id'] if planrow else visit_id
            if visit_id and db.execute("SELECT 1 FROM sqlite_master WHERE name='crm_visit_records'").fetchone():
                db.execute('INSERT OR IGNORE INTO crm_visit_records VALUES (?,?,?,?,?)',(row['owner'],visit_id,row['record_id'],'recap' if intent=='recap' else 'supplement',now))
                db.execute('INSERT INTO crm_visit_record_history(owner,record_id,old_visit_id,new_visit_id,note,created_at) VALUES (?,?,NULL,?,?,?)',
                    (row['owner'],row['record_id'],visit_id,'秘书对话归入同一次交流',now))
            if plan.get('opportunity_id'):
                for identifier in {canonical,row['record_id']}:
                    self._link_project(db,row['owner'],'record',identifier,plan['opportunity_id'],now)
                if visit_id:self._link_project(db,row['owner'],'visit',visit_id,plan['opportunity_id'],now)
            self._link_timeline(row["owner"],row["record_id"],plan,recap=intent=="recap")
            if intent!="recap":
                reply=self._reply(plan,question)
                if semantic['changes'].get('settle_deadline'):
                    deadline=semantic['changes']['settle_deadline']
                    reply+=' '+('最晚' if deadline['strength']=='required' else '希望')+deadline['time_spec']['date']+'把安排定下来。'
                if semantic['changes'].get('next_check'):
                    reply+=' 下次推进：'+semantic['changes']['next_check']['time_spec']['raw_text']+'。'
                if synced.get('effect') in ('paused','abandoned','cancelled','withdrawn'):
                    reply=synced.get('receipt', reply)
                elif 'check_after_deadline' in synced.get('arrangement', {}).get('attention_flags', []):
                    reply+=' 下一推进点晚于确定期限，两项选择已保留；你可以提前回看或调整期限。'
        else:
            target=changes.get("customer_id") or context["scope"].get("customer_id")
            selected={k:changes.get(k,context['scope'].get(k)) for k in ('customer_id','contact_id','opportunity_id')}
            result['scope']=self._scope(db,row['owner'],{k:v for k,v in selected.items() if v is not None})
            if target:
                db.execute("UPDATE crm_records SET customer_id=?,category=?,updated_at=? WHERE owner=? AND id=?",
                    (target,"visit_review" if intent=="recap" else "idea",now,row["owner"],row["record_id"]))
            if intent=="recap":reply="复盘已保存为客户资料；还未确定是哪次交流，可以稍后关联。"
            if intent in ("research","discussion","work_plan"):reply="目标已记下，可以继续研究或讨论；原话已保留。"
            if not target and intent=='note':
                db.execute("UPDATE crm_records SET status='unfiled',classified=0 WHERE owner=? AND id=?",(row['owner'],row['record_id']))
            person_id=changes.get('contact_id') or context['scope'].get('contact_id')
            if person_id:self._link_timeline(row['owner'],row['record_id'],{'contact_id':person_id},recap=intent=='recap')
            if target and result['scope'].get('opportunity_id'):
                self._link_project(db,row['owner'],'record',row['record_id'],result['scope']['opportunity_id'],now)
        scope=result.get('scope') or context['scope']
        visit=self._require_plan(db,row['owner'],plan_id)['visit_id'] if plan_id else None
        self.attachments.link(db,row['owner'],[item['material_id'] for item in attachments],scope,now,
            plan_id=plan_id,visit_id=visit,link_project=self._link_project)
        if visit and scope.get('opportunity_id'):self._link_project(db,row['owner'],'visit',visit,scope['opportunity_id'],now)
        self.attachments.used(db,row['owner'],row['id'],context.get('attachments',[]))
        result['attachments']=self.attachments.read(db,row['owner'],self.attachments.ids(db,row['owner'],turn_id=row['id'],plan_id=plan_id))
        attachment_message=self.attachments.message(result['attachments'])
        if attachment_message:reply+=' '+attachment_message
        if self.matters and decision:
            from .matter_flow_integration import apply_decision
            routed = apply_decision(self.matters, db, row, decision, text=text, now=now,
                plan_id=plan_id, scope=result.get('scope') or context['scope'])
            result['matter_route'] = routed
            if routed.get('matter'):
                reply += ' 已归入“' + routed['matter']['title'] + '”，可在同一件事里继续推进。'
            elif routed.get('kind') == 'ambiguous':
                question = routed['reason']
        db.execute("""UPDATE crm_secretary_turns SET plan_id=?,status='done',reply=?,question=?,
            data_json=?,error='',lease=NULL,claimed_at=NULL,updated_at=? WHERE id=? AND lease=?""",
            (plan_id,reply,question,encoded(result),now,row["id"],lease))
        self._release_arrangement_hold(db,row)

    @staticmethod
    def update_settings_in_transaction(db,owner,business,minutes,now):
        if minutes is not None and (type(minutes) is not int or not 0<=minutes<=10080):raise ValueError("提醒偏好无效。")
        previous=db.execute("SELECT revision FROM crm_secretary_settings WHERE owner=?",(owner,)).fetchone()
        revision=previous["revision"]+1 if previous else 1
        db.execute("""INSERT INTO crm_secretary_settings(owner,business_context,remind_minutes,revision,updated_at) VALUES (?,?,?,?,?)
            ON CONFLICT(owner) DO UPDATE SET business_context=excluded.business_context,
            remind_minutes=excluded.remind_minutes,revision=excluded.revision,updated_at=excluded.updated_at""",
            (owner,business,minutes,revision,now))

    def retry(self,owner,identifier):
        item=self.turn(owner,identifier)
        if item["status"] not in ("failed","needs_attention"):raise ValueError("这条已处理或正在整理，不必重复提交。")
        with self.crm._transaction() as db:
            row=db.execute("SELECT * FROM crm_secretary_turns WHERE owner=? AND id=?",(owner,identifier)).fetchone()
            self.crm._require_record(db,owner,row['record_id'])
            revision=self._require_plan(db,owner,row["plan_id"])["revision"] if row["plan_id"] else None
            if self.matters and row['matter_id']:
                current = self.matters.get(owner, row['matter_id'])
                if current['visibility'] != 'active' or current['status'] == 'ended':
                    raise FlowConflict('这件事已收起或结束，原话保留；请恢复后再重试。')
                db.execute('UPDATE crm_secretary_turns SET matter_revision=? WHERE owner=? AND id=?', (current['revision'],owner,identifier))
            db.execute("UPDATE crm_secretary_turns SET status='queued',base_revision=?,error='',updated_at=? WHERE owner=? AND id=?",
                (revision,self.clock(),owner,identifier))
            if row['plan_id']:
                generation = self.arrangements.begin_hold(db, owner, row['plan_id'], identifier, self.clock())
                db.execute('UPDATE crm_secretary_turns SET data_json=? WHERE owner=? AND id=?',
                    (encoded({'arrangement_hold_generation': generation}), owner, identifier))
        return self.turn(owner,identifier)

    def adopt(self,owner,identifier,index):
        item=self.turn(owner,identifier)
        if item["status"]!="done":raise ValueError("请等这条整理完成。")
        if type(index) is not int or not 1<=index<=len(item["result"].get("suggestions",[])):raise ValueError("建议编号无效。")
        with self.crm._transaction() as db:
            source=self.crm._require_record(db,owner,item["record_id"])
            if item['plan_id']:self._require_plan(db,owner,item['plan_id'])
            previous=db.execute("SELECT * FROM crm_secretary_adoptions WHERE owner=? AND turn_id=? AND action_index=?",(owner,identifier,index)).fetchone()
            if previous:return self.crm.get_record(owner,previous["record_id"])
            suggestion=item["result"]["suggestions"][index-1]
            now=self.clock()
            if self.matters and item.get('matter_id'):
                detail=self.matters.get(owner,item['matter_id'])
                existing=next((step for step in detail['actions'] if step['title'].strip()==suggestion['title'].strip()),None)
                if existing:
                    db.execute("INSERT INTO crm_secretary_adoptions VALUES (?,?,?,?,?)",(owner,identifier,index,existing['id'],now))
                    return self.crm.get_record(owner,existing['id'])
            record_id=db.execute("""INSERT INTO crm_records(owner,title,content,original_content,source,status,customer_id,
                classified,kind,parent_record_id,category,created_at,updated_at)
                VALUES (?,?,?,?,'web','following',?,1,'action',?,'idea',?,?)""",
                (owner,suggestion["title"],suggestion["reason"],suggestion["reason"],source["customer_id"],source["id"],now,now)).lastrowid
            db.execute("INSERT INTO crm_secretary_adoptions VALUES (?,?,?,?,?)",(owner,identifier,index,record_id,now))
            if self.matters and item.get('matter_id'):
                self.matters.attach(owner,item['matter_id'],'record',record_id,role='action')
        return self.crm.get_record(owner,record_id)

    def prospects(self,owner):
        owner=_owner(owner)
        with self.crm._lock:
            return {"items":[{key:row[key] for key in row.keys() if key!="owner"} for row in self.crm._db.execute(
                """SELECT p.* FROM crm_secretary_prospects p JOIN crm_records r
                    ON r.owner=p.owner AND r.id=p.source_record_id AND r.hidden=0
                    WHERE p.owner=? ORDER BY p.updated_at DESC LIMIT 200""",(owner,))]}

    def link_prospect(self,owner,identifier,data):
        owner,identifier=_owner(owner),_identifier(identifier)
        if not isinstance(data,dict) or set(data)-{'customer_id','contact_id','expected_updated_at'}:
            raise ValueError('请选择联系人所属单位。')
        now=self.clock()
        with self.crm._transaction() as db:
            row=db.execute('SELECT * FROM crm_secretary_prospects WHERE owner=? AND id=?',(owner,identifier)).fetchone()
            if row is None:raise KeyError('未找到联系人线索。')
            self.crm._require_record(db,owner,row['source_record_id'])
            if data.get('expected_updated_at')!=row['updated_at']:raise FlowConflict('联系人线索已有更新，请刷新。')
            customer_id=_identifier(data.get('customer_id'))
            self.crm._require_customer(db,owner,customer_id)
            contact_id=data.get('contact_id')
            if row['status']=='linked':
                if customer_id!=row['customer_id'] or contact_id not in (None,row['contact_id']):raise FlowConflict('已归入档案，请从联系人档案调整归属。')
                return dict(row)
            return self._finalize_prospect_link(db,owner,row,customer_id,contact_id,now)

    def _finalize_prospect_link(self,db,owner,row,customer_id,contact_id,now):
        source=self.crm._require_record(db,owner,row['source_record_id'])
        if contact_id:
            contact_id=_identifier(contact_id)
            person=self.crm._require_contact(db,owner,customer_id,contact_id)
            if person['archived']:raise ValueError('联系人已归档，请先恢复。')
        else:
            if self.crm._contacts_named(db,owner,customer_id,row['name']):raise FlowConflict('该单位已有同名联系人，请选中已有联系人后关联，避免重复。')
            values=self.crm._contact_values({'name':row['name'],'phone':row['phone']})
            contact_id=self.crm._insert_contact(db,owner,customer_id,values,now)
        db.execute('UPDATE crm_secretary_prospects SET customer_id=?,contact_id=?,status=\'linked\',updated_at=? WHERE owner=? AND id=?',
            (customer_id,contact_id,now,owner,row['id']))
        db.execute('UPDATE crm_records SET customer_id=?,updated_at=? WHERE owner=? AND id=?',
            (customer_id,now,owner,row['source_record_id']))
        known_channel=db.execute("SELECT 1 FROM crm_customer_facts WHERE owner=? AND customer_id=? AND contact_id=? AND key='communication_channel' AND value!='' LIMIT 1",(owner,customer_id,contact_id)).fetchone()
        if row['wechat'] and row['wechat'] in source['original_content'] and not known_channel:
            values=self.crm._fact_values({'key':'communication_channel','value':'微信：'+row['wechat'],'basis':'reported',
                'evidence':row['wechat'],'source_record_id':row['source_record_id'],'contact_id':contact_id})
            self.crm._check_fact_links(db,owner,customer_id,values)
            self.crm._insert_fact(db,owner,customer_id,values,now)
        self._link_timeline(owner,row['source_record_id'],{'contact_id':contact_id})
        return {**dict(row),'customer_id':customer_id,'contact_id':contact_id,'status':'linked','updated_at':now}

    def close(self):
        self.closed=True
