# TokenShare — Settlement Audit Anchor (Chainlink CRE)

**Bounty: Chainlink "Best workflow with CRE" ($3k).** A CRE workflow that turns
every `TokenShare Escrow.Settled` event into an on-chain audit report: it
recomputes from the seller relay's authenticated EIP-712 receipt what the
per-model Registry pricing **at the trigger block** implies, and appends the
resulting verdict (plus the receipt hash) to an append-only on-chain
report/event history via `writeReport`. The pricing arithmetic an
authenticated receipt asserts is independently recomputed by the CRE
workflow and appended on-chain — not the relay's word alone. It makes no
claim about a locked quoted price, true upstream usage, cumulative
settlement totals, or the simulation mock's authenticity.

> **Scope note (official position):** per Chainlink's official Discord —
> *"simulation is fine! our team will deploy it"* — **simulate-only delivery
> is accepted** for this bounty. All commands below are local simulation;
> production registration (`cre workflow deploy`) exists but is NOT
> documented or included here. **Update 2026-10-08:** the simulation track
> has since been **actually completed end-to-end** — dry-run AND one
> `--broadcast` against Monad-testnet's **mock** forwarder with on-chain
> readback (full evidence and exact hashes:
> [`docs/CRE_DEMO_EVIDENCE.md`](../docs/CRE_DEMO_EVIDENCE.md)). That is still
> NOT a production DON deployment; production registration remains
> undocumented here.

> **Simulation limits — read before claiming anything.** Every command below
> routes through the **permissionless, zero-validation MockKeystoneForwarder**
> (`0xB9F79d863261869B234c481D1f9A7af84AeAd192`, Monad-testnet simulation
> tenant). A simulation therefore proves the workflow's **arithmetic and
> flow**; it does **not** prove production DON authenticity (no DON consensus
> signatures are validated) and does **not** exercise a real TEE for
> ConfidentialHTTP. Production deployments swap in the real
> `KeystoneForwarder` and execute inside the DON enclave.

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
│  2. EVM read   Registry.getPrice(seller, model) — exact trigger-block       │
│                price for the receipt's model (strict; NO getListing read,   │
│                NO fallback height — any failure ⇒ NO report, fail closed)   │
│  3. Confidential HTTP  GET {relay}/receipt/{paymentId}                      │
│       — Production DESIGN: bearer template {{.receipts_bearer}} resolves    │
│         inside the DON enclave (simulation from .env; deployed from the     │
│         Vault DON), meant to be invisible to node memory.                   │
│         THIS DEMO ran locally and proved only that secret-template          │
│         resolution works — enclave execution / TEE confidentiality /        │
│         node-memory isolation were NOT demonstrated.                        │
│  4. Verify     receipt.actualAmount  vs  per-model on-chain estimate        │
│                (exact trigger-block Registry getPrice, ±1 unit)             │
│  5. writeReport → ReceiptAnchor.onReport via Keystone forwarder             │
│       — runtime.report signs the RAW seven-field business ABI payload       │
│         (NOT onReport(...) calldata); the forwarder frames the call and     │
│         manages metadata; verdict carried IN the report, anchored verbatim  │
│         (production DON signatures; simulation mock validates none)         │
└──────────────────────────────────────────────┬──────────────────────────────┘
                                               ▼
                     ┌──────────────────────────────────────────────────┐
                     │ ReceiptAnchor (consumer contract, this repo)     │
                     │ records[paymentId] = AuditRecord{amounts,        │
                     │   receiptHash, upstreamHost, model, verdict}     │
                     │   — LATEST record per payment (new reports       │
                     │     overwrite); the report/event history is the  │
                     │     append-only trail (events do NOT repeat the  │
                     │     model/host fields)                           │
                     │ events: ReceiptAnchored / DiscrepancyFlagged     │
                     └──────────────────────────────────────────────────┘
```

**Network facts (verified 2026-10-07):** Monad testnet chain id `10143`,
RPC `https://testnet-rpc.monad.xyz`; Escrow
`0xe4D5Eb0dBDB6DB8063C07ECF7EDFcCdDB9Ad514c` (v3.2, deployed 2026-10-07,
tx `0xc7ca3ac798e16ace9f4554df51912aed235753f360d7f0a6417b7b0523afce4e`);
Registry `0xeD347cDc1761750E20C024459b38dedFb1462254` (Registry v4).

