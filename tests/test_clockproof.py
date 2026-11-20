import hashlib
import json
import shutil
from pathlib import Path

import pytest

from burstweave.clockproof import (
    build_report,
    certify,
    query_time,
    register_design,
)
from burstweave.engine import dt, ingest, setup
from burstweave.store import connect


ROOT = Path(__file__).resolve().parents[1]
DESIGN = ROOT / "fixtures/clockproof/design.json"
POINTS = ROOT / "fixtures/clockproof/points.jsonl"
CASE_ID = "clock-D-20260401"


def _db(tmp_path):
    db = tmp_path / "clock.db"
    setup(db, {"assets": ["A"], "sensors": [{"id": "S", "asset": "A", "role": "heat"}]})
    return db


def _fixture_case(tmp_path):
    db = _db(tmp_path)
    assert ingest(db, POINTS) == 17
    registered = register_design(db, DESIGN)
    return db, registered


def _anchor(seq, at, ref, offset, uncertainty=1, master_attested=False,
            device="D", kind="anchor"):
    return {"at": at, "device": device, "kind": kind,
            "master_attested": master_attested, "offset_seconds": offset,
            "reference_id": ref, "sensor": "S", "seq": seq,
            "uncertainty_seconds": uncertainty}


def _event(seq, at, kind="anomaly"):
    return {"at": at, "device": "D", "kind": kind, "sensor": "S",
            "seq": seq, "value": 7}


class SyntheticCase:
    """Ingest a modified raw source and lock a fresh design against it.

    Every verdict is recomputed from these raw rows; the design only freezes
    their hash and the contract parameters.
    """

    def __init__(self, tmp_path, name, rows, *, references=None, quorum=2,
                 bridge=15, drift=0.2, master="A"):
        references = references or ["A", "B", "C"]
        self.case_dir = ROOT / "fixtures" / "clockproof" / f".cf-test-{name}"
        self.case_dir.mkdir(parents=True, exist_ok=True)
        self.points = self.case_dir / "points.jsonl"
        self.points.write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        design = {
            "case_id": f"clock-{name}",
            "source_path": str(self.points.relative_to(ROOT)),
            "source_sha256": hashlib.sha256(self.points.read_bytes()).hexdigest(),
            "device": "D", "references": references, "master_reference": master,
            "max_drift_seconds_per_minute": drift,
            "min_independent_references": quorum, "max_bridge_minutes": bridge,
        }
        self.design = self.case_dir / "design.json"
        self.design.write_text(json.dumps(design, indent=2), encoding="utf-8")
        self.db = tmp_path / f"{name}.db"
        setup(self.db, {"assets": ["A"],
                        "sensors": [{"id": "S", "asset": "A", "role": "heat"}]})
        self.ingested = ingest(self.db, self.points)
        self.case_id = register_design(self.db, self.design)["case_id"]

    def cleanup(self):
        shutil.rmtree(self.case_dir, ignore_errors=True)


@pytest.fixture
def synthetic():
    created = []

    def factory(*args, **kwargs):
        case = SyntheticCase(*args, **kwargs)
        created.append(case)
        return case

    yield factory
    for case in created:
        case.cleanup()


# --------------------------------------------------------------------------- #
# Design freezing (original behaviour preserved).
# --------------------------------------------------------------------------- #

