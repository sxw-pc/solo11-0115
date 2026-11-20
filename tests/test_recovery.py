import json
from pathlib import Path

import pytest

from burstweave import recovery
from burstweave.engine import ingest, setup
from burstweave.parity import register
from burstweave.store import connect

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'
CONTRACT = json.loads((FIXTURES / 'parity-contract.json').read_text())


def sealed_db(tmp_path):
    """Fixture acceptance state: seq1=10, seq3=30 received; seq2/seq4 missing."""
    db = tmp_path / 'telemetry.db'
    setup(db, CONTRACT)
    assert ingest(db, FIXTURES / 'parity-points.jsonl') == 2
    register(db, 'block-17', FIXTURES / 'parity-case.json')
    return db


def point(seq, value, device='telemetry-A'):
    return {'sensor': 'meter-A', 'device': device, 'seq': seq,
            'at': f'2030-06-01T00:00:{(seq - 1) % 60:02d}Z', 'kind': 'heartbeat', 'value': value}


def build(tmp_path, doc, points, case_id='case-x'):
    db = tmp_path / f'{case_id}.db'
    setup(db, CONTRACT)
    src = tmp_path / f'{case_id}.jsonl'
    src.write_text('\n'.join(json.dumps(p) for p in points))
    ingest(db, src)
    fixture = tmp_path / f'{case_id}.json'
    base = {'sensor': 'meter-A', 'device': 'telemetry-A', 'field_prime': 257,
            'allowed_protocols': ['single_parity', 'dual_parity'],
            'parity_source': 'transmitter-frame-test'}
    base.update(doc)
    fixture.write_text(json.dumps(base))
    register(db, case_id, fixture)
    return db


def run_row(db, rid):
    with connect(db) as conn:
        return dict(conn.execute('SELECT * FROM recovery_run WHERE id=?', (rid,)).fetchone())


def detail(db, rid):
    return json.loads(run_row(db, rid)['detail'])


def items(db, rid):
    with connect(db) as conn:
        return {r['seq']: dict(r) for r in conn.execute(
            'SELECT * FROM recovery_item WHERE run_id=? ORDER BY seq', (rid,))}


def test_dual_parity_recovers_two_missing_points(tmp_path):
    db = sealed_db(tmp_path)
    rid = recovery.recover(db, 'block-17', 'dual_parity')
    run = run_row(db, rid)
    assert run['status'] == '已恢复' and run['reason'] == 'unique_solution'
    d = detail(db, rid)
    assert d['protocol_version'] == 'gf257-recovery-v1'
    assert (d['rank'], d['unknowns'], d['consistent']) == (2, 2, True)
    eqs = {e['frame']: e for e in d['equations']}
    assert eqs['p0_sum']['coefficients'] == {'2': 1, '4': 1}
    assert eqs['p0_sum']['rhs'] == 60            # x2 + x4 = 60 (mod 257)
    assert eqs['p1_weighted']['coefficients'] == {'2': 2, '4': 4}
    assert eqs['p1_weighted']['rhs'] == 200      # 2x2 + 4x4 = 200 (mod 257)
    assert d['p2_check'] == {'frame': 'p2_check', 'frame_value': 229, 'computed': 229, 'ok': True}
    got = items(db, rid)
    assert got[2]['state'] == 'recovered' and got[2]['value'] == 20
    assert got[4]['state'] == 'recovered' and got[4]['value'] == 40
    with connect(db) as conn:
        hashes = {r['seq']: r['hash'] for r in conn.execute('SELECT seq,hash FROM point')}
    assert json.loads(got[1]['detail'])['point_hash'] == hashes[1]
    assert json.loads(got[3]['detail'])['point_hash'] == hashes[3]
    assert recovery.recover(db, 'block-17', 'dual_parity') == rid
    with connect(db) as conn:
        assert conn.execute('SELECT COUNT(*) FROM recovery_run').fetchone()[0] == 1


def test_single_parity_leaves_both_missing_undetermined(tmp_path):
    db = sealed_db(tmp_path)
    rid = recovery.recover(db, 'block-17', 'single_parity')
    run = run_row(db, rid)
    assert run['status'] == '未定' and run['reason'] == 'insufficient_rank'
    d = detail(db, rid)
    # 单校验合同只授权 p0：方程列表不得出现 p1/p2，也不得有 p2 校验记录。
    assert [e['frame'] for e in d['equations']] == ['p0_sum']
    assert d['equations'][0]['rhs'] == 60
    assert d['p2_check'] is None
    assert (d['rank'], d['unknowns']) == (1, 2)
    got = items(db, rid)
    for seq in (2, 4):
        assert got[seq]['state'] == 'undetermined' and got[seq]['value'] is None
        constraint = json.loads(got[seq]['detail'])['constraints'][0]
        assert constraint['frame'] == 'p0_sum' and constraint['rhs'] == 60


