"""Display deadlines and check dates without inventing execution appointments."""
from datetime import datetime
import json

from .agenda import _window
from .store import SHANGHAI


def planning_nodes(crm,owner,*,period='day',date=None,now):
    anchor=datetime.fromtimestamp(now,SHANGHAI)
    if date:
        try:anchor=datetime.strptime(date,'%Y-%m-%d').replace(tzinfo=SHANGHAI)
        except (ValueError,TypeError):raise ValueError('日期需要为YYYY-MM-DD') from None
    start,end=_window(period,anchor)
    items=[]
    with crm._lock:
        rows=crm._db.execute('''SELECT r.id,r.title,r.customer_id,c.name AS customer_name,a.terms_json
            FROM crm_records r JOIN crm_action_terms a ON a.owner=r.owner AND a.record_id=r.id
            LEFT JOIN crm_customers c ON c.owner=r.owner AND c.id=r.customer_id
            WHERE r.owner=? AND r.kind='action' AND r.status!='done' AND r.hidden=0''',(owner,)).fetchall()
        for row in rows:
            terms=json.loads(row['terms_json'])
            for kind in ('deadline','check'):
                stamp=terms.get(kind+'_at'); day=terms.get(kind+'_date')
                if stamp is not None:
                    point=datetime.fromtimestamp(stamp,SHANGHAI); day=point.date().isoformat()
                elif day:
                    try:point=datetime.strptime(day,'%Y-%m-%d').replace(tzinfo=SHANGHAI)
                    except (ValueError,TypeError):continue
                else:continue
                if not start<=point<end:continue
                items.append({'key':f"{kind}:R{row['id']}",'kind':kind,'record_id':row['id'],
                    'title':row['title'],'customer_id':row['customer_id'],'customer_name':row['customer_name'],
                    'date':day,'at':stamp,'date_only':stamp is None,'executor_kind':terms.get('executor_kind','unknown'),
                    'evidence':terms.get(kind+'_evidence',''),'reminder_active':False,
                    'label':'截止要求' if kind=='deadline' else '回访／检查要求'})
    return sorted(items,key=lambda i:(i['date'],i['kind'],i['record_id']))
