from pathlib import Path

from burstweave.engine import ingest,setup
from burstweave.parity import register
from burstweave.store import connect
import json
import pytest


FIXTURES=Path(__file__).resolve().parents[1]/'fixtures'


def test_parity_case_freezes_two_real_points_and_two_frames(tmp_path):
    db=tmp_path/'telemetry.db'
    setup(db,json.loads((FIXTURES/'parity-contract.json').read_text()))
    assert ingest(db,FIXTURES/'parity-points.jsonl')==2
    got=register(db,'block-17',FIXTURES/'parity-case.json')
    assert got['observed_count']==2
    assert register(db,'block-17',FIXTURES/'parity-case.json')==got
    with connect(db) as conn:
        assert conn.execute('SELECT COUNT(*) FROM parity_case').fetchone()[0]==1
        assert conn.execute('SELECT COUNT(*) FROM point').fetchone()[0]==2


def test_parity_case_rejects_empty_block(tmp_path):
    db=tmp_path/'telemetry.db'
    setup(db,json.loads((FIXTURES/'parity-contract.json').read_text()))
    with pytest.raises(ValueError,match='empty_parity_block'):
        register(db,'block-17',FIXTURES/'parity-case.json')