def test_two_legal_protocols_yield_distinct_certified_products(tmp_path):
    db = sealed_db(tmp_path)
    single = recovery.recover(db, 'block-17', 'single_parity')
    dual = recovery.recover(db, 'block-17', 'dual_parity')
    assert single != dual
    s, d = run_row(db, single), run_row(db, dual)
    assert s['version'] != d['version']
    assert (s['status'], d['status']) == ('未定', '已恢复')
    with connect(db) as conn:
        assert conn.execute('SELECT COUNT(*) FROM recovery_run').fetchone()[0] == 2
        # 恢复值只入账，不回写 point。
        assert conn.execute('SELECT COUNT(*) FROM point').fetchone()[0] == 2


def test_complete_block_verifies_frames(tmp_path):
    doc = {'seq_start': 1, 'seq_end': 2,
           'parity': {'p0_sum': 30, 'p1_weighted': 50, 'p2_check': 90}}
    db = build(tmp_path, doc, [point(1, 10), point(2, 20)])
    rid = recovery.recover(db, 'case-x', 'dual_parity')
    run = run_row(db, rid)
    assert run['status'] == '已完整' and run['reason'] == 'no_gap'
    d = detail(db, rid)
    assert d['missing'] == []
    assert all(c['ok'] for c in d['frame_checks'])


def test_complete_block_detects_frame_conflict(tmp_path):
    doc = {'seq_start': 1, 'seq_end': 2,
           'parity': {'p0_sum': 30, 'p1_weighted': 50, 'p2_check': 91}}
    db = build(tmp_path, doc, [point(1, 10), point(2, 20)])
    rid = recovery.recover(db, 'case-x', 'dual_parity')
    run = run_row(db, rid)
    assert run['status'] == '冲突' and run['reason'] == 'frame_inconsistent'


def test_single_missing_point_recovered_under_both_protocols(tmp_path):
    doc = {'seq_start': 1, 'seq_end': 3,
           'parity': {'p0_sum': 60, 'p1_weighted': 140, 'p2_check': 103}}
    db = build(tmp_path, doc, [point(1, 10), point(3, 30)])
    single = recovery.recover(db, 'case-x', 'single_parity')
    assert run_row(db, single)['status'] == '已恢复'
    assert items(db, single)[2]['value'] == 20
    dual = recovery.recover(db, 'case-x', 'dual_parity')
    assert detail(db, dual)['p2_check']['ok'] is True
    assert items(db, dual)[2]['value'] == 20


def test_triple_missing_stays_undetermined(tmp_path):
    doc = {'seq_start': 1, 'seq_end': 5,
           'parity': {'p0_sum': 150, 'p1_weighted': 36, 'p2_check': 194}}
    db = build(tmp_path, doc, [point(1, 10), point(5, 50)])
    rid = recovery.recover(db, 'case-x', 'dual_parity')
    run = run_row(db, rid)
    assert run['status'] == '未定' and run['reason'] == 'insufficient_rank'
    d = detail(db, rid)
    assert (d['rank'], d['unknowns']) == (2, 3)
    got = items(db, rid)
    for seq in (2, 3, 4):
        assert got[seq]['state'] == 'undetermined' and got[seq]['value'] is None
    single = recovery.recover(db, 'case-x', 'single_parity')
    assert detail(db, single)['rank'] == 1
    assert run_row(db, single)['status'] == '未定'


def test_tampered_p1_frame_is_inconsistent(tmp_path):
    doc = {'seq_start': 1, 'seq_end': 3,
           'parity': {'p0_sum': 60, 'p1_weighted': 141, 'p2_check': 103}}
    db = build(tmp_path, doc, [point(1, 10), point(3, 30)])
    rid = recovery.recover(db, 'case-x', 'dual_parity')
    run = run_row(db, rid)
    assert run['status'] == '冲突' and run['reason'] == 'frame_inconsistent'
    assert detail(db, rid)['consistent'] is False
    assert items(db, rid)[2]['state'] == 'undetermined'


