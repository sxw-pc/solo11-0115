import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from burstweave import engine, operations, service, timeline
from burstweave.store import connect

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'
T = '2026-01-01T10:{:02d}:00Z'


def make_incident(tmp_path, n_assets=2):
    """Closed run with one credible episode per asset (ids 1..n)."""
    db = tmp_path / 'ledger.db'
    with connect(db) as con:
        con.execute("INSERT INTO run(id,version,status) VALUES (1,'v1','已闭合')")
        for i in range(n_assets):
            asset = f'A{i}'
            con.execute('INSERT INTO asset VALUES (?)', (asset,))
            con.execute('''INSERT INTO episode(id,run_id,asset,start,end,status,replaces)
                VALUES (?,1,?,'2026-01-01T10:00:00Z',
                '2026-01-01T10:30:00Z','可信','[]')''', (i + 1, asset))
    return db


def copy_fixture(tmp_path, name):
    src = FIXTURES / name
    dst = tmp_path / name
    shutil.copy(src, dst)
    return dst


def register_from(db, tmp_path, service_id, name='service-impact.json'):
    fixture = copy_fixture(tmp_path, name)
    return service.register(db, service_id, 1, fixture), fixture


def witness_offsets(witness):
    onset, recovery = {}, {}
    for asset, part in witness['assets'].items():
        onset[asset] = part['onset']
        recovery[asset] = part['recovery']
    return onset, recovery


# ---------------------------------------------------------------------------
# Headline synthetic example: the two topologies disagree on the same evidence.
# ---------------------------------------------------------------------------

def test_series_breaches_while_redundant_clears_on_same_evidence(tmp_path):
    db = make_incident(tmp_path)
    register_from(db, tmp_path, 'svc-series')
    register_from(db, tmp_path, 'svc-redundant')

    series = service.assess(db, 'svc-series', 1, 'series', ['A0', 'A1'], 15)
    assert series['verdict'] == 'definitely_breached'
    assert (series['outage_minutes_min'], series['outage_minutes_max']) == (20, 25)

    redundant = service.assess(db, 'svc-redundant', 1, 'redundant_2',
                               ['A0', 'A1'], 15)
    assert redundant['verdict'] == 'definitely_clear'
    assert (redundant['outage_minutes_min'],
            redundant['outage_minutes_max']) == (5, 10)

    # Both certificates live side by side with frozen service identities.
    with connect(db) as con:
        rows = con.execute('SELECT id,payload,digest FROM service_certificate').fetchall()
        assert {r['id'] for r in rows} == {'svc-series', 'svc-redundant'}
        for row in rows:
            assert json.loads(row['payload'])['segment_semantics'] == \
                'half_open_onset_inclusive_recovery_exclusive'
            assert row['digest']


