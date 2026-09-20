#!/usr/bin/env python3
"""Fail if the embedded Rust ABI differs from the compiled Solidity contract."""
import json
from pathlib import Path
root = Path(__file__).resolve().parents[1]
compiled = json.loads((root/'contracts/out/AegisOracle.sol/AegisOracle.json').read_text())['abi']
embedded = json.loads((root/'oracle-node/abi/AegisOracle.json').read_text())
assert compiled == embedded, 'ABI drift: regenerate oracle-node/abi/AegisOracle.json from forge build output'
print('ABI matches Solidity build')
