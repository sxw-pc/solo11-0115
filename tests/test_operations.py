import json
from burstweave import operations, engine
from burstweave.store import connect

def fixture(tmp_path,statuses=('可信','可信'),overlap=True):
    db=tmp_path/'ledger.db';conn=connect(db)
    with conn:
        conn.execute("INSERT INTO run(version,status) VALUES ('v1','已闭合')")
        for index,status in enumerate(statuses):
            asset=f'A{index}';conn.execute('INSERT INTO asset VALUES (?)',(asset,))
            start='2026-01-01T00:00:00Z' if overlap or index==0 else '2026-01-02T00:00:00Z'
            end='2026-01-01T01:00:00Z' if overlap or index==0 else '2026-01-02T01:00:00Z'
            eid=conn.execute('INSERT INTO episode(run_id,asset,start,end,status,replaces) VALUES (?,?,?,?,?,?)',(1,asset,start,end,status,'[]')).lastrowid
            point=conn.execute("INSERT INTO point(device,seq,at,kind,raw,hash) VALUES (?,?,?,?,?,?)",(asset,index,start,'anomaly','{}',asset)).lastrowid
            conn.execute('INSERT INTO evidence VALUES (?,?)',(eid,point))
    conn.close();return db

def test_common_cause_escalates_dispatch_and_is_idempotent(tmp_path):
    db=fixture(tmp_path)
    assert operations.fleet(db,1)['status']=='共因待查'
    orders=operations.triage(db,1)
    assert len(orders)==2 and {o['priority'] for o in orders}=={'critical'}
    assert operations.triage(db,1)==orders
    conn=connect(db)
    assert conn.execute('SELECT count(*) FROM work_order').fetchone()[0]==2
    assert json.loads(conn.execute('SELECT episode_ids FROM fleet_decision').fetchone()[0])==[1,2]

def test_distinct_windows_do_not_escalate_and_unknown_blocks_dispatch(tmp_path):
    db=fixture(tmp_path,overlap=False)
    assert operations.fleet(db,1)['status']=='局部事件'
    assert {o['priority'] for o in operations.triage(db,1)}=={'normal'}
    conn=connect(db)
    with conn:conn.execute("UPDATE episode SET status='待核' WHERE id=2")
    assert operations.fleet(db,1)['status']=='证据不足'
    orders=operations.triage(db,1)
    assert orders[1]['status']=='待复核'

def test_maintenance_and_missing_evidence_prevent_work_order(tmp_path):
    db=fixture(tmp_path,('可信','维护影响'))
    conn=connect(db)
    with conn:conn.execute('DELETE FROM evidence WHERE episode_id=1')
    operations.fleet(db,1)
    orders=operations.triage(db,1)
    assert orders[0]['status']=='待复核'
    assert orders[1]['status']=='暂缓'

def test_ingest_to_fleet_to_dispatch_real_chain(tmp_path):
    db=tmp_path/'real.db'
    engine.setup(db,{'assets':['A','B'],
                     'sensors':[{'id':'S1','asset':'A','role':'heat'}, {'id':'S2','asset':'B','role':'heat'}],
                     'health':[{'sensor':s,'start':'2026-01-01T00:00:00Z','end':'2026-01-02T00:00:00Z','status':'active'} for s in ('S1','S2')]})
    rows=[]
    for sensor,device in (('S1','D1'),('S2','D2')):
        for seq,kind,at in [(1,'anchor','2026-01-01T00:00:00Z'),
                            (2,'heartbeat','2026-01-01T01:00:00Z'),
                            (3,'anomaly','2026-01-01T01:00:01Z')]:
            rows.append({'sensor':sensor,'device':device,'seq':seq,'kind':kind,'at':at,'offset_seconds':0,'value':7})
    source=tmp_path/'events.jsonl';source.write_text('\n'.join(json.dumps(x) for x in rows))
    engine.ingest(db,source);rid=engine.reconcile(db)
    assert operations.fleet(db,rid)['status']=='共因待查'
    assert {x['priority'] for x in operations.triage(db,rid)}=={'critical'}