> **Registry address — authoritative source:** always take the Registry (and
> Escrow) address from `contracts/deployed.monad.json` at the repo root; this
> README, the `main.ts` header comment, and both
> `settlement-audit/config.*.json` files are snapshots of it. The current
> snapshot is **Registry v4** (`0xeD347cDc1761750E20C024459b38dedFb1462254`),
> whose `getPrice` returns the per-model `Price` row the workflow decodes
> (static 3-word tuple). The workflow no longer reads `getListing` at all
> (its shape — ONE dynamic Listing tuple, head word 0x20 — is pinned strictly
> in the unit tests instead). The v1 Registry (`0x3a44dB…CA93`) is
> deprecated. **After any future redeployment, refresh all four places**
> (snapshot is authoritative; the orchestrator's deploy chain does this).

> **Current runtime state (updated 2026-10-08, post-demo):** Escrow v3.2 and
> Registry v4 are deployed (addresses above) and the shared TEE relay is live
> at
> `https://c30652f4833465adda9bdc7ac557e8b45e8e66c6-8787.dstack-pha-prod5.phala.network`
> (`/health` → ok; relay receipts are EIP-712 signed by the relay's shared
> signer `0x49CA3eADB3b8F23623f735918A6F2b945F60fb2D`, which is **distinct
> from the economic seller** — sellers approve the relay as their Escrow v3.2
> settle delegate, funds still land on the seller). The CRE demo instance is
> **deployed and has actually completed one dry-run + one `--broadcast`
> report** on the testnet MOCK forwarder: demo ReceiptAnchor
> `0xE2d468cdD917E73C9F02d4Ac629223Ac062D901D` (deploy tx
> `0x211b9094…5802a`, block 69225003), staging
> `settlement-audit/config.staging.json` holds its REAL anchor/owner values
> (locked — `anchorAddress 0xE2d468cd…901D`, `workflowOwner
> 0x38c26E…8Dd6`). Full trace incl. report tx `0x9125c198…7330` and on-chain
> readback: [`docs/CRE_DEMO_EVIDENCE.md`](../docs/CRE_DEMO_EVIDENCE.md).
> Never re-deploy/re-broadcast p5 blindly — its demo is complete; reproduce
> via the read-only paths in that doc, or run a fresh payment with fresh
> gates.

Relay receipt endpoint:
`GET /receipt/{paymentId}` (see `relay/app/receipt.py` — EIP-712
`{domain, message, signature}` JSON).

## Verdict semantics

The verdict is computed **once**, by the workflow: `receipt.actualAmount` vs
the estimate recomputed from the receipt's own token counts and the per-model
on-chain price — `expected = (cached×cachedIn + (prompt−cached)×input +
completion×output) // 1e6`, with the price taken by an EXACT
`getPrice(operator, model)` read AT THE TRIGGER BLOCK: no Listing fallback
and no last-finalized read (implemented and verified in the reviewed working
tree — see Verification status below).
Receipts failing schema/signature validation are rejected without a report
(fail closed — likewise implemented under test). The verdict is carried
**inside the report** as `uint8 verdict` (1 = Match, 2 = Mismatch) and anchored
verbatim by `ReceiptAnchor` — the contract validates the value and never
recomputes `settled-vs-receipt` on-chain.

Why the contract must not derive the verdict from `settledAmount` vs
`receiptAmount` (those two fields legitimately diverge):

1. **Over-max settle reverts; the relay clamps.** Escrow v3.2 `settle`
   REVERTS with `ExceedsMaxAmount` when `actual > maxAmount` — there is no
   contract clamp. The relay clamps the settle amount to the remaining locked
   budget, while the EIP-712 receipt always carries the call's own unclamped
   amount: `settledAmount < receiptAmount` can therefore be the honest
   outcome of a budget-exhausted call, not fraud.
2. **Price finality race.** The DON prices at the trigger (`Escrow.Settled`)
   block. A contract-side recompute would read current state instead, so a
   `Registry.updateModelPrice` landing after settle would fabricate a
   MISMATCH — permanently anchored on-chain.

Additionally, `settledAmount` (the cumulative `Settled` actual) and
`receiptAmount` (the **latest per-call** receipt kept by the relay) address
different quantities in `settlePartial` flows. Both fields are therefore
**observational**, not verdict inputs: consumers who care compare them
directly. Real pricing discrepancies still anchor as MISMATCH and emit
`DiscrepancyFlagged` — anchoring a dispute is the trust-gap signal; refusing
to anchor it would hide the gap.

**What MATCH proves — and what it does not.** MATCH proves that the
authenticated receipt's pricing arithmetic is consistent with the Registry's
per-model prices **at the trigger block's registry state, within ±1 native
unit**. It does **not** prove: the truth of the token counts themselves (the
relay self-reports them), that the receipt's `upstreamHost`/`model` was
actually served, the buyer's agreed lock-time price, or full cumulative
settlement equality.

## Files (Chainlink CRE template inventory)

This directory mirrors the `cre init` TypeScript "Hello World" template layout:

| File | Role |
| --- | --- |
| `project.yaml` | Project settings; `staging-settings.rpcs` carries the monad-testnet RPC (required by simulate) |
| `secrets.yaml` | Declares secret **names** (`receipts_bearer`, AES key); values come from `.env` in simulation / Vault DON when deployed |
| `.env.example` | `CRE_ETH_PRIVATE_KEY` (64-hex, no 0x) + secret values. **Never commit the real `.env`** |
| `.gitignore` | `.env`, build artifacts, node_modules |
| `settlement-audit/main.ts` | Composition root of the workflow (single `main` export — CRE requires the entry bundle to export only parameter-less functions) |
| `settlement-audit/src/*.ts` | Workflow modules: pinned ABIs, zod config schema (+ trusted receipt signer), EIP-712 receipt schema + authenticated recovery, PIN pricing (±1), report payload, fail-closed handler |
| `settlement-audit/workflow.yaml` | Per-workflow artifacts: entry path, config path, secrets path, workflow names per target |
| `settlement-audit/config.staging.json` / `config.production.json` | Addresses + relay URL + trusted receipt signer, injected into the workflow via `runtime.config` |
| `settlement-audit/package.json` / `tsconfig.json` | Per-workflow deps (`@chainlink/cre-sdk`, `viem`, `zod`, `@noble/curves`) and TS config |
| `contracts/src/ReceiptAnchor.sol` | Consumer contract: `onReport(bytes metadata, bytes report)`, forwarder set in constructor, anchors `ReceiptAnchored` / `DiscrepancyFlagged` |
| `contracts/test/ReceiptAnchor.t.sol` | Foundry tests (10) for the consumer |
| `contracts/foundry.toml` | Solc 0.8.26; `libs` includes the repo-wide forge-std |

## Quickstart (6 steps)

### Step 0 — Install tooling

```bash
curl -sSL https://app.chain.link/cre/install.sh | bash   # CRE CLI >= 1.30
bun --version                                             # Bun >= 1.2.21
```

Then (optional in this demo — simulation ran without login; see note below)
authenticate the CLI:

```bash
cre login
# or non-interactively: export CRE_API_KEY=<key from app.chain.link Account Settings>
cre whoami        # prints your account when logged in
cre workflow supported-chains   # confirm monad-testnet is enabled for your tenant
```

> `cre workflow simulate` itself has been observed to run WITHOUT a logged-in
> user in this demo (`cre whoami` not logged in; dry-run and `--broadcast`
> both succeeded locally — capability schemas were resolved locally).
> Should your CLI build print `Authentication required`, run `cre login` or
> set `CRE_API_KEY` (see Troubleshooting). Secret values live only in your
> local `.env` (git-ignored) or in the Vault DON when deployed — never commit
> them, never echo them into logs or shell history.

### Step 1 — Deploy the consumer contract

```bash
cd cre/contracts   # from the repository root
forge build && forge test          # consumer suite (10 tests) must pass
```

Before deploying, load the deployer key securely — import it into a Foundry
keystore ONCE (`cast wallet import <name>`), or plan to use `--interactive` /
`--account` at deploy time. If you prefer the env variable, load
`CRE_ETH_PRIVATE_KEY` in a private shell from a secured source; keep the key
value out of argv, logs, and shell history.

```bash
forge create src/ReceiptAnchor.sol:ReceiptAnchor \
  --constructor-args 0xB9F79d863261869B234c481D1f9A7af84AeAd192 \
  --rpc-url https://testnet-rpc.monad.xyz \
  --account <KEYSTORE_NAME>    # or --interactive — no secret argv
```

Foundry 1.5 simulates first and requires an explicit **`--broadcast`** to
send (validated against the installed CLI: `forge create --help`). Review the
dry-run output, re-run the same command with `--broadcast`, and paste the
deployed address from the output into
`settlement-audit/config.staging.json` → `anchorAddress`. The demo's anchor
already exists on-chain — `0xE2d468cdD917E73C9F02d4Ac629223Ac062D901D`
(deploy tx `0x211b9094…5802a`, block 69225003, byte-exact code match to
artifact sha256 `a91504c5…6517`; see evidence doc). Never invent an address,
and don't deploy a second instance for p5 — p5's demo is complete.

The constructor argument is the **MockKeystoneForwarder for monad-testnet**
used by `cre workflow simulate --broadcast` (docs: *Simulation Testnets*
table). It is a **permissionless zero-validation mock**: it accepts reports
without checking DON consensus signatures — which is exactly why simulation
cannot prove production DON authenticity. Re-verify it for your tenant with
`cre workflow supported-chains` before deploying, and remember production
swaps in the real `KeystoneForwarder` (which does validate DON consensus).

### Step 2 — Configure the workflow

```bash
cd cre/settlement-audit   # from the repository root
bun install
cp ../.env.example ../.env         # then fill in:
#   CRE_ETH_PRIVATE_KEY  = 64-hex test key (no 0x), funded with a little MON
#                        (load it securely per Step 1; never in argv/logs)
#   RECEIPTS_BEARER_ALL  = dummy value is fine (GET /receipt is
#                          unauthenticated by design in the demo relay)
#   AES_ENC_KEY_ALL      = optional 64-hex AES key if you enable encryptOutput
```

Keep secret values only in `.env` (git-ignored, `.gitignore` handles it);
never log or commit them — the workflow reads them via local injection at
simulate time.

Edit `config.staging.json`:

- `anchorAddress` → the ReceiptAnchor address from Step 1. In the CURRENT
  config BOTH `anchorAddress` (demo anchor
  `0xE2d468cdD917E73C9F02d4Ac629223Ac062D901D`) and `workflowOwner`
  (`0x38c26E…8Dd6`) already hold the demo's real values — these are the
  values the completed dry-run + broadcast ran against (config file sha256
  `ea77cedb…608a4`), so leave them as-is for read-only reproduction; for a
  NEW demo instance you'd deploy your own anchor and update both accordingly
- `registryAddress` → pinned to the authoritative Registry v4
  `0xeD347cDc1761750E20C024459b38dedFb1462254` from
  `contracts/deployed.monad.json`; refresh it after any redeployment. Never
  point it at the deprecated v1 Registry (`0x3a44dB…CA93`).
- `relayBaseUrl` → the deployed shared TEE relay
  `https://c30652f4833465adda9bdc7ac557e8b45e8e66c6-8787.dstack-pha-prod5.phala.network`
  (already pinned in the config). A local dev relay at
  `http://127.0.0.1:8787` also works for a closed loop, BUT the workflow
  recovers each receipt signature and requires it to equal
  `receiptSignerAddress` exactly — so point `receiptSignerAddress` at the
  shared TEE signer `0x49CA3eADB3b8F23623f735918A6F2b945F60fb2D` for the
  public relay, or at your LOCAL relay's actual signing address for a local
  run; on any mismatch the receipt is rejected and nothing is anchored
  (fail closed). The shared TEE signer is a relay key, **distinct from the
  economic seller**: the seller receives the funds and the relay settles on
  their behalf as their approved settle delegate
- `receiptSignerAddress` → see above; must match the relay that serves the
  receipts
- `workflowOwner` → your CRE workflow owner address (from `cre whoami`)

### Step 3 — Audit scenario, then dry-run simulate

The relay performs the per-call partial capture (`settlePartial`), so a buyer
call does NOT finalize a payment, and the old M5b proof tx belongs to retired
contracts — never reuse it as a trigger. Every scenario below anchors ONE
receipt's pricing check for ONE payment; multi-call cumulative settlements
are out of scope for both scenarios. **Status 2026-10-08: Option A
(LIMITED p5) has been PERFORMED end-to-end — settle `0x04249c…d1785`, dry-run,
one `--broadcast` report `0x9125c198…7330`, readback PASSED
([evidence](../docs/CRE_DEMO_EVIDENCE.md)); Option B remains UNSAT (never
performed).** Do NOT re-run the A-chain on p5; its sections below are kept as
the exact gates that were enforced (the reproduction paths are read-only or
fresh-payment only). Two scenarios; pick accordingly:

#### Option A — LIMITED: the existing p5 payment (allowed without any
independent single-call artifact) — **PERFORMED 2026-10-08, see evidence doc;
the remaining legal runs on p5 are the read-only replays below**

