"""Freeze crews, parts and procedures for a closed incident run."""
import hashlib
import json
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from .engine import align, dt
from .store import connect

STRATEGIES = ('root_first', 'branch_containment_first')
EVENT_KINDS = ('step_start', 'spare_checkout', 'step_end', 'field_result')


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


def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _points_digest(db):
    rows = [(r['id'], r['hash']) for r in db.execute('SELECT id,hash FROM point ORDER BY id')]
    return _sha(_canon(rows))


def _schedule(orders, links, procedures, crews, spares, strategy):
    """Order steps by strategy and fit them into crew shifts and spare stock."""
    procedures = {p['asset']: p for p in procedures}
    ids = [o['episode_id'] for o in orders]
    asset_of = {o['episode_id']: o['asset'] for o in orders}
    for ep in ids:
        if asset_of[ep] not in procedures:
            raise ValueError('procedure_gap')
    edges = [(s, t) for s, t, _ in links if s in ids and t in ids]
    if strategy != 'root_first':
        edges = [(t, s) for s, t in edges]
    indegree = {i: 0 for i in ids}
    follows = defaultdict(set)
    for a, b in edges:
        if b not in follows[a]:
            follows[a].add(b)
            indegree[b] += 1
    ready = sorted(i for i in ids if not indegree[i])
    order = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for nxt in sorted(follows[node]):
            indegree[nxt] -= 1
            if not indegree[nxt]:
                ready.append(nxt)
        ready.sort()
    if len(order) != len(ids):
        raise ValueError('link_cycle')
    downstreams = defaultdict(list)
    for s, t, _ in links:
        if s in ids and t in ids:
            proc = procedures[asset_of[s]]
            if (proc.get('checkpoint_minutes', 0) > 0 and
                    asset_of[t] in proc.get('expected_downstream', [])):
                downstreams[s].append(t)
    demand = defaultdict(int)
    for ep in ids:
        proc = procedures[asset_of[ep]]
        if proc.get('spare') and proc.get('spare_count', 0):
            demand[proc['spare']] += proc['spare_count']
    for spare, count in demand.items():
        if count > spares.get(spare, 0):
            raise ValueError('spare_stock_insufficient')
    free = {c['id']: dt(c['shift_start']) for c in crews}
    wait = {}
    steps = []
    seq = 0
    for ep in order:
        proc = procedures[asset_of[ep]]
        eligible = [c for c in crews if proc['skill'] in c['skills']]
        if not eligible:
            raise ValueError('skill_unavailable')
        crew = min(eligible, key=lambda c: free[c['id']])
        start = free[crew['id']]
        if ep in wait:
            start = max(start, wait[ep])
        end = start + timedelta(minutes=proc['minutes'])
        if end > dt(crew['shift_end']):
            raise ValueError('shift_infeasible')
        seq += 1
        steps.append({'seq': seq, 'kind': 'repair', 'episode_id': ep,
                      'asset': asset_of[ep], 'crew': crew['id'], 'skill': proc['skill'],
                      'minutes': proc['minutes'], 'spare': proc.get('spare'),
                      'spare_count': proc.get('spare_count', 0),
                      'window_start': start.isoformat(), 'window_end': end.isoformat(),
                      'observes': [], 'after_seq': None})
        free[crew['id']] = end
        if downstreams.get(ep):
            close = end + timedelta(minutes=proc['checkpoint_minutes'])
            seq += 1
            steps.append({'seq': seq, 'kind': 'checkpoint', 'episode_id': None,
                          'asset': asset_of[ep], 'crew': None, 'skill': None,
                          'minutes': proc['checkpoint_minutes'], 'spare': None,
                          'spare_count': 0, 'window_start': end.isoformat(),
                          'window_end': close.isoformat(),
                          'observes': sorted(downstreams[ep]), 'after_seq': seq - 1})
            for target in downstreams[ep]:
                wait[target] = close
    return steps


