"""Decode or refute missing seq-block readings from sealed transmitter parity frames.

Parity frames live on the prime field GF(257): p0_sum=Σv_i, p1_weighted=Σi·v_i,
p2_check=Σi²·v_i with i the 1-based position inside the contract-declared block.
`single_parity` may only use p0_sum; `dual_parity` solves with p0_sum/p1_weighted
and then certifies the candidate against the independent p2_check frame. Nothing
learned here is written back to `point`: recovered readings exist only in the
recovery ledger, and every run is keyed by a version hash of its inputs so changed
inputs produce a new version instead of rewriting an earlier one.
"""
import hashlib
import json
from pathlib import Path

from .store import connect

PRIME = 257
PROTOCOL_VERSION = 'gf257-recovery-v1'
# Frames each protocol may solve with. p2_check is never a solving frame: under
# dual_parity it only certifies a candidate afterwards, and under single_parity
# it must not be consulted at all.
SOLVE_FRAMES = {'single_parity': ('p0_sum',), 'dual_parity': ('p0_sum', 'p1_weighted')}
CHECK_FRAMES = {'single_parity': (), 'dual_parity': ('p2_check',)}
WEIGHT = {'p0_sum': lambda i: 1, 'p1_weighted': lambda i: i, 'p2_check': lambda i: i * i}


def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'))


def _inv(a):
    """Legal modular inverse in GF(257) via Fermat's little theorem."""
    a %= PRIME
    if not a:
        raise ZeroDivisionError('singular_mod_inverse')
    return pow(a, PRIME - 2, PRIME)


def _rref(matrix, rhs):
    """Reduced row echelon form of [A|b] over GF(257); returns (rows, pivots)."""
    rows = [[x % PRIME for x in row] + [b % PRIME] for row, b in zip(matrix, rhs)]
    width = len(matrix[0]) if matrix else 0
    pivots = {}
    rank = 0
    for col in range(width):
        pivot = next((r for r in range(rank, len(rows)) if rows[r][col]), None)
        if pivot is None:
            continue
        rows[rank], rows[pivot] = rows[pivot], rows[rank]
        inv = _inv(rows[rank][col])
        rows[rank] = [(x * inv) % PRIME for x in rows[rank]]
        for r in range(len(rows)):
            if r != rank and rows[r][col]:
                factor = rows[r][col]
                rows[r] = [(a - factor * b) % PRIME for a, b in zip(rows[r], rows[rank])]
        pivots[col] = rank
        rank += 1
    return rows, pivots


def _solve(matrix, rhs, unknowns):
    """Solve A x = b over GF(257).

    Returns (consistent, rank, determined) where determined maps a column index
    to its value only when every solution agrees on that variable (its unit
    vector lies in the row space). Underdetermined variables are simply absent.
    """
    if not matrix:
        return True, 0, {}
    rows, pivots = _rref(matrix, rhs)
    consistent = all(any(row[:unknowns]) or not row[unknowns] for row in rows)
    free = [c for c in range(unknowns) if c not in pivots]
    determined = {}
    if consistent:
        for col, r in pivots.items():
            if all(rows[r][c] == 0 for c in free):
                determined[col] = rows[r][unknowns]
    return consistent, len(pivots), determined


def _verify_received(rows, frozen, value_max):
    """Check identity, hash, integer domain of received points against the seal."""
    by_seq = {r['seq']: r for r in rows}
    received = {}
    for r in rows:
        parsed = json.loads(r['raw']).get('value')
        recomputed = hashlib.sha256(r['raw'].encode()).hexdigest()
        if (recomputed != r['hash'] or isinstance(parsed, bool) or
                not isinstance(parsed, int) or parsed != r['value'] or
                not 0 <= parsed <= value_max):
            return None, {'reason': 'received_point_tampered', 'seq': r['seq'],
                          'stored_hash': r['hash'], 'recomputed_hash': recomputed}
        received[r['seq']] = parsed
    for seq, sealed_hash in sorted(frozen.items()):
        current = by_seq.get(seq)
        if current is None or current['hash'] != sealed_hash:
            return None, {'reason': 'received_point_tampered', 'seq': seq,
                          'sealed_hash': sealed_hash,
                          'current_hash': current['hash'] if current else None}
    return received, None