p5 (paymentId 5) ALREADY carries one relay charge recorded on-chain, and the
full gate chain was enforced then executed: facts revalidated 2026-10-08
(read-only reads) showed `getPayment(5)` = buyer
`0xA0ffF55bfa23AF58970f7C4b30DAba3e985eFef7`, seller
`0x38c26E1782b7E6F656Aa35EbF473e2ff0D718Dd6`, max 1,500,000, state 1 (Locked);
`capturedOf(5)` = 918; latest signed receipt = EIP-712 domain
`{name "TokenShare Relay", version "1", chainId 10143}` with model `k3-256k`
(upstreamHost api.kimi.com), promptTokens 93 / cachedTokens 0 /
completionTokens 40, `actualAmount` 918, seller == escrow seller; the
receipt's signature recovers to the shared relay signer
`0x49CA3eADB3b8F23623f735918A6F2b945F60fb2D` — which is also the seller's
approved settle delegate (`settleDelegateOf(0x38c2…8Dd6)` → `0x49CA…fb2D`).

Gates — all must hold at settle time or STOP (no settle, no report):

1. Payment is untouched: `state == 1`, `capturedOf(5) == 918`,
   `getPayment(5)` maxAmount == 1,500,000, and the identity pair matches
   (buyer `0xA0ff…Fef7`, seller `0x38c2…8Dd6`). NO new p5 relay call between
   establishing the capture and settling — a new call would change the
   payment's latest receipt and void this gate.
