# Docker / Phala Cloud TEE deployment (M7-A + M15 R1 files)

> ## DOCUMENT STATUS — read before deploying (updated 2026-10-07: v3.2 deployed + image pushed; TEE still gated)
>
> - **M15 R1 (shared custody)**: code complete; the independent review gate
>   has **PASSED** (static suite, 285 tests). The docker files below are
>   **preparation**: the AMD64 image is now **built and pushed** —
>   `docker.io/warku123/tokenshare-relay:m15-audited-20261007T015955Z`,
>   OCI index digest
>   `sha256:5a8c33af444a35fad57cc1922f9d0a32f4327611734e4a10709ef12b044c66e0`
>   (linux/amd64 leaf `sha256:4d36db02dd2384355d004b972748eb815b4a242b52a15b9549336a7c692df6e2`),
>   anonymous-public — reference **that digest**, never a mutable tag.
>   Still **no CVM/TEE deployed and no git commit made** — the on-chain part
   is real: Monad **Escrow v3.2 is deployed** (2026-10-07,
   `0xe4D5Eb0dBDB6DB8063C07ECF7EDFcCdDB9Ad514c`, tx `0xc7ca…afce4e`).
> - **Phala Cloud remains NOT AUTHORIZED**: no CVM may be created, started or
>   prepared until (1) prerequisites are met, (2) the image exists with a
>   recorded digest — **now satisfied** — and (3) a **fresh explicit user
>   confirmation** of the exact host, create/start command, resources and
>   cost (§7) is given — before ANY prepare/commit/start. No automatic
>   restart tomorrow, no DELETE, no terms acceptance.
> - **Shared custody deploys use `relay/docker-compose.shared.yml` ONLY**
>   (dedicated file, single `relay-shared` service + persistent
>   `keystore-data` volume). `docker-compose.yml` is single-mode only: it
>   pins `RELAY_MODE=single` literally and contains NO shared service.
> - **M15 R2 (shared chat)**: **code implemented**; the independent R2 review
>   gate has **PASSED** (318-test static suite excluding the compose-env
>   tests, incl. 24 R2 tests). All of that evidence is **local/mock** — NOT a
>   live chain, NOT live TEE, NOT a final audit. The remaining shared-chat
>   flow work is **in flight and not yet passed**; shared mode is not
>   end-to-end buyer-ready yet.
> - **No live TEE for M15**: no CVM has been deployed; **no DCAP proof** of any
>   M15 relay exists. `Escrow v3.2` is **now DEPLOYED on Monad testnet
>   (2026-10-07)**: `0xe4D5Eb0dBDB6DB8063C07ECF7EDFcCdDB9Ad514c` (tx
>   `0xc7ca3ac798e16ace9f4554df51912aed235753f360d7f0a6417b7b0523afce4e`,
>   block 68854360); `contracts/deployed.monad.json` and all doc/config pins
>   now reference v3.2 — Registry v4 and official USDC unchanged. Shared-custody
>   on-chain enablement still requires the **seller's own
>   `approveSettleDelegate` authorization** after any shared TEE deployment.
> - **Deployment is gated**: prerequisites + independent review + a pushed
>   AMD64 **image digest** come FIRST, then a **fresh explicit go-ahead**,
>   BEFORE any prepare/commit/deploy (see §6 "Readiness gates" and §7
>   "Proposed Phala intent"). The §7 intent is a PROPOSAL — executing it is
>   **not** yet permitted.

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

