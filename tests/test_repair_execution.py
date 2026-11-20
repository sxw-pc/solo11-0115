import json
from pathlib import Path

import pytest

from burstweave import engine, operations, repair, topology
from burstweave.store import connect

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'


def scenario(base, recovery='recovery-a1.jsonl', roster_text=None):
    """Closed run with credible A0/A1 orders and a proven A0->A1 link."""
    base.mkdir(parents=True, exist_ok=True)
    db = base / 'ledger.db'
    engine.setup(db, {'assets': ['A0', 'A1'],
                      'sensors': [{'id': 'S0', 'asset': 'A0', 'role': 'heat'},
                                  {'id': 'S1', 'asset': 'A1', 'role': 'heat'}],
                      'health': [{'sensor': s, 'start': '2026-01-01T00:00:00Z',
                                  'end': '2026-01-01T06:00:00Z', 'status': 'active'}
                                 for s in ('S0', 'S1')]})
    rows = [
        {'sensor': 'S0', 'device': 'D0', 'seq': 1, 'kind': 'anchor',
         'at': '2026-01-01T00:00:00Z', 'offset_seconds': 0},
        {'sensor': 'S0', 'device': 'D0', 'seq': 2, 'kind': 'heartbeat',
         'at': '2026-01-01T00:00:00Z'},
        {'sensor': 'S0', 'device': 'D0', 'seq': 3, 'kind': 'anomaly',
         'at': '2026-01-01T00:05:00Z', 'value': 7},
        {'sensor': 'S0', 'device': 'D0', 'seq': 4, 'kind': 'anomaly',
         'at': '2026-01-01T00:10:00Z', 'value': 8},
        {'sensor': 'S1', 'device': 'D1', 'seq': 1, 'kind': 'anchor',
         'at': '2026-01-01T00:00:00Z', 'offset_seconds': 0},
        {'sensor': 'S1', 'device': 'D1', 'seq': 2, 'kind': 'heartbeat',
         'at': '2026-01-01T00:00:00Z'},
        {'sensor': 'S1', 'device': 'D1', 'seq': 3, 'kind': 'anomaly',
         'at': '2026-01-01T00:15:00Z', 'value': 9},
    ]
    source = base / 'fault.jsonl'
    source.write_text('\n'.join(json.dumps(r) for r in rows))
    engine.ingest(db, source)
    rid = engine.reconcile(db)
    topology.edge(db, 'A0', 'A1', 0, 10)
    orders = operations.triage(db, rid)
    assert {o['status'] for o in orders} == {'待派工'}
    assert {o['reason'] for o in orders} == {'topology_spread'}
    roster = FIXTURES / 'repair-roster.json'
    if roster_text is not None:
        roster = base / 'roster.json'
        roster.write_text(roster_text)
    repair.register(db, 'roster-A', rid, roster)
    if recovery:
        engine.ingest(db, FIXTURES / recovery)
    return db, rid


def episodes(db):
    with connect(db) as con:
        return {r['asset']: r['id'] for r in con.execute('SELECT id,asset FROM episode')}


def outcomes(db, plan_id):
    with connect(db) as con:
        return {r['episode_id']: r['status'] for r in con.execute(
            'SELECT episode_id,status FROM repair_outcome WHERE plan_id=?', (plan_id,))}


def certificates(db, plan_id):
    with connect(db) as con:
        return {r['asset']: r['issued_at'] for r in con.execute('''
            SELECT e.asset, c.issued_at FROM repair_certificate c
            JOIN episode e ON e.id=c.episode_id WHERE c.plan_id=?''', (plan_id,))}


def order_status(db):
    with connect(db) as con:
        return {r['asset']: r['status'] for r in con.execute('''
            SELECT e.asset, w.status FROM work_order w
            JOIN episode e ON e.id=w.episode_id''')}