2. Receipt is unchanged and valid — RE-FETCH it from the configured live
   relay: `GET /receipt/5` MUST return **HTTP 200**, and the live response
   must be identical to the preserved validated snapshot — raw body AND the
   full `{domain, message, signature}` — with the signature recovering
   exactly to the trusted pin `0x49CA3eADB3b8F23623f735918A6F2b945F60fb2D`
   (never taken from the receipt itself). The snapshot is a byte-comparison
   comparator ONLY — never a workflow input and never a fallback; a non-200
   (404 included) is an explicit STOP for Option A even though the verified
   snapshot exists locally (see Troubleshooting). On the live 200, revalidate
   the FULL schema, then the values: domain `{name "TokenShare Relay",
   version "1", chainId 10143}`; message `paymentId == 5`, `seller` equal
   to the Escrow seller `0x38c2…8Dd6`, model `k3-256k`, upstreamHost
   `api.kimi.com`, tokens 93/0/40, `actualAmount == 918`. Record the
   signature you observe and require the post-settle re-fetch to be
   byte-identical.
3. Settlement authority: the settle sender is the seller or its approved
   settle delegate (verified live: the delegate is the receipt signer).

Settle exactly `settle(5, 918)` on the Escrow — the captured amount itself.
NEVER settle a higher amount, no matter what recalculated prices say — an
amount deviation belongs in the anchored verdict, not in an inflated settle.
Effect: zero NEW seller charge (sellerTopUp = 0), zero NEW protocol fee, and
1,499,082 is refunded to the buyer through escrow; `capturedOf(5)` afterwards
reads 1,500,000 (max) — a terminal ACCOUNTING MARKER for full consumption,
not evidence that 1,500,000 was ever paid.