def test_joint_onsets_pin_the_only_supported_pair_and_verify_passes(tmp_path):
    db = make_incident(tmp_path)
    register_from(db, tmp_path, 'svc-series')
    result = service.assess(db, 'svc-series', 1, 'series', ['A0', 'A1'], 15)

    with connect(db) as con:
        payload = json.loads(
            con.execute("SELECT payload FROM service_certificate WHERE id='svc-series'")
            .fetchone()['payload'])
    doc = json.loads((FIXTURES / 'service-impact.json').read_text())
    model = service._build_doc_model(doc)
    assert timeline.feasible_onsets(model) == [
        {'A0': 5, 'A1': 10}]  # only 10:05 / 10:10 satisfies the joint lag

    for witness in (payload['min_witness'], payload['max_witness']):
        onset_dt, recovery_dt = witness_offsets(witness)
        onset = {a: int((engine.dt(v) - model['start']).total_seconds() // 60)
                 for a, v in onset_dt.items()}
        recovery = {a: int((engine.dt(v) - model['start']).total_seconds() // 60)
                    for a, v in recovery_dt.items()}
        assert timeline.check_witness(model, onset, recovery)
        length, segments = timeline.outage(
            model, 'series', ['A0', 'A1'], onset, recovery)
        assert length == witness['outage_minutes']
        # Every feasible timeline starts at the pinned 10:05 joint onset.
        assert segments[0][0] == 5

    # Independent endpoint picking (A0 onset 10:00, A1 onset 10:10) is rejected
    # because the 10-minute gap violates the 0-5 minute joint relation.
    assert not timeline.check_witness(model, {'A0': 0, 'A1': 10},
                                      {'A0': 15, 'A1': 25})
    verified = service.verify(db, 'svc-series')
    assert verified['verdict'] == 'definitely_breached'
    assert (verified['outage_minutes_min'],
            verified['outage_minutes_max']) == (20, 25)


def test_redundant_at_seven_minute_sla_is_indeterminate(tmp_path):
    db = make_incident(tmp_path)
    register_from(db, tmp_path, 'svc-redundant')
    result = service.assess(db, 'svc-redundant', 1, 'redundant_2',
                            ['A0', 'A1'], 7)
    assert result['verdict'] == 'indeterminate'
    assert (result['outage_minutes_min'],
            result['outage_minutes_max']) == (5, 10)
    with connect(db) as con:
        payload = json.loads(
            con.execute("SELECT payload FROM service_certificate WHERE id='svc-redundant'")
            .fetchone()['payload'])
    assert 'span_crosses_sla' in payload['reasons']


def test_sla_boundary_uses_strict_comparisons(tmp_path):
    db = make_incident(tmp_path)
    register_from(db, tmp_path, 'svc-sb1')
    at_min = service.assess(db, 'svc-sb1', 1, 'series', ['A0', 'A1'], 20)
    assert at_min['verdict'] == 'indeterminate'          # lower bound == SLA is not a breach
    register_from(db, tmp_path, 'svc-sb2')
    above = service.assess(db, 'svc-sb2', 1, 'series', ['A0', 'A1'], 19)
    assert above['verdict'] == 'definitely_breached'
    register_from(db, tmp_path, 'svc-rb1')
    at_max = service.assess(db, 'svc-rb1', 1, 'redundant_2',
                            ['A0', 'A1'], 10)
    assert at_max['verdict'] == 'definitely_clear'       # upper bound == SLA still clear
    register_from(db, tmp_path, 'svc-rb2')
    below = service.assess(db, 'svc-rb2', 1, 'redundant_2',
                           ['A0', 'A1'], 9)
    assert below['verdict'] == 'indeterminate'


# ---------------------------------------------------------------------------
# Joint onset relations change the extrema vs. independent per-asset bounds.
# ---------------------------------------------------------------------------

def test_onset_relation_tightens_extrema_against_independent_endpoints(tmp_path):
    db = make_incident(tmp_path)
    _, fixture = register_from(db, tmp_path, 'svc-joint')

    # Same observations but with the lag contract stripped: independent
    # endpoint combinations that the joint evidence forbids become feasible.
    loose_doc = json.loads(fixture.read_text())
    loose_doc['onset_relations'] = []
    loose_path = tmp_path / 'service-impact-loose.json'
    loose_path.write_text(json.dumps(loose_doc))
    service.register(db, 'svc-loose', 1, loose_path)

    joint = service.assess(db, 'svc-joint', 1, 'series', ['A0', 'A1'], 15)
    loose = service.assess(db, 'svc-loose', 1, 'series', ['A0', 'A1'], 15)
    assert (joint['outage_minutes_min'], joint['outage_minutes_max']) == (20, 25)
    assert (loose['outage_minutes_min'], loose['outage_minutes_max']) == (20, 30)
    # The loose maximum is built from an infeasible endpoint combination:
    # A0 onset 10:00 and A1 recovery 10:30 cannot co-exist under the 0-5 lag.
    with connect(db) as con:
        max_witness = json.loads(con.execute(
            "SELECT payload FROM service_certificate WHERE id='svc-loose'")
            .fetchone()['payload'])['max_witness']
    assert max_witness['assets']['A0']['onset'] == '2026-01-01T10:00:00Z'
    assert max_witness['assets']['A1']['recovery'] == '2026-01-01T10:30:00Z'


# ---------------------------------------------------------------------------
# Half-open segment semantics: touching segments merge without double count.
# ---------------------------------------------------------------------------

def test_half_open_touching_segments_merge(tmp_path):
    db = make_incident(tmp_path)
    doc = {
        'period_start': '2026-01-01T10:00:00Z',
        'period_end': '2026-01-01T11:00:00Z',
        'assets': ['A0', 'A1'],
        'impacts': [
            {'asset': 'A0', 'episode_id': 1,
             'onset_between': [T.format(0), T.format(0)],
             'recovery_between': [T.format(10), T.format(10)]},
            {'asset': 'A1', 'episode_id': 2,
             'onset_between': [T.format(10), T.format(10)],
             'recovery_between': [T.format(20), T.format(20)]},
        ],
        'onset_relations': [],
    }
    path = tmp_path / 'touching.json'
    path.write_text(json.dumps(doc))
    service.register(db, 'svc-touch', 1, path)
    result = service.assess(db, 'svc-touch', 1, 'series', ['A0', 'A1'], 0)
    assert result['outage_minutes_min'] == 20
    with connect(db) as con:
        payload = json.loads(con.execute(
            "SELECT payload FROM service_certificate WHERE id='svc-touch'")
            .fetchone()['payload'])
    witness = payload['min_witness']
    assert witness['outage_minutes'] == 20
    assert len(witness['outage_segments']) == 1  # [10:00,10:10) U [10:10,10:20)
    assert witness['outage_segments'][0] == {
        'start': T.format(0), 'end': T.format(20), 'length_minutes': 20}


# ---------------------------------------------------------------------------
# Three-asset exhaustive cross-check against full timeline enumeration.
# ---------------------------------------------------------------------------

def test_three_asset_bounds_match_brute_force_enumeration(tmp_path):
    db = make_incident(tmp_path, n_assets=3)
    register_from(db, tmp_path, 'svc-3a', 'service-impact-3.json')
    result = service.assess(db, 'svc-3a', 1, 'series', ['A0', 'A1', 'A2'], 60)
    assert result['verdict'] == 'definitely_clear'

    doc = json.loads((FIXTURES / 'service-impact-3.json').read_text())
    model = service._build_doc_model(doc)
    brute_min, brute_max = None, None
    count = 0
    for onset, recovery in timeline.exhaustive_timelines(model):
        length, _ = timeline.outage(model, 'series', ['A0', 'A1', 'A2'],
                                    onset, recovery)
        brute_min = length if brute_min is None else min(brute_min, length)
        brute_max = length if brute_max is None else max(brute_max, length)
        count += 1
    assert count > 1
    assert result['outage_minutes_min'] == brute_min
    assert result['outage_minutes_max'] == brute_max

    register_from(db, tmp_path, 'svc-3b', 'service-impact-3.json')
    redundant = service.assess(db, 'svc-3b', 1, 'redundant_2', ['A0', 'A2'], 60)
    brute_rmin = min(timeline.outage(model, 'redundant_2', ['A0', 'A2'], o, r)[0]
                     for o, r in timeline.exhaustive_timelines(model))
    assert redundant['outage_minutes_min'] == brute_rmin
    service.verify(db, 'svc-3a')
    service.verify(db, 'svc-3b')


# ---------------------------------------------------------------------------
# Contradictory joint constraints and missing evidence => indeterminate.
# ---------------------------------------------------------------------------

def test_contradictory_onset_relation_is_indeterminate(tmp_path):
    db = make_incident(tmp_path)
    register_from(db, tmp_path, 'svc-bad', 'service-impact-contradiction.json')
    result = service.assess(db, 'svc-bad', 1, 'series', ['A0', 'A1'], 15)
    assert result['verdict'] == 'indeterminate'
    assert result['outage_minutes_min'] is None
    with connect(db) as con:
        payload = json.loads(con.execute(
            "SELECT payload FROM service_certificate WHERE id='svc-bad'")
            .fetchone()['payload'])
    assert 'unsatisfiable_joint_timeline' in payload['reasons']
    assert payload['min_witness'] is None
    # An indeterminate certificate still re-verifies honestly.
    verified = service.verify(db, 'svc-bad')
    assert verified['verdict'] == 'indeterminate'


def test_required_asset_without_evidence_is_indeterminate(tmp_path):
    db = make_incident(tmp_path, n_assets=3)
    register_from(db, tmp_path, 'svc-missing')
    result = service.assess(db, 'svc-missing', 1, 'series',
                            ['A0', 'A1', 'A2'], 15)
    assert result['verdict'] == 'indeterminate'
    with connect(db) as con:
        payload = json.loads(con.execute(
            "SELECT payload FROM service_certificate WHERE id='svc-missing'")
            .fetchone()['payload'])
    assert any(r.startswith('topology_asset_without_evidence')
               for r in payload['reasons'])
    service.verify(db, 'svc-missing')


def test_unproven_episode_makes_certificate_indeterminate(tmp_path):
    db = make_incident(tmp_path)
    register_from(db, tmp_path, 'svc-ep')
    with connect(db) as con:
        con.execute("UPDATE episode SET status='待核' WHERE id=2")
    result = service.assess(db, 'svc-ep', 1, 'series', ['A0', 'A1'], 15)
    assert result['verdict'] == 'indeterminate'
    service.verify(db, 'svc-ep')


# ---------------------------------------------------------------------------
# Tamper handling: any rewritten source or ledger invalidates the certificate.
# ---------------------------------------------------------------------------

def test_impact_file_rewrite_invalidates_certificate(tmp_path):
    db = make_incident(tmp_path)
    _, fixture = register_from(db, tmp_path, 'svc-tamper')
    service.assess(db, 'svc-tamper', 1, 'series', ['A0', 'A1'], 15)
    fixture.write_text(fixture.read_text().replace('10:05:00Z', '10:06:00Z'))
    with pytest.raises(ValueError, match='impact_source_tampered'):
        service.verify(db, 'svc-tamper')
    with connect(db) as con:
        assert con.execute(
            "SELECT count(*) FROM service_certificate WHERE id='svc-tamper'") \
            .fetchone()[0] == 0


def test_run_reopened_invalidates_certificate(tmp_path):
    db = make_incident(tmp_path)
    register_from(db, tmp_path, 'svc-run')
    service.assess(db, 'svc-run', 1, 'series', ['A0', 'A1'], 15)
    with connect(db) as con:
        con.execute("UPDATE run SET status='待闭合' WHERE id=1")
    with pytest.raises(ValueError, match='run_not_closed'):
        service.verify(db, 'svc-run')
    with connect(db) as con:
        assert con.execute(
            "SELECT count(*) FROM service_certificate WHERE id='svc-run'") \
            .fetchone()[0] == 0


def test_certificate_row_tampering_is_rejected(tmp_path):
    db = make_incident(tmp_path)
    register_from(db, tmp_path, 'svc-row')
    service.assess(db, 'svc-row', 1, 'series', ['A0', 'A1'], 15)
    # Rewriting the stored payload without its digest fails the self-digest.
    with connect(db) as con:
        row = con.execute(
            "SELECT payload FROM service_certificate WHERE id='svc-row'").fetchone()
        payload = json.loads(row['payload'])
        payload['sla_allowed_minutes'] = 99
        con.execute("UPDATE service_certificate SET payload=? WHERE id='svc-row'",
                    (json.dumps(payload, sort_keys=True, separators=(',', ':')),))
    with pytest.raises(ValueError, match='certificate_tampered'):
        service.verify(db, 'svc-row')
    with connect(db) as con:
        assert con.execute(
            "SELECT count(*) FROM service_certificate WHERE id='svc-row'") \
            .fetchone()[0] == 0


def test_ledger_evidence_rewrite_makes_stored_certificate_stale(tmp_path):
    db = make_incident(tmp_path)
    register_from(db, tmp_path, 'svc-stale')
    service.assess(db, 'svc-stale', 1, 'series', ['A0', 'A1'], 15)
    # Rewrite the frozen observation bounds inside the ledger row while keeping
    # the raw impact file unchanged: the frozen digest no longer matches the
    # raw source, so the certificate is rejected and discarded.
    with connect(db) as con:
        row = con.execute(
            "SELECT payload FROM service_impact WHERE id='svc-stale'").fetchone()
        impact = json.loads(row['payload'])
        impact['doc']['impacts'][0]['onset_between'][1] = \
            '2026-01-01T10:08:00Z'
        con.execute("UPDATE service_impact SET payload=? WHERE id='svc-stale'",
                    (json.dumps(impact, sort_keys=True, separators=(',', ':')),))
    with pytest.raises(ValueError, match='impact_source_tampered'):
        service.verify(db, 'svc-stale')
    with connect(db) as con:
        assert con.execute(
            "SELECT count(*) FROM service_certificate WHERE id='svc-stale'") \
            .fetchone()[0] == 0


def test_certificate_columns_cannot_be_silently_recomputed(tmp_path):
    db = make_incident(tmp_path)
    register_from(db, tmp_path, 'svc-col')
    service.assess(db, 'svc-col', 1, 'series', ['A0', 'A1'], 15)
    # Forging the certificate's own run_id (a ledger rewrite with intact
    # sources) breaks the row self-digest, so it is rejected outright.
    with connect(db) as con:
        row = con.execute(
            "SELECT payload FROM service_certificate WHERE id='svc-col'").fetchone()
        cert = json.loads(row['payload'])
        cert['run_id'] = 999
        con.execute("UPDATE service_certificate SET payload=? WHERE id='svc-col'",
                    (json.dumps(cert, sort_keys=True, separators=(',', ':')),))
    with pytest.raises(ValueError, match='certificate_tampered'):
        service.verify(db, 'svc-col')
    with connect(db) as con:
        assert con.execute(
            "SELECT count(*) FROM service_certificate WHERE id='svc-col'") \
            .fetchone()[0] == 0


def test_missing_impact_file_invalidates_certificate(tmp_path):
    db = make_incident(tmp_path)
    _, fixture = register_from(db, tmp_path, 'svc-gone')
    service.assess(db, 'svc-gone', 1, 'series', ['A0', 'A1'], 15)
    fixture.unlink()
    with pytest.raises(ValueError, match='impact_source_unreadable'):
        service.verify(db, 'svc-gone')


# ---------------------------------------------------------------------------
# Old fault episodes and work orders remain untouched and fully compatible.
# ---------------------------------------------------------------------------

def test_service_certificate_does_not_rewrite_episodes_or_work_orders(tmp_path):
    db = make_incident(tmp_path)
    register_from(db, tmp_path, 'svc-compat')
    # Give the run real work orders through the existing triage flow.
    orders = operations.triage(db, 1)
    assert orders
    before = {row['id']: dict(row)
              for row in connect(db).execute('SELECT * FROM episode')}
    work_before = [dict(row) for row in connect(db).execute(
        'SELECT * FROM work_order ORDER BY episode_id')]

    service.assess(db, 'svc-compat', 1, 'series', ['A0', 'A1'], 15)
    service.verify(db, 'svc-compat')

    after = {row['id']: dict(row)
             for row in connect(db).execute('SELECT * FROM episode')}
    work_after = [dict(row) for row in connect(db).execute(
        'SELECT * FROM work_order ORDER BY episode_id')]
    assert before == after
    assert work_before == work_after
    # Re-running triage remains idempotent alongside the certificate.
    assert operations.triage(db, 1) == orders


def test_certificate_contract_conflict_is_rejected_but_rerun_is_idempotent(tmp_path):
    db = make_incident(tmp_path)
    register_from(db, tmp_path, 'svc-id')
    first = service.assess(db, 'svc-id', 1, 'series', ['A0', 'A1'], 15)
    assert service.assess(db, 'svc-id', 1, 'series', ['A0', 'A1'], 15) == first
    with pytest.raises(ValueError, match='certificate_contract_conflict'):
        service.assess(db, 'svc-id', 1, 'redundant_2', ['A0', 'A1'], 15)
    with pytest.raises(ValueError, match='certificate_contract_conflict'):
        service.assess(db, 'svc-id', 1, 'series', ['A0', 'A1'], 16)
    with pytest.raises(ValueError, match='certificate_contract_conflict'):
        service.assess(db, 'svc-id', 1, 'series', ['A0'], 15)


def test_unknown_service_and_bad_arguments(tmp_path):
    db = make_incident(tmp_path)
    with pytest.raises(ValueError, match='service_impact_not_registered'):
        service.assess(db, 'nope', 1, 'series', ['A0'], 15)
    register_from(db, tmp_path, 'svc-bad')
    with pytest.raises(ValueError, match='unknown_topology'):
        service.assess(db, 'svc-bad', 1, 'star', ['A0', 'A1'], 15)
    with pytest.raises(ValueError, match='redundant_2_requires_two_assets'):
        service.assess(db, 'svc-bad', 1, 'redundant_2', ['A0'], 15)
    with pytest.raises(ValueError, match='bad_sla_minutes'):
        service.assess(db, 'svc-bad', 1, 'series', ['A0', 'A1'], -1)
    with pytest.raises(ValueError, match='certificate_not_found'):
        service.verify(db, 'svc-bad')


def test_subminute_bounds_are_rejected(tmp_path):
    db = make_incident(tmp_path)
    doc = json.loads((FIXTURES / 'service-impact.json').read_text())
    # Whole-minute period, but a sub-minute impact bound: registration only
    # freezes the bytes, the minute-grid solver rejects the precision.
    doc['impacts'][0]['onset_between'][1] = '2026-01-01T10:05:30Z'
    path = tmp_path / 'subminute.json'
    path.write_text(json.dumps(doc))
    service.register(db, 'svc-sub', 1, path)
    with pytest.raises(ValueError, match='subminute_precision'):
        service.assess(db, 'svc-sub', 1, 'series', ['A0', 'A1'], 15)


# ---------------------------------------------------------------------------
# Timezone, zero-length redundancy and three-asset cycle contradictions.
# ---------------------------------------------------------------------------

def test_non_utc_timezone_offsets_are_equivalent(tmp_path):
    from datetime import datetime, timezone, timedelta
    tz = timezone(timedelta(hours=8))
    start = datetime(2026, 1, 1, 18, 0, tzinfo=tz)
    end = datetime(2026, 1, 1, 19, 0, tzinfo=tz)

    def h(m):
        return (start + timedelta(minutes=m)).isoformat()

    doc = {
        'period_start': start.isoformat(), 'period_end': end.isoformat(),
        'assets': ['A0', 'A1'],
        'impacts': [
            {'asset': 'A0', 'episode_id': 1,
             'onset_between': [h(0), h(5)], 'recovery_between': [h(15), h(20)]},
            {'asset': 'A1', 'episode_id': 2,
             'onset_between': [h(10), h(15)], 'recovery_between': [h(25), h(30)]},
        ],
        'onset_relations': [{'from': 'A0', 'to': 'A1',
                             'min_lag_minutes': 0, 'max_lag_minutes': 5}],
    }
    path = tmp_path / 'tz.json'
    path.write_text(json.dumps(doc))
    db = make_incident(tmp_path)
    service.register(db, 'svc-tz', 1, path)
    result = service.assess(db, 'svc-tz', 1, 'series', ['A0', 'A1'], 15)
    assert (result['outage_minutes_min'],
            result['outage_minutes_max']) == (20, 25)


def test_redundant_disjoint_windows_have_zero_outage_and_empty_segments(tmp_path):
    doc = {
        'period_start': '2026-01-01T10:00:00Z',
        'period_end': '2026-01-01T11:00:00Z',
        'assets': ['A0', 'A1'],
        'impacts': [
            {'asset': 'A0', 'episode_id': 1,
             'onset_between': [T.format(0), T.format(0)],
             'recovery_between': [T.format(10), T.format(10)]},
            {'asset': 'A1', 'episode_id': 2,
             'onset_between': [T.format(10), T.format(10)],
             'recovery_between': [T.format(20), T.format(20)]},
        ],
        'onset_relations': [],
    }
    path = tmp_path / 'disjoint.json'
    path.write_text(json.dumps(doc))
    db = make_incident(tmp_path)
    service.register(db, 'svc-disjoint', 1, path)
    result = service.assess(db, 'svc-disjoint', 1, 'redundant_2', ['A0', 'A1'], 5)
    assert result['verdict'] == 'definitely_clear'
    assert (result['outage_minutes_min'],
            result['outage_minutes_max']) == (0, 0)
    with connect(db) as con:
        payload = json.loads(con.execute(
            "SELECT payload FROM service_certificate WHERE id='svc-disjoint'")
            .fetchone()['payload'])
    assert payload['min_witness']['outage_segments'] == []
    assert payload['min_witness']['outage_minutes'] == 0
    service.verify(db, 'svc-disjoint')


def test_three_asset_relation_cycle_is_unsatisfiable(tmp_path):
    db = make_incident(tmp_path, n_assets=3)
    doc = json.loads((FIXTURES / 'service-impact-3.json').read_text())
    # A2 must start 10-15 minutes after A0, but the windows force A2 >= A0+10
    # while A0 can be at most 10 and A2 at least 10; demand a strictly larger
    # gap than any joint onset pair can supply.
    doc['onset_relations'].append(
        {'from': 'A0', 'to': 'A2', 'min_lag_minutes': 30, 'max_lag_minutes': 40})
    path = tmp_path / 'cycle.json'
    path.write_text(json.dumps(doc))
    service.register(db, 'svc-cycle', 1, path)
    result = service.assess(db, 'svc-cycle', 1, 'series',
                            ['A0', 'A1', 'A2'], 15)
    assert result['verdict'] == 'indeterminate'
    assert result['outage_minutes_min'] is None
    service.verify(db, 'svc-cycle')


# ---------------------------------------------------------------------------
# CLI end-to-end.
# ---------------------------------------------------------------------------

def test_cli_register_assess_verify(tmp_path):
    db = make_incident(tmp_path)
    fixture = copy_fixture(tmp_path, 'service-impact.json')
    base = [sys.executable, '-m', 'burstweave']
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    run = lambda *args: subprocess.run(base + list(args), check=True,
                                       capture_output=True, text=True, env=env,
                                       cwd=str(tmp_path))
    run('service-register', '--db', str(db), '--service-id', 'svc-cli',
        '--run-id', '1', str(fixture))
    out = json.loads(run('service-assess', '--db', str(db), '--service-id',
                         'svc-cli', '--run-id', '1', '--topology', 'series',
                         '--assets', 'A0,A1', '--sla-minutes', '15').stdout)
    assert out['verdict'] == 'definitely_breached'
    verified = json.loads(run('service-verify', '--db', str(db),
                              '--service-id', 'svc-cli').stdout)
    assert verified['outage_minutes_min'] == 20
