# ═══════════════════════════════════════════════════════════════════════════
# TokenShare — root Makefile
#
#   make demo     one-command local demo (relay :8787 + web :8080)
#   make stop     stop the demo processes
#   make tunnel   demo + cloudflared quick tunnel for the relay API
#   make test     all four suites — relay and cli run in SEPARATE pytest
#                 processes (both test packages are named `tests`; one shared
#                 pytest invocation collides on the package name)
#   make check    alias of test
# ═══════════════════════════════════════════════════════════════════════════
PY ?= python3

.DEFAULT_GOAL := help
.PHONY: help demo stop tunnel test check test-relay test-cli test-e2e test-contracts

help:
	@echo "TokenShare targets:"
	@echo "  make demo            start relay (8787) + static web (8080)"
	@echo "  make stop            stop the demo processes"
	@echo "  make tunnel          demo + cloudflared quick tunnel (relay API)"
	@echo "  make test            all four suites (relay | cli | e2e | contracts)"
	@echo "  make check           alias of test"
	@echo "  make test-relay / test-cli / test-e2e / test-contracts   one suite"

demo:
	bash scripts/demo.sh

stop:
	bash scripts/demo.sh stop

tunnel:
	bash scripts/demo.sh --tunnel

# Each suite in its own pytest process — the subtargets keep them separate
# (package-name-conflict lesson: relay/tests and cli/tests are both package
# `tests` → one shared pytest invocation collides on the package name).
test-relay:
	$(PY) -m pytest relay/tests -q

test-cli:
	$(PY) -m pytest cli/tests -q

test-e2e:
	$(PY) -m pytest e2e/tests -q

test-contracts:
	forge test --root contracts

test: test-relay test-cli test-e2e test-contracts

check: test
