"""Freeze crews, parts and procedures for a closed incident run."""
import hashlib
import json
from pathlib import Path

from .engine import dt
from .store import connect


def register(path, plan_id, run_id, input_path):
    if not plan_id:
        raise ValueError('empty_repair_plan')
    input_path = Path(input_path).resolve()
    raw = input_path.read_bytes()
    doc = json.loads(raw)
    crews = doc.get('crews', [])
    procedures = doc.get('procedures', [])
    if not crews or not procedures or len({c['id'] for c in crews}) != len(crews):
        raise ValueError('bad_repair_roster')
    if len({p['asset'] for p in procedures}) != len(procedures):
        raise ValueError('duplicate_procedure')
    for crew in crews:
        if not crew['skills'] or dt(crew['shift_start']) >= dt(crew['shift_end']):
            raise ValueError('bad_crew_shift')
    for procedure in procedures:
        if (procedure['minutes'] <= 0 or not procedure['skill'] or
                procedure['spare_count'] < 0 or
                procedure.get('checkpoint_minutes', 0) < 0):
            raise ValueError('bad_repair_procedure')
    db = connect(path)
    try:
        with db:
            run = db.execute('SELECT status FROM run WHERE id=?', (run_id,)).fetchone()
            if not run or run['status'] != '已闭合':
                raise ValueError('incident_run_not_closed')
            order_assets = {r['asset'] for r in db.execute('''
                SELECT e.asset FROM work_order w JOIN episode e ON e.id=w.episode_id
                WHERE w.run_id=? AND w.status='待派工' ''', (run_id,))}
            if not order_assets or not order_assets.issubset(
                    {p['asset'] for p in procedures}):
                raise ValueError('procedure_gap')
            for procedure in procedures:
                for downstream in procedure.get('expected_downstream', []):
                    if not db.execute('''SELECT 1 FROM topology_edge
                        WHERE source_asset=? AND target_asset=?''',
                        (procedure['asset'], downstream)).fetchone():
                        raise ValueError('unproven_recovery_dependency')
            digest = hashlib.sha256(raw).hexdigest()
            payload = json.dumps({'run':run_id,'file':str(input_path),
                                  'digest':digest,'doc':doc},
                                 sort_keys=True,separators=(',',':'))
            old = db.execute('SELECT payload FROM repair_roster WHERE id=?',
                             (plan_id,)).fetchone()
            if old and old['payload'] != payload:
                raise ValueError('repair_roster_conflict')
            db.execute('INSERT OR IGNORE INTO repair_roster VALUES (?,?)',
                       (plan_id,payload))
    finally:
        db.close()
    return {'plan_id':plan_id,'digest':digest,'orders':len(order_assets)}