def test_root_first_exempts_downstream_after_verified_recovery(tmp_path):
    db, rid = scenario(tmp_path / 'root')
    ep = episodes(db)
    planned = repair.plan(db, 'P-root', 'roster-A', rid, 'root_first')
    assert repair.plan(db, 'P-root', 'roster-A', rid, 'root_first') == planned
    steps = planned['steps']
    assert [(s['seq'], s['kind'], s['asset']) for s in steps] == [
        (1, 'repair', 'A0'), (2, 'checkpoint', 'A0'), (3, 'repair', 'A1')]
    assert steps[0]['window_start'] == '2026-01-01T02:00:00+00:00'
    assert steps[0]['window_end'] == '2026-01-01T02:30:00+00:00'
    assert (steps[1]['window_start'], steps[1]['window_end']) == (
        '2026-01-01T02:30:00+00:00', '2026-01-01T02:35:00+00:00')
    assert steps[1]['observes'] == [ep['A1']]
    assert steps[2]['window_start'] == '2026-01-01T02:35:00+00:00'
    assert order_status(db) == {'A0': '已派工', 'A1': '已派工'}

    result = repair.apply(db, 'P-root', FIXTURES / 'repair-field-root-first.jsonl')
    assert result == {'plan_id': 'P-root', 'applied': 4, 'skipped': 0}
    assert order_status(db)['A0'] == '维修中'
    closed = repair.checkpoint(db, 'P-root', 2)
    assert closed['observed'][0]['sufficient'] is True
    assert len(closed['observed'][0]['evidence']) == 2
    assert repair.checkpoint(db, 'P-root', 2)['skipped'] is True
    assert outcomes(db, 'P-root') == {ep['A0']: '已修复', ep['A1']: '上游修复免修'}

    verified = repair.verify(db, 'P-root')
    assert verified['status'] == '已核证复工'
    assert verified['full_line_resume_at'] == '2026-01-01T02:35:00Z'
    assert certificates(db, 'P-root') == {
        'A0': '2026-01-01T02:30:00Z', 'A1': '2026-01-01T02:35:00Z'}
    assert order_status(db) == {'A0': '已复工', 'A1': '已复工'}
    assert repair.verify(db, 'P-root') == verified
    with connect(db) as con:
        # A1 local step stayed untouched; the only fuse went to A0.
        assert con.execute("SELECT count(*) FROM repair_event WHERE plan_id='P-root' AND seq=3").fetchone()[0] == 0
        assert con.execute("SELECT SUM(delta) FROM spare_ledger WHERE plan_id='P-root'").fetchone()[0] == -1
        # Diagnostic, execution, checkpoint and certificate times stay distinct.
        assert con.execute("SELECT start FROM episode WHERE asset='A0'").fetchone()[0] == '2026-01-01T00:05:00+00:00'
        kinds = {r['kind'] for r in con.execute("SELECT kind FROM repair_event WHERE plan_id='P-root'")}
        assert kinds == {'step_start', 'spare_checkout', 'step_end', 'field_result', 'checkpoint_close'}


def test_branch_containment_first_keeps_downstream_effort(tmp_path):
    db, rid = scenario(tmp_path / 'branch', recovery='recovery-a1-branch.jsonl')
    ep = episodes(db)
    planned = repair.plan(db, 'P-branch', 'roster-A', rid, 'branch_containment_first')
    steps = planned['steps']
    assert [(s['seq'], s['kind'], s['asset']) for s in steps] == [
        (1, 'repair', 'A1'), (2, 'repair', 'A0'), (3, 'checkpoint', 'A0')]
    assert (steps[0]['window_start'], steps[0]['window_end']) == (
        '2026-01-01T02:00:00+00:00', '2026-01-01T02:10:00+00:00')
    assert (steps[1]['window_start'], steps[1]['window_end']) == (
        '2026-01-01T02:10:00+00:00', '2026-01-01T02:40:00+00:00')

    result = repair.apply(db, 'P-branch', FIXTURES / 'repair-field-branch-first.jsonl')
    assert result == {'plan_id': 'P-branch', 'applied': 7, 'skipped': 0}
    closed = repair.checkpoint(db, 'P-branch', 3)
    # A1 is observed recovering with upstream, but its local repair stands.
    assert closed['observed'][0]['sufficient'] is True
    assert outcomes(db, 'P-branch') == {ep['A0']: '已修复', ep['A1']: '已修复'}

    verified = repair.verify(db, 'P-branch')
    assert verified['status'] == '已核证复工'
    assert verified['full_line_resume_at'] == '2026-01-01T02:40:00Z'
    assert certificates(db, 'P-branch') == {
        'A0': '2026-01-01T02:40:00Z', 'A1': '2026-01-01T02:10:00Z'}
    with connect(db) as con:
        assert con.execute("SELECT count(*) FROM repair_event WHERE plan_id='P-branch' AND seq=1").fetchone()[0] == 3
        assert con.execute("SELECT SUM(delta) FROM spare_ledger WHERE plan_id='P-branch'").fetchone()[0] == -1