def plan(path, plan_id, roster_id, run_id, strategy):
    """Freeze run, orders, links, crews, spares, procedures and strategy into a plan."""
    if strategy not in STRATEGIES:
        raise ValueError('unknown_strategy')
    db = connect(path)
    try:
        run = db.execute('SELECT * FROM run WHERE id=?', (run_id,)).fetchone()
        if not run or run['status'] != '已闭合':
            raise ValueError('incident_run_not_closed')
        roster = db.execute('SELECT payload FROM repair_roster WHERE id=?', (roster_id,)).fetchone()
        if not roster:
            raise ValueError('unknown_repair_roster')
        roster_doc = json.loads(roster['payload'])
        if roster_doc['run'] != run_id:
            raise ValueError('roster_run_mismatch')
        doc = roster_doc['doc']
        crews, spares, procedures = doc['crews'], doc.get('spares', {}), doc['procedures']
        orders = []
        for r in db.execute('''
            SELECT w.episode_id,e.asset,w.priority,w.reason,w.evidence
            FROM work_order w JOIN episode e ON e.id=w.episode_id
            WHERE w.run_id=? AND w.status IN ('待派工','已派工') AND e.status='可信'
            AND EXISTS (SELECT 1 FROM evidence ev WHERE ev.episode_id=w.episode_id)
            ORDER BY w.episode_id''', (run_id,)):
            orders.append({'episode_id': r['episode_id'], 'asset': r['asset'],
                           'priority': r['priority'], 'reason': r['reason'],
                           'evidence': json.loads(r['evidence'])})
        if not orders:
            raise ValueError('no_eligible_orders')
        links = [[r['source_episode'], r['target_episode'], r['lag_minutes']]
                 for r in db.execute('''SELECT * FROM propagation_link WHERE run_id=?
                     ORDER BY source_episode,target_episode''', (run_id,))]
        steps = _schedule(orders, links, procedures, crews, spares, strategy)
        payload = {'run_id': run_id, 'run_version': run['version'], 'strategy': strategy,
                   'roster_id': roster_id, 'roster_payload': roster['payload'],
                   'orders': orders, 'links': links, 'crews': crews, 'spares': spares,
                   'procedures': procedures, 'points_digest': _points_digest(db),
                   'steps': steps}
        digest = _sha(_canon(payload))
        old = db.execute('SELECT digest FROM repair_plan WHERE id=?', (plan_id,)).fetchone()
        if old:
            if old['digest'] != digest:
                raise ValueError('repair_plan_conflict')
            return {'plan_id': plan_id, 'digest': digest, 'steps': steps}
        with db:
            db.execute('INSERT INTO repair_plan VALUES (?,?,?,?,?)',
                       (plan_id, run_id, strategy, digest, _canon(payload)))
            for s in steps:
                db.execute('INSERT INTO repair_step VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (plan_id, s['seq'], s['kind'], s['episode_id'], s['asset'],
                            s['crew'], s['skill'], s['minutes'], s['spare'],
                            s['spare_count'], s['window_start'], s['window_end'],
                            _canon(s['observes']), s['after_seq']))
            marks = ','.join('?' * len(orders))
            db.execute(f"UPDATE work_order SET status='已派工' WHERE episode_id IN ({marks})",
                       [o['episode_id'] for o in orders])
        return {'plan_id': plan_id, 'digest': digest, 'steps': steps}
    finally:
        db.close()


def _load_plan(db, plan_id):
    row = db.execute('SELECT * FROM repair_plan WHERE id=?', (plan_id,)).fetchone()
    if not row:
        raise ValueError('unknown_repair_plan')
    return row, json.loads(row['payload'])


