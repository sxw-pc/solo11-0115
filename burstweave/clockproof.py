"""Freeze multi-reference clock evidence before certifying any event time.

The design registered by :func:`register_design` freezes the raw JSONL source,
the allowed independent references and the drift/bridge contract.  All
certification below is recomputed from the raw reference rows stored by
:func:`burstweave.engine.ingest` (never from pre-filled answers):

* each ``reference_id`` contributes at most one independent support vote at a
  raw timestamp; closed offset intervals must share a non-empty intersection;
* the unique largest independent support set reaching the quorum certifies the
  timestamp, equal-sized exclusive maximum sets are a dispute and the master
  may only attest a provisional timestamp under the master-fallback contract;
* adjacent certified timestamps form continuous calibration segments when the
  bridge and drift contracts allow a continuous offset trajectory.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import timedelta
from pathlib import Path

from .engine import dt, ingest, setup
from .store import connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS clock_design(
  case_id TEXT PRIMARY KEY, design_hash TEXT NOT NULL, raw TEXT NOT NULL,
  source_hash TEXT NOT NULL, anchor_ids TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS clock_anchor_verdict(
  case_id TEXT NOT NULL, anchor_id INTEGER NOT NULL, point_id INTEGER NOT NULL,
  source_id INTEGER NOT NULL, line_no INTEGER NOT NULL, at TEXT NOT NULL,
  reference_id TEXT NOT NULL, master_attested INTEGER NOT NULL,
  offset_seconds REAL NOT NULL, uncertainty_seconds REAL NOT NULL,
  interval_low REAL NOT NULL, interval_high REAL NOT NULL,
  verdict TEXT NOT NULL, reason TEXT NOT NULL, detail TEXT NOT NULL,
  PRIMARY KEY(case_id, anchor_id));
CREATE TABLE IF NOT EXISTS clock_timepoint(
  case_id TEXT NOT NULL, contract TEXT NOT NULL, at TEXT NOT NULL,
  status TEXT NOT NULL, basis TEXT NOT NULL,
  offset_low REAL, offset_high REAL,
  supporting_anchor_ids TEXT NOT NULL, quarantined_anchor_ids TEXT NOT NULL,
  PRIMARY KEY(case_id, contract, at));
CREATE TABLE IF NOT EXISTS clock_bridge(
  case_id TEXT NOT NULL, contract TEXT NOT NULL, at_from TEXT NOT NULL,
  at_to TEXT NOT NULL, connected INTEGER NOT NULL, reason TEXT NOT NULL,
  detail TEXT NOT NULL, PRIMARY KEY(case_id, contract, at_from, at_to));
CREATE TABLE IF NOT EXISTS clock_segment(
  case_id TEXT NOT NULL, contract TEXT NOT NULL, segment_id INTEGER NOT NULL,
  at_from TEXT NOT NULL, at_to TEXT NOT NULL,
  anchor_ids TEXT NOT NULL, PRIMARY KEY(case_id, contract, segment_id));
CREATE TABLE IF NOT EXISTS clock_certificate(
  case_id TEXT NOT NULL, contract TEXT NOT NULL, point_id INTEGER NOT NULL,
  device TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL, at TEXT NOT NULL,
  certified INTEGER NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL,
  segment_id INTEGER, offset_low REAL, offset_high REAL,
  utc_low TEXT, utc_high TEXT, anchor_ids TEXT NOT NULL,
  PRIMARY KEY(case_id, contract, point_id));
"""

