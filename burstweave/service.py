"""Freeze service dependency and interval-censored impact observations."""
import hashlib
import json
from pathlib import Path

from .engine import dt
from .store import connect


def register(path, service_id, run_id, input_path):
    if not service_id:
        raise ValueError('empty_service_id')
    input_path = Path(input_path).resolve()
    raw = input_path.read_bytes()
    doc = json.loads(raw)
    assets = doc.get('assets')
    impacts = doc.get('impacts')
    if (not isinstance(assets, list) or not assets or len(set(assets)) != len(assets)
            or not isinstance(impacts, list) or len(impacts) != len(assets)):
        raise ValueError('bad_service_contract')
    for relation in doc.get('onset_relations', []):
        if (relation['from'] not in assets or relation['to'] not in assets or
                relation['from'] == relation['to'] or
                relation['min_lag_minutes'] < 0 or
                relation['max_lag_minutes'] < relation['min_lag_minutes']):
            raise ValueError('bad_onset_relation')
    start, end = dt(doc['period_start']), dt(doc['period_end'])
    if start >= end:
        raise ValueError('bad_service_period')
    db = connect(path)
    try:
        with db:
            run = db.execute('SELECT status FROM run WHERE id=?', (run_id,)).fetchone()
            if not run or run['status'] != '已闭合':
                raise ValueError('incident_run_not_closed')
            seen = set()
            for item in impacts:
                asset = item['asset']
                if asset not in assets or asset in seen:
                    raise ValueError('bad_impact_asset')
                seen.add(asset)
                episode = db.execute('SELECT asset,status FROM episode WHERE id=? AND run_id=?',
                                     (item['episode_id'],run_id)).fetchone()
                if not episode or episode['asset'] != asset or episode['status'] != '可信':
                    raise ValueError('unproven_impact_episode')
                a,b = [dt(x) for x in item['onset_between']]
                c,d = [dt(x) for x in item['recovery_between']]
                if not (start <= a <= b <= c <= d <= end):
                    raise ValueError('bad_impact_bounds')
            digest = hashlib.sha256(raw).hexdigest()
            payload = json.dumps({'run':run_id,'file':str(input_path),
                                  'digest':digest,'doc':doc},
                                 sort_keys=True,separators=(',',':'))
            old = db.execute('SELECT payload FROM service_impact WHERE id=?',
                             (service_id,)).fetchone()
            if old and old['payload'] != payload:
                raise ValueError('service_impact_conflict')
            db.execute('INSERT OR IGNORE INTO service_impact VALUES (?,?)',
                       (service_id,payload))
    finally:
        db.close()
    return {'service_id':service_id,'digest':digest,'assets':len(assets)}