def test_root_first_without_recovery_still_repairs_downstream(tmp_path):
    db, rid = scenario(tmp_path / 'dry', recovery=None)
    ep = episodes(db)
    repair.plan(db, 'P-root', 'roster-A', rid, 'root_first')
    repair.apply(db, 'P-root', FIXTURES / 'repair-field-root-first.jsonl')
    closed = repair.checkpoint(db, 'P-root', 2)
    assert closed['observed'][0]['sufficient'] is False
    assert outcomes(db, 'P-root')[ep['A1']] == '待维修'
    # Not exempted: the frozen local procedure still has to run.
    repair.apply(db, 'P-root', FIXTURES / 'repair-field-a1-local.jsonl')
    verified = repair.verify(db, 'P-root')
    assert verified['status'] == '已核证复工'
    assert verified['full_line_resume_at'] == '2026-01-01T02:45:00Z'
    assert certificates(db, 'P-root') == {
        'A0': '2026-01-01T02:30:00Z', 'A1': '2026-01-01T02:45:00Z'}


def test_root_first_without_recovery_and_repair_is_not_certified(tmp_path):
    db, rid = scenario(tmp_path / 'open', recovery=None)
    ep = episodes(db)
    repair.plan(db, 'P-root', 'roster-A', rid, 'root_first')
    repair.apply(db, 'P-root', FIXTURES / 'repair-field-root-first.jsonl')
    repair.checkpoint(db, 'P-root', 2)
    verified = repair.verify(db, 'P-root')
    assert verified['status'] == '未通过'
    assert f"outcome_unproven:{ep['A1']}" in verified['problems']
    assert certificates(db, 'P-root') == {}
    assert order_status(db)['A1'] == '待核证'


def test_late_heartbeat_is_not_recovery(tmp_path):
    db, rid = scenario(tmp_path / 'late', recovery='recovery-a1-late.jsonl')
    ep = episodes(db)
    repair.plan(db, 'P-root', 'roster-A', rid, 'root_first')
    repair.apply(db, 'P-root', FIXTURES / 'repair-field-root-first.jsonl')
    closed = repair.checkpoint(db, 'P-root', 2)
    # Recover inside the window plus a heartbeat one minute late is not proof.
    assert closed['observed'][0]['sufficient'] is False
    assert outcomes(db, 'P-root')[ep['A1']] == '待维修'
    repair.apply(db, 'P-root', FIXTURES / 'repair-field-a1-local.jsonl')
    assert repair.verify(db, 'P-root')['status'] == '已核证复工'


def test_missing_heartbeat_is_not_recovery(tmp_path):
    db, rid = scenario(tmp_path / 'nohb', recovery='recovery-a1-no-heartbeat.jsonl')
    ep = episodes(db)
    repair.plan(db, 'P-root', 'roster-A', rid, 'root_first')
    repair.apply(db, 'P-root', FIXTURES / 'repair-field-root-first.jsonl')
    assert repair.checkpoint(db, 'P-root', 2)['observed'][0]['sufficient'] is False
    assert outcomes(db, 'P-root')[ep['A1']] == '待维修'
    assert repair.verify(db, 'P-root')['status'] == '未通过'


def test_spare_stock_conflict_blocks_plan(tmp_path):
    roster = json.loads((FIXTURES / 'repair-roster.json').read_text())
    roster['procedures'][1]['spare_count'] = 1
    db, rid = scenario(tmp_path / 'spare', roster_text=json.dumps(roster))
    with pytest.raises(ValueError, match='spare_stock_insufficient'):
        repair.plan(db, 'P-spare', 'roster-A', rid, 'root_first')


def test_shift_and_skill_conflicts_block_plan(tmp_path):
    roster = json.loads((FIXTURES / 'repair-roster.json').read_text())
    roster['crews'][0]['shift_end'] = '2026-01-01T02:30:00Z'
    db, rid = scenario(tmp_path / 'shift', roster_text=json.dumps(roster))
    with pytest.raises(ValueError, match='shift_infeasible'):
        repair.plan(db, 'P-shift', 'roster-A', rid, 'root_first')
    roster = json.loads((FIXTURES / 'repair-roster.json').read_text())
    roster['crews'][0]['skills'] = ['mechanical']
    db, rid = scenario(tmp_path / 'skill', roster_text=json.dumps(roster))
    with pytest.raises(ValueError, match='skill_unavailable'):
        repair.plan(db, 'P-skill', 'roster-A', rid, 'root_first')