def _require_fresh(db, payload):
    run = db.execute('SELECT * FROM run WHERE id=?', (payload['run_id'],)).fetchone()
    if not run or run['status'] != '已闭合':
        raise ValueError('run_not_closed')
    if run['version'] != payload['run_version']:
        raise ValueError('run_version_changed')
    if db.execute('SELECT 1 FROM run WHERE id>?', (payload['run_id'],)).fetchone():
        raise ValueError('stale_run')
    if _points_digest(db) != payload['points_digest']:
        raise ValueError('stale_evidence')


def _event_hash(prev_hash, seq, kind, at, detail):
    return _sha(prev_hash + '|' + _canon({'seq': seq, 'kind': kind, 'at': at, 'detail': detail}))


def _set_outcome(db, plan_id, episode_id, status, basis):
    db.execute('''INSERT INTO repair_outcome VALUES (?,?,?,?)
        ON CONFLICT(plan_id,episode_id) DO UPDATE SET status=excluded.status,basis=excluded.basis''',
        (plan_id, episode_id, status, _canon(basis)))


def _check_event(db, plan_id, payload, step, kind, at, detail):
    kinds = {r['kind'] for r in db.execute(
        'SELECT kind FROM repair_event WHERE plan_id=? AND seq=?', (plan_id, step['seq']))}
    if kind == 'step_start':
        if kinds:
            raise ValueError('event_order')
        if detail.get('crew') != step['crew']:
            raise ValueError('crew_mismatch')
        if dt(at) != dt(step['window_start']):
            raise ValueError('window_mismatch')
        outcome = db.execute('SELECT status FROM repair_outcome WHERE plan_id=? AND episode_id=?',
                             (plan_id, step['episode_id'])).fetchone()
        if outcome and outcome['status'] == '上游修复免修':
            raise ValueError('step_exempted')
    elif kind == 'spare_checkout':
        if 'step_start' not in kinds or 'step_end' in kinds:
            raise ValueError('event_order')
        if not step['spare'] or not step['spare_count']:
            raise ValueError('no_spare_required')
        if detail.get('spare') != step['spare'] or int(detail.get('count', 0)) != step['spare_count']:
            raise ValueError('spare_mismatch')
        used = db.execute('SELECT COALESCE(SUM(delta),0) s FROM spare_ledger WHERE plan_id=? AND spare=?',
                          (plan_id, step['spare'])).fetchone()['s']
        if payload['spares'].get(step['spare'], 0) + used < step['spare_count']:
            raise ValueError('spare_unavailable')
    elif kind == 'step_end':
        if 'step_start' not in kinds or 'step_end' in kinds:
            raise ValueError('event_order')
        if step['spare_count'] and 'spare_checkout' not in kinds:
            raise ValueError('spare_not_checked_out')
        if dt(at) != dt(step['window_end']):
            raise ValueError('window_mismatch')
    elif kind == 'field_result':
        if 'step_end' not in kinds:
            raise ValueError('event_order')
        if detail.get('result') not in ('修复成功', '修复失败'):
            raise ValueError('bad_field_result')
        if dt(at) != dt(step['window_end']):
            raise ValueError('window_mismatch')