def test_tampered_p0_frame_fails_p2_certification(tmp_path):
    # p0 被改一格：2x2 系统仍可解，但独立 p2 校验必须拦下认证。
    doc = {'seq_start': 1, 'seq_end': 4,
           'parity': {'p0_sum': 101, 'p1_weighted': 43, 'p2_check': 229}}
    db = build(tmp_path, doc, [point(1, 10), point(3, 30)])
    rid = recovery.recover(db, 'case-x', 'dual_parity')
    run = run_row(db, rid)
    assert run['status'] == '冲突' and run['reason'] == 'p2_check_failed'
    d = detail(db, rid)
    assert d['p2_check']['ok'] is False
    assert d['rejected_candidates'] == {'2': 22, '4': 39}
    got = items(db, rid)
    assert got[2]['state'] == 'undetermined' and got[2]['value'] is None
    assert json.loads(got[2]['detail'])['rejected_candidate'] == 22


def test_tampered_received_value_is_rejected_and_old_version_kept(tmp_path):
    db = sealed_db(tmp_path)
    before = recovery.recover(db, 'block-17', 'dual_parity')
    with connect(db) as conn:
        conn.execute('UPDATE point SET value=11 WHERE device=? AND seq=?', ('telemetry-A', 1))
    after = recovery.recover(db, 'block-17', 'dual_parity')
    assert after != before
    run = run_row(db, after)
    assert run['status'] == '拒收' and run['reason'] == 'received_point_tampered'
    assert detail(db, after)['rejection']['seq'] == 1
    old = run_row(db, before)
    assert old['status'] == '已恢复' and old['version'] != run['version']


def test_rewritten_raw_line_is_rejected(tmp_path):
    db = sealed_db(tmp_path)
    with connect(db) as conn:
        conn.execute("UPDATE point SET raw=raw||' ' WHERE seq=1")
    rid = recovery.recover(db, 'block-17', 'dual_parity')
    assert run_row(db, rid)['reason'] == 'received_point_tampered'


def test_identity_conflict_blocks_certification(tmp_path):
    db = sealed_db(tmp_path)
    clash = tmp_path / 'clash.jsonl'
    clash.write_text(json.dumps(point(1, 99)))  # 同 (device,seq) 异文
    assert ingest(db, clash) == 0
    with connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM issue WHERE code='identity_conflict'").fetchone()[0] == 1
    rid = recovery.recover(db, 'block-17', 'dual_parity')
    run = run_row(db, rid)
    assert run['status'] == '拒收' and run['reason'] == 'identity_conflict'
    assert detail(db, rid)['identity_conflicts'] == [1]


def test_candidate_outside_sensor_domain_not_certified(tmp_path):
    doc = {'seq_start': 1, 'seq_end': 2, 'value_max': 200,
           'parity': {'p0_sum': (10 + 250) % 257, 'p1_weighted': (10 + 2 * 250) % 257,
                      'p2_check': (10 + 4 * 250) % 257}}
    db = build(tmp_path, doc, [point(1, 10)])
    rid = recovery.recover(db, 'case-x', 'single_parity')
    run = run_row(db, rid)
    assert run['status'] == '冲突' and run['reason'] == 'candidate_out_of_domain'
    assert detail(db, rid)['rejected_candidates'] == {'2': 250}
    assert items(db, rid)[2]['value'] is None


def test_boundary_positions_use_block_relative_weights(tmp_path):
    # 块 seq3..5：缺首位置(seq3,i=1)与末位置(seq5,i=5→i=3)，权重必须按块内位置。
    doc = {'seq_start': 3, 'seq_end': 5,
           'parity': {'p0_sum': 50, 'p1_weighted': 200, 'p2_check': 129}}
    db = build(tmp_path, doc, [point(4, 7)])
    rid = recovery.recover(db, 'case-x', 'dual_parity')
    assert run_row(db, rid)['status'] == '已恢复'
    got = items(db, rid)
    assert got[3]['value'] == 100 and got[5]['value'] == 200
    d = detail(db, rid)
    assert d['equations'][1]['frame'] == 'p1_weighted'
    assert d['equations'][1]['coefficients'] == {'3': 1, '5': 3}
    assert d['missing'] == [{'seq': 3, 'position': 1}, {'seq': 5, 'position': 3}]


def test_singleton_block_boundary(tmp_path):
    doc = {'seq_start': 7, 'seq_end': 7,
           'parity': {'p0_sum': 42, 'p1_weighted': 42, 'p2_check': 42}}
    db = build(tmp_path, doc, [point(7, 42)])
    rid = recovery.recover(db, 'case-x', 'dual_parity')
    assert run_row(db, rid)['status'] == '已完整'