CONTRACTS = ("quorum-only", "master-fallback")
_EPS = 1e-9


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def register_design(db_path: str | Path, design_path: str | Path) -> dict:
    root = Path(__file__).resolve().parents[1]
    path = Path(design_path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("clock design must be in repository")
    raw = path.read_bytes()
    doc = json.loads(raw.decode("utf-8-sig"))
    if set(doc) != {"case_id", "source_path", "source_sha256", "device",
                    "references", "master_reference", "max_drift_seconds_per_minute",
                    "min_independent_references", "max_bridge_minutes"}:
        raise ValueError("clock design fields differ")
    if not doc["case_id"] or not doc["device"]:
        raise ValueError("clock identity required")
    references = doc["references"]
    if (not isinstance(references, list) or len(set(references)) != len(references)
            or len(references) < 2 or any(not isinstance(x, str) or not x for x in references)):
        raise ValueError("independent clock references required")
    if doc["master_reference"] not in references:
        raise ValueError("master must be a listed reference")
    drift = doc["max_drift_seconds_per_minute"]
    quorum = doc["min_independent_references"]
    bridge = doc["max_bridge_minutes"]
    if (isinstance(drift, bool) or not isinstance(drift, (int, float))
            or not math.isfinite(drift) or drift < 0 or isinstance(quorum, bool)
            or not isinstance(quorum, int) or not 2 <= quorum <= len(references)
            or isinstance(bridge, bool) or not isinstance(bridge, (int, float))
            or not math.isfinite(bridge) or bridge <= 0):
        raise ValueError("invalid clock design limits")
    source_path = (root / doc["source_path"]).resolve()
    if not source_path.is_relative_to(root) or not source_path.is_file() or source_path.is_symlink():
        raise ValueError("clock source unavailable")
    source_raw = source_path.read_bytes()
    if _sha(source_raw) != doc["source_sha256"]:
        raise ValueError("clock source bytes changed")
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        source = db.execute("SELECT id FROM source WHERE hash=?", (doc["source_sha256"],)).fetchone()
        if not source:
            raise ValueError("ingest clock source first")
        anchors = []
        for point in db.execute("SELECT * FROM point WHERE source_id=? AND device=? AND kind='anchor' ORDER BY at,id",
                                (source["id"], doc["device"])):
            item = json.loads(point["raw"])
            ref = item.get("reference_id")
            offset = item.get("offset_seconds")
            uncertainty = item.get("uncertainty_seconds")
            if (ref not in references or isinstance(offset, bool)
                    or not isinstance(offset, (int, float)) or not math.isfinite(offset)
                    or isinstance(uncertainty, bool)
                    or not isinstance(uncertainty, (int, float))
                    or not math.isfinite(uncertainty) or uncertainty < 0):
                raise ValueError("invalid reference anchor")
            dt(item["at"])
            anchors.append(point["id"])
        if not anchors:
            raise ValueError("no reference anchors")
        with db:
            old = db.execute("SELECT design_hash FROM clock_design WHERE case_id=?",
                             (doc["case_id"],)).fetchone()
            if old and old["design_hash"] != _sha(raw):
                raise ValueError("clock design identity conflict")
            db.execute("INSERT OR IGNORE INTO clock_design VALUES (?,?,?,?,?)",
                       (doc["case_id"], _sha(raw), raw.decode("utf-8-sig"),
                        doc["source_sha256"], json.dumps(anchors)))
    finally:
        db.close()
    return {"case_id": doc["case_id"], "design_hash": _sha(raw),
            "source_hash": doc["source_sha256"], "anchor_count": len(anchors),
            "anchor_ids": anchors}


# --------------------------------------------------------------------------- #
# Evidence loading: everything is recomputed from the frozen raw source rows.
# --------------------------------------------------------------------------- #

def _load_case(db_path: str | Path, case_id: str) -> dict:
    db = connect(db_path)
    try:
        row = db.execute("SELECT * FROM clock_design WHERE case_id=?", (case_id,)).fetchone()
        if not row:
            raise ValueError("unknown_clock_case")
        design = json.loads(row["raw"])
        frozen_ids = json.loads(row["anchor_ids"])
        source = db.execute("SELECT * FROM source WHERE hash=?", (row["source_hash"],)).fetchone()
        if not source:
            raise ValueError("clock source missing")
        if _sha(source["body"]) != row["source_hash"]:
            raise ValueError("clock source bytes changed")
        anchors = []
        for point in db.execute(
                "SELECT p.*, s.hash AS source_hash FROM point p JOIN source s ON s.id=p.source_id "
                "WHERE p.source_id=? AND p.device=? AND p.kind='anchor' ORDER BY p.at,p.id",
                (source["id"], design["device"])):
            if point["id"] not in frozen_ids:
                raise ValueError("anchor set differs from frozen design")
            if _sha(point["raw"].encode()) != point["hash"]:
                raise ValueError("point raw line changed")
            item = json.loads(point["raw"])
            anchors.append({
                "anchor_id": point["id"], "point_id": point["id"],
                "source_id": source["id"], "line_no": point["line_no"],
                "at": point["at"], "instant": dt(point["at"]),
                "reference_id": item["reference_id"],
                "master_attested": bool(item.get("master_attested", False)),
                "offset_seconds": float(item["offset_seconds"]),
                "uncertainty_seconds": float(item["uncertainty_seconds"]),
                "raw_line": point["raw"], "raw_hash": point["hash"],
                "source_hash": source["hash"],
            })
        if sorted(a["anchor_id"] for a in anchors) != sorted(frozen_ids):
            raise ValueError("anchor set differs from frozen design")
        events = []
        for point in db.execute(
                "SELECT * FROM point WHERE source_id=? AND device=? AND kind!='anchor' ORDER BY at,id",
                (source["id"], design["device"])):
            if _sha(point["raw"].encode()) != point["hash"]:
                raise ValueError("point raw line changed")
            events.append({"point_id": point["id"], "device": point["device"],
                           "seq": point["seq"], "kind": point["kind"],
                           "at": point["at"], "instant": dt(point["at"]),
                           "raw_line": point["raw"], "raw_hash": point["hash"]})
        return {"design": design, "source_hash": source["hash"],
                "design_hash": row["design_hash"], "anchors": anchors, "events": events}
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Step 1: per-timestamp independent reference arbitration.
# --------------------------------------------------------------------------- #

def _intersect_intervals(intervals):
    low = max(x[0] for x in intervals)
    high = min(x[1] for x in intervals)
    return (low, high) if low <= high + _EPS else None


def _arbitrate(case: dict) -> list[dict]:
    """Arbitrate every raw anchor timestamp; contract-independent facts."""
    design = case["design"]
    groups: dict = {}
    for anchor in case["anchors"]:
        # Group by the parsed instant so timezone-equivalent writings
        # (Z / +00:00 / +02:00) describe one and the same raw timestamp.
        groups.setdefault(_iso(anchor["instant"]), []).append(anchor)

    timepoints = []
    for at in sorted(groups, key=lambda x: dt(x)):
        rows = groups[at]
        # One reference contributes at most one independent support: repeated
        # rows must never inflate the vote.  Conflicting repeats invalidate
        # that reference for this timestamp.
        per_ref: dict = {}
        for anchor in rows:
            interval = (anchor["offset_seconds"] - anchor["uncertainty_seconds"],
                        anchor["offset_seconds"] + anchor["uncertainty_seconds"])
            anchor["interval"] = interval
            per_ref.setdefault(anchor["reference_id"], []).append(anchor)

        ref_intervals: dict = {}
        conflicted_refs = set()
        for ref, items in per_ref.items():
            common = _intersect_intervals([x["interval"] for x in items])
            if common is None:
                conflicted_refs.add(ref)
            else:
                ref_intervals[ref] = common

        usable = {ref: iv for ref, iv in ref_intervals.items() if ref not in conflicted_refs}
        # Largest independent support sets: every reference subset whose closed
        # intervals share one non-empty common intersection.  Enumerating the
        # subsets keeps the maximum-set selection exact for arbitrary overlap
        # patterns (greedy growth can miss a larger clique).
        refs = sorted(usable)
        intersecting = []
        for mask in range(1, 1 << len(refs)):
            members = tuple(refs[i] for i in range(len(refs)) if mask & (1 << i))
            if _intersect_intervals([usable[r] for r in members]) is not None:
                intersecting.append(members)
        max_size = max((len(m) for m in intersecting), default=0)
        best_sets = [list(m) for m in intersecting if len(m) == max_size]

        winner = None
        certified_interval = None
        if len(best_sets) == 1 and len(best_sets[0]) >= design["min_independent_references"]:
            winner = best_sets[0]
            certified_interval = _intersect_intervals([usable[r] for r in winner])

        verdicts = []
        for anchor in rows:
            ref = anchor["reference_id"]
            if ref in conflicted_refs:
                verdict, reason = "quarantined", "duplicate_reference_conflict"
                detail = (f"reference {ref} repeats at {at} with disjoint offset "
                          "intervals; its votes are invalid")
            elif winner is not None:
                if ref in winner:
                    verdict, reason = "supported", "independent_support"
                    detail = (f"reference {ref} belongs to the unique maximum support "
                              f"set {winner} of {len(winner)} independent references")
                else:
                    verdict, reason = "quarantined", "incompatible_outlier"
                    detail = (f"reference {ref} interval {anchor['interval']} is "
                              f"incompatible with certified interval {certified_interval}")
            elif len(best_sets) > 1 and len(best_sets[0]) >= design["min_independent_references"]:
                verdict, reason = "disputed", "tied_maximum_support_sets"
                detail = (f"equal-sized exclusive maximum support sets {best_sets}; "
                          "no side is chosen and no average is taken")
            else:
                verdict, reason = "uncertified", "below_independent_quorum"
                size = best_sets[0] and len(best_sets[0]) or 0
                detail = (f"largest support set has {size} references, "
                          f"quorum requires {design['min_independent_references']}")
            verdicts.append({
                "anchor_id": anchor["anchor_id"], "point_id": anchor["point_id"],
                "source_id": anchor["source_id"], "line_no": anchor["line_no"],
                "at": at, "reference_id": ref,
                "master_attested": anchor["master_attested"],
                "offset_seconds": anchor["offset_seconds"],
                "uncertainty_seconds": anchor["uncertainty_seconds"],
                "interval_low": anchor["interval"][0],
                "interval_high": anchor["interval"][1],
                "raw_line": anchor["raw_line"], "raw_hash": anchor["raw_hash"],
                "source_hash": anchor["source_hash"],
                "verdict": verdict, "reason": reason, "detail": detail,
            })

        supporting = sorted(a["anchor_id"] for a in rows
                            if winner is not None and a["reference_id"] in winner
                            and a["reference_id"] not in conflicted_refs)
        quarantined = sorted(v["anchor_id"] for v in verdicts if v["verdict"] == "quarantined")

        if winner is not None:
            tp_status, basis = "certified", "independent_quorum"
        elif best_sets and len(best_sets) > 1 and len(best_sets[0]) >= design["min_independent_references"]:
            tp_status, basis = "disputed", "tied_maximum_support_sets"
        else:
            tp_status, basis = "uncertified", "below_independent_quorum"

        timepoints.append({
            "at": at, "instant": dt(at), "status": tp_status, "basis": basis,
            "offset_low": certified_interval[0] if certified_interval else None,
            "offset_high": certified_interval[1] if certified_interval else None,
            "supporting_anchor_ids": supporting,
            "quarantined_anchor_ids": quarantined,
            "anchor_verdicts": verdicts,
            "best_sets": best_sets,
            "conflicted_refs": sorted(conflicted_refs),
        })
    return timepoints


def _master_fallback(case: dict, timepoints: list[dict]) -> dict | None:
    """Provisional single-master certification, only under explicit rules."""
    design = case["design"]
    master = design["master_reference"]
    by_at = {tp["at"]: tp for tp in timepoints}
    rows_by_at: dict = {}
    for anchor in case["anchors"]:
        rows_by_at.setdefault(_iso(anchor["instant"]), []).append(anchor)

    provisional = {}
    for at, rows in rows_by_at.items():
        master_rows = [a for a in rows if a["reference_id"] == master]
        if not master_rows:
            continue
        tp = by_at[at]
        if tp["status"] == "certified":
            continue  # quorum certification stands on its own
        if tp["status"] == "disputed":
            continue  # a dispute cannot be settled by the master alone
        if any(not a["master_attested"] for a in master_rows):
            continue  # fallback requires an explicit master_attested reading
        master_interval = _intersect_intervals([a["interval"] for a in master_rows])
        if master_interval is None:
            continue  # conflicting master repeats
        contradicted = any(
            a["reference_id"] != master
            and a["reference_id"] not in tp["conflicted_refs"]
            and _intersect_intervals([a["interval"], master_interval]) is None
            for a in rows)
        if contradicted:
            continue
        provisional[at] = master_interval
    return provisional


# --------------------------------------------------------------------------- #
# Step 2: continuous calibration segments and bounded UTC queries.
# --------------------------------------------------------------------------- #

def _bridge_ok(left: dict, right: dict, design: dict) -> tuple[bool, str, str]:
    gap_minutes = (right["instant"] - left["instant"]).total_seconds() / 60.0
    if gap_minutes > design["max_bridge_minutes"] + _EPS:
        return (False, "bridge_gap",
                f"gap {gap_minutes:g} min exceeds max_bridge_minutes "
                f"{design['max_bridge_minutes']:g}")
    allowance = design["max_drift_seconds_per_minute"] * gap_minutes
    # A continuous trajectory exists iff some pair (o1, o2) in the two closed
    # intervals satisfies |o2-o1| <= allowance: L2 <= H1+a and L1 <= H2+a.
    if right["offset_low"] - (left["offset_high"] + allowance) > _EPS:
        return (False, "clock_jump",
                "no continuous offset trajectory under "
                f"max_drift_seconds_per_minute={design['max_drift_seconds_per_minute']:g} "
                f"and allowance {allowance:g}s over {gap_minutes:g} min")
    if left["offset_low"] - (right["offset_high"] + allowance) > _EPS:
        return (False, "clock_jump",
                "no continuous offset trajectory under "
                f"max_drift_seconds_per_minute={design['max_drift_seconds_per_minute']:g} "
                f"and allowance {allowance:g}s over {gap_minutes:g} min")
    return (True, "continuous",
            f"gap {gap_minutes:g} min within bridge limit and drift allowance {allowance:g}s")


def _build_segments(case: dict, certified: list[dict]) -> tuple[list[dict], list[dict]]:
    design = case["design"]
    segments: list[dict] = []
    bridges: list[dict] = []
    current: list[dict] = []
    for tp in certified:
        if not current:
            current = [tp]
            continue
        ok, reason, detail = _bridge_ok(current[-1], tp, design)
        bridges.append({"at_from": current[-1]["at"], "at_to": tp["at"],
                        "connected": ok, "reason": reason, "detail": detail})
        if ok:
            current.append(tp)
        else:
            segments.append(current)
            current = [tp]
    if current:
        segments.append(current)
    return segments, bridges


def _offset_feasible(segment: list[dict], instant, design: dict):
    """Intersect the drift constraints of every bridge covering the query.

    Each certified anchor contributes its closed offset interval, and each
    bridge to an adjacent anchor contributes that interval expanded by the
    drift allowance over the elapsed minutes.  The query is bounded by the
    intersection of *all* applicable constraints, so an interior provisional
    anchor (master fallback) tightens the range instead of being skipped.
    """
    drift = design["max_drift_seconds_per_minute"]
    low, high = -math.inf, math.inf
    for tp in segment:
        minutes = abs((instant - tp["instant"]).total_seconds()) / 60.0
        allowance = drift * minutes
        low = max(low, tp["offset_low"] - allowance)
        high = min(high, tp["offset_high"] + allowance)
    if low > high + _EPS:
        return None
    return low, high


def _iso(instant) -> str:
    return instant.astimezone().isoformat().replace("+00:00", "Z")


def _round(value):
    if value is None:
        return None
    return round(float(value), 9)


def _certify_contract(case: dict, timepoints: list[dict], contract: str,
                      provisional: dict | None) -> dict:
    design = case["design"]
    certified = []
    for tp in timepoints:
        entry = dict(tp)
        if tp["status"] == "certified":
            entry["basis"] = "independent_quorum"
            certified.append(entry)
        elif contract == "master-fallback" and tp["at"] in (provisional or {}):
            low, high = provisional[tp["at"]]
            entry["status"] = "provisional"
            entry["basis"] = "master_fallback"
            entry["offset_low"], entry["offset_high"] = low, high
            entry["supporting_anchor_ids"] = sorted(
                a["anchor_id"] for a in case["anchors"]
                if _iso(a["instant"]) == tp["at"]
                and a["reference_id"] == design["master_reference"])
            certified.append(entry)
    certified.sort(key=lambda x: x["instant"])
    segments, bridges = _build_segments(case, certified)

    segment_records = []
    point_certificates = []
    for segment in segments:
        anchor_ids = sorted(i for tp in segment for i in tp["supporting_anchor_ids"])
        segment_records.append({
            "segment_id": len(segment_records), "at_from": segment[0]["at"],
            "at_to": segment[-1]["at"],
            "anchor_ids": anchor_ids, "timepoints": segment,
        })

    for event in case["events"]:
        located = None
        for rec in segment_records:
            if rec["timepoints"][0]["instant"] <= event["instant"] <= rec["timepoints"][-1]["instant"]:
                located = rec
                break
        record = {"point_id": event["point_id"], "device": event["device"],
                  "seq": event["seq"], "kind": event["kind"], "at": event["at"],
                  "anchor_ids": []}
        if located is None:
            record.update(certified=False, status="uncertified",
                          reason="outside_calibration_segment",
                          segment_id=None, offset_low=None, offset_high=None,
                          utc_low=None, utc_high=None)
        else:
            feasible = _offset_feasible(located["timepoints"], event["instant"], design)
            if feasible is None:
                record.update(certified=False, status="uncertified",
                              reason="empty_feasible_offset_intersection",
                              segment_id=located["segment_id"],
                              offset_low=None, offset_high=None,
                              utc_low=None, utc_high=None)
            else:
                low, high = feasible
                record.update(
                    certified=True,
                    status="provisional" if any(tp["basis"] == "master_fallback"
                                                for tp in located["timepoints"]) else "certified",
                    reason="bounded_utc_derived",
                    segment_id=located["segment_id"],
                    offset_low=_round(low), offset_high=_round(high),
                    utc_low=_iso(event["instant"] - timedelta(seconds=high)),
                    utc_high=_iso(event["instant"] - timedelta(seconds=low)),
                    anchor_ids=located["anchor_ids"])
        point_certificates.append(record)

    timepoint_rows = []
    for tp in timepoints:
        row = {k: tp[k] for k in ("at", "status", "basis", "offset_low", "offset_high",
                                  "supporting_anchor_ids", "quarantined_anchor_ids")}
        row["offset_low"] = _round(row["offset_low"])
        row["offset_high"] = _round(row["offset_high"])
        if row["status"] == "uncertified" and tp["at"] in (provisional or {}) \
                and contract == "master-fallback":
            row["status"], row["basis"] = "provisional", "master_fallback"
            row["offset_low"], row["offset_high"] = provisional[tp["at"]]
            row["supporting_anchor_ids"] = sorted(
                a["anchor_id"] for a in case["anchors"]
                if _iso(a["instant"]) == tp["at"]
                and a["reference_id"] == design["master_reference"])
        timepoint_rows.append(row)

    return {"contract": contract, "timepoints": timepoint_rows,
            "bridges": bridges, "segments": segment_records,
            "certificates": point_certificates}


# --------------------------------------------------------------------------- #
# Public certification API and SQLite persistence.
# --------------------------------------------------------------------------- #

def certify(db_path: str | Path, case_id: str) -> dict:
    """Recompute both contracts from raw rows and persist every verdict."""
    case = _load_case(db_path, case_id)
    timepoints = _arbitrate(case)
    provisional = _master_fallback(case, timepoints)
    contract_results = {
        name: _certify_contract(case, timepoints, name, provisional) for name in CONTRACTS}

    db = connect(db_path)
    try:
        with db:
            for table in ("clock_certificate", "clock_segment", "clock_bridge",
                          "clock_timepoint", "clock_anchor_verdict"):
                db.execute(f"DELETE FROM {table} WHERE case_id=?", (case_id,))
            for tp in timepoints:
                for verdict in tp["anchor_verdicts"]:
                    db.execute(
                        "INSERT INTO clock_anchor_verdict VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (case_id, verdict["anchor_id"], verdict["point_id"],
                         verdict["source_id"], verdict["line_no"], verdict["at"],
                         verdict["reference_id"], int(verdict["master_attested"]),
                         verdict["offset_seconds"], verdict["uncertainty_seconds"],
                         _round(verdict["interval_low"]), _round(verdict["interval_high"]),
                         verdict["verdict"], verdict["reason"], verdict["detail"]))
            for name, result in contract_results.items():
                for tp in result["timepoints"]:
                    db.execute("INSERT INTO clock_timepoint VALUES (?,?,?,?,?,?,?,?,?)",
                               (case_id, name, tp["at"], tp["status"], tp["basis"],
                                tp["offset_low"], tp["offset_high"],
                                json.dumps(tp["supporting_anchor_ids"]),
                                json.dumps(tp["quarantined_anchor_ids"])))
                for bridge in result["bridges"]:
                    db.execute("INSERT INTO clock_bridge VALUES (?,?,?,?,?,?,?)",
                               (case_id, name, bridge["at_from"], bridge["at_to"],
                                int(bridge["connected"]), bridge["reason"], bridge["detail"]))
                for segment in result["segments"]:
                    db.execute("INSERT INTO clock_segment VALUES (?,?,?,?,?,?)",
                               (case_id, name, segment["segment_id"],
                                segment["at_from"], segment["at_to"],
                                json.dumps(segment["anchor_ids"])))
                for cert in result["certificates"]:
                    db.execute("INSERT INTO clock_certificate VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (case_id, name, cert["point_id"], cert["device"], cert["seq"],
                                cert["kind"], cert["at"], int(cert["certified"]),
                                cert["status"], cert["reason"], cert["segment_id"],
                                cert["offset_low"], cert["offset_high"],
                                cert["utc_low"], cert["utc_high"],
                                json.dumps(cert["anchor_ids"])))
    finally:
        db.close()

    return build_report(db_path, case_id)


