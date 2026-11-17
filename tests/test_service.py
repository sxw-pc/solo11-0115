from pathlib import Path

import pytest

from burstweave import service
from burstweave.store import connect


def incident(tmp_path):
    db = tmp_path/'ledger.db'
    with connect(db) as con:
        con.execute("INSERT INTO run(id,version,status) VALUES (1,'v1','已闭合')")
        for i in (0,1):
            asset = f'A{i}'
            con.execute('INSERT INTO asset VALUES (?)',(asset,))
            con.execute('''INSERT INTO episode(id,run_id,asset,start,end,status,replaces)
                VALUES (?,1,?,'2026-01-01T10:00:00Z',
                '2026-01-01T10:30:00Z','可信','[]')''',(i+1,asset))
    return db


def test_register_impact_observations(tmp_path):
    db = incident(tmp_path)
    fixture = Path(__file__).resolve().parents[1]/'fixtures'/'service-impact.json'
    result = service.register(db,'svc-A',1,fixture)
    assert result['assets'] == 2
    assert service.register(db,'svc-A',1,fixture) == result
    with connect(db) as con:
        assert con.execute('SELECT count(*) FROM service_impact').fetchone()[0] == 1


def test_reject_changed_or_unproven_impact(tmp_path):
    db = incident(tmp_path)
    fixture = Path(__file__).resolve().parents[1]/'fixtures'/'service-impact.json'
    service.register(db,'svc-A',1,fixture)
    changed = tmp_path/'changed.json'
    changed.write_text(fixture.read_text().replace('"episode_id":2','"episode_id":3'))
    with pytest.raises(ValueError,match='unproven_impact_episode'):
        service.register(db,'svc-B',1,changed)
    changed.write_text(fixture.read_text().replace('10:05:00Z','10:06:00Z'))
    with pytest.raises(ValueError,match='service_impact_conflict'):
        service.register(db,'svc-A',1,changed)
