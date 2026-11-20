"""Freeze service dependency and interval-censored impact observations."""
import hashlib
import json
from pathlib import Path

from . import timeline
from .engine import dt
from .store import connect


def register(db_path, service_id, run_id, input_path):
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
    db = connect(db_path)
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


def _canonical(payload):
    return json.dumps(payload, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False)


def _iso(value):
    if value is None:
        return None
    text = value.isoformat()
    return text[:-6] + 'Z' if text.endswith('+00:00') else text


# Failures of the frozen run or raw impact source, not of the certificate row.
SOURCE_FAILURES = ('run_missing', 'run_not_closed', 'impact_source_tampered',
                   'impact_source_unreadable')


def _witness_to_json(witness):
    if witness is None:
        return None
    return {
        'assets': {asset: {'onset': _iso(part['onset']),
                           'recovery': _iso(part['recovery'])}
                   for asset, part in sorted(witness['assets'].items())},
        'outage_segments': [{'start': _iso(seg['start']),
                             'end': _iso(seg['end']),
                             'length_minutes': seg['length_minutes']}
                            for seg in witness['outage_segments']],
        'outage_minutes': witness['outage_minutes'],
    }


def _witness_to_offsets(witness):
    onset = {asset: part['onset'] for asset, part in witness['assets'].items()}
    recovery = {asset: part['recovery'] for asset, part in witness['assets'].items()}
    return onset, recovery


def _load_frozen(db, service_id, run_id):
    """Re-read the closed run and original impact file; return evidence state."""
    reasons = []
    row = db.execute('SELECT payload FROM service_impact WHERE id=?',
                     (service_id,)).fetchone()
    if not row:
        raise ValueError('service_impact_not_registered')
    frozen = json.loads(row['payload'])
    doc = frozen['doc']
    if run_id != frozen['run']:
        raise ValueError('run_identity_mismatch')

    run = db.execute('SELECT status,version FROM run WHERE id=?', (run_id,)).fetchone()
    if not run:
        reasons.append('run_missing')
    elif run['status'] != '已闭合':
        reasons.append('run_not_closed')

    raw_digest = None
    impact_path = Path(frozen['file'])
    try:
        raw = impact_path.read_bytes()
        raw_digest = hashlib.sha256(raw).hexdigest()
        if raw_digest != frozen['digest']:
            reasons.append('impact_source_tampered')
        elif json.loads(raw) != doc:
            reasons.append('impact_source_tampered')
    except OSError:
        reasons.append('impact_source_unreadable')

    for item in doc['impacts']:
        episode = db.execute(
            'SELECT asset,status FROM episode WHERE id=? AND run_id=?',
            (item['episode_id'], run_id)).fetchone()
        if (not episode or episode['asset'] != item['asset']
                or episode['status'] != '可信'):
            reasons.append('impact_episode_not_credible')
            break

    return doc, frozen, raw_digest, reasons


def _build_doc_model(doc):
    impacts = {
        item['asset']: {
            'onset': (dt(item['onset_between'][0]), dt(item['onset_between'][1])),
            'recovery': (dt(item['recovery_between'][0]),
                         dt(item['recovery_between'][1])),
        }
        for item in doc['impacts']
    }
    relations = [
        (rel['from'], rel['to'], rel['min_lag_minutes'], rel['max_lag_minutes'])
        for rel in doc.get('onset_relations', [])
    ]
    return timeline.build_model(dt(doc['period_start']), dt(doc['period_end']),
                                list(doc['assets']), impacts, relations)


def _compute(db_path, service_id, run_id, topology, topology_assets, sla_minutes):
    if topology not in timeline.TOPOLOGIES:
        raise ValueError('unknown_topology')
    if not isinstance(sla_minutes, int) or isinstance(sla_minutes, bool) or sla_minutes < 0:
        raise ValueError('bad_sla_minutes')
    if (not topology_assets or len(set(topology_assets)) != len(topology_assets)
            or any(not isinstance(a, str) or not a for a in topology_assets)):
        raise ValueError('bad_topology_assets')
    db = connect(db_path)
    try:
        doc, frozen, raw_digest, reasons = _load_frozen(
            db, service_id, run_id)
        if topology == timeline.REDUNDANT_2 and len(topology_assets) != 2:
            raise ValueError('redundant_2_requires_two_assets')
        missing = [a for a in topology_assets if a not in doc['assets']]
        if missing:
            reasons.append('topology_asset_without_evidence:' + ','.join(sorted(missing)))
        result = None
        model = None
        if not reasons:
            model = _build_doc_model(doc)
            if any(a not in model['impacts'] for a in topology_assets):
                reasons.append('topology_asset_without_evidence')
            else:
                result = timeline.bound_service(
                    model, topology, list(topology_assets), sla_minutes)
                if not result['feasible']:
                    reasons.append(result['reason'])
        if reasons:
            result = {'feasible': False, 'verdict': timeline.INDETERMINATE,
                      'reason': reasons[0], 'min_minutes': None,
                      'max_minutes': None, 'min_witness': None,
                      'max_witness': None}
        payload = {
            'service_id': service_id,
            'run_id': run_id,
            'frozen_impact_digest': frozen['digest'],
            'current_impact_digest': raw_digest,
            'period_start': doc['period_start'],
            'period_end': doc['period_end'],
            'service_assets': list(doc['assets']),
            'topology': topology,
            'topology_assets': list(topology_assets),
            'segment_semantics': 'half_open_onset_inclusive_recovery_exclusive',
            'sla_allowed_minutes': sla_minutes,
            'verdict': result['verdict'],
            'reasons': reasons or [result['reason'] or 'bounded'],
            'outage_minutes_min': result['min_minutes'],
            'outage_minutes_max': result['max_minutes'],
            'min_witness': _witness_to_json(result['min_witness']),
            'max_witness': _witness_to_json(result['max_witness']),
            'sources_summary': {
                'impact_file': frozen['file'],
                'impact_digest': frozen['digest'],
                'onset_relations': list(doc.get('onset_relations', [])),
                'episodes': [{'asset': item['asset'],
                              'episode_id': item['episode_id']}
                             for item in doc['impacts']],
            },
        }
        return payload, model, result
    finally:
        db.close()