def apply(path, plan_id, events_path):
    """Append field events; each confirmed event is immutable and retry-safe."""
    db = connect(path)
    try:
        row, payload = _load_plan(db, plan_id)
        _require_fresh(db, payload)
        steps = {s['seq']: s for s in payload['steps']}
        applied = skipped = 0
        for line in Path(events_path).read_text(encoding='utf-8').splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            seq, kind, at = event.get('seq'), event.get('kind'), event.get('at')
            if kind not in EVENT_KINDS or seq not in steps or not isinstance(at, str):
                raise ValueError('bad_repair_event')
            dt(at)
            step = steps[seq]
            if step['kind'] != 'repair':
                raise ValueError('checkpoint_not_manual')
            detail = _canon({k: v for k, v in event.items() if k not in ('seq', 'kind', 'at')})
            old = db.execute('SELECT * FROM repair_event WHERE plan_id=? AND seq=? AND kind=?',
                             (plan_id, seq, kind)).fetchone()
            if old:
                if old['at'] == at and old['detail'] == detail:
                    skipped += 1
                    continue
                with db:
                    db.execute("INSERT INTO issue(code,subject,detail) VALUES (?,?,?)",
                               ('repair_event_conflict', plan_id, f'seq{seq}:{kind}'))
                raise ValueError('repair_event_conflict')
            with db:
                _check_event(db, plan_id, payload, step, kind, at, json.loads(detail))
                prev = db.execute('''SELECT hash FROM repair_event WHERE plan_id=?
                    ORDER BY id DESC LIMIT 1''', (plan_id,)).fetchone()
                prev_hash = prev['hash'] if prev else row['digest']
                eid = db.execute('''INSERT INTO repair_event(plan_id,seq,kind,at,detail,prev_hash,hash)
                    VALUES (?,?,?,?,?,?,?)''',
                    (plan_id, seq, kind, at, detail, prev_hash,
                     _event_hash(prev_hash, seq, kind, at, json.loads(detail)))).lastrowid
                if kind == 'spare_checkout':
                    db.execute('INSERT INTO spare_ledger(plan_id,spare,delta,event_id) VALUES (?,?,?,?)',
                               (plan_id, step['spare'], -step['spare_count'], eid))
                elif kind == 'step_start':
                    db.execute("UPDATE work_order SET status='维修中' WHERE episode_id=?",
                               (step['episode_id'],))
                elif kind == 'field_result' and json.loads(detail).get('result') == '修复成功':
                    ids = [r['id'] for r in db.execute('''SELECT id FROM repair_event
                        WHERE plan_id=? AND seq=? ORDER BY id''', (plan_id, seq))]
                    _set_outcome(db, plan_id, step['episode_id'], '已修复',
                                 {'events': ids, 'result': '修复成功'})
            applied += 1
        return {'plan_id': plan_id, 'applied': applied, 'skipped': skipped}
    finally:
        db.close()


def _recovery_evidence(db, asset, start, end):
    """Independent recover/heartbeat proof for an asset inside a frozen window."""
    sensors = {r['id']: r['asset_id'] for r in db.execute('SELECT id,asset_id FROM sensor')}
    points = [dict(x) for x in db.execute('SELECT * FROM point ORDER BY at,id')]
    ws, we = dt(start), dt(end)
    hits = [p for p in align(db, points)
            if sensors.get(p['sensor']) == asset and p['kind'] in ('recover', 'heartbeat')
            and not p['clock_bad'] and ws <= p['time'] <= we]
    kinds = {p['kind'] for p in hits}
    return {'sufficient': {'recover', 'heartbeat'} <= kinds,
            'points': sorted(p['id'] for p in hits)}


