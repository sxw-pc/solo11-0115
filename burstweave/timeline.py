"""Joint interval-censored timelines for customer-service availability.

Every fault onset/recovery is only known through *half-open* observation
bounds: the true event minute lies inside a closed minute-grid domain, while
the resulting impact segment is ``[onset, recovery)``.  Onset relations are
joint evidence (lag windows between two assets) and must be satisfied by one
common timeline; taking per-asset extreme endpoints independently can combine
into timelines that no evidence supports.

The solver enumerates every jointly feasible onset assignment with
constraint-checking backtracking and derives the global outage extrema from
the monotonicity of the half-open segment algebra, so recoveries do not need
to be enumerated.
"""
from datetime import timedelta

SERIES = 'series'
REDUNDANT_2 = 'redundant_2'
TOPOLOGIES = (SERIES, REDUNDANT_2)

INDETERMINATE = 'indeterminate'
DEFINITELY_BREACHED = 'definitely_breached'
DEFINITELY_CLEAR = 'definitely_clear'

MINUTE = timedelta(minutes=1)


def _check_minute(value):
    if value.tzinfo is None:
        raise ValueError('timezone_required')
    if value.second or value.microsecond:
        raise ValueError('subminute_precision')
    return value


def _offset(start, value):
    delta = value - start
    seconds = delta.total_seconds()
    if seconds < 0 or seconds % 60:
        raise ValueError('subminute_precision')
    return int(seconds // 60)


def build_model(period_start, period_end, assets, impacts, relations):
    """Freeze one bounded observation problem on a whole-minute grid.

    ``impacts`` maps each asset to ``{'onset': (lo, hi),
    'recovery': (lo, hi)}`` timezone-aware datetimes; ``relations`` is a list
    of ``(from_asset, to_asset, min_lag, max_lag)`` minute tuples.
    """
    _check_minute(period_start)
    _check_minute(period_end)
    if period_start >= period_end:
        raise ValueError('bad_service_period')
    if not assets or len(set(assets)) != len(assets):
        raise ValueError('bad_service_contract')
    rows = {}
    for asset in assets:
        item = impacts.get(asset)
        if item is None:
            raise ValueError('missing_impact_evidence')
        onset_lo_dt, onset_hi_dt = item['onset']
        rec_lo_dt, rec_hi_dt = item['recovery']
        for bound in (onset_lo_dt, onset_hi_dt, rec_lo_dt, rec_hi_dt):
            _check_minute(bound)
        onset_lo = _offset(period_start, onset_lo_dt)
        onset_hi = _offset(period_start, onset_hi_dt)
        rec_lo = _offset(period_start, rec_lo_dt)
        rec_hi = _offset(period_start, rec_hi_dt)
        period_hi = _offset(period_start, period_end)
        if not (0 <= onset_lo <= onset_hi <= rec_lo <= rec_hi <= period_hi):
            raise ValueError('bad_impact_bounds')
        rows[asset] = {'onset_lo': onset_lo, 'onset_hi': onset_hi,
                       'rec_lo': rec_lo, 'rec_hi': rec_hi}
    adjacency = {asset: [] for asset in assets}
    for from_asset, to_asset, min_lag, max_lag in relations:
        if from_asset not in rows or to_asset not in rows or from_asset == to_asset:
            raise ValueError('bad_onset_relation')
        if min_lag < 0 or max_lag < min_lag:
            raise ValueError('bad_onset_relation')
        adjacency[from_asset].append((to_asset, min_lag, max_lag))
    return {'start': period_start, 'end': period_end,
            'assets': list(assets), 'impacts': rows, 'adjacency': adjacency}


def _lag_ok(adjacency, x, vx, y, vy):
    for target, lo, hi in adjacency.get(x, ()):
        if target == y and not (lo <= vy - vx <= hi):
            return False
    for target, lo, hi in adjacency.get(y, ()):
        if target == x and not (lo <= vx - vy <= hi):
            return False
    return True


def feasible_onsets(model):
    """All jointly feasible onset minute assignments under every lag edge."""
    impacts = model['impacts']
    adjacency = model['adjacency']
    assets = model['assets']
    domains = {a: set(range(impacts[a]['onset_lo'], impacts[a]['onset_hi'] + 1))
               for a in assets}
    assignment = {}
    results = []

    def select():
        chosen, chosen_domain = None, None
        for asset in assets:
            if asset in assignment:
                continue
            domain = set()
            for value in domains[asset]:
                if value >= impacts[asset]['rec_hi']:
                    continue  # no strictly later recovery minute would exist
                if all(_lag_ok(adjacency, asset, value, other, assignment[other])
                       for other in assignment):
                    domain.add(value)
            if not domain:
                return asset, set()
            if chosen is None or len(domain) < len(chosen_domain):
                chosen, chosen_domain = asset, domain
        return chosen, chosen_domain or set()

    def backtrack():
        if len(assignment) == len(assets):
            results.append(dict(assignment))
            return
        asset, domain = select()
        if not domain:
            return
        for value in sorted(domain):
            assignment[asset] = value
            backtrack()
            del assignment[asset]

    backtrack()
    return results


def merge_half_open(intervals):
    """Merge ``[lo, hi)`` minute intervals; touching intervals stay continuous."""
    merged = []
    for lo, hi in sorted(intervals):
        if merged and lo <= merged[-1][1]:
            if hi > merged[-1][1]:
                merged[-1] = (merged[-1][0], hi)
        else:
            merged.append((lo, hi))
    return merged


def outage(model, topology, topology_assets, onset, recovery):
    """Return ``(length_minutes, half_open_segments)`` for one timeline."""
    if topology == SERIES:
        segments = merge_half_open((onset[a], recovery[a]) for a in topology_assets)
    elif topology == REDUNDANT_2:
        lo = max(onset[a] for a in topology_assets)
        hi = min(recovery[a] for a in topology_assets)
        segments = [(lo, hi)] if lo < hi else []
    else:
        raise ValueError('unknown_topology')
    return sum(hi - lo for lo, hi in segments), segments


def _to_dt(model, minute):
    return model['start'] + MINUTE * minute


def describe_witness(model, topology, topology_assets, onset, recovery):
    length, segments = outage(model, topology, topology_assets, onset, recovery)
    return {
        'assets': {a: {'onset': _to_dt(model, onset[a]),
                       'recovery': _to_dt(model, recovery[a])}
                   for a in model['assets']},
        'outage_segments': [{'start': _to_dt(model, lo), 'end': _to_dt(model, hi),
                             'length_minutes': hi - lo} for lo, hi in segments],
        'outage_minutes': length,
    }


def bound_service(model, topology, topology_assets, sla_minutes):
    """Global min/max service outage over every feasible joint timeline."""
    if topology not in TOPOLOGIES:
        raise ValueError('unknown_topology')
    if not topology_assets or len(set(topology_assets)) != len(topology_assets):
        raise ValueError('bad_topology_assets')
    if topology == REDUNDANT_2 and len(topology_assets) != 2:
        raise ValueError('redundant_2_requires_two_assets')
    for asset in topology_assets:
        if asset not in model['impacts']:
            raise ValueError('topology_asset_not_in_service')
    if not isinstance(sla_minutes, int) or isinstance(sla_minutes, bool) or sla_minutes < 0:
        raise ValueError('bad_sla_minutes')

    minimum = maximum = None
    min_witness = max_witness = None
    for onset in feasible_onsets(model):
        # Outage length is componentwise non-decreasing in every recovery, so
        # each fixed onset attains its extrema at the recovery bound corners.
        rec_min = {a: model['impacts'][a]['rec_lo'] for a in model['assets']}
        rec_max = {a: model['impacts'][a]['rec_hi'] for a in model['assets']}
        low_len, _ = outage(model, topology, topology_assets, onset, rec_min)
        high_len, _ = outage(model, topology, topology_assets, onset, rec_max)
        if minimum is None or low_len < minimum:
            minimum = low_len
            min_witness = describe_witness(model, topology, topology_assets,
                                           onset, rec_min)
        if maximum is None or high_len > maximum:
            maximum = high_len
            max_witness = describe_witness(model, topology, topology_assets,
                                           onset, rec_max)
    if minimum is None:
        return {'feasible': False, 'verdict': INDETERMINATE,
                'reason': 'unsatisfiable_joint_timeline',
                'min_minutes': None, 'max_minutes': None,
                'min_witness': None, 'max_witness': None}
    if minimum > sla_minutes:
        verdict, reason = DEFINITELY_BREACHED, None
    elif maximum <= sla_minutes:
        verdict, reason = DEFINITELY_CLEAR, None
    else:
        verdict, reason = INDETERMINATE, 'span_crosses_sla'
    return {'feasible': True, 'verdict': verdict, 'reason': reason,
            'min_minutes': minimum, 'max_minutes': maximum,
            'min_witness': min_witness, 'max_witness': max_witness}


def check_witness(model, onset, recovery):
    """Independently re-check a stored witness against every frozen bound."""
    if set(onset) != set(model['assets']) or set(recovery) != set(model['assets']):
        return False
    for asset, bounds in model['impacts'].items():
        o, r = onset[asset], recovery[asset]
        if not (bounds['onset_lo'] <= o <= bounds['onset_hi']):
            return False
        if not (bounds['rec_lo'] <= r <= bounds['rec_hi']):
            return False
        if o >= r:
            return False
    for source, edges in model['adjacency'].items():
        for target, lo, hi in edges:
            if not (lo <= onset[target] - onset[source] <= hi):
                return False
    return True


def exhaustive_timelines(model):
    """Yield every ``(onset, recovery)`` joint timeline; cross-check helper."""
    assets = model['assets']
    for onset in feasible_onsets(model):
        recovery = {}

        def recurse(index):
            if index == len(assets):
                yield dict(recovery)
                return
            asset = assets[index]
            bounds = model['impacts'][asset]
            for value in range(bounds['rec_lo'], bounds['rec_hi'] + 1):
                if value > onset[asset]:
                    recovery[asset] = value
                    yield from recurse(index + 1)
                    del recovery[asset]

        for full_recovery in recurse(0):
            yield dict(onset), full_recovery