Post-settle verification (the settle was performed 2026-10-08 — the actual
tx is `0x04249c263cdc64661141bc3a0911ddec2b2c05e57f79d21b5fd0f682c07d1785`,
block 69225090; use it read-only or replicate against a fresh payment):

```bash
ESCROW=0xe4D5Eb0dBDB6DB8063C07ECF7EDFcCdDB9Ad514c   # authoritative address lives in contracts/deployed.monad.json
P5_FINAL_SETTLE_TX=0x04249c263cdc64661141bc3a0911ddec2b2c05e57f79d21b5fd0f682c07d1785
cast receipt $P5_FINAL_SETTLE_TX --json --rpc-url https://testnet-rpc.monad.xyz \
  | jq --arg e "$ESCROW" --arg b "0x000000000000000000000000a0fff55bfa23af58970f7c4b30daba3e985efef7" \
       --arg s "0x00000000000000000000000038c26e1782b7e6f656aa35ebf473e2ff0d718dd6" \
       --arg d "0x0000000000000000000000000000000000000000000000000000000000000396000000000000000000000000000000000000000000000000000000000016dfca" \
    '.status == "0x1"
     and (.logs | length == 1)
     and (.logs[0].address | ascii_downcase == ($e | ascii_downcase))
     and (.logs[0].topics[0] == "0xfee3b370ed9b189860c3135373a45fcc2660969d0c7892c7e952f1ab4b82c7cc")
     and (.logs[0].topics[1] == "0x0000000000000000000000000000000000000000000000000000000000000005")
     and (.logs[0].topics[2] == $b) and (.logs[0].topics[3] == $s)
     and (.logs[0].data == $d)'
# --arg d is the two 32-byte data words: exactly actualAmount 918 then
# refundedAmount 1499082 (0x...396 / 0x...16dfca). Expect ALL comparisons
# true: txStatus=1, ONE Settled log with paymentId 5 (topics[1]), buyer and
# seller exactly as above (topics[2]/[3]). Also re-read `getPayment(5)` →
# state 2 (Settled) and re-fetch /receipt/5 → byte-identical.
```

Do not grep for the word "Settled" or trust a log's position in printed
output — the emitter+topic0 filter is the authoritative selection, and the
buyer/seller topics and decoded data must exact-match the values above.

Epistemics — say exactly this and no more: it is true that exactly ONE
on-chain partial capture was found (918). It is NOT proved that exactly one
upstream call happened (no independent call artifact exists), and the
finalization does NOT prove the earlier charge was correct — the settle only
releases the buyer's remaining locked balance under the Escrow v3.2 rules.
The audit value is NARROW: the LAST per-call receipt's amount vs Registry
per-model pricing at the settle tx's block. Acceptance MATCH only if the 918
recalculates from the receipt's OWN token counts — (0·cachedIn + 93·input +
40·output) // 1e6 — against the k3-256k Registry row read AT THE TRIGGER
BLOCK, within ±1 native unit.