| file                        | purpose                                                        |
|-----------------------------|----------------------------------------------------------------|
| `Dockerfile`                | python:3.12-slim + uvicorn, non-root UID 10001; COPYs `registrar.py` (M7-B sidecar); pre-creates `/data/tokenshare` owned 10001/0750 for the M15 shared keystore volume-init |
| `registrar.py`              | one-shot Registry registration signed by the TEE-derived key (**single mode only**, M7-B); fail-fasts on `RELAY_MODE=shared` before any key derivation/network/registration |
| `docker-compose.yml`        | **SINGLE mode only**: mounts `/var/run/dstack.sock`, publishes 8787, literal `RELAY_MODE=single` + `PORT=8787`, healthcheck; required runtime settings referenced as `${VAR}` interpolation (chain pins, `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `RELAY_CORS_ORIGINS`, dev-fallback `RELAY_SELLER_KEY`); `registrar` one-shot (`--profile register`, its six inputs interpolated too). NO shared service here |
| `docker-compose.shared.yml` | **DEDICATED shared-custody compose** (M15 R1): single `relay-shared` service, literal `RELAY_MODE=shared` / `SHARED_KEYSTORE_PATH=/data/tokenshare/keystore.json` / `PORT=8787`; required runtime settings referenced as `${VAR}` interpolation (`RELAY_PUBLIC_ORIGIN`, chain pins, `RELAY_CORS_ORIGINS`, dev-fallback `SHARED_*` keys), persistent `keystore-data` volume, amd64, dstack socket, no registrar |
| `relay/.env.example`        | env template incl. the SHARED block (RELAY_MODE/RELAY_PUBLIC_ORIGIN/SHARED_KEYSTORE_PATH/SHARED_* keys) |

## 1. Build

Build context is the **repo root** (the image embeds the Foundry ABI artifacts
that `relay/app/chain.py` loads). Phala Cloud CVMs are **x86_64 (Intel TDX)** —
on Apple Silicon build/push with:

```bash
forge build --root contracts       # fresh clone: generate the ABI artifacts first
docker buildx build --platform linux/amd64 -f relay/Dockerfile -t <registry>/tokenshare-relay . --push
docker buildx imagetools inspect <registry>/tokenshare-relay | grep Digest   # RECORD THIS
```

The **pushed image must be referenced by digest**, never by a mutable tag:

```
<your-registry>/tokenshare-relay@sha256:<digest-from-above>
```

 Never invent a registry/tag/digest — the registry is yours to create, the
digest comes from your own push. Plain `docker build -f relay/Dockerfile -t
tokenshare-relay .` works on x86_64 hosts/CI (no `--platform` needed). The
build context is whitelisted in `.dockerignore` (relay/app + requirements +
**registrar.py** + Dockerfile + the two ABI artifacts — nothing else, so no
secrets ever enter the image; `.env`, keystores, app-data and `secrets` dirs
are additionally re-excluded by name).

## 2. Local smoke (no TEE on a laptop — SINGLE mode)

```bash
cp ../.env.example ../.env   # fill in RPC/addresses/keys (SINGLE block)
docker run --rm -p 8787:8787 --env-file ../.env tokenshare-relay
curl http://127.0.0.1:8787/info        # "tee": {"enabled": false, ...}
curl http://127.0.0.1:8787/attestation # 404 — no dstack socket (expected)
```

Local shared-mode smoke uses the **dedicated compose file** (never
`docker-compose.yml`) and needs the three `SHARED_*` env keys on a
socket-less laptop (no dstack socket): see relay/.env.example "Key source B".
Compose interpolation resolves the `${...}` references from the `.env` in
the compose file's own directory (the `cd relay && cp ../.env.example .env`
flow) — set/uncomment `RELAY_CORS_ORIGINS` too (e.g. `*` for the local demo
only; any other official value for a real origin). Missing required vars
fail the render with a clear `required variable <NAME>` error instead of
booting half-configured:

```bash
cd relay
cp ../.env.example .env            # SHARED block incl. RELAY_MODE=shared + RELAY_PUBLIC_ORIGIN + RELAY_CORS_ORIGINS
docker compose -f docker-compose.shared.yml up
curl http://127.0.0.1:8787/info    # expect "mode": "shared" (host-mode keys)
```

## 3. Shared custody on Phala Cloud (M15 R1 — the new deployment shape)

`RELAY_MODE=shared` turns the relay into a key-custody host for EXTERNAL
sellers: each seller enrolls its upstream api key through the web frontend —
the key arrives **app-layer encrypted** (ECDH-P256-HKDF-SHA256-A256GCM envelope
to the relay's pinned upload key) and is stored KEK-wrapped (AES-256-GCM,
per-entry random DEK) in the encrypted keystore file. Shared custody ALWAYS
deploys via the **dedicated compose file** — locally AND on Phala Cloud.
Never `docker compose up relay-shared` from `docker-compose.yml` (that
service no longer exists there; that file pins `RELAY_MODE=single` and has
no keystore volume — a "shared" start from it is impossible by design):

```bash
cd relay
cp ../.env.example .env    # SHARED block: RELAY_MODE=shared + RELAY_PUBLIC_ORIGIN
docker compose -f docker-compose.shared.yml up          # LOCAL
# Phala Cloud (still gated — §6/§7; tdx.small is the authoritative size):
phala deploy -n <name> -c relay/docker-compose.shared.yml -e relay/.env -t tdx.small --wait
```

**One compose file per CVM, one relay container per CVM.** Do not merge the
two compose files with `-f a -f b` (both publish 8787) and never run the
single `relay` and the shared `relay-shared` on the same CVM.

- **Env delivery — compose INTERPOLATION, not `env_file` (NB1, corrected)**:
  neither compose file bakes in any secret or a defaulted image/digest, and
  **every runtime setting the container needs is referenced EXPLICITLY as
  `${VAR}` inside the `environment:` blocks** — `RPC_URL`, `CHAIN_ID`,
  `ESCROW_ADDR`, `REGISTRY_ADDR`, `USDC_ADDR`, plus `RELAY_PUBLIC_ORIGIN` +
  `RELAY_CORS_ORIGINS` in shared, plus `OPENAI_API_KEY` / `OPENAI_BASE_URL` /
  `RELAY_SELLER_KEY` in single, and the six registrar inputs (`RPC_URL`,
  `CHAIN_ID`, `REGISTRY_ADDR`, `REGISTRAR_ENDPOINT`, `REGISTRAR_MODELS`,
  `REGISTRAR_PRICES`). Why: on Phala, `phala deploy -e relay/.env` feeds the
  file to the **server-side compose interpolation** (stored encrypted by the
  cloud) — it is NOT a container `env_file`. A setting that is not
  `${...}`-referenced NEVER reaches the CVM container; before NB1 the
  container got only the literal pins and silently missed everything else
  (CORS silently fell back to the demo `*`). Locally the SAME `${...}`
  mechanism resolves from the `.env` sitting in the compose file's own
  directory (`relay/.env`) — that is compose's default lookup — or from
  whatever `--env-file` names. The `env_file: .env, required: false` block
  remains a LOCAL-only convenience that merges optional tunables/dev flags
  (it does not exist on the CVM and is not the cloud delivery path).
  Placeholder shape (never paste real values):
  `RPC_URL=https://rpc.example`, `RELAY_CORS_ORIGINS=https://front.example`.
  Required settings use `${VAR:?message}` — missing = render-time failure
  with a clear error, no silent half-configured boot; `RELAY_MODE`,
  `SHARED_KEYSTORE_PATH` and `PORT` stay pinned LITERALLY and override
  anything in `.env` by design (compose `environment` > `env_file`).
- **`RELAY_PUBLIC_ORIGIN` is REQUIRED** — the one canonical
  `https://host[:port]` buyer-facing origin (no path, no trailing slash,
  e.g. the gateway URL). Non-https origins are refused unless
  `ALLOW_INSECURE_ENDPOINT=1` (dev/test only). The relay fails fast without
  it (no degraded mode).
- **`SHARED_KEYSTORE_PATH` is pinned by `docker-compose.shared.yml`** to
  `/data/tokenshare/keystore.json` (the code default) — inside the persistent
  `keystore-data` volume below.
- **No `OPENAI_API_KEY` / `RELAY_SELLER_KEY` needed** in shared mode with dstack.
- With a dstack socket, the relay's three operational keys are **derived inside
  the TEE** via the **bare legacy** get_key paths
  `tokenshare/shared/signer/v1`, `tokenshare/shared/kek/v1`,
  `tokenshare/shared/upload/v1` (raw 32-byte outputs). The `SHARED_*` env keys
  are then IGNORED. This pins `dstack-sdk>=0.5.4,<0.6`: the **host-side 0.6
  surface is NOT used**, **no v1 KDF** is used, and the dstack **secp256r1
  (P-256) algorithm mode is NOT supported/used** (the upload P-256 scalar is
  mapped relay-side from the raw-32 signer-path style output).

### 3.0 Shared post-deploy assertions (describe what must hold — verify live)

Run against the live gateway (`https://<app-id>-8787.<gateway-domain>`) after
a shared deploy; these are the /info **mode / signature / container**
assertions. None of them is proven by these files — they must be checked on
the real CVM (and none is claimed done here):

- **Mode**: `GET /info` reports `"mode": "shared"`.
- **Signature (signer identity)**: `seller` == `shared.signer` == the
  TEE-derived shared signer address (`tokenshare/shared/signer/v1` path) —
  and it is **NOT** any seller's external EOA and **NOT** a registered
  Registry listing operator by relay action. `shared.uploadPubkeySha256` is
  non-empty and must be pinned after first boot for offline attestation
  verification (§6).
- **Container**: the Phala console shows **exactly one** relay container
  (`relay-shared`) and **no** `registrar` container; `GET /health` returns
  200; the keystore file exists at `/data/tokenshare/keystore.json` inside
  the `keystore-data` volume.
- **Origin**: `shared.origin` == the `RELAY_PUBLIC_ORIGIN` from `.env`
  (gateway form), and `tee.enabled: true` with `tee.keyPath:
  tokenshare/shared/signer/v1` inside the CVM.

### 3.1 Persistent keystore (volume, ownership, security)

- `docker-compose.shared.yml` mounts the named volume `keystore-data` at
  `/data/tokenshare`. The **image pre-creates that directory owned by
  uid 10001, mode 0750** (Dockerfile `install -d`), and Docker initializes a
  fresh named volume **from that image directory** — preserving uid/mode.
  That is the explicit volume-init approach: **no root init container, no
  `user:` override, never 0777** at runtime. The container keeps running as
  UID 10001; the keystore **file** itself is always written atomically with
  mode **0600** by `relay/app/keystore.py`.
- **KEK domain — read before changing anything:** each entry's DEK is wrapped
  under the KEK derived from the TEE's dstack app root, and the keystore
  records are authenticated (AAD) over
  `seller | schema | v | chain | escrow | registry | origin`. The key domain
  is therefore bound to the **app identity / KMS root (Phala Cloud KMS)** on
  which the CVM was enrolled, the **`RELAY_PUBLIC_ORIGIN`**, and the
  **chain id + Escrow/Registry addresses**. Changing ANY of those (new app
  id, different KMS root/api, new origin, new contract deployment) makes the
  existing keystore entries un-decryptable by design — **migration /
  re-enrollment is required**. Do not edit those values on a live keystore.
- **Restart continuity** (same CVM, same app/origin/contracts, persistent
  volume → keystore readable after stop/start) is the intended behavior but
  is **NOT verified** — it must be **real-tested** against a live CVM before
  being trusted. It is not guaranteed by these files alone.

### 3.2 Registrar is single-mode-only; sellers act themselves in shared mode

The `registrar` sidecar (§5) enforces the **historical single-mode invariant**
`listing.operator == the relay's own TEE-derived address`. In shared mode the
listing operator is the **seller's own external EOA**: the seller registers
its Registry listing and signs Escrow **`approveSettleDelegate`** through
**explicit web-frontend transactions** (never via the relay/registrar, never
via any key held inside the CVM). Do not run `registrar` in a shared CVM —
and you cannot by accident: the dedicated shared compose has no registrar
service, and `registrar.py` itself now **fail-fasts on `RELAY_MODE=shared`
before any key derivation, network access or registration** (test:
`relay/tests/test_registrar_shared_guard.py`).

### 3.3 Bootstrap gate — two-phase first boot (RELAY_BOOTSTRAP_MODE)

A fresh CVM must NOT start straight into full shared serving: the operator
first needs the TEE-derived identity (signer address, upload-pubkey hash,
app id) to compare against expectations and to learn the FINAL gateway
origin, before any seller key is ever stored. `RELAY_BOOTSTRAP_MODE=1`
(shared mode only) provides that read-only phase:

- **Phase 1 — boot gated**: start with `RELAY_BOOTSTRAP_MODE=1` and a
  temporary `https://<placeholder>.invalid` origin (https only; the
  reserved `.invalid` TLD is rejected in normal mode, so a placeholder can
  never survive into serving). The relay refuses to boot if the keystore
  path already exists (any content — empty entries, corrupt JSON, wrong KEK
  — is never parsed, read, migrated or deleted; a dangling symlink counts
  as existing). The gate serves ONLY `GET`/`HEAD` on `/health`, `/info`,
  `/attestation` (canonical path plus single trailing slash); every other
  method/path — including plain OPTIONS — answers an exact
  `503 {"detail":"bootstrap_mode"}` without touching routing. No keystore,
  no nonce store, no chain reads, no upstream calls happen in this phase;
  `/health` and `/info` report `"bootstrap": true` and omit
  `keystoreEntries`.
- **Between phases — read-only network lookup**: from `GET /info` take
  `shared.signer`, `shared.uploadPubkeySha256`, and (from the Phala console)
  the app id and the FINAL gateway origin.
- **Phase 2 — one env update, one restart**: in the SAME compose env set
  `RELAY_PUBLIC_ORIGIN=<final gateway origin>` and
  `RELAY_BOOTSTRAP_MODE=0`, then restart ONCE. The flag is frozen at
  startup — there is no runtime or HTTP switch.
- **Post-restart comparison**: signer address, `uploadPubkeySha256`, appId
  and the final origin must match the phase-1 values exactly; `/health`
  must report `"bootstrap": false`. ANY drift → **abort** and investigate
  before storing any seller key.
- **Honest caveats**: a same-compose restart preserving the KMS/appId is
  expected but NOT guaranteed by these files alone — it needs live
  verification; treat identity drift as fatal. Do not store any seller key
  until the phase-2 identity comparison AND an `/attestation` check have
  passed (a hardware quote is a self-report, not DCAP verification). The
  previously published image tag has NO bootstrap gate; a gated image is a
  new build/push, which is a separately authorized action.

## 4. Deploy to Phala Cloud (manual — your Phala account; still gated)

```bash
npm i -g phala
phala login
phala deploy -n <name> -c relay/docker-compose.yml -e relay/.env -t tdx.small --wait
phala cvms get <name> --json
```

Instance class: **tdx.small — 1 vCPU / 2 GB RAM / 20 GB disk** is the intended
size (the relay is a uvicorn service + read-mostly web3 usage; 1 CPU / 2 GB
is viable for the demo). `platform: linux/amd64` stays mandatory (TDX CVMs
are x86_64).

**Trust facts (corrected — replaces the old "Phala proxy" wording):**

- **Outbound** traffic (official LLM endpoints, RPC) leaves the CVM as
  **direct upstream HTTPS** with standard TLS certificate verification;
  the relay's HTTP client does **not follow redirects**. There is no relay
  usage of a Phala outbound proxy module.
- **Inbound via the default Phala gateway** (`https://<app-id>-8787.<gateway-domain>`):
  the TEE itself sees only transport-decrypted traffic, so a transport-level
  observer INSIDE the trusted boundary sees plaintext *transport* bytes — but
  the **seller api keys are protected at the app layer**: buyers'/sellers'
  browsers encrypt the upstream api key into the ECDH upload envelope to the
  **pinned upload key**, and only the KEK-wrapped ciphertext ever hits disk.
  The transport viewer never sees the plaintext upstream key.
- **Residual trust**: whoever governs the **Phala Cloud KMS root / app
  identity** can re-derive the shared keys; that governance is a residual
  dependency by design, not eliminated by TEE.
- The `/attestation` **quote is a self-report** produced by the CVM's own
  code path — it is **not independent verification** of the CVM's behavior.
  Independent verification = DCAP-style offline verification (dstack-verifier
  image or `POST https://cloud-api.phala.com/api/v1/attestations/verify`)
  against pinned values (app id, derived signer address, upload pubkey
  SHA-256). **None has been run yet for M15.**

## 5. SINGLE mode (M7-A flow below — unchanged)

> Everything in §5/§5.x describes the **single-mode TEE deployment**. It does
> NOT apply to shared deployments (§3.2).

### 5.1 Post-deploy (manual)

1. Verify TEE mode:
   ```bash
   curl https://<app-id>-8787.<gateway-domain>/info
   # expect "tee": {"enabled": true, ...} — and "mode": "single"
   ```
2. Fetch the quote:
   ```bash
   curl https://<app-id>-8787.<gateway-domain>/attestation > quote.json
   ```
3. ⚠️ **The TEE-derived address ≠ your previous seller address.** Run the
   **registrar sidecar** to re-register the Registry listing **from the
   derived address** and fund it with MON — the full procedure is
   **§5.2 TEE on-chain registration** below.
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
   prints this reminder because it only does best-effort structural parsing,
   and the quote is the CVM's own self-report (see the trust facts above).

### 5.2 TEE on-chain registration (registrar sidecar, M7-B) — single mode

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

**Full sequence** (single mode only):

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
   REGISTRY_ADDR=$REGISTRY_ADDR                   # from YOUR current deployment pins
   REGISTRAR_ENDPOINT=https://<app-id>-8787.<gateway-domain>   # the buyer-facing gateway URL
   REGISTRAR_MODELS=<your models>
   REGISTRAR_PRICES=<cachedIn:input:output triples>
   ```
   ⚠️ Addresses are hard-coded nowhere. The current pins are **Escrow v3.2
   `0xe4D5Eb0dBDB6DB8063C07ECF7EDFcCdDB9Ad514c` (deployed 2026-10-07,
   `contracts/deployed.monad.json` snapshot sha256 261527d2…9932be)** with
   Registry v4 unchanged — always re-read the snapshot after any redeploy.
   A shared TEE deployment additionally needs the **seller's own
   `approveSettleDelegate` authorization** before settlement can route
   through the shared relay (§3.2).
   These registrar inputs are referenced as `${...}` interpolation in the
   compose file, so they resolve server-side from the `phala deploy -e .env`
   inputs and DO reach the CVM container (NB1). NB2 split: the chain trio
   (`RPC_URL`/`CHAIN_ID`/`REGISTRY_ADDR`) is render-fatal when missing; the
   three registration-ONLY inputs (`REGISTRAR_ENDPOINT`/`REGISTRAR_MODELS`/
   `REGISTRAR_PRICES`) default EMPTY — the first deploy does NOT need
   `REGISTRAR_ENDPOINT` (the gateway URL only exists after it). When the
   registrar actually runs, registrar.py validates all of them up front and
   fail-fasts before any key derivation, network access or signing if one
   is missing (no degraded registration).

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
   ($RPC_URL/$CHAIN_ID as in `.env` — the example values above are the Monad
   testnet ones from `.env.example`.) The web market page and
   `tokenshare listings` show the same listing. Buyers then use the **TEE
   gateway URL** as the endpoint and can verify
   `/attestation` → `derivedAddress == listing.operator` before paying
   (quote verification: §5.1 steps 2/4/5 above).

### 5.3 Retire the old EOA seller's listing (pre-TEE migration — single mode)

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

## 6. Readiness gates — ALL of these BEFORE any deploy/confirm

- [ ] **M15 R1 review gate PASSED** (285-test static suite). M15 R2: code
      implemented, independent R2 review gate PASSED (318-test static suite
      excl. compose-env tests, 24 R2 tests) — all local/mock evidence, NOT
      live chain/TEE/final audit; in-flight e2e work not yet passed —
      NOT buyer-ready end to end.
- [ ] **AMD64 image built + pushed**, digest recorded, and the deployment
      references **`<registry>/tokenshare-relay@sha256:<digest>`** (own
      registry/digest — never an invented one).
- [ ] **Persistent keystore volume wired** (the dedicated
      `docker-compose.shared.yml` with the `keystore-data` volume) and the
      **restart-continuity real test PASSED** on the actual CVM.
- [ ] **CORS pinned to the production frontend origin(s)**:
      `RELAY_CORS_ORIGINS` — enforced at compose RENDER time now (NB1):
      both compose files fail the render when it is missing, and the demo
      default `*` is never applied silently.
- [x] **New Escrow v3.2 pins**: v3.2 **deployed 2026-10-07**
      (`0xe4D5Eb0dBDB6DB8063C07ECF7EDFcCdDB9Ad514c`, tx `0xc7ca…afce4e`,
      block 68854360, gasUsed 1436119); snapshot + pins updated. Registry v4
      and official USDC unchanged; settle delegate still **ZERO** (no
      authorization granted yet).
- [ ] **Offline attestation verification ready**: dstack-verifier /
      cloud-api verify runnable, with **pinned appId, derived signer
      address, upload-pubkey SHA-256** from this instance's own `/info` +
      `/attestation` (not invented ones).
- [ ] **Fresh explicit user go-ahead** for the exact intent in §7 — the
      prerequisite list above first, then the question. No deploy before that.
- [ ] **Phala Cloud stays NOT AUTHORIZED until then**: no CVM creation,
      prepare, commit or start without the fresh explicit confirmation of
      the **exact host, create/start command, affected resources and cost**
      (§7). No automatic restart tomorrow, no DELETE, no terms acceptance,
      no node-price change.

## 7. PROPOSED Phala intent — NOT approved, NOT executed

This is the **planned** (proposed-only) deployment; it is **not permission to
run anything**. It goes out only after the §6 gates pass, and only after a
fresh explicit confirmation:

| field        | proposed value                                        |
|--------------|-------------------------------------------------------|
| CVM name     | `tokenshare-m15-demo`                                 |
| type         | `tdx.small` — 1 CPU, 2 GB RAM, 20 GB disk             |
| region       | US-West (prod)                                        |
| network/env  | prod, node 26                                         |
| KMS          | Phala Cloud KMS enabled                               |
| runtime      | debug window **4 h**, then **stop**                   |
| NOT doing    | no DELETE, no terms acceptance, no node-price change  |

**Costs (proposed plan, per Phala pricing):** compute **$0.058/h** + storage
20 GB × **$0.000139/GB/h** = **$0.06078/h** total; the planned **4 h debug
window ≈ $0.24312**. Storage alone bills **$0.06672/day**. Platform credits
are **not guaranteed** and will be deducted normally. **Stopping the CVM does
NOT stop billing**: disk storage keeps billing until the CVM is deleted, and
**nothing restarts it automatically tomorrow** — starting it again is a
manual action, and deletion (stopping storage billing) is an explicit user
decision.

## Notes / pitfalls

- In TEE mode `RELAY_SELLER_KEY` is ignored — leave it unset in the CVM `.env`.
  In shared mode `RELAY_SELLER_KEY` / `OPENAI_API_KEY` are not needed at all,
  and with a dstack socket the `SHARED_*` env keys are likewise ignored.
- The dstack socket must be readable by the non-root container user; Phala
  Cloud mounts it world-accessible. If you see `Permission denied` on the
  socket, run the container with `user: root` locally (host mode) to debug.
- The listing endpoint may change across redeploys — **freeze the final URL
  into the Registry listing before the demo**. Changing `RELAY_PUBLIC_ORIGIN`
  later breaks the keystore key-domain (§3.1) — plan the origin up front.
- `docker-compose.yml` forces `platform: linux/amd64` so Apple Silicon builds
  produce a runnable image for TDX CVMs (`docker-compose.shared.yml` pins the
  same platform; its `build:` section is for gated production builds — the
  deployment must reference the pushed **digest**, §6).
- The `registrar` service is skipped by a plain local `docker compose up`
  (`profiles: ["register"]`) but is part of the container set Phala Cloud
  starts at CVM boot. ⚠️ NB2: profile-skipping does NOT skip compose
  INTERPOLATION — `docker compose config`/up resolves `${...}` in ALL
  services, profile-gated or not. That is why the three registration-ONLY
  inputs (`REGISTRAR_ENDPOINT`/`REGISTRAR_MODELS`/`REGISTRAR_PRICES`) use
  empty defaults: they are required only when the registrar actually RUNS,
  and `REGISTRAR_ENDPOINT` is unknown before the first deploy — a first
  deploy with only the seven relay settings renders and boots fine. If the
  registrar then runs without them (e.g. a phala CLI version that DOES
  start profile-gated services, or deliberately dropped `profiles:` lines),
  registrar.py fail-fasts in its own pre-execution validation — missing
  required environment variable(s), exit != 0 — BEFORE any key derivation,
  network access or signing; the one-shot `restart: "no"` design makes that
  harmless. The shared chain values (`RPC_URL`/`CHAIN_ID`/`REGISTRY_ADDR`)
  stay render-fatal. For a **shared** deploy use
  the **dedicated `docker-compose.shared.yml`**: it contains no
  `registrar` service and no default `relay`, so the CVM boots **exactly
  one** relay container (`relay-shared`); the original compose cannot start
  a shared relay by design (`RELAY_MODE=single` pinned, no shared service).
  Verify one-container in the Phala console.
- The registrar only ever prints the TEE-derived **address**, never the key.
- Shared-mode enrollment flow / custody endpoints: see `relay/app/main.py`
  (`/sellers/nonce`, `/sellers/keys`, `/sellers/{address}/status`) and
  `relay/app/custody.py` + `relay/app/keystore.py` — R2 (shared chat) is
  code implemented (independent R2 gate PASSED on the static suite — local/
  mock evidence only, not live chain/TEE/final audit); the remaining
  shared-chat flow work is in flight and not yet passed.
