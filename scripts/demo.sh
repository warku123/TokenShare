#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# TokenShare one-command local demo.
#
#   scripts/demo.sh            start relay (8787) + static web (8080)
#   scripts/demo.sh --tunnel   additionally open a cloudflared quick tunnel
#                              for the RELAY API and print the public URL
#   scripts/demo.sh stop       stop everything this script started
#
# Startup mirrors e2e/run.py:
#   - .env is parsed by a STRICT python regex (NEVER `source .env` — inline
#     comments like `ESCROW_ADDR=  # note` break shell sourcing);
#   - contracts/deployed.monad.json is the AUTHORITATIVE address source
#     (escrow/registry/usdc/chainId override whatever .env carries);
#   - the relay runs via `uvicorn relay.app.main:app` with the full env
#     assembly (names verbatim relay/app/config.py ENV_*).
#
# Idempotent: already-serving ports are detected and reused, never double-
# started. Logs and pidfiles live OUTSIDE the repo (in ${TMPDIR:-/tmp}).
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RELAY_PORT="${RELAY_PORT:-8787}"
WEB_PORT="${WEB_PORT:-8080}"

LOG_DIR="${TMPDIR:-/tmp}"
RELAY_LOG="$LOG_DIR/tokenshare-demo-relay.log"
WEB_LOG="$LOG_DIR/tokenshare-demo-web.log"
TUNNEL_LOG="$LOG_DIR/tokenshare-demo-tunnel.log"
RELAY_PIDFILE="$LOG_DIR/tokenshare-demo-relay.pid"
WEB_PIDFILE="$LOG_DIR/tokenshare-demo-web.pid"
TUNNEL_PIDFILE="$LOG_DIR/tokenshare-demo-tunnel.pid"

say()  { printf '%s\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# ── process helpers ──────────────────────────────────────────────────────────
pid_alive() { [[ -n "${1:-}" ]] && kill -0 "$1" 2>/dev/null; }

read_pidfile() { # $1=pidfile → echoes pid or empty
  [[ -f "$1" ]] && cat "$1" 2>/dev/null | tr -d '[:space:]' || true
}

spawn() { # $1=name $2=pidfile $3=logfile, rest=cmd (run from REPO_ROOT)
  local name="$1" pidfile="$2" logfile="$3"; shift 3
  (
    cd "$REPO_ROOT"
    nohup "$@" >"$logfile" 2>&1 &
    echo $! >"$pidfile"
  )
  say "  started $name (pid $(read_pidfile "$pidfile"), log $logfile)"
}

stop_one() { # $1=name $2=pidfile
  local name="$1" pidfile="$2" pid
  pid="$(read_pidfile "$pidfile")"
  if pid_alive "$pid"; then
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 25); do pid_alive "$pid" || break; sleep 0.2; done
    pid_alive "$pid" && kill -9 "$pid" 2>/dev/null || true
    say "  stopped $name (pid $pid)"
  elif [[ -f "$pidfile" ]]; then
    say "  $name already stopped (stale pidfile removed)"
  else
    say "  $name was not started by demo.sh (no pidfile)"
  fi
  rm -f "$pidfile"
}

port_up() { # $1=port $2=path → 0 when HTTP 200
  curl -sf -o /dev/null --max-time 2 "http://127.0.0.1:$1$2"
}

# ── stop subcommand ──────────────────────────────────────────────────────────
if [[ "${1:-}" == "stop" ]]; then
  say "stopping TokenShare demo:"
  stop_one "web"    "$WEB_PIDFILE"
  stop_one "relay"  "$RELAY_PIDFILE"
  stop_one "tunnel" "$TUNNEL_PIDFILE"
  say "done. (a port still busy? something else owns it: lsof -nP -iTCP:$RELAY_PORT -sTCP:LISTEN)"
  exit 0
fi

# ── .env parsing: STRICT python regex — never `source` (inline ` # comment`
#    lines like `ESCROW_ADDR=  # auto from deployed.json` would break; and
#    existing shell env wins over .env, same as e2e/dotenv_loader.py).
eval "$(python3 - "$REPO_ROOT/.env" <<'PY'
import os, re, shlex, sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    text = path.read_text(encoding="utf-8")
except OSError:
    sys.exit(0)  # no .env → nothing to load
pat = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
for raw in text.splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    m = pat.match(raw)
    if not m:
        continue  # strict: not a KEY=VALUE line → skip, never guess
    key, value = m.group(1), m.group(2)
    if value[:1] in ('"', "'"):
        close = value.find(value[0], 1)
        value = value[1:close] if close != -1 else value[1:]
    elif " #" in value:
        value = value.split(" #", 1)[0]  # dotenv inline-comment convention
    value = value.strip()
    if not value or key in os.environ:  # real env wins; empty = unset
        continue
    print(f"export {key}={shlex.quote(value)}")
PY
)"

# ── authoritative addresses from deployed.monad.json (e2e/run.py semantics) ──
DEPLOYED="$REPO_ROOT/contracts/deployed.monad.json"
[[ -f "$DEPLOYED" ]] || DEPLOYED="$REPO_ROOT/contracts/deployed.json"
[[ -f "$DEPLOYED" ]] || die "no contracts/deployed.monad.json (nor deployed.json) — deploy first"
read -r ESCROW_ADDR REGISTRY_ADDR USDC_ADDR CHAIN_ID <<<"$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
print(d["escrow"], d["registry"], d["usdc"], d["chainId"])
' "$DEPLOYED")"
export ESCROW_ADDR REGISTRY_ADDR USDC_ADDR CHAIN_ID