#### Option B — OPTIONAL fresh single-call payment (fuller invariants; not a
prerequisite for Option A)

Run one fresh payment from scratch (web console or `cli`, from the repository
root): deposit → lock a budget large enough that one call cannot cross the
relay flush threshold (a capture at ≈90 % of `maxAmount` triggers the relay's
immediate partial flush — it does NOT block subsequent calls) and still keep
the payment well short of its TTL window (the TTL backstop flush applies
regardless of budget size) → ONE relay call — sub-1 native dust is absorbed,
not captured — and the relay emits exactly ONE signed receipt (amount A) →
verify pre-final `capturedOf == A` and state
1 (Locked) → the seller (or approved delegate) sends the final
`settle(<paymentId>, A)` with the SAME amount — zero top-up, refund
`maxAmount − A`, payment → Settled (state 2, `captured` bumped to max).
When actually performed, Option B additionally records the one-call ↔
one-receipt ↔ one-capture lineage for that new payment — a stronger
demonstration than Option A's captured-amount settle (Option A is done and
pinned in the evidence doc; Option B is still UNSAT and remains the only path
for a NEW demonstration payment);
it still does not cover multi-call cumulative settlement. The same
emitter+topic0 event filter applies with `<paymentId>` and
`<FINAL_SETTLE_TX>` placeholders.

**Dry run (either option; no gas, no broadcast)** — the trigger is the final
settle tx of the scenario you actually ran (Option A: payment 5's own settle,
which exists since 2026-10-08: `0x04249c…d1785`, event ARRAY index 0 —
`cd cre/settlement-audit`, the settings/target live next to `workflow.yaml`):

```bash
cd cre/settlement-audit   # from the repository root (NOT from cre/)
~/.cre/bin/cre workflow simulate settlement-audit --target staging-settings \
  --non-interactive \
  --trigger-index 0 \
  --evm-tx-hash <SETTLED_TRIGGER_TX_HASH> \
  --evm-event-index <EVENT_INDEX_OF_THE_SETTLED_LOG>
```
   * `--trigger-index 0` — per the installed CLI help: "Index of the trigger
     to run (0-based)"; it selects the only handler in `main.ts`.
   * `--evm-event-index` — per the installed CLI help: "EVM trigger log index
     (0-based)". Take the index from the ACTUAL tx-receipt log list (position
     of the emitter+topic0-filtered log), never from grep line numbers or
     printed positions; re-verify the convention against your installed CLI
     before running.
   * Observed console shape (actual p5 dry-run, 2026-10-08):
     `Settled: paymentId=5 …` → `Trigger block: 69225090 — all reads at this
     exact height` → `getPayment(5)` → `getPrice(k3-256k)` at the same block →
     `Receipt auth: recovered <signer> == trusted <pin>` →
     `Compare: model=k3-256k expected=918 receipt=918 → MATCH` → the dry-run
     write `0x (dry-run — add --broadcast)`. Numbers depend on your payment —
     record real output; never present placeholder or historical values as
     live results.
   * Implemented price resolution (verified in the completed runs): an EXACT
     Registry `getPrice(operator, model)` read AT THE TRIGGER BLOCK — no
     Listing fallback, no last-finalized read, STRICT fail-closed: any
     transport/revert/decode/model-absent failure returns
     `NO ANCHOR … NO fallback` and anchors nothing.

### Step 4 — Broadcast the anchor and verify tx, event, and records readback

**This step was performed for p5 on 2026-10-08** (report tx
`0x9125c1983a1f50402f883521bcfd6b1f848051560a6f98c69fe13ce53ea97330`,
block 69239836, readback PASSED — do NOT broadcast p5 again; evidence in
[`docs/CRE_DEMO_EVIDENCE.md`](../docs/CRE_DEMO_EVIDENCE.md)). The commands
below are for a FRESH payment's own gates.

```bash
cd cre/settlement-audit   # from the repository root (NOT from cre/)
~/.cre/bin/cre workflow simulate settlement-audit --target staging-settings \
  --non-interactive \
  --trigger-index 0 \
  --evm-tx-hash <SETTLED_TRIGGER_TX_HASH> \
  --evm-event-index <EVENT_INDEX_OF_THE_SETTLED_LOG> \
  --broadcast
```

`--broadcast` routes the report through the mock forwarder into
`ReceiptAnchor.onReport`. Only after running this yourself, verify **all
three** layers:

