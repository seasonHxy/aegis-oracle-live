#!/usr/bin/env python3
"""Opt-in real market smoke check. Never submits transactions or uses a wallet."""
import argparse
import datetime
import json
from pathlib import Path
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='config/live-eth-usd.json')
    parser.add_argument('--samples', type=int, default=3)
    parser.add_argument('--output', type=Path, default=ROOT/'state/live-smoke.json')
    args = parser.parse_args()
    if not 1 <= args.samples <= 20:
        parser.error('--samples must be 1..20')
    config_path = (ROOT/args.config).resolve()
    config = json.loads(config_path.read_text())
    if config.get('simulation') or any(s['kind'] not in ['coinbase','kraken','bitstamp'] for s in config['sources']):
        parser.error('live smoke requires native venue adapters without simulation')
    report = {'project':'Aegis Oracle Live','started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'feed':config['feed'],'chain_publication':False,'quorum':config['min_groups'],
              'note':'Point-in-time public API observations; not an uptime guarantee or production benchmark.', 'samples':[]}
    with tempfile.TemporaryDirectory(prefix='aegis-live-smoke-') as temporary:
        for i in range(args.samples):
            started = time.monotonic()
            p = subprocess.run([str(ROOT/'target/debug/aegis-oracle'),'--config',str(config_path),'--once','--state',str(Path(temporary)/'state.json')],cwd=ROOT,capture_output=True,text=True,timeout=40)
            try:
                value=json.loads(p.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                value={'status':'process_failed','error':'node returned no JSON snapshot','source_errors':[]}
            sample={k:value.get(k) for k in ['feed','mode','simulated','status','cycle_at_ms','aggregate','quotes','source_errors','error']}
            sample['exit_code']=p.returncode
            report['samples'].append(sample)
            print(f"Sample {i+1}: {sample['status']}; sources={len(sample.get('quotes') or [])}",flush=True)
            if i+1<args.samples:
                time.sleep(max(0,config['interval_ms']/1000-(time.monotonic()-started)))
    report['passed']=all(s['exit_code']==0 and s['status']=='dry_run' and s['simulated'] is False for s in report['samples'])
    report['finished_at']=datetime.datetime.now(datetime.timezone.utc).isoformat()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(f"Evidence: {args.output}; passed={report['passed']}")
    raise SystemExit(0 if report['passed'] else 1)

if __name__=='__main__':
    main()
