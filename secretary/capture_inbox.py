"""Durable save-first thoughts. Models propose; the owner chooses attribution/use."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time

from .crm import _identifier, _owner, _text, analysis_fingerprint
from .record_categories import resolve_category
from .store import _timestamp


class CaptureService:
    def __init__(self, crm, workspace, lock, *, resolution=None, organizer=None, clock=time.time):
        self.crm, self.workspace, self.lock = crm, workspace, lock
        self.resolution, self.organizer, self.clock = resolution, organizer, clock
        with crm._transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS crm_captures (
                id INTEGER PRIMARY KEY,owner TEXT NOT NULL,request_id TEXT NOT NULL,
                signature TEXT NOT NULL,record_id INTEGER NOT NULL,status TEXT NOT NULL,
                purpose TEXT,opportunity_id INTEGER,resolution_json TEXT,error TEXT NOT NULL DEFAULT '',
                fingerprint TEXT NOT NULL,lease TEXT,claimed_at REAL,created_at REAL NOT NULL,
                updated_at REAL NOT NULL,UNIQUE(owner,request_id),UNIQUE(owner,record_id))''')
            db.execute('CREATE INDEX IF NOT EXISTS capture_pending ON crm_captures(status,claimed_at,id)')

    def capture(self, owner, data):
        owner, now = _owner(owner), _timestamp(self.clock())
        if not isinstance(data, dict) or set(data)-{'text','request_id','category','original_transcript','customer_id'}:
            raise ValueError('随手记录字段无效')
        text = _text(data.get('text'), '记录原话', 20000, required=True).strip()
        request_id = _text(data.get('request_id'), '记录请求编号', 200, required=True).strip()
        original=_text(data.get('original_transcript',text),'原始转写',20000,required=True)
        customer_id=data.get('customer_id')
        if customer_id is not None:_identifier(customer_id)
        category = resolve_category(data.get('category','auto'), text, customer_id)
        signature = hashlib.sha256(json.dumps([text,category,original,customer_id],ensure_ascii=False).encode()).hexdigest()
        with self.crm._transaction() as db:
            prior = db.execute('SELECT * FROM crm_captures WHERE owner=? AND request_id=?',(owner,request_id)).fetchone()
            if prior:
                if prior['signature'] != signature:
                    raise ValueError('这次记录已保存；修改内容请使用新的记录请求编号')
                return self.get(owner, prior['id'])
            self.crm._require_customer(db,owner,customer_id)
            record_id = db.execute('''INSERT INTO crm_records
                (owner,source_id,title,content,original_content,source,category,customer_id,created_at,updated_at)
                VALUES (?,?,?,?,?,'web',?,?,?,?)''',
                (owner,'capture:'+request_id,text[:120],text,original,category,customer_id,now,now)).lastrowid
            if 'original_transcript' in data:
                db.execute('INSERT INTO crm_record_transcripts VALUES (?,?,?,?,?,?)',(owner,record_id,original,text,now,now))
            record = self.crm.get_record(owner,record_id)
            identifier = db.execute('''INSERT INTO crm_captures
                (owner,request_id,signature,record_id,status,fingerprint,created_at,updated_at)
                VALUES (?,?,?,?,'queued',?,?,?)''',
                (owner,request_id,signature,record_id,analysis_fingerprint(record),now,now)).lastrowid
        return self.get(owner,identifier)

    def _public(self, row):
        if row is None:
            raise KeyError('未找到这条随手记录')
        record = self.crm.get_record(row['owner'],row['record_id'])
        if record is None:
            raise KeyError('未找到这条随手记录')
        return {key:row[key] for key in ('id','record_id','status','purpose','opportunity_id','error','created_at','updated_at')} | {
            'customer_id':record['customer_id'],'record':record,
            'resolution':json.loads(row['resolution_json']) if row['resolution_json'] else None,
            'analysis':self.crm.get_analysis(row['owner'],row['record_id'])}

    def get(self,owner,identifier):
        owner,identifier = _owner(owner),_identifier(identifier)
        with self.crm._lock:
            return self._public(self.crm._db.execute('SELECT * FROM crm_captures WHERE owner=? AND id=?',(owner,identifier)).fetchone())

    def for_record(self,owner,record_id):
        owner,record_id=_owner(owner),_identifier(record_id)
        with self.crm._lock:
            row=self.crm._db.execute('SELECT * FROM crm_captures WHERE owner=? AND record_id=?',(owner,record_id)).fetchone()
            return self._public(row) if row else None

    def list(self,owner,*,status='',page=1,page_size=20):
        from .crm import _pagination
        owner,offset=_owner(owner),_pagination(page,page_size)
        if status not in ('','queued','processing','review','needs_details','failed','filed'):
            raise ValueError('随手记录状态无效')
        where='j.owner=? AND r.hidden=0'+(' AND j.status=?' if status else '')
        params=[owner]+([status] if status else [])
        with self.crm._lock:
            query=' FROM crm_captures j JOIN crm_records r ON r.owner=j.owner AND r.id=j.record_id WHERE '+where
            total=self.crm._db.execute('SELECT COUNT(*)'+query,params).fetchone()[0]
            rows=self.crm._db.execute('SELECT j.*'+query+' ORDER BY j.created_at DESC,j.id DESC LIMIT ? OFFSET ?',params+[page_size,offset]).fetchall()
            return {'items':[self._public(row) for row in rows],'total':total,'page':page,'page_size':page_size}

    async def process_one(self):
        import secrets
        now,lease=_timestamp(self.clock()),secrets.token_hex(16)
        async with self.lock:
            with self.crm._transaction() as db:
                row=db.execute("SELECT j.* FROM crm_captures j JOIN crm_records r ON r.owner=j.owner AND r.id=j.record_id WHERE r.hidden=0 AND (j.status='queued' OR (j.status='processing' AND j.claimed_at<?)) ORDER BY j.id LIMIT 1",(now-240,)).fetchone()
                if not row: return False
                db.execute("UPDATE crm_captures SET status='processing',lease=?,claimed_at=?,updated_at=? WHERE id=?",(lease,now,now,row['id']))
                record=self.crm.get_record(row['owner'],row['record_id'])
                fingerprint=analysis_fingerprint(record)
                customer=self.crm.get_customer(row['owner'],record['customer_id']) if record['customer_id'] else None
        result=None
        analysis=None
        status='needs_details'
        error=''
        try:
            if self.resolution:
                result=await asyncio.wait_for(self.resolution.resolve(row['owner'],record['content'],context_customer_id=record['customer_id']),90)
            if self.organizer and getattr(self.organizer,'api_key',True):
                context={'customer':{key:customer[key] for key in ('name','stage') if key in customer} if customer else {},'recent_records':[],
                         'attribution_candidates':(result or {}).get('items',[]),
                         'notice':'候选归属未经用户确认；只根据原话整理，不补造姓名、日期或承诺。'}
                analysis=await asyncio.wait_for(self.organizer.organize(record['content'],now,context),90)
                analysis={**analysis,'input_fingerprint':fingerprint}
                status='filed' if row['purpose'] else 'review'
            else:
                error='原话已保存。AI 整理尚未配置，可先补充归属和用途。'
        except asyncio.CancelledError:
            async with self.lock:
                with self.crm._transaction() as db:
                    db.execute("UPDATE crm_captures SET status='queued',lease=NULL,claimed_at=NULL WHERE id=? AND lease=?",(row['id'],lease))
            raise
        except Exception:
            status='failed'; error='原话已保存，AI 暂时未能整理。可手动补充或稍后重试。'
        async with self.lock:
            with self.crm._lock:
                current=self.crm._db.execute('SELECT * FROM crm_captures WHERE id=? AND lease=? AND status=\'processing\'',(row['id'],lease)).fetchone()
                latest=self.crm.get_record(row['owner'],row['record_id'])
                if not current: return True
                if latest is None or analysis_fingerprint(latest)!=fingerprint:
                    analysis=None;result=None;status='needs_details';error='原话或归属已更新，请按最新内容重新整理。'
            if analysis is not None:
                try:self.crm.save_analysis(row['owner'],row['record_id'],analysis,self.clock())
                except (ValueError,KeyError):
                    status='needs_details';error='记录已更新，请按最新内容重新整理。'
            with self.crm._transaction() as db:
                db.execute('''UPDATE crm_captures SET status=?,resolution_json=?,error=?,lease=NULL,
                    claimed_at=NULL,updated_at=? WHERE id=? AND lease=? AND status='processing' ''',
                    (status,json.dumps(result,ensure_ascii=False) if result else None,error,self.clock(),row['id'],lease))
        return True

    def classify(self,owner,identifier,data):
        owner,identifier,now=_owner(owner),_identifier(identifier),_timestamp(self.clock())
        if not isinstance(data,dict) or set(data)-{'customer_id','opportunity_id','purpose','expected_updated_at'}:
            raise ValueError('整理归属字段无效')
        purpose=data.get('purpose')
        if purpose not in ('action','schedule','project_reference','note'):
            raise ValueError('请选择这句话的用途')
        customer_id=data.get('customer_id'); opportunity_id=data.get('opportunity_id')
        if customer_id is not None:_identifier(customer_id)
        if opportunity_id is not None:_identifier(opportunity_id)
        if purpose=='project_reference' and opportunity_id is None:
            raise ValueError('项目研判资料需要选择一个项目')
        with self.crm._transaction() as db:
            row=db.execute('SELECT * FROM crm_captures WHERE owner=? AND id=?',(owner,identifier)).fetchone()
            if row is None:raise KeyError('未找到这条随手记录')
            record=self.crm._require_record(db,owner,row['record_id'])
            expected=_timestamp(data.get('expected_updated_at'))
            if expected!=record['updated_at']:
                raise ValueError('这条记录已更新，请刷新后再核对')
            now=max(now,record['updated_at']+.000001)
            if record['proposal_id'] is not None or record['status']=='done':
                raise ValueError('这条记录已有安排或完成结果，请从记录详情继续跟进')
            if db.execute('SELECT 1 FROM crm_analysis_actions WHERE owner=? AND parent_record_id=? LIMIT 1',(owner,record['id'])).fetchone():
                raise ValueError('这条原话已有采纳的待办，请从待办或新的补充记录继续跟进')
            self.crm._require_customer(db,owner,customer_id)
            if opportunity_id is not None:
                project=self.workspace._require_opportunity(db,owner,customer_id,opportunity_id)
                if project['archived']:raise ValueError('已归档项目不能加入新的资料')
            # Use is a filing hint. Concrete actions require a separate adoption;
            # preserve legacy explicitly-created actions rather than downgrade them.
            db.execute("UPDATE crm_records SET customer_id=?,kind=?,status='following',classified=1,updated_at=? WHERE owner=? AND id=?",
                (customer_id,record['kind'],now,owner,record['id']))
            # Inline the explicit link in this transaction; workspace.link uses its
            # own transaction and would break atomic classification on SQLite.
            updated=self.crm._require_record(db,owner,record['id'])
            snapshot=self.workspace._link_snapshot(db,owner,'record',updated)
            previous=db.execute("SELECT revision FROM crm_opportunity_links WHERE owner=? AND entity_type='record' AND entity_id=?",(owner,record['id'])).fetchone()
            revision=previous['revision']+1 if previous else 1
            db.execute("INSERT INTO crm_opportunity_links VALUES (?,'record',?,?,?,?,?,?) ON CONFLICT(owner,entity_type,entity_id) DO UPDATE SET customer_id=excluded.customer_id,opportunity_id=excluded.opportunity_id,revision=excluded.revision,source_snapshot=excluded.source_snapshot,updated_at=excluded.updated_at",
                (owner,record['id'],customer_id,opportunity_id,revision,snapshot,now))
            db.execute("INSERT INTO crm_opportunity_link_history(owner,entity_type,entity_id,customer_id,opportunity_id,revision,source_snapshot,created_at) VALUES (?,'record',?,?,?,?,?,?)",
                (owner,record['id'],customer_id,opportunity_id,revision,snapshot,now))
            analysis=self.crm.get_analysis(owner,record['id'])
            # A changed customer invalidates interpretation. Queue the same saved
            # source automatically; do not require the user to record it again.
            status='queued' if analysis and analysis.get('stale') and self.organizer and getattr(self.organizer,'api_key',True) else 'filed'
            db.execute("UPDATE crm_captures SET status=?,purpose=?,opportunity_id=?,fingerprint=?,lease=NULL,claimed_at=NULL,error='',updated_at=? WHERE owner=? AND id=?",
                (status,purpose,opportunity_id,analysis_fingerprint(updated),now,owner,identifier))
        capture=self.get(owner,identifier)
        return {'capture':capture,'record':capture['record']}

    def retry(self,owner,identifier):
        item=self.get(owner,identifier)
        if item['status']=='processing':raise ValueError('正在整理，请稍候')
        with self.crm._transaction() as db:
            changed=db.execute("UPDATE crm_captures SET status='queued',error='',lease=NULL,claimed_at=NULL,updated_at=? WHERE owner=? AND id=? AND status!='processing'",(self.clock(),owner,identifier))
            if not changed.rowcount:raise ValueError('正在整理，请稍候')
        return self.get(owner,identifier)
