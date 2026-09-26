# TokenShare — Settlement Audit Anchor (Chainlink CRE)

**Bounty: Chainlink "Best workflow with CRE" ($3k).** A CRE workflow that turns
every `TokenShare Escrow.Settled` event into a **DON-audited, on-chain-anchored
settlement record**: it re-prices the receipt's model from the on-chain
Registry, cross-checks the seller relay's EIP-712 signed receipt against that
estimate, and writes the DON verdict (plus the receipt hash) to a consumer
contract via `writeReport`. This closes the one trust gap a relay can never
close by itself — *"did the seller charge exactly what the buyer was told?"* —
and makes the answer permanent and on-chain.

> **Scope note (official position):** per Chainlink's official Discord —
> *"simulation is fine! our team will deploy it"* — **simulate-only delivery
> is accepted** for this bounty. All commands below are local simulation;
> production registration (`cre workflow deploy`) is optional and documented.

## Architecture

```
                     ┌──────────────────────────────────────────────────┐
                     │  Monad testnet (chain 10143)                     │
                     │  Escrow.Settled(paymentId, buyer, seller,        │
                     │                 actualAmount, refundedAmount)    │
                     └───────────────┬──────────────────────────────────┘
                                     │ EVM log trigger (topic0 filter)
                                     ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ CRE workflow (TypeScript, cre/settlement-audit/main.ts)                     │
│                                                                             │
│  1. EVM read   Escrow.getPayment(paymentId)   → buyer/max/expiresAt/state   │
│  2. EVM read   Registry.getListing(seller)    → Listing v2: models[] +      │
│                parallel per-model prices[] (Registry v2)                    │
│  3. Confidential HTTP  GET {relay}/receipt/{paymentId}                      │
│       — bearer credential template-resolved INSIDE the enclave              │
│         ({{.receipts_bearer}}; simulation value from .env,                  │
│          deployed value from the Vault DON — never in node memory)          │
│  4. Verify     receipt.actualAmount  vs  per-model on-chain estimate        │
│                (getPrice @ trigger block / listing fallback, ±1 unit)       │
│  5. writeReport → ReceiptAnchor.onReport via Keystone forwarder             │
│       — DON-consensus signed report; verdict carried IN the report and      │
│         anchored verbatim by the consumer (MATCH / MISMATCH)                │
└──────────────────────────────────────────────┬──────────────────────────────┘
                                               ▼
                     ┌──────────────────────────────────────────────────┐
                     │ ReceiptAnchor (consumer contract, this repo)     │
                     │ records[paymentId] = AuditRecord{amounts,        │
                     │   receiptHash, upstreamHost, model, verdict}     │
                     │ events: ReceiptAnchored / DiscrepancyFlagged     │
                     └──────────────────────────────────────────────────┘
```

**Network facts (verified 2026-09-23):** Monad testnet chain id `10143`,
RPC `https://testnet-rpc.monad.xyz`; Escrow
`0x157C551D145d3c4bBF8f3554c43Fb3C931D71aD5`; Registry
`0xeD347cDc1761750E20C024459b38dedFb1462254` (Registry v2).

