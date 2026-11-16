"""Trace ordered fault propagation across an immutable asset dependency graph."""
import json
from .store import connect
from .engine import dt


def edge(path, source_asset, target_asset, min_lag_minutes, max_lag_minutes):
    if (source_asset == target_asset or min_lag_minutes < 0 or
            max_lag_minutes < min_lag_minutes):
        raise ValueError('bad_topology_edge')
    db = connect(path)
    try:
        with db:
            for asset in (source_asset, target_asset):
                if not db.execute('SELECT 1 FROM asset WHERE id=?', (asset,)).fetchone():
                    raise ValueError('unknown_asset')
            old = db.execute('SELECT * FROM topology_edge WHERE source_asset=? AND target_asset=?',
                             (source_asset, target_asset)).fetchone()
            if old and (old['min_lag_minutes'], old['max_lag_minutes']) != (min_lag_minutes, max_lag_minutes):
                raise ValueError('edge_identity_conflict')
            links = [(x['source_asset'], x['target_asset']) for x in db.execute(
                'SELECT source_asset,target_asset FROM topology_edge')]
            graph = {}
            for a, b in links + [(source_asset, target_asset)]:
                graph.setdefault(a, set()).add(b)
            seen = set()
            stack = [target_asset]
            while stack:
                node = stack.pop()
                if node == source_asset:
                    raise ValueError('topology_cycle')
                if node not in seen:
                    seen.add(node)
                    stack.extend(graph.get(node, ()))
            db.execute('INSERT OR IGNORE INTO topology_edge VALUES (?,?,?,?)',
                       (source_asset, target_asset, min_lag_minutes, max_lag_minutes))
    finally:
        db.close()


def trace(path, run_id):
    db = connect(path)
    try:
        run = db.execute('SELECT status FROM run WHERE id=?', (run_id,)).fetchone()
        if not run:
            raise ValueError('unknown_run')
        episodes = [dict(x) for x in db.execute('SELECT * FROM episode WHERE run_id=? ORDER BY start,id',
                                                (run_id,))]
        evidence = {e['id']: bool(db.execute('SELECT 1 FROM evidence WHERE episode_id=?',
                                             (e['id'],)).fetchone()) for e in episodes}
        edges = list(db.execute('SELECT * FROM topology_edge ORDER BY source_asset,target_asset'))
        links = []
        for edge_row in edges:
            for a in episodes:
                if a['asset'] != edge_row['source_asset'] or a['status'] != '可信' or not evidence[a['id']]:
                    continue
                for b in episodes:
                    if b['asset'] != edge_row['target_asset'] or b['status'] != '可信' or not evidence[b['id']]:
                        continue
                    lag = (dt(b['start']) - dt(a['end'])).total_seconds() / 60
                    if edge_row['min_lag_minutes'] <= lag <= edge_row['max_lag_minutes']:
                        links.append((a['id'], b['id'], lag))
        outgoing = {}
        incoming = set()
        for a, b, _ in links:
            outgoing.setdefault(a, set()).add(b)
            incoming.add(b)
        decisions = []
        roots = [e for e in episodes if e['id'] not in incoming]
        for root in roots:
            reached = set()
            stack = [root['id']]
            while stack:
                current = stack.pop()
                if current in reached:
                    continue
                reached.add(current)
                stack.extend(outgoing.get(current, ()))
            if run['status'] != '已闭合' or root['status'] != '可信' or not evidence[root['id']]:
                status, reason = '不可判', 'uncertain_evidence'
            elif len(reached) > 1:
                status, reason = '扩散待处置', 'ordered_dependency_chain'
            else:
                status, reason = '孤立', 'no_propagation'
            decisions.append({'root_episode': root['id'], 'status': status,
                              'episode_ids': sorted(reached), 'reason': reason})
        with db:
            db.execute('DELETE FROM propagation_link WHERE run_id=?', (run_id,))
            db.execute('DELETE FROM propagation_decision WHERE run_id=?', (run_id,))
            for a, b, lag in links:
                db.execute('INSERT INTO propagation_link VALUES (?,?,?,?)', (run_id, a, b, lag))
            for item in decisions:
                db.execute('INSERT INTO propagation_decision VALUES (?,?,?,?,?)',
                           (run_id, item['root_episode'], item['status'],
                            json.dumps(item['episode_ids']), item['reason']))
        return decisions
    finally:
        db.close()