def recover(db_path, case_id, protocol):
    """Decode one sealed parity case under one authorized protocol.

    Returns the recovery_run id. Evidence tables (point/issue/episode/health)
    are only read, never modified; identical inputs return the existing run.
    """
    if protocol not in SOLVE_FRAMES:
        raise ValueError('unknown_protocol')
    db = connect(db_path)
    try:
        row = db.execute('SELECT payload FROM parity_case WHERE id=?', (case_id,)).fetchone()
        if not row:
            raise ValueError('unknown_parity_case')
        sealed = json.loads(row['payload'])
        doc = sealed['doc']
        if protocol not in doc.get('allowed_protocols', []):
            raise ValueError('protocol_not_allowed')
        value_max = doc.get('value_max', PRIME - 1)
        if isinstance(value_max, bool) or not isinstance(value_max, int) or not 0 <= value_max < PRIME:
            raise ValueError('bad_value_domain')
        frames = doc['parity']
        if not set(SOLVE_FRAMES[protocol]) | set(CHECK_FRAMES[protocol]) <= set(frames):
            raise ValueError('missing_parity_frame')
        start, end = int(doc['seq_start']), int(doc['seq_end'])
        device, sensor = doc['device'], doc['sensor']
        rows = [dict(r) for r in db.execute(
            'SELECT p.seq,p.value,p.raw,p.hash,s.hash AS source_hash FROM point p '
            'JOIN source s ON s.id=p.source_id '
            'WHERE p.device=? AND p.sensor=? AND p.seq BETWEEN ? AND ? ORDER BY p.seq',
            (device, sensor, start, end))]
        conflicts = sorted({int(r['detail']) for r in db.execute(
            "SELECT detail FROM issue WHERE code='identity_conflict' AND subject=?", (device,))
            if r['detail'].isdigit() and start <= int(r['detail']) <= end})
        version = hashlib.sha256(_canon({
            'case_id': case_id, 'sealed': sealed, 'protocol': protocol,
            'protocol_version': PROTOCOL_VERSION,
            'evidence': [[r['seq'], r['hash'], r['value']] for r in rows],
            'conflicts': conflicts}).encode()).hexdigest()
        old = db.execute('SELECT id FROM recovery_run WHERE case_id=? AND protocol=? AND version=?',
                         (case_id, protocol, version)).fetchone()
        if old:
            return old['id']

        by_seq = {r['seq']: r for r in rows}
        received, rejection = _verify_received(rows, {int(s): h for s, h in sealed['observed']},
                                               value_max)
        if rejection is None and conflicts:
            rejection = {'reason': 'identity_conflict', 'seq': conflicts[0],
                         'conflicts': conflicts}
        n = end - start + 1
        received_pos = {seq - start + 1: v for seq, v in (received or {}).items()}
        missing = [i for i in range(1, n + 1) if i not in received_pos]
        detail = {
            'protocol_version': PROTOCOL_VERSION, 'field_prime': PRIME,
            'sensor': sensor, 'device': device,
            'seq_start': start, 'seq_end': end, 'value_domain': [0, value_max],
            'fixture_digest': sealed['digest'],
            'frame_digest': hashlib.sha256(_canon({
                'field_prime': doc['field_prime'], 'seq_start': start, 'seq_end': end,
                'parity': frames, 'parity_source': doc.get('parity_source')}).encode()).hexdigest(),
            'parity_source': doc.get('parity_source'),
            'allowed_protocols': doc.get('allowed_protocols', []),
            'received': [{'seq': start + i - 1, 'position': i, 'value': received_pos[i],
                          'hash': by_seq[start + i - 1]['hash'],
                          'source_hash': by_seq[start + i - 1]['source_hash']}
                         for i in sorted(received_pos)],
            'missing': [{'seq': start + i - 1, 'position': i} for i in missing],
            'identity_conflicts': conflicts,
            'rejection': rejection,
            'equations': [], 'frame_checks': [],
            'rank': 0, 'unknowns': len(missing), 'consistent': None,
            'determined': {}, 'p2_check': None, 'rejected_candidates': None,
        }
        status = reason = undetermined_reason = None
        recovered = {}
        if rejection is not None:
            status, reason = '拒收', rejection['reason']
        elif not missing:
            for frame in SOLVE_FRAMES[protocol] + CHECK_FRAMES[protocol]:
                w = WEIGHT[frame]
                computed = sum(w(i) * received_pos[i] for i in received_pos) % PRIME
                detail['frame_checks'].append({'frame': frame, 'frame_value': frames[frame],
                                               'computed': computed,
                                               'ok': computed == frames[frame]})
            detail['consistent'] = all(c['ok'] for c in detail['frame_checks'])
            status, reason = ('已完整', 'no_gap') if detail['consistent'] else ('冲突', 'frame_inconsistent')
        else:
            for frame in SOLVE_FRAMES[protocol]:
                w = WEIGHT[frame]
                contribution = sum(w(i) * received_pos[i] for i in received_pos) % PRIME
                detail['equations'].append({
                    'frame': frame,
                    'coefficients': {start + i - 1: w(i) for i in missing},
                    'rhs': (frames[frame] - contribution) % PRIME,
                    'frame_value': frames[frame],
                    'received_contribution': contribution})
            matrix = [[eq['coefficients'][start + i - 1] for i in missing]
                      for eq in detail['equations']]
            consistent, rank, determined = _solve(matrix, [e['rhs'] for e in detail['equations']],
                                                  len(missing))
            detail['rank'] = rank
            detail['consistent'] = consistent
            detail['determined'] = {str(start + missing[c] - 1): v for c, v in determined.items()}
            if not consistent:
                status, reason = '冲突', 'frame_inconsistent'
                undetermined_reason = 'frame_inconsistent'
            elif len(determined) == len(missing):
                candidates = {missing[c]: v for c, v in determined.items()}
                if any(not 0 <= v <= value_max for v in candidates.values()):
                    status, reason = '冲突', 'candidate_out_of_domain'
                    undetermined_reason = 'candidate_out_of_domain'
                    detail['rejected_candidates'] = {str(start + i - 1): v for i, v in candidates.items()}
                else:
                    if protocol == 'dual_parity':
                        full = dict(received_pos)
                        full.update(candidates)
                        computed = sum(i * i * full[i] for i in range(1, n + 1)) % PRIME
                        detail['p2_check'] = {'frame': 'p2_check', 'frame_value': frames['p2_check'],
                                              'computed': computed, 'ok': computed == frames['p2_check']}
                        if not detail['p2_check']['ok']:
                            status, reason = '冲突', 'p2_check_failed'
                            undetermined_reason = 'p2_check_failed'
                            detail['rejected_candidates'] = {str(start + i - 1): v
                                                             for i, v in candidates.items()}
                    if status is None:
                        status, reason = '已恢复', 'unique_solution'
                        recovered = candidates
            else:
                status, reason = '未定', 'insufficient_rank'
                undetermined_reason = 'insufficient_rank'
                if protocol == 'single_parity':
                    recovered = {missing[c]: v for c, v in determined.items()
                                 if 0 <= v <= value_max}

        items = []
        if status != '拒收':
            for i in range(1, n + 1):
                seq = start + i - 1
                if i in received_pos:
                    items.append((seq, 'received', received_pos[i],
                                  {'point_hash': by_seq[seq]['hash'],
                                   'source_hash': by_seq[seq]['source_hash']}))
                elif i in recovered:
                    items.append((seq, 'recovered', recovered[i],
                                  {'equations': [e['frame'] for e in detail['equations']],
                                   'p2_check': detail['p2_check']['ok'] if detail['p2_check']
                                   else 'not_authorized'}))
                else:
                    items.append((seq, 'undetermined', None,
                                  {'reason': undetermined_reason,
                                   'constraints': detail['equations'],
                                   'value_domain': [0, value_max],
                                   'rejected_candidate': (detail['rejected_candidates'] or {}).get(str(seq))}))
        with db:
            rid = db.execute(
                'INSERT INTO recovery_run(case_id,protocol,version,status,reason,detail) '
                'VALUES (?,?,?,?,?,?)',
                (case_id, protocol, version, status, reason,
                 json.dumps(detail, ensure_ascii=False, sort_keys=True))).lastrowid
            for seq, state, value, item_detail in items:
                db.execute('INSERT INTO recovery_item(run_id,seq,state,value,detail) VALUES (?,?,?,?,?)',
                           (rid, seq, state, value,
                            json.dumps(item_detail, ensure_ascii=False, sort_keys=True)))
        return rid
    finally:
        db.close()