def build_report(db_path: str | Path, case_id: str) -> dict:
    """Structured, item-by-item recomputable report for a frozen case."""
    case = _load_case(db_path, case_id)
    db = connect(db_path)
    try:
        anchor_verdicts = [dict(x) for x in db.execute(
            "SELECT * FROM clock_anchor_verdict WHERE case_id=? ORDER BY at,anchor_id",
            (case_id,))]
        for row in anchor_verdicts:
            row["master_attested"] = bool(row["master_attested"])
            point = db.execute("SELECT raw,hash FROM point WHERE id=?",
                               (row["point_id"],)).fetchone()
            row["raw_line"] = point["raw"]
            row["raw_hash"] = point["hash"]
            source = db.execute("SELECT hash FROM source WHERE id=?",
                                (row["source_id"],)).fetchone()
            row["source_hash"] = source["hash"]
        contracts = {}
        for name in CONTRACTS:
            timepoints = [dict(x) for x in db.execute(
                "SELECT * FROM clock_timepoint WHERE case_id=? AND contract=? ORDER BY at",
                (case_id, name))]
            for row in timepoints:
                row["supporting_anchor_ids"] = json.loads(row["supporting_anchor_ids"])
                row["quarantined_anchor_ids"] = json.loads(row["quarantined_anchor_ids"])
            bridges = [dict(x) for x in db.execute(
                "SELECT * FROM clock_bridge WHERE case_id=? AND contract=? ORDER BY at_from",
                (case_id, name))]
            for row in bridges:
                row["connected"] = bool(row["connected"])
            segments = [dict(x) for x in db.execute(
                "SELECT * FROM clock_segment WHERE case_id=? AND contract=? ORDER BY segment_id",
                (case_id, name))]
            for row in segments:
                row["anchor_ids"] = json.loads(row["anchor_ids"])
            certificates = [dict(x) for x in db.execute(
                "SELECT * FROM clock_certificate WHERE case_id=? AND contract=? ORDER BY at,point_id",
                (case_id, name))]
            for row in certificates:
                row["certified"] = bool(row["certified"])
                row["anchor_ids"] = json.loads(row["anchor_ids"])
            contracts[name] = {"timepoints": timepoints, "bridges": bridges,
                               "segments": segments, "certificates": certificates}
    finally:
        db.close()
    design = case["design"]
    return {
        "case_id": case_id,
        "device": design["device"],
        "design_hash": case["design_hash"],
        "source_hash": case["source_hash"],
        "parameters": {
            "references": list(design["references"]),
            "master_reference": design["master_reference"],
            "min_independent_references": design["min_independent_references"],
            "max_bridge_minutes": design["max_bridge_minutes"],
            "max_drift_seconds_per_minute": design["max_drift_seconds_per_minute"],
        },
        "anchor_verdicts": anchor_verdicts,
        "contracts": contracts,
    }