def assess(db_path, service_id, run_id, topology, topology_assets, sla_minutes):
    """Freeze a service availability certificate with joint timeline bounds."""
    payload, _, _ = _compute(db_path, service_id, run_id, topology,
                             tuple(topology_assets), sla_minutes)
    digest = hashlib.sha256(_canonical(payload).encode()).hexdigest()
    db = connect(db_path)
    try:
        with db:
            existing = db.execute(
                'SELECT payload,digest FROM service_certificate WHERE id=?',
                (service_id,)).fetchone()
            if existing:
                old = json.loads(existing['payload'])
                identity = ('run_id', 'topology', 'topology_assets',
                            'sla_allowed_minutes', 'frozen_impact_digest')
                if any(old[key] != payload[key] for key in identity):
                    raise ValueError('certificate_contract_conflict')
            db.execute(
                'INSERT OR REPLACE INTO service_certificate(id,run_id,payload,digest)'
                ' VALUES (?,?,?,?)',
                (service_id, run_id, _canonical(payload), digest))
    finally:
        db.close()
    return {'service_id': service_id, 'verdict': payload['verdict'],
            'outage_minutes_min': payload['outage_minutes_min'],
            'outage_minutes_max': payload['outage_minutes_max'],
            'digest': digest}


def verify(db_path, service_id):
    """Recompute the certificate from raw sources and reject any rewritten ledger."""
    db = connect(db_path)
    try:
        row = db.execute(
            'SELECT run_id,payload,digest FROM service_certificate WHERE id=?',
            (service_id,)).fetchone()
        if not row:
            raise ValueError('certificate_not_found')
        stored_digest = row['digest']
        stored = json.loads(row['payload'])
        if hashlib.sha256(row['payload'].encode()).hexdigest() != stored_digest:
            with db:
                _discard(db, service_id)
            raise ValueError('certificate_tampered')
    finally:
        db.close()

    payload, model, result = _compute(
        db_path, service_id, stored['run_id'], stored['topology'],
        tuple(stored['topology_assets']), stored['sla_allowed_minutes'])

    # A rewritten run, impact file or episode ledger invalidates the
    # certificate on its own; surface the concrete evidence failure.
    source_failure = next((reason for reason in payload['reasons']
                           if reason.startswith(SOURCE_FAILURES)), None)
    if source_failure:
        db = connect(db_path)
        try:
            with db:
                db.execute('DELETE FROM service_certificate WHERE id=?',
                           (service_id,))
        finally:
            db.close()
        raise ValueError(source_failure)

    mismatches = []
    for key in ('run_id', 'topology', 'topology_assets', 'sla_allowed_minutes',
                'frozen_impact_digest', 'verdict', 'outage_minutes_min',
                'outage_minutes_max', 'period_start', 'period_end',
                'service_assets'):
        if stored.get(key) != payload.get(key):
            mismatches.append(key)
    if stored.get('min_witness') != payload.get('min_witness') or \
            stored.get('max_witness') != payload.get('max_witness'):
        mismatches.append('witnesses')

    if not mismatches and result is not None and result['feasible']:
        for name in ('min_witness', 'max_witness'):
            witness = stored[name]
            onset_json, recovery_json = _witness_to_offsets(witness)
            onset = {a: int((dt(v) - model['start']).total_seconds() // 60)
                     for a, v in onset_json.items()}
            recovery = {a: int((dt(v) - model['start']).total_seconds() // 60)
                        for a, v in recovery_json.items()}
            if not timeline.check_witness(model, onset, recovery):
                mismatches.append(name + '_infeasible')
                continue
            length, _ = timeline.outage(
                model, stored['topology'], stored['topology_assets'], onset, recovery)
            if length != witness['outage_minutes'] or \
                    length != (stored['outage_minutes_min'] if name == 'min_witness'
                               else stored['outage_minutes_max']):
                mismatches.append(name + '_length')

    if mismatches:
        db = connect(db_path)
        try:
            with db:
                db.execute('DELETE FROM service_certificate WHERE id=?',
                           (service_id,))
        finally:
            db.close()
        raise ValueError('stale_certificate:' + ','.join(mismatches))
    return {'service_id': service_id, 'verdict': payload['verdict'],
            'outage_minutes_min': payload['outage_minutes_min'],
            'outage_minutes_max': payload['outage_minutes_max'],
            'digest': stored_digest}


def _discard(db, service_id):
    db.execute('DELETE FROM service_certificate WHERE id=?', (service_id,))
