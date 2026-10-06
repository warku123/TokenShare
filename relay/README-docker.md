# Docker / Phala Cloud TEE deployment (M7-A)

Why TEE: buyers currently trust the relay's reported usage on good faith.
Running the relay inside a Phala Cloud **Intel TDX CVM (dstack)** closes part of
that gap:

- the **seller key is derived inside the enclave** (`dstack get_key`) — it never
  exists in env vars or on disk outside the CVM;
- `GET /attestation` returns the **TDX quote** with `reportData` bound to the
  TEE-derived seller address (44 zero bytes + 20-byte address, the same
  left-padding layout as Solidity `bytes32(uint256(uint160(addr)))`);
- `GET /info` reports TEE state, seller address and the official-endpoint
  upstream policy.

Outside a CVM (laptop / CI / plain docker) nothing changes: no
`/var/run/dstack.sock` → the relay requires `RELAY_SELLER_KEY` exactly as
before and `/attestation` returns 404.

## Files

| file                 | purpose                                                        |
|----------------------|----------------------------------------------------------------|
| `Dockerfile`         | python:3.12-slim + uvicorn, non-root user; also COPYs `registrar.py` (M7-B sidecar) |
| `registrar.py`       | one-shot Registry registration signed by the TEE-derived key (M7-B) |
| `docker-compose.yml` | mounts `/var/run/dstack.sock`, publishes 8787, healthcheck; `registrar` one-shot service (`--profile register`) |

## 1. Build

Build context is the **repo root** (the image embeds the Foundry ABI artifacts
that `relay/app/chain.py` loads). Phala Cloud CVMs are **x86_64 (Intel TDX)** —
on Apple Silicon build/push with:

```bash
forge build --root contracts       # fresh clone: generate the ABI artifacts first
docker buildx build --platform linux/amd64 -f relay/Dockerfile -t <registry>/tokenshare-relay:latest . --push
```

Plain `docker build -f relay/Dockerfile -t tokenshare-relay .` works on x86_64
hosts/CI (no `--platform` needed). The build context is whitelisted in
`.dockerignore` (relay/app + requirements + the two ABI artifacts — nothing
else, so no secrets ever enter the image).

## 2. Local smoke (no TEE on a laptop)

```bash
cp ../.env.example ../.env   # fill in RPC/addresses/keys
docker run --rm -p 8787:8787 --env-file ../.env tokenshare-relay
curl http://127.0.0.1:8787/info        # "tee": {"enabled": false, ...}
curl http://127.0.0.1:8787/attestation # 404 — no dstack socket (expected)
```

## 3. Deploy to Phala Cloud (manual — your Phala account)

```bash
npm i -g phala
phala login
phala deploy -n tokenshare-relay -c docker-compose.yml -e .env -t tdx.small --wait
phala cvms get tokenshare-relay --json
```

Costs: `tdx.small` ≈ $0.058/h (~$1.46/day) + storage $0.000139/GB/h (min 20 GB).
⚠️ **Stopped CVMs still bill storage — delete the CVM when you're done.**

Outbound traffic (official LLM endpoints, RPC) goes through the Phala proxy
module — no extra config. Inbound: `https://<app-id>-8787.<gateway-domain>`.

## 4. Post-deploy (manual)

1. Verify TEE mode:
   ```bash
   curl https://<app-id>-8787.<gateway-domain>/info
   # expect "tee": {"enabled": true, ...}
   ```
2. Fetch the quote:
   ```bash
   curl https://<app-id>-8787.<gateway-domain>/attestation > quote.json
   ```
3. ⚠️ **The TEE-derived address ≠ your previous seller address.** Run the
   **registrar sidecar** to re-register the Registry listing **from the
   derived address** and fund it with MON — the full procedure is
   **§5 TEE on-chain registration** below.
4. Anchor the quote digest on-chain (timestamp + bind quote ↔ address):
   ```bash
   cd contracts && forge script script/Deploy.s.sol ... # or deploy AttestationAnchor directly:
   forge create src/AttestationAnchor.sol:AttestationAnchor --rpc-url $RPC_URL --private-key $KEY
   cast send $ANCHOR_ADDR "anchor(bytes32)" $(python3 -c "import json;print(json.load(open('quote.json'))['quoteDigest'])") \
     --rpc-url $RPC_URL --private-key $KEY
   ```
5. Buyers verify without any SDK:
   ```bash
   tokenshare verify-attestation --quote "$(cat quote.json)" --anchor $ANCHOR_ADDR
   ```
   Full cryptographic verification of the quote itself (RSA/PCK cert chain,
   MRTD/RTMR values) needs the **dstack-verifier** docker image, or
   `POST https://cloud-api.phala.com/api/v1/attestations/verify` — the CLI
   prints this reminder because it only does best-effort structural parsing.

## 5. TEE on-chain registration (registrar sidecar, M7-B)

In TEE mode the seller key is derived **inside the enclave** (`dstack
get_key`) — it cannot be exported, so no external account can sign
`Registry.register`/`deactivate`. But the listing's `operator` MUST equal the
relay's signing address (the address buyers verify request signatures
against), so registration has to happen **from inside the CVM**.
`docker-compose.yml` therefore ships a one-shot **`registrar` service** (same
relay image, same dstack socket, `restart: "no"`): it derives the same key via
`dstack get_key("wallet/ethereum/tokenshare")` — the SAME path
`relay/app/config.py` uses — prints only the derived **address** (never the
key), asserts the RPC chain id, and:

- listing already active **and** endpoint/models/prices all equal → prints
  `already OK`, exits **0** (idempotent — safe on every CVM reboot);
