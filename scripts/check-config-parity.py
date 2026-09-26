#!/usr/bin/env python3
"""Config parity guard: every deployed-address consumer must match
contracts/deployed.monad.json. Prevents the recurring 'deploy refreshed
some configs but missed others' class (registry v1 miss, escrow v2 miss)."""
import json
import re
import sys

DEP = json.load(open("contracts/deployed.monad.json"))
ok = True

PAIRS = [
    ("cre/settlement-audit/config.staging.json", "registryAddress", "registry"),
    ("cre/settlement-audit/config.staging.json", "escrowAddress", "escrow"),
    ("cre/settlement-audit/config.production.json", "registryAddress", "registry"),
    ("cre/settlement-audit/config.production.json", "escrowAddress", "escrow"),
]
for path, cfg_key, dep_key in PAIRS:
    cfg = json.load(open(path))
    if cfg[cfg_key].lower() != DEP[dep_key].lower():
        print(f"PARITY FAIL {path} {cfg_key}={cfg[cfg_key]} != deployed.{dep_key}={DEP[dep_key]}")
        ok = False

web = open("web/config.js", encoding="utf-8").read()
for key, dep_key in [("registryAddr", "registry"), ("escrowAddr", "escrow")]:
    m = re.search(key + r'\s*:\s*"(0x[0-9a-fA-F]{40})"', web)
    if not m or m.group(1).lower() != DEP[dep_key].lower():
        print(f"PARITY FAIL web/config.js {key} != deployed.{dep_key}")
        ok = False

print("CONFIG PARITY OK" if ok else "CONFIG PARITY FAILED")
sys.exit(0 if ok else 1)
