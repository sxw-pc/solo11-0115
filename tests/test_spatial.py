from pathlib import Path

import pytest

from burstweave import engine, spatial
from burstweave.store import connect

FIX = Path(__file__).resolve().parents[1]/'fixtures'


def prepared(tmp_path):
    db = tmp_path/'db.sqlite'
    engine.setup(db, {'assets':['A'], 'sensors':[
        {'id':s,'asset':'A','role':'environment'} for s in ('S00','S10','S01','S11')]})
    assert engine.ingest(db, FIX/'spatial-points.jsonl') == 4
    return db


def test_freeze_real_sensor_points(tmp_path):
    db = prepared(tmp_path)
    result = spatial.register(db, 'grid-A', FIX/'spatial-grid.json')
    assert result['point_count'] == 4
    assert spatial.register(db, 'grid-A', FIX/'spatial-grid.json') == result
    with connect(db) as con:
        assert con.execute('SELECT count(*) FROM spatial_survey').fetchone()[0] == 1


def test_changed_grid_cannot_reuse_identity(tmp_path):
    db = prepared(tmp_path)
    spatial.register(db, 'grid-A', FIX/'spatial-grid.json')
    changed = tmp_path/'changed.json'
    changed.write_text((FIX/'spatial-grid.json').read_text().replace('"threshold":5','"threshold":6'))
    with pytest.raises(ValueError, match='spatial_survey_conflict'):
        spatial.register(db, 'grid-A', changed)