def checkpoint(path, plan_id, seq):
    """Close a frozen observation window and re-read downstream raw evidence."""
    db = connect(path)
    try:
        row, payload = _load_plan(db, plan_id)
        _require_fresh(db, payload)
        steps = {s['seq']: s for s in payload['steps']}
        step = steps.get(seq)
        if not step or step['kind'] != 'checkpoint':
            raise ValueError('not_checkpoint_step')
        upstream = steps[step['after_seq']]
        result = db.execute('''SELECT detail FROM repair_event
            WHERE plan_id=? AND seq=? AND kind='field_result' ''',
            (plan_id, upstream['seq'])).fetchone()
        if not result or json.loads(result['detail']).get('result') != '修复成功':
            raise ValueError('upstream_not_repaired')
        assets = {o['episode_id']: o['asset'] for o in payload['orders']}
        at = dt(step['window_end']).isoformat().replace('+00:00', 'Z')
        observed = []
        for ep in step['observes']:
            evidence = _recovery_evidence(db, assets[ep], step['window_start'], step['window_end'])
            observed.append({'episode_id': ep, 'asset': assets[ep],
                             'sufficient': evidence['sufficient'], 'evidence': evidence['points']})
        detail = {'observed': observed}
        old = db.execute('''SELECT * FROM repair_event WHERE plan_id=? AND seq=?
            AND kind='checkpoint_close' ''', (plan_id, seq)).fetchone()
        if old:
            if old['at'] == at and old['detail'] == _canon(detail):
                return {'plan_id': plan_id, 'seq': seq, 'observed': observed, 'skipped': True}
            with db:
                db.execute("INSERT INTO issue(code,subject,detail) VALUES (?,?,?)",
                           ('repair_event_conflict', plan_id, f'seq{seq}:checkpoint_close'))
            raise ValueError('repair_event_conflict')
        with db:
            prev = db.execute('''SELECT hash FROM repair_event WHERE plan_id=?
                ORDER BY id DESC LIMIT 1''', (plan_id,)).fetchone()
            prev_hash = prev['hash'] if prev else row['digest']
            eid = db.execute('''INSERT INTO repair_event(plan_id,seq,kind,at,detail,prev_hash,hash)
                VALUES (?,?,?,?,?,?,?)''',
                (plan_id, seq, 'checkpoint_close', at, _canon(detail), prev_hash,
                 _event_hash(prev_hash, seq, 'checkpoint_close', at, detail))).lastrowid
            for item in observed:
                current = db.execute('''SELECT status FROM repair_outcome
                    WHERE plan_id=? AND episode_id=?''', (plan_id, item['episode_id'])).fetchone()
                if current and current['status'] == '已修复':
                    continue  # containment work already done cannot be erased
                if item['sufficient']:
                    _set_outcome(db, plan_id, item['episode_id'], '上游修复免修',
                                 {'checkpoint_event': eid, 'points': item['evidence']})
                else:
                    _set_outcome(db, plan_id, item['episode_id'], '待维修',
                                 {'checkpoint_event': eid})
        return {'plan_id': plan_id, 'seq': seq, 'observed': observed, 'skipped': False}
    finally:
        db.close()


def _step_dict(r):
    return {'seq': r['seq'], 'kind': r['kind'], 'episode_id': r['episode_id'],
            'asset': r['asset'], 'crew': r['crew'], 'skill': r['skill'],
            'minutes': r['minutes'], 'spare': r['spare'], 'spare_count': r['spare_count'],
            'window_start': r['window_start'], 'window_end': r['window_end'],
            'observes': json.loads(r['observes']), 'after_seq': r['after_seq']}