# ── relay env assembly (names verbatim relay/app/config.py ENV_*) ────────────
RELAY_SELLER_KEY="${RELAY_SELLER_KEY:-${SELLER_PRIVATE_KEY:-}}"
[[ -n "$RELAY_SELLER_KEY" ]] || die "RELAY_SELLER_KEY/SELLER_PRIVATE_KEY missing (.env or shell)"
RPC_URL="${RPC_URL:-https://testnet-rpc.monad.xyz}"
OPENAI_API_KEY="${OPENAI_API_KEY:-}"
OPENAI_BASE_URL="${OPENAI_BASE_URL:-}"
[[ -n "$OPENAI_API_KEY" && -n "$OPENAI_BASE_URL" ]] || \
  die "OPENAI_API_KEY / OPENAI_BASE_URL missing (.env) — relay config requires both"
export RELAY_SELLER_KEY RPC_URL OPENAI_API_KEY OPENAI_BASE_URL

# ── relay (8787) ──────────────────────────────────────────────────────────────
say "TokenShare demo — repo $REPO_ROOT"
if port_up "$RELAY_PORT" /health; then
  say "  relay already serving :$RELAY_PORT — reusing (idempotent)"
else
  if lsof -nP -iTCP:"$RELAY_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    die "port $RELAY_PORT busy but /health not OK — another process owns it (lsof -nP -iTCP:$RELAY_PORT)"
  fi
  spawn "relay" "$RELAY_PIDFILE" "$RELAY_LOG" \
    python3 -m uvicorn relay.app.main:app --host 127.0.0.1 --port "$RELAY_PORT" --log-level info
fi
for _ in $(seq 1 120); do port_up "$RELAY_PORT" /health && break; sleep 0.5; done
port_up "$RELAY_PORT" /health || {
  say "relay did not become healthy — last log lines ($RELAY_LOG):"
  tail -20 "$RELAY_LOG" >&2 || true
  exit 1
}
say "  relay healthy: $(curl -sf "http://127.0.0.1:$RELAY_PORT/health")"

# ── static web (8080) ────────────────────────────────────────────────────────
if port_up "$WEB_PORT" /; then
  say "  web already serving :$WEB_PORT — reusing (idempotent)"
else
  if lsof -nP -iTCP:"$WEB_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    die "port $WEB_PORT busy — another process owns it (lsof -nP -iTCP:$WEB_PORT)"
  fi
  spawn "web" "$WEB_PIDFILE" "$WEB_LOG" \
    python3 -m http.server "$WEB_PORT" -d web
fi
for _ in $(seq 1 30); do port_up "$WEB_PORT" / && break; sleep 0.5; done
port_up "$WEB_PORT" / || {
  say "web did not become ready — last log lines ($WEB_LOG):"
  tail -20 "$WEB_LOG" >&2 || true
  exit 1
}
for page in / /console.html /market.html; do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$WEB_PORT$page" || true)"
  [[ "$code" == "200" ]] || die "web page $page → HTTP ${code:-none} (log $WEB_LOG)"
done

# ── optional quick tunnel for the RELAY API ──────────────────────────────────
PUBLIC_URL=""
if [[ "${1:-}" == "--tunnel" ]]; then
  command -v cloudflared >/dev/null 2>&1 || die "cloudflared not installed (brew install cloudflared)"
  if pid_alive "$(read_pidfile "$TUNNEL_PIDFILE")"; then
    say "  tunnel already running — reusing (idempotent)"
  else
    spawn "tunnel" "$TUNNEL_PIDFILE" "$TUNNEL_LOG" \
      cloudflared tunnel --url "http://localhost:$RELAY_PORT" --no-autoupdate
  fi
  for _ in $(seq 1 60); do
    PUBLIC_URL="$(grep -Eo 'https://[A-Za-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null | head -1 || true)"
    [[ -n "$PUBLIC_URL" ]] && break
    pid_alive "$(read_pidfile "$TUNNEL_PIDFILE")" || { tail -20 "$TUNNEL_LOG" >&2 || true; die "cloudflared exited"; }
    sleep 0.5
  done
  [[ -n "$PUBLIC_URL" ]] || { tail -20 "$TUNNEL_LOG" >&2 || true; die "no trycloudflare URL in tunnel log"; }
fi

# ── summary ──────────────────────────────────────────────────────────────────
say ""
say "✅ TokenShare demo is up:"
say "   market:    http://localhost:$WEB_PORT/"
say "   console:   http://localhost:$WEB_PORT/console.html"
say "   market UI: http://localhost:$WEB_PORT/market.html"
say "   relay API: http://localhost:$RELAY_PORT  (health: /health)"
if [[ -n "$PUBLIC_URL" ]]; then
  say ""
  say "🌐 public relay URL: $PUBLIC_URL"
  say "   buyers can only reach the NEW address after re-registering the listing:"
  say "     scripts/re-register-endpoint.sh --network monad_testnet --endpoint $PUBLIC_URL"
  say "   (the tunnel proxies the relay API only; the web pages stay on :$WEB_PORT)"
fi
say ""
say "stop everything:  scripts/demo.sh stop"
say "logs:             $RELAY_LOG / $WEB_LOG$([[ -n "$PUBLIC_URL" ]] && printf " / %s" "$TUNNEL_LOG")"