def test_clock_fixture_frozen_after_ingest(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(ValueError, match="ingest clock source first"):
        register_design(db, DESIGN)
    assert ingest(db, POINTS) == 17
    first = register_design(db, DESIGN)
    assert first["anchor_count"] == 13
    assert len(set(first["anchor_ids"])) == 13
    assert register_design(db, DESIGN) == first


def test_clock_design_rejects_changed_source_digest(tmp_path):
    db = _db(tmp_path)
    ingest(db, POINTS)
    altered = json.loads(DESIGN.read_text(encoding="utf-8"))
    altered["source_sha256"] = "0" * 64
    temporary = ROOT / "fixtures/clockproof/tamper.tmp.json"
    try:
        temporary.write_text(json.dumps(altered), encoding="utf-8")
        with pytest.raises(ValueError, match="source bytes changed"):
            register_design(db, temporary)
    finally:
        temporary.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Step 1: per-timestamp independent reference arbitration on the frozen fixture.
# --------------------------------------------------------------------------- #

def test_fixture_arbitration_isolates_single_outlier_at_0920(tmp_path):
    db, registered = _fixture_case(tmp_path)
    report = certify(db, registered["case_id"])
    verdicts = {v["anchor_id"]: v for v in report["anchor_verdicts"]}
    # B/C form the unique two-reference support set at 09:20; A is isolated.
    assert verdicts[8]["verdict"] == "supported"
    assert verdicts[9]["verdict"] == "supported"
    assert verdicts[7]["verdict"] == "quarantined"
    assert verdicts[7]["reason"] == "incompatible_outlier"
    at_0920 = next(t for t in report["contracts"]["quorum-only"]["timepoints"]
                   if t["at"] == "2026-04-01T09:20:00Z")
    assert at_0920["status"] == "certified"
    assert at_0920["supporting_anchor_ids"] == [8, 9]
    assert at_0920["offset_low"] == pytest.approx(-1.0)
    assert at_0920["offset_high"] == pytest.approx(1.0)
    # The raw line and source hash travel with every verdict for recomputation.
    raw = json.loads(verdicts[7]["raw_line"])
    assert raw["offset_seconds"] == 180
    assert hashlib.sha256(verdicts[7]["raw_line"].encode()).hexdigest() == verdicts[7]["raw_hash"]


def test_fixture_three_reference_quorum_after_the_jump(tmp_path):
    db, registered = _fixture_case(tmp_path)
    report = certify(db, registered["case_id"])
    for at in ("2026-04-01T09:30:00Z", "2026-04-01T09:40:00Z"):
        tp = next(t for t in report["contracts"]["quorum-only"]["timepoints"] if t["at"] == at)
        assert tp["status"] == "certified"
        assert len(tp["supporting_anchor_ids"]) == 3
        assert (tp["offset_low"], tp["offset_high"]) == (pytest.approx(179.0), pytest.approx(181.0))


def test_master_attested_single_reading_remains_uncertified_under_quorum(tmp_path):
    db, registered = _fixture_case(tmp_path)
    report = certify(db, registered["case_id"])
    verdict = next(v for v in report["anchor_verdicts"] if v["anchor_id"] == 5)
    assert verdict["verdict"] == "uncertified"
    assert verdict["reason"] == "below_independent_quorum"
    assert verdict["master_attested"] is True
    tp = next(t for t in report["contracts"]["quorum-only"]["timepoints"]
              if t["at"] == "2026-04-01T09:10:00Z")
    assert tp["status"] == "uncertified"


def test_master_fallback_only_certifies_uncontradicted_master_reading(tmp_path):
    db, registered = _fixture_case(tmp_path)
    report = certify(db, registered["case_id"])
    fallback = report["contracts"]["master-fallback"]
    tp = next(t for t in fallback["timepoints"] if t["at"] == "2026-04-01T09:10:00Z")
    assert tp["status"] == "provisional"
    assert tp["basis"] == "master_fallback"
    assert tp["supporting_anchor_ids"] == [5]
    assert (tp["offset_low"], tp["offset_high"]) == (pytest.approx(-1.0), pytest.approx(1.0))
    # The 09:20 master anchor is contradicted by B/C: fallback must not adopt it.
    tp20 = next(t for t in fallback["timepoints"] if t["at"] == "2026-04-01T09:20:00Z")
    assert tp20["status"] == "certified" and tp20["basis"] == "independent_quorum"
    assert tp20["supporting_anchor_ids"] == [8, 9]


def test_master_fallback_requires_explicit_attestation(synthetic, tmp_path):
    rows = [_anchor(1, "2026-05-01T00:00:00Z", "A", 0),
            _anchor(2, "2026-05-01T00:00:00Z", "B", 0),
            _anchor(3, "2026-05-01T00:10:00Z", "A", 0, master_attested=False),
            _anchor(4, "2026-05-01T00:20:00Z", "A", 0),
            _anchor(5, "2026-05-01T00:20:00Z", "B", 0)]
    case = synthetic(tmp_path, "no-attest", rows)
    report = certify(case.db, case.case_id)
    fallback = report["contracts"]["master-fallback"]
    tp = next(t for t in fallback["timepoints"] if t["at"] == "2026-05-01T00:10:00Z")
    assert tp["status"] == "uncertified"


def test_master_fallback_refused_when_another_reference_contradicts(synthetic, tmp_path):
    rows = [_anchor(1, "2026-05-01T00:00:00Z", "A", 0),
            _anchor(2, "2026-05-01T00:00:00Z", "B", 0),
            _anchor(3, "2026-05-01T00:10:00Z", "A", 0, master_attested=True),
            _anchor(4, "2026-05-01T00:10:00Z", "B", 60)]
    case = synthetic(tmp_path, "contradicted", rows)
    report = certify(case.db, case.case_id)
    tp = next(t for t in report["contracts"]["master-fallback"]["timepoints"]
              if t["at"] == "2026-05-01T00:10:00Z")
    assert tp["status"] == "uncertified"


# --------------------------------------------------------------------------- #
# Step 2: segments, bridge/drift contracts and bounded UTC certificates.
# --------------------------------------------------------------------------- #

def test_quorum_contract_bridge_gap_and_jump_on_fixture(tmp_path):
    db, registered = _fixture_case(tmp_path)
    report = certify(db, registered["case_id"])
    bridges = {(b["at_from"], b["at_to"]): b
               for b in report["contracts"]["quorum-only"]["bridges"]}
    gap = bridges[("2026-04-01T09:00:00Z", "2026-04-01T09:20:00Z")]
    assert gap["connected"] is False and gap["reason"] == "bridge_gap"
    jump = bridges[("2026-04-01T09:20:00Z", "2026-04-01T09:30:00Z")]
    assert jump["connected"] is False and jump["reason"] == "clock_jump"
    fine = bridges[("2026-04-01T09:30:00Z", "2026-04-01T09:40:00Z")]
    assert fine["connected"] is True
    segments = report["contracts"]["quorum-only"]["segments"]
    assert [(s["at_from"], s["at_to"]) for s in segments] == [
        ("2026-04-01T09:00:00Z", "2026-04-01T09:00:00Z"),
        ("2026-04-01T09:20:00Z", "2026-04-01T09:20:00Z"),
        ("2026-04-01T09:30:00Z", "2026-04-01T09:40:00Z"),
    ]


def test_quorum_contract_event_certificates(tmp_path):
    db, registered = _fixture_case(tmp_path)
    report = certify(db, registered["case_id"])
    certs = {c["at"]: c for c in report["contracts"]["quorum-only"]["certificates"]}
    assert not certs["2026-04-01T09:05:00Z"]["certified"]
    assert not certs["2026-04-01T09:10:30Z"]["certified"]
    assert not certs["2026-04-01T09:25:00Z"]["certified"]
    c35 = certs["2026-04-01T09:35:00Z"]
    assert c35["certified"]
    assert c35["offset_low"] == pytest.approx(178.0)
    assert c35["offset_high"] == pytest.approx(182.0)
    assert dt(c35["utc_low"]) == dt("2026-04-01T09:31:58Z")
    assert dt(c35["utc_high"]) == dt("2026-04-01T09:32:02Z")
    assert c35["anchor_ids"] == [11, 12, 13, 15, 16, 17]


def test_master_fallback_contract_extends_telemetry_range(tmp_path):
    db, registered = _fixture_case(tmp_path)
    report = certify(db, registered["case_id"])
    certs = {c["at"]: c for c in report["contracts"]["master-fallback"]["certificates"]}
    # 09:10:30 bounded by BOTH bridges 09:00->09:10 and 09:10->09:20.
    c = certs["2026-04-01T09:10:30Z"]
    assert c["certified"] and c["status"] == "provisional"
    assert c["offset_low"] == pytest.approx(-1.1)
    assert c["offset_high"] == pytest.approx(1.1)
    assert dt(c["utc_low"]) == dt("2026-04-01T09:10:28.9Z")
    assert dt(c["utc_high"]) == dt("2026-04-01T09:10:31.1Z")
    # The jump gap still cannot be bridged by either contract.
    assert not certs["2026-04-01T09:25:00Z"]["certified"]
    # 09:35 is certified under both contracts with identical bounds.
    c35 = certs["2026-04-01T09:35:00Z"]
    assert c35["certified"] and c35["offset_low"] == pytest.approx(178.0)
    assert c35["offset_high"] == pytest.approx(182.0)


def test_query_entry_matches_persisted_certificates(tmp_path):
    db, registered = _fixture_case(tmp_path)
    certify(db, registered["case_id"])
    answer = query_time(db, CASE_ID, "2026-04-01T09:35:00Z")
    assert answer["certified"] and answer["offset_low"] == pytest.approx(178.0)
    provisional = query_time(db, CASE_ID, "2026-04-01T09:10:30Z", "master-fallback")
    assert provisional["certified"] and provisional["status"] == "master_fallback"
    assert provisional["offset_low"] == pytest.approx(-1.1)
    denied = query_time(db, CASE_ID, "2026-04-01T09:25:00Z")
    assert not denied["certified"] and denied["reason"] == "outside_calibration_segment"
    with pytest.raises(ValueError, match="unknown_contract"):
        query_time(db, CASE_ID, "2026-04-01T09:35:00Z", "veto")


def test_certification_is_idempotent_and_persisted(tmp_path):
    db, registered = _fixture_case(tmp_path)
    certify(db, registered["case_id"])
    report = build_report(db, registered["case_id"])
    certify(db, registered["case_id"])
    assert build_report(db, registered["case_id"]) == report
    conn = connect(db)
    counts = {table: conn.execute(
        f"SELECT count(*) FROM {table} WHERE case_id=?", (CASE_ID,)).fetchone()[0]
        for table in ("clock_anchor_verdict", "clock_timepoint", "clock_bridge",
                      "clock_segment", "clock_certificate")}
    conn.close()
    assert counts == {"clock_anchor_verdict": 13, "clock_timepoint": 10,
                      "clock_bridge": 7, "clock_segment": 5,
                      "clock_certificate": 8}


def test_structured_report_is_written_alongside_markdown(tmp_path):
    db, registered = _fixture_case(tmp_path)
    certify(db, registered["case_id"])
    from burstweave.clockproof import write_report
    out = tmp_path / "certificate.json"
    returned = write_report(db, registered["case_id"], out)
    assert returned == out and out.is_file()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["source_hash"] == registered["source_hash"]
    assert payload["parameters"]["min_independent_references"] == 2
    assert set(payload["contracts"]) == {"quorum-only", "master-fallback"}
    assert out.with_suffix(".md").is_file()


# --------------------------------------------------------------------------- #
# Step 3: the modified source must flip the verdict from the raw rows.
# --------------------------------------------------------------------------- #

def test_modified_raw_source_flips_support_set_and_bridges_gap(synthetic, tmp_path):
    rows = [json.loads(line) for line in POINTS.read_text(encoding="utf-8").splitlines()]
    original_b = next(row for row in rows if row["seq"] == 8)
    assert original_b["offset_seconds"] == 0
    original_b["offset_seconds"] = 180
    case = synthetic(tmp_path, "counterfactual", rows)
    report = certify(case.db, case.case_id)
    verdicts = {v["anchor_id"]: (v["reference_id"], v["verdict"])
                for v in report["anchor_verdicts"]
                if v["at"] == "2026-04-01T09:20:00Z"}
    assert verdicts == {7: ("A", "supported"), 8: ("B", "supported"),
                        9: ("C", "quarantined")}
    tp = next(t for t in report["contracts"]["quorum-only"]["timepoints"]
              if t["at"] == "2026-04-01T09:20:00Z")
    assert tp["supporting_anchor_ids"] == [7, 8]
    assert (tp["offset_low"], tp["offset_high"]) == (pytest.approx(179.0), pytest.approx(181.0))
    bridges = {(b["at_from"], b["at_to"]): b["connected"]
               for b in report["contracts"]["quorum-only"]["bridges"]}
    assert bridges[("2026-04-01T09:20:00Z", "2026-04-01T09:30:00Z")] is True
    c25 = next(c for c in report["contracts"]["quorum-only"]["certificates"]
               if c["at"] == "2026-04-01T09:25:00Z")
    assert c25["certified"]
    assert (c25["offset_low"], c25["offset_high"]) == (pytest.approx(178.0), pytest.approx(182.0))
    # It was genuinely recomputed from a different raw line, not relabelled.
    changed = next(v for v in report["anchor_verdicts"] if v["anchor_id"] == 8)
    assert json.loads(changed["raw_line"])["offset_seconds"] == 180
    assert changed["raw_hash"] != hashlib.sha256(
        json.dumps(original_b | {"offset_seconds": 0}).encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Supplementary boundaries: repeats, ties, timezone writings, equal limits.
# --------------------------------------------------------------------------- #

def test_duplicate_same_reference_rows_never_inflate_votes(synthetic, tmp_path):
    rows = [_anchor(1, "2026-05-01T00:00:00Z", "A", 0),
            _anchor(2, "2026-05-01T00:00:00Z", "A", 0),
            _anchor(3, "2026-05-01T00:00:00Z", "B", 0),
            _anchor(4, "2026-05-01T00:00:00Z", "C", 100)]
    case = synthetic(tmp_path, "dup-agree", rows)
    report = certify(case.db, case.case_id)
    tp = report["contracts"]["quorum-only"]["timepoints"][0]
    # Two identical A rows still count as ONE independent reference: A+B only.
    assert tp["status"] == "certified"
    assert tp["supporting_anchor_ids"] == [1, 2, 3]
    c_row = next(v for v in report["anchor_verdicts"] if v["anchor_id"] == 4)
    assert c_row["verdict"] == "quarantined" and c_row["reason"] == "incompatible_outlier"


def test_conflicting_duplicate_rows_invalidate_the_reference(synthetic, tmp_path):
    rows = [_anchor(1, "2026-05-01T00:10:00Z", "A", 0),
            _anchor(2, "2026-05-01T00:10:00Z", "A", 50),
            _anchor(3, "2026-05-01T00:10:00Z", "B", 0)]
    case = synthetic(tmp_path, "dup-conflict", rows)
    report = certify(case.db, case.case_id)
    assert all(v["reason"] == "duplicate_reference_conflict"
               for v in report["anchor_verdicts"] if v["reference_id"] == "A")
    tp = report["contracts"]["quorum-only"]["timepoints"][0]
    assert tp["status"] == "uncertified"
    assert report["contracts"]["master-fallback"]["timepoints"][0]["status"] == "uncertified"


def test_all_conflicting_repeats_leave_timestamp_uncertified(synthetic, tmp_path):
    rows = [_anchor(1, "2026-05-01T00:10:00Z", "A", 0),
            _anchor(2, "2026-05-01T00:10:00Z", "A", 50),
            _anchor(3, "2026-05-01T00:10:00Z", "B", 0),
            _anchor(4, "2026-05-01T00:10:00Z", "B", 60)]
    case = synthetic(tmp_path, "all-conflict", rows)
    report = certify(case.db, case.case_id)
    tp = report["contracts"]["quorum-only"]["timepoints"][0]
    assert tp["status"] == "uncertified"
    assert tp["basis"] == "below_independent_quorum"
    assert all(v["verdict"] == "quarantined" for v in report["anchor_verdicts"])


def test_tied_exclusive_maximum_support_sets_are_disputed(synthetic, tmp_path):
    rows = [_anchor(1, "2026-05-01T00:00:00Z", "A", 0),
            _anchor(2, "2026-05-01T00:00:00Z", "B", 0),
            _anchor(3, "2026-05-01T00:00:00Z", "C", 10),
            _anchor(4, "2026-05-01T00:00:00Z", "D", 10)]
    case = synthetic(tmp_path, "tie", rows, references=["A", "B", "C", "D"])
    report = certify(case.db, case.case_id)
    assert all(v["verdict"] == "disputed" for v in report["anchor_verdicts"])
    for contract in report["contracts"].values():
        assert contract["timepoints"][0]["status"] == "disputed"
        assert contract["segments"] == []
    # A dispute cannot be rescued by the master either.
    fallback = report["contracts"]["master-fallback"]["timepoints"][0]
    assert fallback["status"] == "disputed"


def test_overlapping_but_incomplete_cliques_still_yield_a_tie(synthetic, tmp_path):
    # A intersects both B and C, while B and C are mutually disjoint: the two
    # size-two cliques {A,B} and {A,C} tie, so nothing is certified.
    rows = [_anchor(1, "2026-05-01T00:00:00Z", "A", 0),
            _anchor(2, "2026-05-01T00:00:00Z", "B", 1.5),
            _anchor(3, "2026-05-01T00:00:00Z", "C", -1.5)]
    case = synthetic(tmp_path, "overlap-tie", rows)
    report = certify(case.db, case.case_id)
    assert all(v["verdict"] == "disputed" for v in report["anchor_verdicts"])
    assert report["contracts"]["quorum-only"]["timepoints"][0]["status"] == "disputed"


def test_timezone_equivalent_writings_share_one_timestamp(synthetic, tmp_path):
    rows = [_anchor(1, "2026-05-01T00:00:00+00:00", "A", 0),
            _anchor(2, "2026-05-01T00:00:00Z", "B", 0),
            _anchor(3, "2026-05-01T02:00:00+02:00", "C", 0)]
    case = synthetic(tmp_path, "tz", rows)
    report = certify(case.db, case.case_id)
    timepoints = report["contracts"]["quorum-only"]["timepoints"]
    assert len(timepoints) == 1
    assert timepoints[0]["at"] == "2026-05-01T00:00:00Z"
    assert timepoints[0]["supporting_anchor_ids"] == [1, 2, 3]


def test_master_fallback_matches_normalized_timezone_writing(synthetic, tmp_path):
    # The provisional master anchor is written with an offset timezone; the
    # fallback must still find it when timestamps are normalized to UTC.
    rows = [_anchor(1, "2026-05-01T00:00:00Z", "A", 0),
            _anchor(2, "2026-05-01T00:00:00Z", "B", 0),
            _anchor(3, "2026-05-01T02:10:00+02:00", "A", 0, master_attested=True),
            _anchor(4, "2026-05-01T00:20:00Z", "A", 0),
            _anchor(5, "2026-05-01T00:20:00Z", "B", 0)]
    case = synthetic(tmp_path, "tz-fallback", rows)
    report = certify(case.db, case.case_id)
    tp = next(t for t in report["contracts"]["master-fallback"]["timepoints"]
              if t["at"] == "2026-05-01T00:10:00Z")
    assert tp["status"] == "provisional"
    assert tp["supporting_anchor_ids"] == [3]


def test_bridge_limit_equality_keeps_segment_connected(synthetic, tmp_path):
    rows = [_anchor(1, "2026-05-01T00:00:00Z", "A", 0),
            _anchor(2, "2026-05-01T00:00:00Z", "B", 0),
            _anchor(3, "2026-05-01T00:15:00Z", "A", 0),
            _anchor(4, "2026-05-01T00:15:00Z", "B", 0)]
    case = synthetic(tmp_path, "bridge-eq", rows)
    report = certify(case.db, case.case_id)
    assert report["contracts"]["quorum-only"]["bridges"][0]["connected"] is True
    rows[2]["at"] = rows[3]["at"] = "2026-05-01T00:16:00Z"
    over = synthetic(tmp_path, "bridge-over", rows)
    over_report = certify(over.db, over.case_id)
    bridge = over_report["contracts"]["quorum-only"]["bridges"][0]
    assert bridge["connected"] is False and bridge["reason"] == "bridge_gap"


def test_drift_limit_equality_keeps_trajectory_continuous(synthetic, tmp_path):
    rows = [_anchor(1, "2026-05-01T00:00:00Z", "A", 0, uncertainty=0),
            _anchor(2, "2026-05-01T00:00:00Z", "B", 0, uncertainty=0),
            _anchor(3, "2026-05-01T00:10:00Z", "A", 2.0, uncertainty=0),
            _anchor(4, "2026-05-01T00:10:00Z", "B", 2.0, uncertainty=0),
            _event(5, "2026-05-01T00:05:00Z")]
    case = synthetic(tmp_path, "drift-eq", rows)
    report = certify(case.db, case.case_id)
    bridge = report["contracts"]["quorum-only"]["bridges"][0]
    assert bridge["connected"] is True
    midpoint = query_time(case.db, case.case_id, "2026-05-01T00:05:00Z")
    assert midpoint["offset_low"] == pytest.approx(1.0)
    assert midpoint["offset_high"] == pytest.approx(1.0)
    rows[2]["offset_seconds"] = rows[3]["offset_seconds"] = 2.01
    over = synthetic(tmp_path, "drift-over", rows)
    over_report = certify(over.db, over.case_id)
    bad = over_report["contracts"]["quorum-only"]["bridges"][0]
    assert bad["connected"] is False and bad["reason"] == "clock_jump"
    assert not over_report["contracts"]["quorum-only"]["certificates"][0]["certified"]


def test_uncertainty_intervals_are_closed_at_the_boundary(synthetic, tmp_path):
    # Touching at exactly one point still counts as a non-empty intersection.
    rows = [_anchor(1, "2026-05-01T00:00:00Z", "A", 0, uncertainty=1),
            _anchor(2, "2026-05-01T00:00:00Z", "B", 2, uncertainty=1)]
    case = synthetic(tmp_path, "closed-touch", rows)
    report = certify(case.db, case.case_id)
    tp = report["contracts"]["quorum-only"]["timepoints"][0]
    assert tp["status"] == "certified"
    assert tp["offset_low"] == pytest.approx(1.0)
    assert tp["offset_high"] == pytest.approx(1.0)