def _replay(db, plan_id, payload, events):
    problems = []
    steps = {s['seq']: s for s in payload['steps']}
    order_of = {'step_start': 0, 'spare_checkout': 1, 'step_end': 2, 'field_result': 3,
                'checkpoint_close': 0}
    by_step = defaultdict(list)
    for e in events:
        step = steps.get(e['seq'])
        if not step:
            problems.append(f"unknown_step_event:{e['seq']}")
            continue
        by_step[e['seq']].append(e)
        if (step['kind'] == 'checkpoint') != (e['kind'] == 'checkpoint_close'):
            problems.append(f"bad_step_event:{e['seq']}")
    for seq, items in by_step.items():
        kinds = [e['kind'] for e in items]
        if kinds != sorted(kinds, key=lambda k: order_of[k]):
            problems.append(f'event_order:{seq}')
        step = steps[seq]
        for e in items:
            if e['kind'] == 'step_start' and dt(e['at']) != dt(step['window_start']):
                problems.append(f'window_mismatch:{seq}')
            if e['kind'] in ('step_end', 'field_result', 'checkpoint_close') and \
                    dt(e['at']) != dt(step['window_end']):
                problems.append(f'window_mismatch:{seq}')
    for e in events:
        if e['kind'] == 'checkpoint_close':
            up = steps[e['seq']]['after_seq']
            fr = [x for x in by_step.get(up, []) if x['kind'] == 'field_result']
            if not fr or fr[0]['id'] > e['id']:
                problems.append(f"upstream_not_repaired:{e['seq']}")
    busy = defaultdict(list)
    for s in payload['steps']:
        if s['kind'] == 'repair' and any(e['kind'] == 'step_start' for e in by_step.get(s['seq'], [])):
            busy[s['crew']].append((dt(s['window_start']), dt(s['window_end'])))
    crews = {c['id']: c for c in payload['crews']}
    for crew, spans in busy.items():
        spans.sort()
        for (_, e1), (s2, _) in zip(spans, spans[1:]):
            if s2 < e1:
                problems.append(f'crew_overlap:{crew}')
        for s, e in spans:
            if s < dt(crews[crew]['shift_start']) or e > dt(crews[crew]['shift_end']):
                problems.append(f'shift_violation:{crew}')
    checkouts = [e for e in events if e['kind'] == 'spare_checkout']
    ledger = [dict(r) for r in db.execute('SELECT * FROM spare_ledger WHERE plan_id=?', (plan_id,))]
    expected = sorted((e['id'], json.loads(e['detail'])['spare'],
                       -int(json.loads(e['detail'])['count'])) for e in checkouts)
    actual = sorted((l['event_id'], l['spare'], l['delta']) for l in ledger)
    if expected != actual:
        problems.append('spare_ledger_mismatch')
    totals = defaultdict(int)
    for e in checkouts:
        d = json.loads(e['detail'])
        totals[d['spare']] += int(d['count'])
    for spare, used in totals.items():
        if used > payload['spares'].get(spare, 0):
            problems.append(f'spare_overdrawn:{spare}')
    return problems


def _check_outcome(db, payload, ep, outcome, events):
    if not outcome:
        return False, None, None
    status = outcome['status']
    basis = json.loads(outcome['basis'])
    assets = {o['episode_id']: o['asset'] for o in payload['orders']}
    if status == '已修复':
        step = next((s for s in payload['steps']
                     if s['kind'] == 'repair' and s['episode_id'] == ep), None)
        if not step:
            return False, None, None
        step_events = [e for e in events if e['seq'] == step['seq']]
        kinds = {e['kind'] for e in step_events}
        need = {'step_start', 'step_end', 'field_result'}
        if step['spare_count']:
            need.add('spare_checkout')
        if not need <= kinds:
            return False, None, None
        fr = next(e for e in step_events if e['kind'] == 'field_result')
        if json.loads(fr['detail']).get('result') != '修复成功':
            return False, None, None
        expected = {'events': [e['id'] for e in step_events], 'result': '修复成功'}
        if basis != expected:
            return False, None, None
        return True, fr['at'], basis
    if status == '上游修复免修':
        ck = next((s for s in payload['steps']
                   if s['kind'] == 'checkpoint' and ep in s['observes']), None)
        if not ck:
            return False, None, None
        close = next((e for e in events
                      if e['seq'] == ck['seq'] and e['kind'] == 'checkpoint_close'), None)
        if not close:
            return False, None, None
        evidence = _recovery_evidence(db, assets[ep], ck['window_start'], ck['window_end'])
        if not evidence['sufficient']:
            return False, None, None
        expected = {'checkpoint_event': close['id'], 'points': evidence['points']}
        if basis != expected:
            return False, None, None
        repair_step = next((s for s in payload['steps']
                            if s['kind'] == 'repair' and s['episode_id'] == ep), None)
        if repair_step and any(e['seq'] == repair_step['seq'] for e in events):
            return False, None, None
        return True, close['at'], basis
    return False, None, None


