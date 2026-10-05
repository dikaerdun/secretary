"""Archive an existing short record into an exchange without copying its work."""
from .crm import _identifier, _owner, _text


class ExchangeRecords:
    def __init__(self,crm,visits,clock):
        self.crm,self.visits,self.clock=crm,visits,clock
        with crm._transaction() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS crm_visit_records (
                owner TEXT NOT NULL,visit_id INTEGER NOT NULL,record_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('recording','recap','supplement')),created_at REAL NOT NULL,
                PRIMARY KEY(owner,record_id),FOREIGN KEY(owner,record_id) REFERENCES crm_records(owner,id),
                FOREIGN KEY(owner,visit_id) REFERENCES crm_visits(owner,id));
                CREATE TABLE IF NOT EXISTS crm_visit_record_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,owner TEXT NOT NULL,record_id INTEGER NOT NULL,
                old_visit_id INTEGER,new_visit_id INTEGER,note TEXT NOT NULL,created_at REAL NOT NULL);''')

    def reference(self,owner,record_id):
        with self.crm._lock:
            row=self.crm._db.execute('SELECT l.*,v.title FROM crm_visit_records l JOIN crm_visits v '
                'ON v.owner=l.owner AND v.id=l.visit_id WHERE l.owner=? AND l.record_id=?',(owner,record_id)).fetchone()
            return {k:v for k,v in dict(row).items() if k!='owner'} if row else None

    def archive(self,owner,record_id,data=None):
        owner,record_id=_owner(owner),_identifier(record_id)
        data=data or {}
        if not isinstance(data,dict) or set(data)-{'visit_id','revision','role','note'}:raise ValueError('交流归档字段无效')
        role=data.get('role','recap')
        if role not in ('recap','recording','supplement'):raise ValueError('来源角色无效')
        note=_text(data.get('note','归入客户交流'),'归档说明',1000,required=True)
        # Read derived material state before BEGIN; all final scope checks,
        # new exchange creation and link history then commit atomically.
        if data.get('visit_id') is not None:
            self.visits.detail(owner,_identifier(data['visit_id']))
        with self.crm._transaction() as db:
            record=self.crm._require_record(db,owner,record_id)
            prior=self.reference(owner,record_id)
            target=data.get('visit_id')
            if target is None and prior:return prior
            if target is None:
                values=self.visits._values({'title':record['title'][:120],'customer_id':record['customer_id'],'occurred_at':None})
                target=db.execute('INSERT INTO crm_visits(owner,title,customer_id,occurred_at,created_at,updated_at) '
                    'VALUES (?,?,?,?,?,?)',(owner,values['title'],values['customer_id'],None,self.clock(),self.clock())).lastrowid
                visit=self.visits._public(db,self.visits._require(db,owner,target))
            else:
                target=_identifier(target)
                visit=self.visits._public(db,self.visits._require(db,owner,target))
                if (prior and prior['visit_id']==target and prior['role']==role
                        and record['customer_id']==visit['customer_id']):return prior
                if data.get('revision')!=visit['revision']:raise ValueError('交流已变化，请刷新后核对归档')
            if record['customer_id']!=visit['customer_id']:raise ValueError('记录与交流客户不同，请先核对客户归属')
            if prior and prior['visit_id']==target and prior['role']==role:return prior
            db.execute('INSERT INTO crm_visit_records VALUES (?,?,?,?,?) ON CONFLICT(owner,record_id) '
                'DO UPDATE SET visit_id=excluded.visit_id,role=excluded.role',
                (owner,target,record_id,role,self.clock()))
            db.execute('INSERT INTO crm_visit_record_history(owner,record_id,old_visit_id,new_visit_id,note,created_at) '
                'VALUES (?,?,?,?,?,?)',(owner,record_id,prior['visit_id'] if prior else None,target,note,self.clock()))
            return self.reference(owner,record_id)

    def auto_archive(self,owner,record_id):
        record=self.crm.get_record(owner,record_id)
        if (record and record['kind']=='note' and record.get('customer_id') is not None
                and record['category'] in ('meeting','conversation','visit_review')):
            return self.archive(owner,record_id,{'role':'recap' if record['category']=='visit_review' else 'supplement'})
        return None
