"""Freeze a synthetic sensor-coordinate field survey, without area analysis."""
import hashlib
import json
from pathlib import Path

from .engine import dt
from .store import connect


def register(db_path, survey_id, fixture):
    if not survey_id:
        raise ValueError('missing_spatial_survey_id')
    source = Path(fixture).resolve()
    raw = source.read_bytes()
    doc = json.loads(raw)
    x0, y0, x1, y1 = doc['extent_km']
    if not (x0 < x1 and y0 < y1 and doc.get('threshold', -1) >= 0):
        raise ValueError('bad_spatial_extent')
    samples = doc.get('samples', [])
    if len(samples) < 4:
        raise ValueError('insufficient_spatial_samples')
    seen = set()
    for sample in samples:
        key = (sample['x_km'], sample['y_km'])
        if (key in seen or not x0 <= key[0] <= x1 or not y0 <= key[1] <= y1 or
                sample['value'] < 0):
            raise ValueError('bad_spatial_sample')
        dt(sample['at'])
        seen.add(key)
    db = connect(db_path)
    try:
        with db:
            point_hashes = []
            for sample in samples:
                matches = db.execute('SELECT hash,value FROM point WHERE sensor=? AND at=?',
                                     (sample['sensor'], sample['at'])).fetchall()
                if len(matches) != 1 or matches[0]['value'] != sample['value']:
                    raise ValueError('unmatched_spatial_point')
                point_hashes.append(matches[0]['hash'])
            digest = hashlib.sha256(raw).hexdigest()
            payload = json.dumps({'fixture': str(source), 'digest': digest,
                                  'doc': doc, 'point_hashes': point_hashes},
                                 sort_keys=True, separators=(',', ':'))
            old = db.execute('SELECT payload FROM spatial_survey WHERE id=?',
                             (survey_id,)).fetchone()
            if old and old['payload'] != payload:
                raise ValueError('spatial_survey_conflict')
            db.execute('INSERT OR IGNORE INTO spatial_survey VALUES (?,?)',
                       (survey_id, payload))
    finally:
        db.close()
    return {'survey_id': survey_id, 'digest': digest,
            'point_count': len(samples)}
