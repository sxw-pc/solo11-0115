"""Freeze a synthetic telemetry block and its transmitter parity frames."""
import hashlib
import json
from pathlib import Path

from .store import connect


def register(db_path, case_id, fixture):
    if not case_id:
        raise ValueError('missing_parity_case_id')
    source = Path(fixture).resolve()
    raw = source.read_bytes()
    doc = json.loads(raw)
    if (doc.get('field_prime') != 257 or
            doc.get('seq_end', 0) < doc.get('seq_start', 1) or
            not isinstance(doc.get('parity'), dict)):
        raise ValueError('bad_parity_contract')
    if any(not isinstance(v,int) or not 0 <= v < 257 for v in doc['parity'].values()):
        raise ValueError('bad_parity_value')
    db = connect(db_path)
    try:
        with db:
            if not db.execute('SELECT 1 FROM sensor WHERE id=?',(doc['sensor'],)).fetchone():
                raise ValueError('unknown_parity_sensor')
            rows = list(db.execute('SELECT seq,value,hash FROM point WHERE device=? AND sensor=? AND seq BETWEEN ? AND ? ORDER BY seq',
                                   (doc['device'],doc['sensor'],doc['seq_start'],doc['seq_end'])))
            if not rows:
                raise ValueError('empty_parity_block')
            for row in rows:
                if row['value'] is None or int(row['value']) != row['value'] or not 0 <= row['value'] < 257:
                    raise ValueError('bad_parity_point')
            payload = json.dumps({'fixture':str(source),
                                  'digest':hashlib.sha256(raw).hexdigest(),
                                  'doc':doc,'observed':[(r['seq'],r['hash']) for r in rows]},
                                 sort_keys=True,separators=(',',':'))
            old = db.execute('SELECT payload FROM parity_case WHERE id=?',(case_id,)).fetchone()
            if old and old['payload'] != payload:
                raise ValueError('parity_case_conflict')
            db.execute('INSERT OR IGNORE INTO parity_case VALUES (?,?)',(case_id,payload))
    finally:
        db.close()
    return {'case_id':case_id,'digest':hashlib.sha256(raw).hexdigest(),
            'observed_count':len(rows)}
