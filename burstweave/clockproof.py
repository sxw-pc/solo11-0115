"""Freeze multi-reference clock evidence before certifying any event time."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from .engine import dt, ingest, setup
from .store import connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS clock_design(
  case_id TEXT PRIMARY KEY, design_hash TEXT NOT NULL, raw TEXT NOT NULL,
  source_hash TEXT NOT NULL, anchor_ids TEXT NOT NULL);
"""


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


def main() -> int:
    import tempfile
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "clock.db"
        setup(db, {"assets": ["A"], "sensors": [{"id": "S", "asset": "A", "role": "heat"}]})
        count = ingest(db, root / "fixtures" / "clockproof" / "points.jsonl")
        result = register_design(db, root / "fixtures" / "clockproof" / "design.json")
    print(json.dumps({"ingested": count, "design": result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
