# TokenShare seller relay — container image (M7-A, Phala Cloud / Intel TDX).
#
# BUILD CONTEXT = THE REPO ROOT (the image also embeds the Foundry ABI
# artifacts that relay/app/chain.py loads from <repo-root>/contracts/out/).
#
#   # from the repo root:
#   forge build --root contracts        # generate ABI artifacts (fresh clone)
#   docker buildx build --platform linux/amd64 -f relay/Dockerfile -t tokenshare-relay .
#   # (Apple Silicon MUST force linux/amd64 — Phala CVMs are x86_64; a plain
#   #  `docker build` on x86_64 hosts/CI works without the --platform flag)
#
# Run (host mode, no TEE — falls back to RELAY_SELLER_KEY):
#   docker run --rm -p 8787:8787 --env-file .env tokenshare-relay
# See relay/README-docker.md for the full Phala Cloud deploy procedure.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# dstack-sdk is installed here but only imported when /var/run/dstack.sock
# exists (lazy import in relay/app/config.py) — host-mode boots never touch it.
COPY relay/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY relay/app/ ./app/

# One-shot Registry registrar (M7-B sidecar — docker-compose service
# `registrar`): SAME image, different command (python registrar.py). It is
# the only component that signs Registry/Register txs from INSIDE the CVM
# (the TEE-derived key never leaves the enclave). Public code, no secrets.
COPY relay/registrar.py ./registrar.py

# Foundry artifact ABIs: chain.py resolves them at
#   Path(__file__).resolve().parents[2] / "contracts" / "out" / ...
# and /app/app/chain.py → parents[2] == "/", so the artifacts belong at
# /contracts/out/... (public ABIs only — zero secrets).
COPY contracts/out/Escrow.sol/Escrow.json /contracts/out/Escrow.sol/Escrow.json
COPY contracts/out/Registry.sol/Registry.json /contracts/out/Registry.sol/Registry.json

# Non-root runtime user. NOTE: the dstack socket mounted at
# /var/run/dstack.sock must be readable by this uid — on Phala Cloud CVMs the
# socket is world-accessible by default (see README-docker.md for troubleshooting).
RUN useradd --system --no-create-home --uid 10001 tokenshare

# M15 R1 shared custody: pre-create the keystore directory OWNED BY THE
# RUNTIME UID (10001) with restrictive mode (0750, never 0777). A fresh named
# volume mounted at /data/tokenshare (docker-compose.yml `keystore-data`)
# initializes FROM this image directory, preserving the uid/gid/mode — that is
# the explicit volume-init mechanism; no root init container, no `user:`,
# no over-permissive dirs at runtime. The keystore FILE itself is always
# written atomically with mode 0600 by relay/app/keystore.py.
RUN install -d -o 10001 -g 10001 -m 0750 /data/tokenshare
USER tokenshare

EXPOSE 8787

# PORT comes from the relay env (default 8787, relay/app/config.py) and must
# match the published port in docker-compose.yml.
CMD ["sh", "-c", "exec python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8787}"]
