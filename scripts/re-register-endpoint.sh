#!/usr/bin/env bash
# Thin wrapper: scripts/re-register-endpoint.py (same flags).
#   scripts/re-register-endpoint.sh --network monad_testnet \
#       --endpoint https://<random>.trycloudflare.com
set -euo pipefail
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/re-register-endpoint.py" "$@"