def test_interrupted_apply_resumes_without_double_checkout(tmp_path):
    db, rid = scenario(tmp_path / 'retry')
    repair.plan(db, 'P-root', 'roster-A', rid, 'root_first')
    lines = (FIXTURES / 'repair-field-root-first.jsonl').read_text().splitlines()
    partial = tmp_path / 'partial.jsonl'
    partial.write_text('\n'.join(lines[:2]))
    assert repair.apply(db, 'P-root', partial)['applied'] == 2
    # A half-done step is not a repair: verification refuses.
    assert repair.verify(db, 'P-root')['status'] == '未通过'
    # Retry after the interruption replays the confirmed prefix.
    result = repair.apply(db, 'P-root', FIXTURES / 'repair-field-root-first.jsonl')
    assert result == {'plan_id': 'P-root', 'applied': 2, 'skipped': 2}
    with connect(db) as con:
        assert con.execute("SELECT count(*) FROM spare_ledger WHERE plan_id='P-root'").fetchone()[0] == 1
        assert con.execute("SELECT SUM(delta) FROM spare_ledger WHERE plan_id='P-root'").fetchone()[0] == -1
        assert con.execute("SELECT count(*) FROM repair_event WHERE plan_id='P-root'").fetchone()[0] == 4
    repair.checkpoint(db, 'P-root', 2)
    assert repair.verify(db, 'P-root')['status'] == '已核证复工'


def test_failed_event_keeps_confirmed_prefix_for_resume(tmp_path):
    db, rid = scenario(tmp_path / 'fail')
    repair.plan(db, 'P-root', 'roster-A', rid, 'root_first')
    lines = (FIXTURES / 'repair-field-root-first.jsonl').read_text().splitlines()
    lines[2] = json.dumps({'seq': 1, 'kind': 'step_end', 'at': '2026-01-01T02:31:00Z'})
    broken = tmp_path / 'broken.jsonl'
    broken.write_text('\n'.join(lines))
    with pytest.raises(ValueError, match='window_mismatch'):
        repair.apply(db, 'P-root', broken)
    with connect(db) as con:
        assert con.execute("SELECT count(*) FROM repair_event WHERE plan_id='P-root'").fetchone()[0] == 2
    result = repair.apply(db, 'P-root', FIXTURES / 'repair-field-root-first.jsonl')
    assert result == {'plan_id': 'P-root', 'applied': 2, 'skipped': 2}


def test_conflicting_field_event_revokes_certificate(tmp_path):
    db, rid = scenario(tmp_path / 'conflict')
    repair.plan(db, 'P-root', 'roster-A', rid, 'root_first')
    repair.apply(db, 'P-root', FIXTURES / 'repair-field-root-first.jsonl')
    repair.checkpoint(db, 'P-root', 2)
    assert repair.verify(db, 'P-root')['status'] == '已核证复工'
    divergent = tmp_path / 'divergent.jsonl'
    divergent.write_text(json.dumps({'seq': 1, 'kind': 'step_end', 'at': '2026-01-01T02:29:00Z'}))
    with pytest.raises(ValueError, match='repair_event_conflict'):
        repair.apply(db, 'P-root', divergent)
    verified = repair.verify(db, 'P-root')
    assert verified['status'] == '未通过'
    assert 'event_conflict' in verified['problems']
    assert certificates(db, 'P-root') == {}
    assert order_status(db)['A0'] == '待核证'


def test_rewritten_ledger_or_steps_revoke_certificate(tmp_path):
    for label, statement in (
            ('event', "UPDATE repair_event SET at='2026-01-01T02:29:00Z' WHERE kind='step_end'"),
            ('step', "UPDATE repair_step SET minutes=31 WHERE seq=1")):
        db, rid = scenario(tmp_path / label)
        repair.plan(db, 'P-root', 'roster-A', rid, 'root_first')
        repair.apply(db, 'P-root', FIXTURES / 'repair-field-root-first.jsonl')
        repair.checkpoint(db, 'P-root', 2)
        assert repair.verify(db, 'P-root')['status'] == '已核证复工'
        with connect(db) as con:
            con.execute(statement)
        verified = repair.verify(db, 'P-root')
        assert verified['status'] == '未通过'
        assert certificates(db, 'P-root') == {}
        if label == 'event':
            assert 'ledger_tampered' in verified['problems']
        else:
            assert 'steps_rewritten' in verified['problems']