def write_report(db_path: str | Path, case_id: str, out_path: str | Path) -> Path:
    report = build_report(db_path, case_id)
    out = Path(out_path)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
                   encoding="utf-8")
    lines = ["# 多参考时钟校准资格证书", "",
             f"案例：{report['case_id']}　设备：{report['device']}",
             f"来源哈希：{report['source_hash']}",
             f"设计哈希：{report['design_hash']}", "",
             "## 逐锚仲裁"]
    for verdict in report["anchor_verdicts"]:
        lines.append(
            f"- anchor {verdict['anchor_id']}（行 {verdict['line_no']}，{verdict['at']}，"
            f"参考 {verdict['reference_id']}，master_attested={verdict['master_attested']}）："
            f"偏移闭区间 [{verdict['interval_low']:g}, {verdict['interval_high']:g}] 秒 → "
            f"{verdict['verdict']} / {verdict['reason']}；{verdict['detail']}")
    for name, result in report["contracts"].items():
        lines += ["", f"## 合同：{name}", "", "### 校准段"]
        for segment in result["segments"]:
            lines.append(f"- 段 {segment['segment_id']}：{segment['at_from']} → "
                         f"{segment['at_to']}，支持锚 {segment['anchor_ids']}")
        lines.append("### 桥接判定")
        for bridge in result["bridges"]:
            lines.append(f"- {bridge['at_from']} → {bridge['at_to']}："
                         f"{'可连接' if bridge['connected'] else '断开'} / {bridge['reason']}")
        lines.append("### 事件证书")
        for cert in result["certificates"]:
            if cert["certified"]:
                lines.append(
                    f"- point {cert['point_id']}（{cert['at']}）：{cert['status']}，"
                    f"偏移可行区间 [{cert['offset_low']:g}, {cert['offset_high']:g}] 秒，"
                    f"真实 UTC ∈ [{cert['utc_low']}, {cert['utc_high']}]，"
                    f"段 {cert['segment_id']}，锚 {cert['anchor_ids']}")
            else:
                lines.append(f"- point {cert['point_id']}（{cert['at']}）：未核证 / {cert['reason']}")
    out.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def query_time(db_path: str | Path, case_id: str, at: str,
               contract: str = "quorum-only") -> dict:
    """Ad-hoc bounded UTC query, recomputed from the frozen certified anchors."""
    if contract not in CONTRACTS:
        raise ValueError("unknown_contract")
    case = _load_case(db_path, case_id)
    instant = dt(at)
    timepoints = _arbitrate(case)
    provisional = _master_fallback(case, timepoints)
    result = _certify_contract(case, timepoints, contract, provisional)
    # Segment timepoint dicts already carry the parsed instants.
    segment = next((seg["timepoints"] for seg in result["segments"]
                    if seg["timepoints"][0]["instant"] <= instant <= seg["timepoints"][-1]["instant"]),
                   None)
    base = {"case_id": case_id, "contract": contract, "at": _iso(instant)}
    if segment is None:
        return {**base, "certified": False, "reason": "outside_calibration_segment"}
    feasible = _offset_feasible(segment, instant, case["design"])
    if feasible is None:
        return {**base, "certified": False, "reason": "empty_feasible_offset_intersection"}
    low, high = feasible
    anchor_ids = sorted(i for tp in segment for i in tp["supporting_anchor_ids"])
    basis = ("master_fallback"
             if any(tp["basis"] == "master_fallback" for tp in segment)
             else "independent_quorum")
    return {**base, "certified": True, "status": basis,
            "offset_low": _round(low), "offset_high": _round(high),
            "utc_low": _iso(instant - timedelta(seconds=high)),
            "utc_high": _iso(instant - timedelta(seconds=low)),
            "anchor_ids": anchor_ids,
            "segment_from": segment[0]["at"], "segment_to": segment[-1]["at"]}


def main() -> int:
    import tempfile
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "clock.db"
        setup(db, {"assets": ["A"], "sensors": [{"id": "S", "asset": "A", "role": "heat"}]})
        count = ingest(db, root / "fixtures" / "clockproof" / "points.jsonl")
        registered = register_design(db, root / "fixtures" / "clockproof" / "design.json")
        report = certify(db, registered["case_id"])
    print(json.dumps({"ingested": count, "design": registered,
                      "certificate": report}, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
