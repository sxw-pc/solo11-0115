import hashlib,json
from collections import defaultdict
from datetime import datetime,timedelta
from pathlib import Path
from .store import connect
def sha(x):return hashlib.sha256(x).hexdigest()
def dt(x):
    d=datetime.fromisoformat(x.replace('Z','+00:00'))
    if d.tzinfo is None:raise ValueError('timezone_required')
    return d
def setup(db_path,contract):
    db=connect(db_path)
    with db:
        for a in contract['assets']:db.execute('INSERT OR IGNORE INTO asset VALUES (?)',(a,))
        for s in contract['sensors']:db.execute('INSERT OR IGNORE INTO sensor VALUES (?,?,?)',(s['id'],s['asset'],s['role']))
        for h in contract.get('health',[]):
            dt(h['start']);dt(h['end']);db.execute('INSERT INTO health(sensor,start,end,status) VALUES (?,?,?,?)',(h['sensor'],h['start'],h['end'],h['status']))
        for m in contract.get('maintenance',[]):
            dt(m['start']);dt(m['end']);db.execute('INSERT INTO maintenance(asset,start,end) VALUES (?,?,?)',(m['asset'],m['start'],m['end']))
    db.close()
def ingest(db_path,path):
    raw=Path(path).read_bytes();lines=raw.decode().splitlines();db=connect(db_path)
    with db:
        old=db.execute('SELECT id FROM source WHERE hash=?',(sha(raw),)).fetchone()
        if old:return 0
        sid=db.execute('INSERT INTO source(hash,body) VALUES (?,?)',(sha(raw),raw)).lastrowid;count=0
        for index,line in enumerate(lines,1):
            try:
                obj=json.loads(line);sensor=obj['sensor'];device=obj['device'];seq=int(obj['seq']);kind=obj['kind'];at=obj['at'];dt(at)
                if not db.execute('SELECT 1 FROM sensor WHERE id=?',(sensor,)).fetchone():raise ValueError('unknown_sensor')
                if kind not in {'anomaly','recover','anchor','heartbeat','stop','start'}:raise ValueError('bad_kind')
                digest=sha(line.encode());old=db.execute('SELECT hash FROM point WHERE device=? AND seq=?',(device,seq)).fetchone()
                if old:
                    if old['hash']!=digest:db.execute('INSERT INTO issue(code,subject,detail) VALUES (?,?,?)',('identity_conflict',device,str(seq)))
                    continue
                db.execute('INSERT INTO point(source_id,line_no,sensor,device,seq,at,kind,value,raw,hash) VALUES (?,?,?,?,?,?,?,?,?,?)',(sid,index,sensor,device,seq,at,kind,obj.get('value'),line,digest));count+=1
            except (ValueError,KeyError,TypeError,json.JSONDecodeError) as exc:
                db.execute('INSERT INTO issue(code,subject,detail) VALUES (?,?,?)',('parse_error',str(index),str(exc)))
    db.close();return count
def align(db,points):
    anchors=defaultdict(list)
    for p in points:
        if p['kind']=='anchor':anchors[p['device']].append(p)
    aligned=[];bad=set()
    for device,items in anchors.items():
        offsets=[json.loads(x['raw']).get('offset_seconds',0) for x in items]
        if offsets and max(offsets)-min(offsets)>120:bad.add(device)
    for p in points:
        if p['kind']=='anchor':continue
        eligible=[a for a in anchors[p['device']] if dt(a['at'])<=dt(p['at'])]
        offset=json.loads(max(eligible,key=lambda x:dt(x['at']))['raw']).get('offset_seconds',0) if eligible else 0
        x=dict(p);x['time']=dt(p['at'])-timedelta(seconds=offset);x['clock_bad']=p['device'] in bad or not eligible;aligned.append(x)
    return sorted(aligned,key=lambda x:(x['time'],x['id']))
def candidates(points,gap=timedelta(minutes=10)):
    by_sensor=defaultdict(list)
    for p in points:
        if p['kind'] in {'anomaly','recover'}:by_sensor[p['sensor']].append(p)
    result=[]
    for sensor,items in by_sensor.items():
        current=[]
        for p in items:
            if p['kind']=='recover':
                if current:result.append(current);current=[]
                continue
            if current and p['time']-current[-1]['time']>gap:result.append(current);current=[]
            current.append(p)
        if current:result.append(current)
    return result
def health_slice(points,start,end):
    relevant=[p for p in points if start<=p['time']<=end and p['kind'] in {'heartbeat','stop','start'}]
    stops=[p for p in relevant if p['kind']=='stop']
    beats=[p for p in relevant if p['kind']=='heartbeat']
    return {'stopped':bool(stops),'heartbeat_seen':bool(beats),'events':[p['id'] for p in relevant]}