- listing active but different → `deactivate()` (waits for receipt,
  `status == 1`) then `register(...)`;
- listing inactive / never registered → `register(...)` directly.

Gas follows the Monad testnet rules baked into the script:
`maxFeePerGas = baseFee*3 + 2 gwei`, `maxPriorityFeePerGas = 2 gwei`,
`gas = estimate*1.3 + 30k`.

### Full sequence

1. **Deploy** with the `registrar` service in the compose and the registrar
   env in `.env` (chain values are env-injected, never hardcoded — the Monad
   testnet values below are examples):
   ```bash
   phala deploy -n tokenshare-relay -c docker-compose.yml -e .env -t tdx.small --wait
   ```
   Registrar inputs in `.env`:
   ```
   RPC_URL=https://testnet-rpc.monad.xyz          # chainId 10143
   CHAIN_ID=10143
   REGISTRY_ADDR=$REGISTRY_ADDR                   # e.g. 0xeD347cDc1761750E20C024459b38dedFb1462254 (Registry v4, Monad testnet)
   REGISTRAR_ENDPOINT=https://<app-id>-8787.<gateway-domain>   # the buyer-facing gateway URL
   REGISTRAR_MODELS=deepseek-v4.1-flash,glm-5.3-flash
   REGISTRAR_PRICES=1000000:2000000:3000000,500000:1000000:1500000
   ```
   Prices are `cachedIn:input:output` per 1M tokens in **USDC native units**
   (6 decimal places, `1 USDC = 1000000`); parallel to `REGISTRAR_MODELS`, a
   single triple is broadcast to every model. ⚠️ Chicken-and-egg: the gateway
   URL exists only after the first deploy — deploy once to learn it, then
   redeploy with `REGISTRAR_ENDPOINT` set. The relay serves fine unregistered
   meanwhile; only buyer discovery waits for the listing.

2. **Fetch the TEE-derived address**: `GET /info` → `.seller` (identical to
   `/attestation` → `.derivedAddress`):
   ```bash
   curl https://<app-id>-8787.<gateway-domain>/info \
     | python3 -c "import json,sys; print(json.load(sys.stdin)['seller'])"
   ```

3. **Fund that address with ≥ 0.5 MON** (it needs gas for the registration
   tx(s)) — `https://faucet.monad.xyz`, or straight from your deployer:
   ```bash
   cast send $DERIVED_ADDR --value 0.5ether --rpc-url "$RPC_URL" --private-key "$DEPLOYER_KEY"
   ```

4. **Run the registrar once** — two paths:
   - **Real path (inside the CVM):** the container defined in the compose
     runs once at CVM boot after `phala deploy` (or a redeploy that includes
     the `registrar` service), signs from inside the enclave and exits.
     Check its container logs in the Phala console; the final log lines echo
     the terminal `getListing`.
   - **Debug-only (laptop):**
     ```bash
     cd relay && docker compose --profile register run --rm registrar
     ```
     ⚠️ This **fails on any non-TEE machine** (`dstack socket not found at
     /var/run/dstack.sock`) — the key only exists inside the CVM. Local runs
     are only useful against a real dstack socket (e.g. debugging inside a
     Phala CVM shell).

5. **Verify the listing on-chain** (`operator` must equal `/info` →
   `.seller`):
   ```bash
   cast call "$REGISTRY_ADDR" \
     "getListing(address)(address,string,string[],(uint256,uint256,uint256)[],bool)" \
     "$DERIVED_ADDR" --rpc-url "$RPC_URL"
   ```
   (`$REGISTRY_ADDR` = Registry v4, `$RPC_URL`/`$CHAIN_ID` as in `.env` —
   Monad testnet: `https://testnet-rpc.monad.xyz`, chainId `10143`.) The web
   market page and `tokenshare listings` show the same listing. Buyers then
   use the **TEE gateway URL** as the endpoint and can verify
   `/attestation` → `derivedAddress == listing.operator` before paying
   (quote verification: §4 steps 2/4/5 above).

### Retire the old EOA seller's listing (pre-TEE migration)

The listing registered from your **old EOA** stays on-chain (data is kept for
auditability) but must be marked inactive manually — the TEE-mode relay
cannot do it, the old key lives outside the enclave:

```bash
cast send "$REGISTRY_ADDR" "deactivate()" \
  --rpc-url "$RPC_URL" --private-key "$OLD_EOA_SELLER_KEY"
```

(`$OLD_EOA_SELLER_KEY` = the pre-TEE `RELAY_SELLER_KEY`.) Escrow is
untouched: `deactivate()` only flips the Registry listing's `active` flag;
locked payments take the normal buyer refund path after TTL.

## Notes / pitfalls

- In TEE mode `RELAY_SELLER_KEY` is ignored — leave it unset in the CVM `.env`.
- The dstack socket must be readable by the non-root container user; Phala
  Cloud mounts it world-accessible. If you see `Permission denied` on the
  socket, run the container with `user: root` locally (host mode) to debug.
- The listing endpoint may change across redeploys — **freeze the final URL
  into the Registry listing before the demo**.
- `docker-compose.yml` forces `platform: linux/amd64` so Apple Silicon builds
  produce a runnable image for TDX CVMs.
- The `registrar` service is skipped by a plain local `docker compose up`
  (`profiles: ["register"]`) but is part of the container set Phala Cloud
  starts at CVM boot. If your phala CLI version skips profile-gated services,
  drop the `profiles:` lines before the production deploy — the one-shot,
  idempotent design makes that change harmless.
- The registrar only ever prints the TEE-derived **address**, never the key.
