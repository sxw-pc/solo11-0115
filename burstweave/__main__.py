import argparse,json,tempfile
from pathlib import Path
from . import engine,operations
def demo():
    root=Path(tempfile.mkdtemp(prefix='burstweave-'));db=root/'ledger.db'
    engine.setup(db,{'assets':['A','B'],'sensors':[{'id':'S1','asset':'A','role':'vibration'},{'id':'S2','asset':'B','role':'heat'}],'health':[{'sensor':s,'start':'2026-01-01T00:00:00Z','end':'2026-01-02T00:00:00Z','status':'active'} for s in ('S1','S2')]})
    rows=[]
    for sensor,device in (('S1','D1'),('S2','D2')):
        rows += [{'sensor':sensor,'device':device,'seq':1,'kind':'anchor','at':'2026-01-01T00:00:00Z','offset_seconds':0},{'sensor':sensor,'device':device,'seq':2,'kind':'heartbeat','at':'2026-01-01T01:00:00Z'},{'sensor':sensor,'device':device,'seq':3,'kind':'anomaly','at':'2026-01-01T01:00:01Z','value':7}]
    p=root/'events.jsonl';p.write_text('\n'.join(json.dumps(x) for x in rows));engine.ingest(db,p);rid=engine.reconcile(db)
    print(operations.fleet(db,rid));print(operations.triage(db,rid));print(engine.report(db,rid,root/'report.json'))
def main():
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='cmd',required=True);sub.add_parser('demo')
    s=sub.add_parser('setup');s.add_argument('--db',required=True);s.add_argument('--contract',required=True)
    s=sub.add_parser('ingest');s.add_argument('--db',required=True);s.add_argument('file')
    for n in ('align','health','candidates','correlate','classify','reconcile'):
        s=sub.add_parser(n);s.add_argument('--db',required=True)
        if n=='health':s.add_argument('--start',required=True);s.add_argument('--end',required=True)
        if n in ('classify','reconcile'):s.add_argument('--gap-minutes',type=int,default=10)
    s=sub.add_parser('report');s.add_argument('--db',required=True);s.add_argument('--run-id',type=int,required=True);s.add_argument('--out',required=True)
    s=sub.add_parser('fleet');s.add_argument('--db',required=True);s.add_argument('--run-id',type=int,required=True);s.add_argument('--min-assets',type=int,default=2)
    s=sub.add_parser('triage');s.add_argument('--db',required=True);s.add_argument('--run-id',type=int,required=True)
    x=p.parse_args()
    if x.cmd=='demo':demo()
    elif x.cmd=='setup':engine.setup(x.db,json.loads(Path(x.contract).read_text()))
    elif x.cmd=='ingest':print(engine.ingest(x.db,x.file))
    elif x.cmd in ('align','health','candidates','correlate'):
        from .store import connect
        conn=connect(x.db);points=engine.align(conn,[dict(p) for p in conn.execute('SELECT * FROM point')]);groups=engine.candidates(points)
        if x.cmd=='align':result=[{'id':p['id'],'time':p['time'].isoformat(),'clock_bad':p['clock_bad']} for p in points]
        elif x.cmd=='health':result=engine.health_slice(points,engine.dt(x.start),engine.dt(x.end))
        elif x.cmd=='candidates':result=[[p['id'] for p in group] for group in groups]
        else:result=[{'asset':g['asset'],'start':g['start'].isoformat(),'end':g['end'].isoformat(),'points':[p['id'] for p in g['points']]} for g in engine.correlate(conn,groups)]
        conn.close();print(json.dumps(result,ensure_ascii=False))
    elif x.cmd in ('classify','reconcile'):print(engine.reconcile(x.db,x.gap_minutes))
    elif x.cmd=='fleet':print(operations.fleet(x.db,x.run_id,x.min_assets))
    elif x.cmd=='triage':print(operations.triage(x.db,x.run_id))
    else:print(engine.report(x.db,x.run_id,x.out))
if __name__=='__main__':main()
