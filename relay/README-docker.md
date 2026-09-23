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

| file            | purpose                                                        |
|-----------------|----------------------------------------------------------------|
| `Dockerfile`    | python:3.12-slim + uvicorn, non-root user                      |
| `docker-compose.yml` | mounts `/var/run/dstack.sock`, publishes 8787, healthcheck |

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
3. ⚠️ **The TEE-derived address ≠ your previous seller address.** Re-register
   the Registry listing **from the derived address** (`derivedAddress`) and fund
   it with MON (`faucet.monad.xyz`).
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

## Notes / pitfalls

- In TEE mode `RELAY_SELLER_KEY` is ignored — leave it unset in the CVM `.env`.
- The dstack socket must be readable by the non-root container user; Phala
  Cloud mounts it world-accessible. If you see `Permission denied` on the
  socket, run the container with `user: root` locally (host mode) to debug.
- The listing endpoint may change across redeploys — **freeze the final URL
  into the Registry listing before the demo**.
- `docker-compose.yml` forces `platform: linux/amd64` so Apple Silicon builds
  produce a runnable image for TDX CVMs.