def test_stale_run_invalidates_plan_and_certificates(tmp_path):
    db, rid = scenario(tmp_path / 'stale')
    repair.plan(db, 'P-root', 'roster-A', rid, 'root_first')
    repair.apply(db, 'P-root', FIXTURES / 'repair-field-root-first.jsonl')
    repair.checkpoint(db, 'P-root', 2)
    assert repair.verify(db, 'P-root')['status'] == '已核证复工'
    late = tmp_path / 'late-point.jsonl'
    late.write_text(json.dumps({'sensor': 'S0', 'device': 'D0', 'seq': 5,
                                'kind': 'heartbeat', 'at': '2026-01-01T03:00:00Z'}))
    engine.ingest(db, late)
    assert engine.reconcile(db) != rid
    verified = repair.verify(db, 'P-root')
    assert verified['status'] == '未通过'
    assert 'stale_run' in verified['problems']
    assert certificates(db, 'P-root') == {}
    with pytest.raises(ValueError, match='stale_run'):
        repair.apply(db, 'P-root', FIXTURES / 'repair-field-root-first.jsonl')
    with pytest.raises(ValueError, match='stale_run'):
        repair.checkpoint(db, 'P-root', 2)


def test_plan_requires_evidence_strategy_and_fresh_ids(tmp_path):
    db, rid = scenario(tmp_path / 'guard')
    with pytest.raises(ValueError, match='unknown_strategy'):
        repair.plan(db, 'P-x', 'roster-A', rid, 'topology_says_so')
    repair.plan(db, 'P-root', 'roster-A', rid, 'root_first')
    with pytest.raises(ValueError, match='repair_plan_conflict'):
        repair.plan(db, 'P-root', 'roster-A', rid, 'branch_containment_first')
    with pytest.raises(ValueError, match='unknown_repair_plan'):
        repair.verify(db, 'P-missing')
    with pytest.raises(ValueError, match='upstream_not_repaired'):
        repair.checkpoint(db, 'P-root', 2)
    db2 = tmp_path / 'empty' / 'ledger.db'
    engine.setup(db2, {'assets': ['A0'], 'sensors': [{'id': 'S0', 'asset': 'A0', 'role': 'heat'}]})
    with connect(db2) as con:
        con.execute("INSERT INTO run(id,version,status) VALUES (1,'v1','已闭合')")
    with pytest.raises(ValueError, match='unknown_repair_roster'):
        repair.plan(db2, 'P-none', 'roster-A', 1, 'root_first')
    db3, rid3 = scenario(tmp_path / 'held', recovery=None)
    with connect(db3) as con:
        con.execute("UPDATE work_order SET status='暂缓'")
    with pytest.raises(ValueError, match='no_eligible_orders'):
        repair.plan(db3, 'P-held', 'roster-A', rid3, 'root_first')
    with connect(db3) as con:
        con.execute("UPDATE run SET status='待核' WHERE id=?", (rid3,))
    with pytest.raises(ValueError, match='incident_run_not_closed'):
        repair.plan(db3, 'P-open', 'roster-A', rid3, 'root_first')


def test_exempted_step_cannot_be_executed_and_checkpoint_not_manual(tmp_path):
    db, rid = scenario(tmp_path / 'exempt')
    repair.plan(db, 'P-root', 'roster-A', rid, 'root_first')
    manual = tmp_path / 'manual.jsonl'
    manual.write_text(json.dumps({'seq': 2, 'kind': 'step_start', 'at': '2026-01-01T02:30:00Z', 'crew': 'T1'}))
    with pytest.raises(ValueError, match='checkpoint_not_manual'):
        repair.apply(db, 'P-root', manual)
    repair.apply(db, 'P-root', FIXTURES / 'repair-field-root-first.jsonl')
    repair.checkpoint(db, 'P-root', 2)
    assert outcomes(db, 'P-root')[episodes(db)['A1']] == '上游修复免修'
    with pytest.raises(ValueError, match='step_exempted'):
        repair.apply(db, 'P-root', FIXTURES / 'repair-field-a1-local.jsonl')


def test_triage_regression_after_repair_flow(tmp_path):
    db, rid = scenario(tmp_path / 'triage')
    first = operations.triage(db, rid)
    assert operations.triage(db, rid) == first
    assert {o['status'] for o in first} == {'待派工'}
    repair.plan(db, 'P-root', 'roster-A', rid, 'root_first')
    repair.apply(db, 'P-root', FIXTURES / 'repair-field-root-first.jsonl')
    repair.checkpoint(db, 'P-root', 2)
    assert repair.verify(db, 'P-root')['status'] == '已核证复工'
    with connect(db) as con:
        assert con.execute('SELECT count(*) FROM fleet_decision WHERE run_id=?', (rid,)).fetchone()[0] == 1
        assert con.execute('SELECT count(*) FROM propagation_link WHERE run_id=?', (rid,)).fetchone()[0] == 1