def test_protocol_must_be_known_and_allowed(tmp_path):
    db = sealed_db(tmp_path)
    with pytest.raises(ValueError, match='unknown_protocol'):
        recovery.recover(db, 'block-17', 'triple_parity')
    with pytest.raises(ValueError, match='unknown_parity_case'):
        recovery.recover(db, 'nope', 'dual_parity')
    doc = {'seq_start': 1, 'seq_end': 2, 'allowed_protocols': ['single_parity'],
           'parity': {'p0_sum': 30, 'p1_weighted': 50, 'p2_check': 90}}
    db2 = build(tmp_path, doc, [point(1, 10), point(2, 20)], case_id='single-only')
    with pytest.raises(ValueError, match='protocol_not_allowed'):
        recovery.recover(db2, 'single-only', 'dual_parity')


def test_late_point_creates_new_version_not_rewrite(tmp_path):
    db = sealed_db(tmp_path)
    first = recovery.recover(db, 'block-17', 'dual_parity')
    late = tmp_path / 'late.jsonl'
    late.write_text(json.dumps(point(2, 20)))
    assert ingest(db, late) == 1
    second = recovery.recover(db, 'block-17', 'dual_parity')
    assert second != first
    old, new = run_row(db, first), run_row(db, second)
    assert old['status'] == '已恢复' and old['version'] != new['version']
    got = items(db, second)
    assert got[2]['state'] == 'received' and got[2]['value'] == 20
    assert got[4]['state'] == 'recovered' and got[4]['value'] == 40
    with connect(db) as conn:
        assert conn.execute('SELECT COUNT(*) FROM recovery_run').fetchone()[0] == 2


def test_changed_parity_frame_needs_new_case_identity(tmp_path):
    db = sealed_db(tmp_path)
    doc = json.loads((FIXTURES / 'parity-case.json').read_text())
    doc['parity']['p0_sum'] = 101
    fixture = tmp_path / 'changed.json'
    fixture.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match='parity_case_conflict'):
        register(db, 'block-17', fixture)
    register(db, 'block-17b', fixture)
    rid = recovery.recover(db, 'block-17b', 'dual_parity')
    assert run_row(db, rid)['reason'] == 'p2_check_failed'


def test_recovery_report_cites_provenance(tmp_path):
    db = sealed_db(tmp_path)
    dual = recovery.recover(db, 'block-17', 'dual_parity')
    out = recovery.recovery_report(db, dual, tmp_path / 'recovery.json')
    data = json.loads(out.read_text(encoding='utf-8'))
    assert data['run']['status'] == '已恢复'
    d = data['detail']
    assert d['protocol_version'] == 'gf257-recovery-v1'
    assert d['frame_digest'] and d['fixture_digest'] and d['parity_source']
    assert {e['frame'] for e in d['equations']} == {'p0_sum', 'p1_weighted'}
    assert d['p2_check']['ok'] is True
    with connect(db) as conn:
        hashes = {r['seq']: r['hash'] for r in conn.execute('SELECT seq,hash FROM point')}
    received = {x['seq']: x for x in data['items'] if x['state'] == 'received'}
    assert received[1]['detail']['point_hash'] == hashes[1]
    assert received[3]['detail']['point_hash'] == hashes[3]
    recovered = {x['seq']: x['value'] for x in data['items'] if x['state'] == 'recovered'}
    assert recovered == {2: 20, 4: 40}
    md = out.with_suffix('.md').read_text(encoding='utf-8')
    assert '已恢复' in md and 'block-17' in md
    single = recovery.recover(db, 'block-17', 'single_parity')
    out2 = recovery.recovery_report(db, single, tmp_path / 'undetermined.json')
    data2 = json.loads(out2.read_text(encoding='utf-8'))
    assert data2['run']['status'] == '未定'
    assert data2['detail']['equations'][0]['rhs'] == 60
    assert {x['seq'] for x in data2['items'] if x['state'] == 'undetermined'} == {2, 4}
    with pytest.raises(ValueError, match='unknown_recovery_run'):
        recovery.recovery_report(db, 9999, tmp_path / 'x.json')


def test_recovery_never_rewrites_original_evidence(tmp_path):
    db = sealed_db(tmp_path)
    tables = ('point', 'issue', 'episode', 'health', 'run', 'source', 'parity_case')
    with connect(db) as conn:
        counts_before = {t: conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in tables}
        points_before = [tuple(r) for r in conn.execute('SELECT seq,hash,value FROM point ORDER BY seq')]
    recovery.recover(db, 'block-17', 'dual_parity')
    recovery.recover(db, 'block-17', 'single_parity')
    with connect(db) as conn:
        counts_after = {t: conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in tables}
        points_after = [tuple(r) for r in conn.execute('SELECT seq,hash,value FROM point ORDER BY seq')]
    assert counts_before == counts_after
    assert points_before == points_after