```bash
ANCHOR=<address from step 1>
# 1) tx — the writeReport transaction actually mined:
cast receipt <ANCHOR_TX_HASH> --rpc-url https://testnet-rpc.monad.xyz   # status 1
# 2) event — decode from the tx's OWN receipt logs, filtered by the exact
#    anchor emitter + ReceiptAnchored topic0 (topic0 came from the installed
#    cast:  cast sig-event "ReceiptAnchored(uint256 indexed,bytes32 indexed,uint256,uint256,uint8)"):
cast receipt <ANCHOR_TX_HASH> --json --rpc-url https://testnet-rpc.monad.xyz \
  | jq --arg a "$ANCHOR" \
    '.logs[] | select((.address|ascii_downcase) == ($a|ascii_downcase))
              | select(.topics[0] == "0x14a7732a7d26cc9c5b9ec7517c6da7e9240c953ee937c8e6211dcefee5f2aab1")'
# 3) records readback — latest record, verdict, counters:
cast call $ANCHOR "isVerified(uint256)(bool)" <paymentId> --rpc-url https://testnet-rpc.monad.xyz
cast call $ANCHOR "records(uint256)(uint256,uint256,uint256,bytes32,string,string,uint8,uint64)" <paymentId> \
  --rpc-url https://testnet-rpc.monad.xyz
cast call $ANCHOR "totalAnchored()(uint256)" --rpc-url https://testnet-rpc.monad.xyz
cast call $ANCHOR "totalMismatched()(uint256)" --rpc-url https://testnet-rpc.monad.xyz
```

Readback semantics: `records[paymentId]` holds the **latest** record — a
later report for the same payment **overwrites** it. The report/event
history (`ReceiptAnchored` / `DiscrepancyFlagged`) is the append-only trail,
but those events do NOT repeat the record's model/host fields, and
`totalAnchored` / `totalMismatched` count **reports delivered, not unique
payments**.

Evidence tooling (read-only) — the two stages verify DIFFERENT things and
neither alone counts as both:
- `python3 cre/scripts/run_demo.py --stage readback --anchor <ANCHOR>
  --tx <ANCHOR_TX_HASH>` verifies the ON-CHAIN side only: identical runtime
  code, immutable forwarder, `records()` fields, counters, event decoding.
  It does NOT touch the relay and does NOT perform receipt raw-equality —
  a readback PASS is not a receipt-equality PASS.
- `python3 cre/scripts/run_demo.py --stage finalize` (no `--broadcast`:
  alreadySettled read-only replay) verifies the RELAY side: live
  `GET /receipt/{id}` HTTP 200 and the full-raw snapshot equality (body +
  `{domain, message, signature}`), preserving the LIVE-GET-200 comparator
  rule.
The p5 report `0x9125c198…7330` was verified through the readback tool, and
the p5 receipt raw-equality was verified through the finalize replay — both
independently (see evidence doc).

Also fetch the receipt the workflow saw, to show the numbers match:
`curl -s <relayBaseUrl>/receipt/<paymentId> | jq .message` (public TEE relay
or a local relay).

### Step 5 — Record the ≤3-minute submission video

The main submission video is capped at **3 minutes** (hard requirement — do
not suggest or produce a 3–5 min cut; `docs/VIDEO_SCRIPT.md` is the
product-level script). CRE beats that must fit inside the cap:

1. **0:00–0:30** — the problem: buyer pays what the relay reports; show the
   `X-Receipt` from `GET /receipt/<paymentId>` on the public relay (EIP-712,
   signed by the pinned relay signer).
2. **0:30–1:15** — open `main.ts`, walk the 5 stages (trigger → EVM reads →
   Confidential HTTP → compare → writeReport). Highlight the enclave-secret
   template `{{.receipts_bearer}}` and that no key ever enters workflow code.
3. **1:15–2:10** — run the Step 3 dry-run live against the scenario the
    README just walked (a FRESH payment's own gates, or show the recorded p5
    dry-run from the evidence doc); call out the trigger/settle tx and the
    MATCH verdict (`0x (dry-run)` write).
4. **2:10–2:45** — `--broadcast` for that fresh payment (p5 broadcast already
    exists and must not repeat); show the mined tx, the
    `ReceiptAnchored` log and the `records()` readback on the explorer.
5. **2:45–3:00** — discrepancy path in one line: an AUTHENTICATED receipt —
   valid signature, but its amount diverging from the trigger-block price
   math — yields MISMATCH and an anchored `DiscrepancyFlagged`; a tampered
   receipt (invalid signature) is rejected with no report at all (fail
   closed — implemented and covered). No live MISMATCH broadcast exists (the
   one broadcast was a MATCH): present this as the unit scenario — the
   `forge test` consumer suite covers the verdict handling. + recap: CRE log
   trigger + EVM client + Confidential HTTP + writeReport, Monad testnet,
   simulate-first delivery.

