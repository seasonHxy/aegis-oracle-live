#!/usr/bin/env python3
"""Compare Rust migration against the preserved Python aggregation on shared cases."""
import copy
from decimal import Decimal, ROUND_CEILING
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'legacy/hip3/src'))
from hip3_oracle.aggregation import PriceAggregator, AggregationError
from hip3_oracle.config import FeedConfig
from hip3_oracle.models import Quote

base = json.loads((ROOT/'config/demo.json').read_text())
cases = []
for label, prices in [('outlier', ['3000', '3001', '2999', '9000']), ('tight', ['3000', '3001', '2999', '3002']), ('flat', ['3000']*4), ('split', ['1000', '2000', '3000', '4000'])]:
    c = copy.deepcopy(base)
    for s, p in zip(c['sources'], prices):
        s['price'] = p
    cases.append((label, c))
c = copy.deepcopy(base)
c['sources'][1]['group'] = c['sources'][0]['group']
cases.append(('correlated-source-quorum', c))

with tempfile.TemporaryDirectory(prefix='aegis-parity-') as tmp:
    for i, (label, c) in enumerate(cases):
        f = FeedConfig(coin=c['feed'], sz_decimals=2, sources=tuple(s['name'] for s in c['sources']), min_sources=c['min_sources'], min_independent_groups=c['min_groups'], max_source_age_ms=c['max_age_secs']*1000, max_future_ms=0, max_spread_bps=Decimal(c['max_spread_bps']), outlier_mad_multiplier=Decimal(c['mad_multiplier']), outlier_min_band_bps=Decimal(c['outlier_band_bps']), max_confidence_bps=Decimal(c['max_confidence_bps']))
        now = int(time.time()*1000)
        quotes = [Quote(coin=c['feed'], source=s['name'], independence_group=s['group'], price=Decimal(s['price']), observed_at_ms=now, received_at_ms=now, weight=Decimal(s.get('weight', 1))) for s in c['sources']]
        try:
            expected = PriceAggregator().aggregate(f, quotes, now_ms=now)
        except AggregationError:
            expected = None
        path = Path(tmp)/f'{i}.json'
        path.write_text(json.dumps(c))
        result = subprocess.run([str(ROOT/'target/debug/aegis-oracle'), '--once', '--config', str(path), '--state', str(Path(tmp)/f'state-{i}.json')], cwd=ROOT, text=True, capture_output=True, timeout=10)
        assert (result.returncode == 0) == (expected is not None), (label, result.stdout, result.stderr)
        if expected:
            actual = json.loads(result.stdout)['aggregate']
            assert Decimal(actual['price']) == expected.price, label
            assert actual['source_count'] == expected.source_count, label
            assert actual['group_count'] == expected.independent_group_count, label
            assert actual['confidence_bps'] == int(expected.confidence_bps.to_integral_value(rounding=ROUND_CEILING)), label
        print('PASS: Python/Rust parity', label)
