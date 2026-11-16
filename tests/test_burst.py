import json
from burstweave import engine

def setup(tmp_path):
    db=tmp_path/'db';engine.setup(db,{'assets':['A'],'sensors':[{'id':'S','asset':'A','role':'heat'}],'health':[{'sensor':'S','start':'2026-01-01T00:00:00Z','end':'2026-01-02T00:00:00Z','status':'active'}]});return db
def write(tmp_path,rows,name='points.jsonl'):
    p=tmp_path/name;p.write_text('\n'.join(json.dumps(x) for x in rows));return p
def point(seq,time,kind='anomaly'):
    return {'sensor':'S','device':'D','seq':seq,'kind':kind,'at':time,'value':7}
def test_ingest_idempotent_and_conflict(tmp_path):
    db=setup(tmp_path);p=write(tmp_path,[point(1,'2026-01-01T01:00:00Z')]);assert engine.ingest(db,p)==1;assert engine.ingest(db,p)==0
    q=write(tmp_path,[point(1,'2026-01-01T02:00:00Z')],'other.jsonl');engine.ingest(db,q)
    assert engine.connect(db).execute("SELECT count(*) FROM issue WHERE code='identity_conflict'").fetchone()[0]==1
def test_window_and_report(tmp_path):
    db=setup(tmp_path);rows=[point(1,'2026-01-01T00:00:00Z','anchor'),point(2,'2026-01-01T01:00:00Z'),point(3,'2026-01-01T01:10:00Z')]
    engine.ingest(db,write(tmp_path,rows));rid=engine.reconcile(db);data=json.loads(engine.report(db,rid,tmp_path/'r.json').read_text(encoding='utf-8'))
    assert len(data['episodes'])==1;assert len(data['episodes'][0]['evidence'])==2
    assert engine.reconcile(db)==rid
def test_gap_split(tmp_path):
    db=setup(tmp_path);rows=[point(1,'2026-01-01T00:00:00Z','anchor'),point(2,'2026-01-01T01:00:00Z'),point(3,'2026-01-01T02:00:00Z')]
    engine.ingest(db,write(tmp_path,rows));rid=engine.reconcile(db)
    assert engine.connect(db).execute('SELECT count(*) FROM episode WHERE run_id=?',(rid,)).fetchone()[0]==2

def test_clock_anchor_required_for_confidence(tmp_path):
    db=setup(tmp_path);engine.ingest(db,write(tmp_path,[point(1,'2026-01-01T01:00:00Z')]))
    rid=engine.reconcile(db)
    assert engine.connect(db).execute('SELECT status FROM episode WHERE run_id=?',(rid,)).fetchone()[0]=='待核'

def test_sequence_gap_lowers_confidence(tmp_path):
    db=setup(tmp_path);rows=[point(1,'2026-01-01T00:00:00Z','anchor'),point(3,'2026-01-01T01:00:00Z')]
    engine.ingest(db,write(tmp_path,rows));rid=engine.reconcile(db);conn=engine.connect(db)
    assert conn.execute("SELECT count(*) FROM issue WHERE code='sequence_gap'").fetchone()[0]==1
    assert conn.execute('SELECT status FROM episode WHERE run_id=?',(rid,)).fetchone()[0]=='待核'

def test_maintenance_classification(tmp_path):
    db=setup(tmp_path);conn=engine.connect(db)
    with conn:conn.execute('INSERT INTO maintenance(asset,start,end) VALUES (?,?,?)',('A','2026-01-01T00:00:00Z','2026-01-01T02:00:00Z'))
    engine.ingest(db,write(tmp_path,[point(1,'2026-01-01T00:00:00Z','anchor'),point(2,'2026-01-01T01:00:00Z')]))
    rid=engine.reconcile(db)
    assert engine.connect(db).execute('SELECT status FROM episode WHERE run_id=?',(rid,)).fetchone()[0]=='维护影响'

def test_late_bridge_creates_new_version(tmp_path):
    db=setup(tmp_path);engine.ingest(db,write(tmp_path,[point(1,'2026-01-01T00:00:00Z','anchor'),point(2,'2026-01-01T01:00:00Z'),point(3,'2026-01-01T01:20:00Z')]))
    before=engine.reconcile(db);engine.ingest(db,write(tmp_path,[point(4,'2026-01-01T01:10:00Z')],'late.jsonl'))
    after=engine.reconcile(db);assert after!=before
    assert engine.connect(db).execute('SELECT count(*) FROM episode WHERE run_id=?',(after,)).fetchone()[0]==1

def test_bad_line_isolated(tmp_path):
    db=setup(tmp_path);p=tmp_path/'bad.jsonl';p.write_text('{bad}\n'+json.dumps(point(1,'2026-01-01T01:00:00Z')))
    assert engine.ingest(db,p)==1
    assert engine.connect(db).execute("SELECT count(*) FROM issue WHERE code='parse_error'").fetchone()[0]==1

def test_heartbeat_allows_trusted_episode(tmp_path):
    db=setup(tmp_path);rows=[point(1,'2026-01-01T00:00:00Z','anchor'),point(2,'2026-01-01T01:00:00Z','heartbeat'),point(3,'2026-01-01T01:00:01Z')]
    engine.ingest(db,write(tmp_path,rows));rid=engine.reconcile(db)
    assert engine.connect(db).execute('SELECT status FROM episode WHERE run_id=?',(rid,)).fetchone()[0]=='可信'

def test_stop_blocks_trusted_episode(tmp_path):
    db=setup(tmp_path);rows=[point(1,'2026-01-01T00:00:00Z','anchor'),point(2,'2026-01-01T01:00:00Z','heartbeat'),point(3,'2026-01-01T01:00:01Z','stop'),point(4,'2026-01-01T01:00:02Z')]
    engine.ingest(db,write(tmp_path,rows));rid=engine.reconcile(db)
    assert engine.connect(db).execute('SELECT status FROM episode WHERE run_id=?',(rid,)).fetchone()[0]=='待核'

def test_cross_sensor_roles_correlate(tmp_path):
    db=setup(tmp_path);conn=engine.connect(db)
    with conn:conn.execute('INSERT INTO sensor VALUES (?,?,?)',('S2','A','vibration'))
    rows=[point(1,'2026-01-01T00:00:00Z','anchor'),point(2,'2026-01-01T01:00:00Z','heartbeat'),point(3,'2026-01-01T01:00:01Z')]
    other={'sensor':'S2','device':'E','seq':1,'kind':'anomaly','at':'2026-01-01T01:00:02Z','value':9}
    engine.ingest(db,write(tmp_path,rows+[other]));rid=engine.reconcile(db)
    assert conn.execute('SELECT count(*) FROM episode WHERE run_id=?',(rid,)).fetchone()[0]==1

def test_health_intervals_derived_from_logs(tmp_path):
    db=setup(tmp_path);rows=[point(1,'2026-01-01T00:00:00Z','anchor'),point(2,'2026-01-01T01:00:00Z','heartbeat'),point(3,'2026-01-01T01:05:00Z','stop')]
    engine.ingest(db,write(tmp_path,rows));engine.reconcile(db)
    conn=engine.connect(db);interval=conn.execute("SELECT start,end FROM health WHERE origin='derived'").fetchone()
    assert engine.dt(interval['start'])==engine.dt('2026-01-01T01:00:00Z')
    assert engine.dt(interval['end'])==engine.dt('2026-01-01T01:05:00Z')
