import json
from pathlib import Path

import pytest

from burstweave.clockproof import register_design
from burstweave.engine import ingest, setup


ROOT = Path(__file__).resolve().parents[1]
DESIGN = ROOT / "fixtures/clockproof/design.json"
POINTS = ROOT / "fixtures/clockproof/points.jsonl"


def _db(tmp_path):
    db = tmp_path / "clock.db"
    setup(db, {"assets": ["A"], "sensors": [{"id": "S", "asset": "A", "role": "heat"}]})
    return db


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