> **Registry address — authoritative source:** always take the Registry (and
> Escrow) address from `contracts/deployed.monad.json` at the repo root; this
> README, the `main.ts` header comment, and both
> `settlement-audit/config.*.json` files are snapshots of it. The v1 Registry
> (`0x3a44dB…CA93`) is deprecated — its old 7-component `getListing` ABI makes
> the workflow's v2 5-component decode throw out-of-bounds. **After the M10 v3
> deployment chain redeploys the Registry, refresh all four places** (snapshot
> is authoritative; the orchestrator's deploy chain does this).

Relay receipt endpoint:
`GET /receipt/{paymentId}` (see `relay/app/receipt.py` — EIP-712
`{domain, message, signature}` JSON).

## Verdict semantics

The verdict is computed **once**, by the workflow, as
`receipt.actualAmount` vs the per-model on-chain estimate
(`getPrice` at the **trigger block** / listing fallback, ±1 native-unit
tolerance), and is carried **inside the report** as `uint8 verdict`
(1 = Match, 2 = Mismatch). `ReceiptAnchor` anchors it verbatim — it validates
the value and never recomputes `settled-vs-receipt` on-chain.

Why the contract must not derive the verdict from `settledAmount` vs
`receiptAmount` (the two would contradict the DON in real flows):

1. **Escrow clamping.** `Escrow.settle` clamps the settled amount
   (`min(actual, maxAmount)`) while the relay receipt's `actualAmount` is
   unclamped. A fair charge can therefore anchor with
   `settledAmount < receiptAmount`; a naive on-chain comparison would flag
   MISMATCH for a receipt the DON just confirmed as correctly priced.
2. **Price finality race.** The DON prices at the trigger
   (`Escrow.Settled`) block. Reading prices at last-finalized instead would
   let a `Registry.updateModelPrice` landing inside the ~600 ms finality
   window after settle fabricate a MISMATCH — permanently anchored on-chain.

Consequently `settledAmount` and `receiptAmount` in the anchored
`AuditRecord` are **observational fields**, not verdict inputs: consumers who
care about clamping compare them directly. Real pricing discrepancies still
anchor as MISMATCH and emit `DiscrepancyFlagged` — anchoring a dispute is the
trust-gap signal; refusing to anchor it would hide the gap.

## Files (Chainlink CRE template inventory)

This directory mirrors the `cre init` TypeScript "Hello World" template layout:

| File | Role |
| --- | --- |
| `project.yaml` | Project settings; `staging-settings.rpcs` carries the monad-testnet RPC (required by simulate) |
| `secrets.yaml` | Declares secret **names** (`receipts_bearer`, AES key); values come from `.env` in simulation / Vault DON when deployed |
| `.env.example` | `CRE_ETH_PRIVATE_KEY` (64-hex, no 0x) + secret values. **Never commit the real `.env`** |
| `.gitignore` | `.env`, build artifacts, node_modules |
| `settlement-audit/main.ts` | The workflow (single `main` export — CRE requires the entry bundle to export only parameter-less functions) |
| `settlement-audit/workflow.yaml` | Per-workflow artifacts: entry path, config path, secrets path, workflow names per target |
| `settlement-audit/config.staging.json` / `config.production.json` | Addresses + relay URL injected into the workflow via `runtime.config` |
| `settlement-audit/package.json` / `tsconfig.json` | Per-workflow deps (`@chainlink/cre-sdk`, `viem`, `zod`) and TS config |
| `contracts/src/ReceiptAnchor.sol` | Consumer contract: `onReport(bytes metadata, bytes report)`, forwarder set in constructor, anchors `ReceiptAnchored` / `DiscrepancyFlagged` |
| `contracts/test/ReceiptAnchor.t.sol` | Foundry tests (10) for the consumer |
| `contracts/foundry.toml` | Solc 0.8.26; `libs` includes the repo-wide forge-std |

## Quickstart (6 steps)

### Step 0 — Install tooling

```bash
curl -sSL https://app.chain.link/cre/install.sh | bash   # CRE CLI >= 1.30
bun --version                                             # Bun >= 1.2.21
```

Then authenticate the CLI (**the one prerequisite we could not do for you**):

```bash
cre login
# or non-interactively: export CRE_API_KEY=<key from app.chain.link Account Settings>
cre whoami        # must print your account before continuing
cre workflow supported-chains   # confirm monad-testnet is enabled for your tenant
```

> `cre workflow simulate` refuses to run without authentication: the capability
> registry lives in the logged-in user context. This is the only step blocked
> in the delivery environment; everything below was verified up to (but not
> including) the live simulator run.

### Step 1 — Deploy the consumer contract

```bash
cd cre/contracts
forge build && forge test          # 10/10 pass
forge create src/ReceiptAnchor.sol:ReceiptAnchor \
  --constructor-args 0xB9F79d863261869B234c481D1f9A7af84AeAd192 \
  --rpc-url https://testnet-rpc.monad.xyz \
  --private-key $CRE_ETH_PRIVATE_KEY
```

The constructor argument is the **MockKeystoneForwarder for monad-testnet** used
by `cre workflow simulate --broadcast` (docs: *Simulation Testnets* table).
Re-verify it for your tenant with `cre workflow supported-chains` before
deploying — and remember production swaps in the real `KeystoneForwarder`.

### Step 2 — Configure the workflow

```bash
cd cre/settlement-audit && bun install
cp ../.env.example ../.env         # then fill in:
#   CRE_ETH_PRIVATE_KEY  = 64-hex test key (no 0x), funded with a little MON
#   RECEIPTS_BEARER_ALL  = dummy value is fine (relay endpoint is open)
#   AES_ENC_KEY_ALL      = optional 64-hex AES key if you enable encryptOutput
```

Edit `config.staging.json`:

- `anchorAddress` → the ReceiptAnchor address from Step 1
- `registryAddress` → already set from the authoritative
  `contracts/deployed.monad.json` snapshot (Registry v2,
  `0x27c7128F…6b77A`); refresh it after any redeployment — the M10 v3
  deployment chain will re-point this. Never point it at the deprecated v1
  Registry: the workflow decodes the v2 5-component `getListing` ABI and the
  v1 shape makes the read throw out-of-bounds
- `relayBaseUrl` → the relay base URL from the Registry listing
  (M5b demo used `http://127.0.0.1:8787`)
- `workflowOwner` → your CRE workflow owner address (from `cre whoami`)

### Step 3 — Simulate (trigger from the real M5b settle tx)

Dry run (no gas, no broadcast):

```bash
cd cre   # project root (cre/project.yaml lives here)
cre workflow simulate settlement-audit --target staging-settings \
  --non-interactive \
  --trigger-index 0 \
  --evm-tx-hash 0x0764b45115591f442d4b64fee28ce3d4c28cf91015a0da69e02ed879bd346781 \
  --evm-event-index 0
```

* `--trigger-index 0` selects the **only** handler in `main.ts` (indices are
  0-based; official multi-trigger examples use 1 for a second entry).
* `--evm-tx-hash` is the **real M5b settlement tx** on Monad testnet — its
  receipt contains exactly one `Settled` log (paymentId=2, actualAmount=730,
  refundedAmount=4,998,246 in 6-dp), verified via `cast receipt`.
* `--evm-event-index 0` = that log's index within the transaction.

Expected console shape: `Settled: paymentId=2 …`, `getPayment: …`,
`getListing: models=4 active=true …`, `Receipt: actualAmount=730 …`,
`Compare: model=… expected=730 receipt=730 → MATCH`, then the dry-run write
with `0x` tx hash. The price is resolved per the receipt's model (Registry
v2 `getPrice(operator, model)` read at the **trigger block**, falling back to
the Listing models/prices parallel arrays). If the model is missing on-chain
(getPrice reverts ModelNotFound and it is absent from listing.models), the
workflow returns `MODEL MISSING …` instead of anchoring.