### Step 6 — README/submit housekeeping

- This README already lists every Chainlink-template file (table above).
- Bounty note: submission is simulate-only (official *"simulation is fine"*,
  quoted in the scope note above); `cre workflow deploy` is NOT required and
  intentionally not included. The video must stay ≤3:00.
- Keep `.env` out of git (`.gitignore` handles it); never log keys; secret
  values are injected locally only, never committed or printed.

## Verification status

**Current evidence (the completed 2026-10-08 demo; full trace and exact
hashes in [`docs/CRE_DEMO_EVIDENCE.md`](../docs/CRE_DEMO_EVIDENCE.md))** —
performed against the reviewed working tree at the time of the runs:

| Check | Result |
| --- | --- |
| Workflow unit suite (`bun test tests/`) | **87 tests / 305 assertions — 0 fail** |
| Runner gate suite (`cre/scripts/run_demo*`; independent review) | **65 PASS** |
| Consumer suite (`forge test` — ReceiptAnchor receipts/tamper/verdict) | **10 test functions, pass in review environment** |
| `tsc --noEmit` (workflow sources, strict) | PASS — 0 errors |
| `cre-compile` / CLI-compiled workflow (dry + broadcast) | PASS — identical `Binary hash da16c4d3…22ae` + `Config hash ea77cedb…608a4` in both runs |
| p5 final settle | **PERFORMED**: `0x04249c…d1785`, block 69225090, `Settled(5, 918, 1499082)` |
| `cre workflow simulate` dry-run | **PERFORMED**: 2026-10-08, evidence log; MATCH verdict recorded |
| `cre workflow simulate --broadcast` | **PERFORMED (once)**: report tx `0x9125c198…7330`, block 69239836; `ReceiptAnchored(5, 918, 918, 0xd0aa99…631d, verdict 1)`; readback PASSED (`isVerified(5) true`, `totalAnchored 1`) |
| Production `cre workflow deploy` / DON | **NOT performed** — out of this demo's scope by design |

**What remains UNSAT (by scope, not by failure):** a fresh single-call
payment (Option B, one-call/receipt/capture lineage), any live MISMATCH
broadcast, multi-call cumulative settlements, and everything a production
DON adds over the mock forwarder. Post-demo reproduction of p5 is READ-ONLY
(runner `readback` / `finalize` replay) and never re-broadcasts.

**Historical build evidence** — performed in an earlier delivery environment
against an OLDER revision of these sources (pre-dating the current modified
working tree); it establishes compile/test status at that time only:

| Check | Historical result |
| --- | --- |
| M5b-era settle tx + `Settled` log verified on Monad testnet | `cast receipt` (1 log) — **old deployment, superseded by v3.2; not a reusable trigger** |

## Troubleshooting

- **`Authentication required` from `cre workflow simulate`** → run
  `cre login` or set `CRE_API_KEY` (created at app.chain.link → Account
  Settings); the README asserts this for simulate specifically.
- **`Exported functions with parameters are not supported`** during build →
  the entry `main.ts` must export **only** `main` (parameter-less). Handler
  callbacks must stay unexported (this repo already complies).
- **Simulate can't find the chain** → confirm `project.yaml` rpcs list
  `monad-testnet` and CLI ≥ 1.30 (Monad support landed in v1.30.0).
- **Receipt 404** → Option A is decided by the configured LIVE relay only:
  `GET /receipt/5` MUST return HTTP 200, the live raw body AND its full
  `{domain, message, signature}` must be identical to the preserved
  validated snapshot, and the signature must recover exactly to the trusted
  pin `0x49CA3eADB3b8F23623f735918A6F2b945F60fb2D` (never taken from the
  receipt itself). The snapshot is a byte-comparison comparator ONLY —
  never a workflow input and never a fallback for a missing or non-identical
  live response. A 404 is therefore an explicit STOP even when a validated
  local capture exists: the relay keeps only the LATEST receipt per
  paymentId in memory (a relay restart loses it) and a final-settle tx
  CANNOT restore a lost receipt. No remote recovery, no relay mutation or
  restart, and never a new p5 relay call (one would replace the payment's
  latest receipt and void the gates) — none is authorized or attempted.
  A failed Option A means STOP (no settle, no report) — there is NO silent
  fallback. If you continue at all, run a fresh payment instead (Step 3
  Option B, optional) and establish ITS gates anew from that payment's own
  on-chain reads and live receipts; p5 evidence is never reused.
- **Simulate reads the wrong Escrow** → the workflow indexes the Escrow from
  `config.staging.json`; a settle tx hash from a retired deployment will not
  decode against the current one. Always use a Step 3-style fresh
  demonstration trigger.
