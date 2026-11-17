from pathlib import Path

import pytest

from burstweave import repair
from burstweave.store import connect


def closed_orders(tmp_path):
    db = tmp_path/'ledger.db'
    with connect(db) as con:
        con.execute("INSERT INTO run(id,version,status) VALUES (1,'v1','已闭合')")
        for i in (0,1):
            asset = f'A{i}'
            con.execute('INSERT INTO asset VALUES (?)',(asset,))
            eid = con.execute('''INSERT INTO episode(run_id,asset,start,end,status,replaces)
                VALUES (1,?,'2026-01-01T00:00:00Z','2026-01-01T01:00:00Z','可信','[]')''',
                (asset,)).lastrowid
            con.execute("INSERT INTO work_order VALUES (?,1,'待派工','normal','isolated_fault','[]')",(eid,))
        con.execute("INSERT INTO topology_edge VALUES ('A0','A1',0,10)")
    return db


def test_register_repair_roster(tmp_path):
    db = closed_orders(tmp_path)
    fixture = Path(__file__).resolve().parents[1]/'fixtures'/'repair-roster.json'
    result = repair.register(db,'repair-A',1,fixture)
    assert result['orders'] == 2
    assert repair.register(db,'repair-A',1,fixture) == result
    with connect(db) as con:
        assert con.execute('SELECT count(*) FROM repair_roster').fetchone()[0] == 1


def test_repair_roster_conflict(tmp_path):
    db = closed_orders(tmp_path)
    fixture = Path(__file__).resolve().parents[1]/'fixtures'/'repair-roster.json'
    repair.register(db,'repair-A',1,fixture)
    changed = tmp_path/'changed.json'
    changed.write_text(fixture.read_text().replace('"minutes":30','"minutes":35'))
    with pytest.raises(ValueError,match='repair_roster_conflict'):
        repair.register(db,'repair-A',1,changed)
