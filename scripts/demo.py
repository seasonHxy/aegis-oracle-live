#!/usr/bin/env python3
"""Deploy a disposable local stack. No external RPCs, funds or private keys are used."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
# Public Anvil test account, never use for funds on any public network.
TEST_KEY = '0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80'
TEST_ADDRESS = '0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266'


def run(args, *, cwd=ROOT, env=None, check=True):
    p = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, timeout=180)
    if check and p.returncode:
        raise RuntimeError(f'{args[0]} failed:\n{p.stdout}\n{p.stderr}')
    return p


def port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def rpc(url, method, params=None):
    data = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or []}).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'}), timeout=3) as r:
        body = json.load(r)
    if 'error' in body:
        raise RuntimeError(body['error'])
    return body['result']


def wait_rpc(url, process):
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError('Anvil failed to start')
        try:
            rpc(url, 'eth_chainId')
            return
        except (OSError, ValueError):
            time.sleep(.1)
    raise RuntimeError('Anvil did not become ready')


def deploy(url, env):
    output = run(['forge', 'script', 'script/Deploy.s.sol:Deploy', '--rpc-url', url, '--broadcast'], cwd=ROOT/'contracts', env=env)
    data = json.loads((ROOT/'contracts/broadcast/Deploy.s.sol/31337/run-latest.json').read_text())
    addresses = {tx.get('contractName'): tx.get('contractAddress') for tx in data['transactions'] if tx.get('transactionType') == 'CREATE'}
    if not addresses.get('AegisOracle') or not addresses.get('CollateralLens'):
        raise RuntimeError('Missing deployed contract addresses: '+output.stdout)
    return addresses


def check_stack(url, addresses, env, temporary):
    """Real HTTP -> Rust -> EIP-712 -> EVM -> checked consumer, including faults."""
    fixture = {'mode': 'normal'}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            idx = int(self.path.rsplit('/', 1)[-1])
            if fixture['mode'] == 'quorum' and idx < 2:
                self.send_error(503)
                return
            price = ['3000.00', '3001.00', '2999.00', '9000.00'][idx]
            if fixture['mode'] == 'jump':
                price = str(int(float(price)*2))
            timestamp = int(time.time()*1000) - (120000 if fixture['mode'] == 'stale' else 0)
            if fixture['mode'] == 'future':
                timestamp += 120000
            data = {'price': price, 'timestamp': timestamp}
            if fixture['mode'] == 'missing_timestamp':
                data.pop('timestamp')
            body = b'{' if fixture['mode'] == 'invalid_json' else json.dumps({'data': data}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    try:
        config = json.loads((ROOT/'config/demo.json').read_text())
        for i, source in enumerate(config['sources']):
            source.pop('price')
            source.update(kind='json', url=f'http://127.0.0.1:{http.server_port}/quote/{i}', price_path='/data/price', timestamp_path='/data/timestamp')
        config_path = Path(temporary)/'rest.json'
        config_path.write_text(json.dumps(config))
        state_path = Path(temporary)/'evm.json'
        command = [str(ROOT/'target/debug/aegis-oracle'), '--config', str(config_path), '--publish', '--rpc-url', url, '--contract', addresses['AegisOracle'], '--state', str(state_path), '--once']
        for mode, success in [('normal', True), ('normal', True), ('stale', False), ('future', False), ('missing_timestamp', False), ('invalid_json', False), ('jump', False), ('quorum', False)]:
            fixture['mode'] = mode
            result = run(command, env=env, check=False)
            if (result.returncode == 0) != success:
                raise AssertionError(f'{mode}: unexpected exit {result.returncode}\n{result.stdout}\n{result.stderr}')
            snapshot = json.loads(result.stdout.strip().splitlines()[-1])
            assert snapshot['status'] == ('published' if success else 'blocked'), snapshot
            if success:
                assert snapshot['aggregate']['source_count'] == 3
                assert snapshot['aggregate']['price'] == '3000.00000000'
            else:
                expected = 'jump' if mode == 'jump' else 'insufficient'
                assert expected in snapshot['error'], snapshot
            print(f'PASS: HTTP pipeline {mode}, status={snapshot["status"]}', flush=True)
        # Sequence did not advance during rejected cycles.
        feed = run(['cast', 'keccak', 'ETH/USD']).stdout.strip()
        result = run(['cast', 'call', addresses['AegisOracle'], 'latestPrice(bytes32)(uint128,uint64,uint64)', feed, '--rpc-url', url]).stdout.strip().splitlines()
        assert int(result[2].split()[0]) == 2, result
        result = run(['cast', 'call', addresses['CollateralLens'], 'quote(uint128)(uint256,uint256)', '2000000000000000000', '--rpc-url', url]).stdout.strip().splitlines()
        assert int(result[0].split()[0]) == 600000000000
        assert int(result[1].split()[0]) == 420000000000
        print('PASS: on-chain sequence and collateral valuation', flush=True)
        # Lost-before-broadcast journal recovery: stop mining, let the node time out,
        # restart and prove the journal is recovered without a duplicate report nonce.
        fixture['mode'] = 'normal'
        rpc(url, 'evm_setAutomine', [False])
        pending = run(command, env=env, check=False)
        assert pending.returncode != 0 and json.loads(pending.stdout.strip().splitlines()[-1])['status'] == 'pending', pending.stderr
        saved = json.loads(state_path.read_text())
        assert saved['pending_raw'] and saved['pending_hash']
        rpc(url, 'evm_mine')
        rpc(url, 'evm_setAutomine', [True])
        run(command, env=env)
        assert json.loads(state_path.read_text())['pending_raw'] is None
        print('PASS: pending transaction recovery across process restart', flush=True)
        rpc(url, 'evm_increaseTime', [61])
        rpc(url, 'evm_mine')
        stale = run(['cast', 'call', addresses['CollateralLens'], 'quote(uint128)(uint256,uint256)', '1000000000000000000', '--rpc-url', url], check=False)
        assert stale.returncode != 0
        print('PASS: consumer rejects expired price', flush=True)
        check_http_service(config, fixture, temporary, env)
    finally:
        http.shutdown()
        http.server_close()


def check_http_service(config, fixture, temporary, env):
    """Exercise the real dashboard API through healthy -> blocked -> recovered cycles."""
    config = {**config, 'interval_ms': 500}
    config_path = Path(temporary)/'monitor.json'
    config_path.write_text(json.dumps(config))
    listen_port = port()
    base = f'http://127.0.0.1:{listen_port}'
    command = [str(ROOT/'target/debug/aegis-oracle'), '--config', str(config_path),
               '--state', str(Path(temporary)/'monitor-state.json'), '--listen', f'127.0.0.1:{listen_port}']
    with open(Path(temporary)/'monitor.log', 'w') as log:
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=log)
        try:
            for mode, status, health_code in [('normal', 'dry_run', 200), ('quorum', 'blocked', 503), ('normal', 'dry_run', 200)]:
                fixture['mode'] = mode
                deadline = time.monotonic() + 10
                # Wait for a cycle started after the fixture change, never accept stale status.
                changed_at = int(time.time()*1000)
                while time.monotonic() < deadline:
                    assert process.poll() is None, 'dashboard process stopped'
                    try:
                        with urllib.request.urlopen(base+'/api/status', timeout=2) as r:
                            snapshot = json.load(r)
                        if snapshot['status'] != status or snapshot['cycle_at_ms'] <= changed_at:
                            time.sleep(.05)
                            continue
                        try:
                            response = urllib.request.urlopen(base+'/healthz', timeout=2)
                        except urllib.error.HTTPError as error:
                            response = error
                        with response:
                            code, body = response.code, json.load(response)
                        if code != health_code:
                            time.sleep(.05)
                            continue
                        assert body['healthy'] == (health_code == 200)
                        assert body['mode'] == 'DRY RUN'
                        assert snapshot['simulated'] is True
                        assert snapshot['tx_hash'] is None
                        if status == 'blocked':
                            assert len(snapshot['source_errors']) == 2
                            assert snapshot['aggregate'] is None
                        else:
                            assert snapshot['aggregate']['price'] == '3000.00000000'
                        print(f'PASS: status API {mode} and health HTTP {code}', flush=True)
                        break
                    except (OSError, ValueError):
                        time.sleep(.05)
                else:
                    raise AssertionError(f'HTTP service did not reach {status}')
            with urllib.request.urlopen(base, timeout=2) as r:
                assert 'Aegis Oracle' in r.read().decode()
            print('PASS: embedded dashboard served', flush=True)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Run end-to-end fault tests and exit')
    parser.add_argument('--live-data', action='store_true', help='Use real ETH/USD quotes on the disposable LOCAL chain')
    parser.add_argument('--no-build', action='store_true')
    parser.add_argument('--port', type=int, default=8787, help='Dashboard port')
    args = parser.parse_args()
    if args.check and args.live_data:
        parser.error("--check uses deterministic fixtures; run live_smoke.py for external market checks")
    if not args.no_build:
        print('Building Rust node and Solidity contracts…', flush=True)
        subprocess.run(['cargo', 'build', '--locked'], cwd=ROOT, check=True)
        subprocess.run(['forge', 'build'], cwd=ROOT/'contracts', check=True)
    env = {**os.environ, 'DEPLOYER_PRIVATE_KEY': TEST_KEY, 'ORACLE_SIGNER_ADDRESS': TEST_ADDRESS, 'ORACLE_PRIVATE_KEY': TEST_KEY}
    processes = []
    with tempfile.TemporaryDirectory(prefix='aegis-demo-') as tmp:
        log = open(Path(tmp)/'anvil.log', 'w')
        try:
            rpc_port = port()
            url = f'http://127.0.0.1:{rpc_port}'
            anvil = subprocess.Popen(['anvil', '--host', '127.0.0.1', '--port', str(rpc_port), '--chain-id', '31337', '--silent'], stdout=log, stderr=log)
            processes.append(anvil)
            wait_rpc(url, anvil)
            addresses = deploy(url, env)
            print('Deployed:', json.dumps(addresses), flush=True)
            if args.check:
                check_stack(url, addresses, env, tmp)
                print('All local end-to-end checks passed.', flush=True)
                return
            (ROOT/'state').mkdir(exist_ok=True)
            (ROOT/'state/demo-deployment.json').write_text(json.dumps({'rpc_url': url, **addresses}, indent=2)+'\n')
            node = subprocess.Popen([str(ROOT/'target/debug/aegis-oracle'), '--config', 'config/live-eth-usd.json' if args.live_data else 'config/demo.json', '--publish', '--rpc-url', url, '--contract', addresses['AegisOracle'], '--state', str(Path(tmp)/'node-state.json'), '--listen', f'127.0.0.1:{args.port}'], cwd=ROOT, env=env)
            processes.append(node)
            print(f'Open http://127.0.0.1:{args.port} — Ctrl+C stops both services.', flush=True)
            while node.poll() is None and anvil.poll() is None:
                time.sleep(.5)
            raise RuntimeError('A demo service stopped unexpectedly')
        except KeyboardInterrupt:
            print('\nStopping local demo.')
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            log.close()


if __name__ == '__main__':
    main()