def verify(path, plan_id):
    """Independently re-check the ledger and issue or revoke resumption certificates."""
    db = connect(path)
    try:
        row, payload = _load_plan(db, plan_id)
        problems = []
        run = db.execute('SELECT * FROM run WHERE id=?', (payload['run_id'],)).fetchone()
        if not run or run['status'] != '已闭合':
            problems.append('run_not_closed')
        elif run['version'] != payload['run_version']:
            problems.append('run_version_changed')
        if db.execute('SELECT 1 FROM run WHERE id>?', (payload['run_id'],)).fetchone():
            problems.append('stale_run')
        if _points_digest(db) != payload['points_digest']:
            problems.append('stale_evidence')
        roster = db.execute('SELECT payload FROM repair_roster WHERE id=?',
                            (payload['roster_id'],)).fetchone()
        if not roster or roster['payload'] != payload['roster_payload']:
            problems.append('roster_changed')
        links = [[r['source_episode'], r['target_episode'], r['lag_minutes']]
                 for r in db.execute('''SELECT * FROM propagation_link WHERE run_id=?
                     ORDER BY source_episode,target_episode''', (payload['run_id'],))]
        if links != payload['links']:
            problems.append('links_changed')
        steps = [_step_dict(r) for r in db.execute(
            'SELECT * FROM repair_step WHERE plan_id=? ORDER BY seq', (plan_id,))]
        if steps != payload['steps']:
            problems.append('steps_rewritten')
        events = [dict(r) for r in db.execute(
            'SELECT * FROM repair_event WHERE plan_id=? ORDER BY id', (plan_id,))]
        prev, intact = row['digest'], True
        for e in events:
            if e['prev_hash'] != prev or e['hash'] != _event_hash(
                    prev, e['seq'], e['kind'], e['at'], json.loads(e['detail'])):
                problems.append('ledger_tampered')
                intact = False
                break
            prev = e['hash']
        if db.execute('''SELECT 1 FROM issue WHERE code='repair_event_conflict'
            AND subject=?''', (plan_id,)).fetchone():
            problems.append('event_conflict')
        if intact:
            problems += _replay(db, plan_id, payload, events)
        certificates = []
        for order in payload['orders']:
            ep = order['episode_id']
            outcome = db.execute('''SELECT * FROM repair_outcome
                WHERE plan_id=? AND episode_id=?''', (plan_id, ep)).fetchone()
            ok, issued_at, basis = _check_outcome(db, payload, ep, outcome, events)
            if not ok:
                problems.append(f'outcome_unproven:{ep}')
            else:
                certificates.append({'episode_id': ep, 'asset': order['asset'],
                                     'status': '已核证复工', 'issued_at': issued_at,
                                     'basis': basis})
        if problems:
            with db:
                db.execute('DELETE FROM repair_certificate WHERE plan_id=?', (plan_id,))
                for order in payload['orders']:
                    db.execute("UPDATE work_order SET status='待核证' WHERE episode_id=?",
                               (order['episode_id'],))
            return {'plan_id': plan_id, 'status': '未通过', 'problems': problems,
                    'certificates': []}
        desired = {c['episode_id']: _sha(_canon(c)) for c in certificates}
        existing = {r['episode_id']: r['digest'] for r in db.execute(
            'SELECT episode_id,digest FROM repair_certificate WHERE plan_id=?', (plan_id,))}
        if existing and existing != desired:
            with db:
                db.execute('DELETE FROM repair_certificate WHERE plan_id=?', (plan_id,))
            return {'plan_id': plan_id, 'status': '未通过',
                    'problems': ['certificate_conflict'], 'certificates': []}
        with db:
            for c in certificates:
                db.execute('INSERT OR IGNORE INTO repair_certificate VALUES (?,?,?,?,?,?)',
                           (plan_id, c['episode_id'], c['status'], c['issued_at'],
                            _canon(c['basis']), desired[c['episode_id']]))
                db.execute("UPDATE work_order SET status='已复工' WHERE episode_id=?",
                           (c['episode_id'],))
        full_line = max((c['issued_at'] for c in certificates), key=dt)
        return {'plan_id': plan_id, 'status': '已核证复工', 'problems': [],
                'certificates': certificates, 'full_line_resume_at': full_line}
    finally:
        db.close()
