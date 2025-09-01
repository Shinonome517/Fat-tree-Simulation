SHELL := /bin/bash

.PHONY: t1-up t1-ecmp t1-test t1-evidence t1-clean t1-check

PY := python3

t1-up:
	@echo "[t1-up] Bringing up k=4 Fat-tree without ECMP and validating basics"
	$(PY) scripts/run_fattree.py --ecmp off --ping-count 3 --iperf-time 10

t1-ecmp:
	@echo "[t1-ecmp] Running k=4 Fat-tree with ECMP (SELECT hash over 5-tuple)"
	$(PY) scripts/run_fattree.py --ecmp on --ping-count 3 --iperf-time 20

t1-test:
	@echo "[t1-test] ECMP-on functional ping/iperf tests"
	$(PY) scripts/run_fattree.py --ecmp on --ping-count 3 --iperf-time 30

t1-evidence:
	@echo "[t1-evidence] Collecting evidence on latest logs directory"
	@LAST=$$(ls -1d logs/* 2>/dev/null | tail -n1); \
	 if [[ -z "$$LAST" ]]; then echo "No logs found"; exit 1; fi; \
	 echo "Using $$LAST"; \
	 $(PY) scripts/evidence.py --logdir "$$LAST"

t1-clean:
	@echo "[t1-clean] Cleaning Mininet and stray processes"
	- mn -c
	- pkill -f osken-manager || true
	- pkill -f ryu-manager || true
	- pkill -f iperf3 || true
	@echo "[t1-clean] Done"

t1-check:
	@echo "[t1-check] Checking dependencies and services..."
	@bash scripts/check_env.sh
	@echo "[t1-check] Done"