### Step 4 — Broadcast the anchor and assert it on-chain

```bash
cre workflow simulate settlement-audit --target staging-settings \
  --non-interactive \
  --trigger-index 0 \
  --evm-tx-hash 0x0764b45115591f442d4b64fee28ce3d4c28cf91015a0da69e02ed879bd346781 \
  --evm-event-index 0 \
  --broadcast
```

`--broadcast` routes the report through the mock forwarder into
`ReceiptAnchor.onReport`. Assert:

```bash
ANCHOR=<address from step 1>
cast call $ANCHOR "isVerified(uint256)(bool)" 2 --rpc-url https://testnet-rpc.monad.xyz   # true
cast call $ANCHOR "records(uint256)(uint256,uint256,uint256,bytes32,string,string,uint8,uint64)" 2 \
  --rpc-url https://testnet-rpc.monad.xyz
cast logs --address $ANCHOR --rpc-url https://testnet-rpc.monad.xyz --from-block latest | head
```

Also fetch the receipt the workflow saw, to show the numbers match:
`curl -s http://127.0.0.1:8787/receipt/2 | jq .message` (run the M5b relay or
point `relayBaseUrl` at the public endpoint).

### Step 5 — Record the 3–5 minute demo video

1. **0:00–0:30** — the problem: buyer pays what the relay reports; show the
   `X-Receipt` from `GET /receipt/2` (EIP-712, signed by seller).
2. **0:30–1:30** — open `main.ts`, walk the 5 stages (trigger → EVM reads →
   Confidential HTTP → compare → writeReport). Highlight the enclave-secret
   template `{{.receipts_bearer}}` and that no key ever enters workflow code.
3. **1:30–2:30** — run the Step 3 simulate live; call out the real M5b tx hash
   and the MATCH verdict.
4. **2:30–3:30** — rerun with `--broadcast`; show `cast call isVerified(2)` →
   `true` and the `ReceiptAnchored` log on Monadscan.
5. **3:30–4:30** — mismatch story: tamper the receipt amount (point the
   workflow at a mocked relay), rerun → `DiscrepancyFlagged` emitted; the DON
   — not the seller — adjudicated.
6. **4:30–5:00** — recap bounty mapping: CRE log trigger + EVM client +
   Confidential HTTP + writeReport, Monad testnet, simulate-first delivery.

### Step 6 — README/submit housekeeping

- This README already lists every Chainlink-template file (table above).
- Bounty note: submission is simulate-only (official *"simulation is fine"*);
  `cre workflow deploy` is NOT required and intentionally not included.
- Keep `.env` out of git (`.gitignore` handles it); never log keys.

## Verification performed in this delivery environment

| Check | Result |
| --- | --- |
| `tsc --noEmit --strict --noUnusedLocals` (workflow) | **0 errors** |
| `cre-compile` (bun → JS → javy → WASM 2.7 MB) | **success** |
| `forge build` (ReceiptAnchor, solc 0.8.26) | **success** |
| `forge test` (10 consumer tests) | **10/10 pass** |
| M5b settle tx + Settled log verified on Monad testnet | `cast receipt` (1 log, paymentId=2, actual=730) |
| `cre workflow simulate` live run | **blocked by CRE login wall** (requires `cre login` / `CRE_API_KEY`; browser-less environment). Follow Step 0 → Step 3. |

## Troubleshooting

- **`Authentication required` from any `cre` command** → run `cre login` or set
  `CRE_API_KEY` (created at app.chain.link → Account Settings).
- **`Exported functions with parameters are not supported`** during build →
  the entry `main.ts` must export **only** `main` (parameter-less). Handler
  callbacks must stay unexported (this repo already complies).
- **Simulate can't find the chain** → confirm `project.yaml` rpcs list
  `monad-testnet` and CLI ≥ 1.30 (Monad support landed in v1.30.0).
- **Receipt 404** → the relay only keeps the latest receipt per paymentId in
  memory; rerun the M5b relay flow or use a fresh settle tx as trigger input.