def derive_health(db,points,max_gap=timedelta(minutes=30)):
    db.execute("DELETE FROM health WHERE origin='derived'")
    by_sensor=defaultdict(list)
    for p in points:
        if p['kind'] in {'heartbeat','stop','start'}:by_sensor[p['sensor']].append(p)
    intervals=[]
    for sensor,events in by_sensor.items():
        start=None;last=None
        for p in events:
            t=p['time']
            if start is not None and last is not None and t-last>max_gap:
                intervals.append((sensor,start,last+max_gap));start=None
            if p['kind']=='stop':
                if start is not None:intervals.append((sensor,start,t))
                start=last=None
            else:
                if start is None:start=t
                last=t
        if start is not None:intervals.append((sensor,start,last+max_gap))
    for sensor,start,end in intervals:
        db.execute('INSERT INTO health(sensor,start,end,status,origin) VALUES (?,?,?,?,?)',(sensor,start.isoformat(),end.isoformat(),'active','derived'))
    return intervals
def correlate(db,cands,gap=timedelta(minutes=5)):
    sensors={s['id']:(s['asset_id'],s['role']) for s in db.execute('SELECT * FROM sensor')};groups=[]
    for cand in sorted(cands,key=lambda c:c[0]['time']):
        asset,role=sensors[cand[0]['sensor']];start=cand[0]['time'];end=cand[-1]['time']
        matches=[g for g in groups if g['asset']==asset and role not in g['roles'] and start<=g['end']+gap and end>=g['start']-gap]
        if matches:
            target=matches[0];target['points']+=cand;target['roles'].add(role);target['start']=min(target['start'],start);target['end']=max(target['end'],end)
        else:groups.append({'asset':asset,'start':start,'end':end,'points':list(cand),'roles':{role}})
    return groups
def reconcile(db_path,gap_minutes=10):
    db=connect(db_path);raw=[dict(x) for x in db.execute('SELECT * FROM point ORDER BY at,id')]
    aligned=align(db,raw);cands=candidates(aligned,timedelta(minutes=gap_minutes));groups=correlate(db,cands)
    version=sha(json.dumps([(p['id'],p['hash']) for p in raw],sort_keys=True).encode());result=[]
    with db:
        old=db.execute('SELECT id FROM run WHERE version=?',(version,)).fetchone()
        if old:return old['id']
        derive_health(db,aligned)
        by_device=defaultdict(set)
        for p in raw:by_device[p['device']].add(p['seq'])
        gap_devices=set()
        for device,seqs in by_device.items():
            if seqs and max(seqs)-min(seqs)+1!=len(seqs):
                gap_devices.add(device)
                db.execute('INSERT INTO issue(code,subject,detail) VALUES (?,?,?)',('sequence_gap',device,'missing sequence'))
        rid=db.execute("INSERT INTO run(version,status) VALUES (?,'待闭合')",(version,)).lastrowid
        previous=[dict(x) for x in db.execute('SELECT e.* FROM episode e JOIN run r ON r.id=e.run_id WHERE r.id<(?)',(rid,))]
        for g in groups:
            maintenance=[m for m in db.execute('SELECT * FROM maintenance WHERE asset=?',(g['asset'],)) if dt(m['start'])<=g['end'] and dt(m['end'])>g['start']]
            health=[h for h in db.execute('SELECT * FROM health WHERE sensor IN (SELECT id FROM sensor WHERE asset_id=?)',(g['asset'],)) if dt(h['start'])<=g['start']<dt(h['end']) and h['status']=='active']
            signals=health_slice(aligned,g['start']-timedelta(minutes=30),g['end']+timedelta(minutes=30))
            status='维护影响' if maintenance and any(dt(m['start'])<=g['start'] and dt(m['end'])>=g['end'] for m in maintenance) else ('待核' if maintenance or not health or signals['stopped'] or not signals['heartbeat_seen'] or any(p['clock_bad'] or p['device'] in gap_devices for p in g['points']) else '可信')
            replaces=[p['id'] for p in previous if p['asset']==g['asset'] and dt(p['start'])<=g['end'] and dt(p['end'])>=g['start']]
            eid=db.execute('INSERT INTO episode(run_id,asset,start,end,status,replaces) VALUES (?,?,?,?,?,?)',(rid,g['asset'],g['start'].isoformat(),g['end'].isoformat(),status,json.dumps(replaces))).lastrowid
            for p in g['points']:db.execute('INSERT INTO evidence VALUES (?,?)',(eid,p['id']))
            result.append(status)
        db.execute('UPDATE run SET status=? WHERE id=?',('待核' if '待核' in result else '已闭合',rid))
    db.close();return rid
def report(db_path,rid,path):
    db=connect(db_path);run=db.execute('SELECT * FROM run WHERE id=?',(rid,)).fetchone()
    if not run:raise ValueError('unknown_run')
    obj={'run':dict(run),'episodes':[],'issues':[dict(x) for x in db.execute('SELECT * FROM issue')]}
    for e in db.execute('SELECT * FROM episode WHERE run_id=?',(rid,)):
        x=dict(e);x['evidence']=[dict(p) for p in db.execute('SELECT p.id,p.sensor,p.at,p.raw,s.hash AS source_hash FROM evidence ev JOIN point p ON p.id=ev.point_id JOIN source s ON s.id=p.source_id WHERE ev.episode_id=?',(e['id'],))];obj['episodes'].append(x)
    path=Path(path);path.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8');path.with_suffix('.md').write_text('# 突发拼窗报告\n\n'+f"事件数：{len(obj['episodes'])}，状态：{run['status']}\n",encoding='utf-8');db.close();return path
