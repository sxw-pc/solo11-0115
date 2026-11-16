"""Fleet common-cause and maintenance dispatch decisions."""
import json
from .store import connect
from .engine import dt

def fleet(path,run_id,min_assets=2):
    if min_assets<2:raise ValueError('min_assets_too_small')
    db=connect(path)
    try:
        with db:
            run=db.execute('SELECT status FROM run WHERE id=?',(run_id,)).fetchone()
            if not run:raise ValueError('unknown_run')
            episodes=[dict(x) for x in db.execute('SELECT * FROM episode WHERE run_id=? ORDER BY start,id',(run_id,))]
            credible=[e for e in episodes if e['status']=='可信']
            overlap=set()
            for i,a in enumerate(credible):
                group=[a]
                for b in credible[i+1:]:
                    if a['asset']!=b['asset'] and dt(a['start'])<=dt(b['end']) and dt(b['start'])<=dt(a['end']):group.append(b)
                if len({e['asset'] for e in group})>=min_assets:
                    overlap.update(e['id'] for e in group)
            if overlap:status,reason='共因待查','overlapping_assets'
            elif any(e['status']=='待核' for e in episodes):status,reason='证据不足','uncertain_episode'
            else:status,reason='局部事件','no_common_window'
            affected=[e for e in episodes if e['id'] in overlap]
            assets=sorted({e['asset'] for e in affected})
            db.execute('INSERT INTO fleet_decision VALUES (?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET status=excluded.status,assets=excluded.assets,episode_ids=excluded.episode_ids,reason=excluded.reason',
                       (run_id,status,json.dumps(assets),json.dumps(sorted(overlap)),reason))
            return {'status':status,'assets':assets,'episode_ids':sorted(overlap)}
    finally:db.close()

def triage(path,run_id):
    fleet(path,run_id)
    db=connect(path)
    try:
        with db:
            run=db.execute('SELECT status FROM run WHERE id=?',(run_id,)).fetchone()
            if not run:raise ValueError('unknown_run')
            common=db.execute('SELECT episode_ids FROM fleet_decision WHERE run_id=?',(run_id,)).fetchone()
            common_ids=set(json.loads(common['episode_ids'])) if common else set()
            outcomes=[]
            for e in db.execute('SELECT * FROM episode WHERE run_id=? ORDER BY id',(run_id,)):
                evidence=[x['point_id'] for x in db.execute('SELECT point_id FROM evidence WHERE episode_id=?',(e['id'],))]
                if run['status']!='已闭合' or e['status']=='待核' or not evidence:status,priority,reason='待复核','none','evidence_gap'
                elif e['status']=='维护影响':status,priority,reason='暂缓','none','planned_maintenance'
                else:
                    status='待派工';priority='critical' if e['id'] in common_ids else 'normal';reason='fleet_overlap' if priority=='critical' else 'isolated_fault'
                db.execute('INSERT INTO work_order VALUES (?,?,?,?,?,?) ON CONFLICT(episode_id) DO UPDATE SET status=excluded.status,priority=excluded.priority,reason=excluded.reason,evidence=excluded.evidence',
                           (e['id'],run_id,status,priority,reason,json.dumps(evidence)))
                outcomes.append({'episode_id':e['id'],'status':status,'priority':priority,'reason':reason})
            return outcomes
    finally:db.close()