def summary(db_path, run_id):
    db = connect(db_path)
    try:
        run = db.execute('SELECT * FROM recovery_run WHERE id=?', (run_id,)).fetchone()
        if not run:
            raise ValueError('unknown_recovery_run')
        items = [dict(x) for x in db.execute(
            'SELECT seq,state,value FROM recovery_item WHERE run_id=? ORDER BY seq', (run_id,))]
        return {'run_id': run_id, 'case_id': run['case_id'], 'protocol': run['protocol'],
                'version': run['version'], 'status': run['status'], 'reason': run['reason'],
                'recovered': {x['seq']: x['value'] for x in items if x['state'] == 'recovered'},
                'undetermined': [x['seq'] for x in items if x['state'] == 'undetermined']}
    finally:
        db.close()


def recovery_report(db_path, run_id, out_path):
    """Render the frozen recovery/undetermined evidence of one run as JSON+Markdown."""
    db = connect(db_path)
    try:
        run = db.execute('SELECT * FROM recovery_run WHERE id=?', (run_id,)).fetchone()
        if not run:
            raise ValueError('unknown_recovery_run')
        detail = json.loads(run['detail'])
        items = [{'seq': x['seq'], 'state': x['state'], 'value': x['value'],
                  'detail': json.loads(x['detail']) if x['detail'] else None}
                 for x in db.execute(
                     'SELECT seq,state,value,detail FROM recovery_item WHERE run_id=? ORDER BY seq',
                     (run_id,))]
        obj = {'run': {'id': run['id'], 'case_id': run['case_id'], 'protocol': run['protocol'],
                       'version': run['version'], 'status': run['status'], 'reason': run['reason'],
                       'created': run['created']},
               'detail': detail, 'items': items}
        path = Path(out_path)
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
        lines = ['# 序号缺口恢复账报告', '',
                 f"运行 {run['id']}，案例 {run['case_id']}，协议 {run['protocol']}（{detail['protocol_version']}）",
                 f"状态：{run['status']}（{run['reason']}），版本 {run['version']}",
                 f"发送端帧摘要 {detail['frame_digest']}，帧来源 {detail['parity_source']}，固件摘要 {detail['fixture_digest']}",
                 f"秩 {detail['rank']}/未知数 {detail['unknowns']}，一致性 {detail['consistent']}", '']
        for item in items:
            if item['state'] == 'received':
                lines.append(f"- seq {item['seq']}：实收值 {item['value']}，点哈希 {item['detail']['point_hash']}")
            elif item['state'] == 'recovered':
                lines.append(f"- seq {item['seq']}：恢复值 {item['value']}"
                             f"（方程 {','.join(item['detail']['equations'])}，p2 校验 {item['detail']['p2_check']}）")
            else:
                lines.append(f"- seq {item['seq']}：未定（{item['detail']['reason']}）")
        path.with_suffix('.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        return path
    finally:
        db.close()
