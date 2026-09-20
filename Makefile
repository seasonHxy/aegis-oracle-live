.PHONY: build test demo check fmt lint live-smoke
lint:
	cargo fmt --all -- --check
	cargo clippy --locked --all-targets -- -D warnings
	cd contracts && forge fmt --check
	python3 -m py_compile scripts/demo.py scripts/parity.py scripts/check-abi.py scripts/live_smoke.py

build:
	cargo build --locked
	cd contracts && forge build

test:
	cargo test --locked
	cd contracts && forge test -vv
	python3 scripts/check-abi.py
	cd legacy/hip3 && PYTHONPATH=src python3 -m unittest discover -s tests -q

check: lint build test
	python3 scripts/parity.py
	python3 scripts/demo.py --check --no-build

demo:
	python3 scripts/demo.py

fmt:
	cargo fmt --all
	cd contracts && forge fmt

live-smoke: build
	python3 scripts/live_smoke.py
