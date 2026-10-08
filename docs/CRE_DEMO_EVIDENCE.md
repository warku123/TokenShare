# CRE Demo Evidence — actual 2026-10-08 completion (Monad testnet, MOCK forwarder)

**What this documents.** On **2026-10-08** the settlement-audit workflow was run
end-to-end on Monad testnet (chain id **10143**) as a **local `cre` CLI
simulation twice — once without `--broadcast` (dry-run) and once WITH
`--broadcast`** — the broadcast routed the DON-style report through the
**permissionless, zero-validation MockKeystoneForwarder**
(`0xB9F79d863261869B234c481D1f9A7af84AeAd192`) into the demo-instance
ReceiptAnchor `0xE2d468cdD917E73C9F02d4Ac629223Ac062D901D`
(`ReceiptAnchor.sol`). Every number below is copied from the public evidence
files of the approved run directory
`/private/var/folders/xn/hs1mq79s29v0j1c1z5mmsf_40000gn/T/opencode/cre-demo/`
— nothing is inferred.

**What this does NOT claim.**
- This is **NOT a production Chainlink DON deployment**: no DON consensus
  signatures exist end-to-end; the mock forwarder validates nothing, and
  ConfidentialHTTP never exercises a production TEE. `cre workflow deploy`
  (production workflow registration) was **not performed** and remains out of
  scope of this demo (see `cre/README.md` scope note).
- The audit claim stays **narrow**: the LAST per-call relay receipt's amount
  vs the per-model Registry pricing **at the trigger block** (±1 native
  unit). No claim about single-call provenance, cumulative settlement
  totals, true upstream token truth, locked quoted prices, provider resale,
  or DON trust.

---

## 1. Public identifiers (all verifiable on-chain)

### Contracts

| Contract | Address / hash | Notes |
| --- | --- | --- |
| Escrow v3.2 | `0xe4D5Eb0dBDB6DB8063C07ECF7EDFcCdDB9Ad514c` | emits `Settled` |
| Registry v4 | `0xeD347cDc1761750E20C024459b38dedFb1462254` | per-model prices |
| ReceiptAnchor (this demo) | `0xE2d468cdD917E73C9F02d4Ac629223Ac062D901D` | deploy tx `0x211b9094f8d0e5dff6856803e50c75332f3aa1fd8dcb8ff1b3ea0e4e80e5802a`, **block 69225003**, deployer `0x38c26E1782b7E6F656Aa35EbF473e2ff0D718Dd6`, ctor forwarder = mock `0xB9F7…192`, on-chain runtime code = artifact byte-for-byte (`code_match: "exact"`, artifact sha256 `a91504c54bfbd81b1ddfd16a4126374fe5d5e2117b8ff60f4e8b240e7bb96517`) |
| MockKeystoneForwarder | `0xB9F79d863261869B234c481D1f9A7af84AeAd192` | simulation tenant; zero-validation |
| Shared relay signer | `0x49CA3eADB3b8F23623f735918A6F2b945F60fb2D` | EIP-712 receipt authenticator (≠ economic seller) |

RPC: `https://testnet-rpc.monad.xyz` (links: paste the hashes into any Monad
testnet explorer — none preferred here).

### p5 timeline (paymentId 5)

| Step | Value |
| --- | --- |
| Pre-state | `getPayment(5)`: buyer `0xA0ffF55bfa23AF58970f7C4b30DAba3e985eFef7`, seller `0x38c26E…8Dd6`, max 1,500,000; one relay capture of exactly **918** recorded |
| Final settle (NOT repeated by this demo — performed BEFORE the CRE runs) | tx **`0x04249c263cdc64661141bc3a0911ddec2b2c05e57f79d21b5fd0f682c07d1785`**, **block 69225090** (txIndex 2, the `Settled` log at **logIndex 4**), event data = `actualAmount 918` / `refundedAmount 1499082` exactly; payment → state 2 |
| Trigger convention | `--trigger-index 0`, `--evm-event-index 0` (EVM trigger log index **0** for the emitter+topic0-filtered log; raw receipt logIndex 4 — both recorded in `finalize.json`) |

### CRE workflow runs (the actual completion)

**Run 1 — dry-run (`--broadcast` absent)** 2026-10-08T19:01:55Z, evidence
`sim-dryrun-p5-tx04249c-event0-20261008b.log`:

- CLI-wired pipeline: trigger → `getPayment(5)` @ **block 69225090** (state=2)
  → `getPrice(k3-256k)` @ **the same block 69225090**
  (`priceCachedIn=3,000,000 priceInput=6,000,000 priceOutput=9,000,000`) →
  confidential `GET /receipt/5` → verify → adjudicate.
- Receipt authentication: signature recovered **`0x49CA…fb2D` ==
  configured trusted pin** (EIP-712 is the ONLY authenticity gate; the HTTP
  endpoint is anonymous HTTP 200 by design and the `receipts_bearer` header
  was a **placeholder-only** dummy — never claimed as authentication).
- Recompute: `expected = (0·cachedIn + 93·input + 40·output)//1e6 = 918`,
  receipt `actualAmount = 918` → **MATCH, verdict 1** (on-chain settled 918).
- Result string (no tx state, dry write `0x (dry-run — add --broadcast)`):
  `"AUDIT OK payment 5: model=k3-256k expected=918 receipt=918 (settled=918) tx=0x (dry-run — add --broadcast)"`.

**Run 2 — `--broadcast` (the actual completion)** 2026-10-08T19:12:06Z,
evidence `broadcast-p5-tx04249c-event0-20261008.log`:

- Identical workflow/config — CLI-printed **`Binary hash:
  da16c4d347b2f08754e408bcd6d9dbc2389eae73219e648b325f893f8a4e22ae`** and
  **`Config hash: ea77cedb460af642f31a6d2409e56da3883f77015f7199cbe82469706a7608a4`**
  are byte-identical between the dry-run and the broadcast (the config hash
  equals the sha256 of `cre/settlement-audit/config.staging.json` itself).
  (The local SDK `cre-compile` verification build of the same sources hashes
  `dda10d98c8bfc1d59abd28e15abd36be2fbed4a06e173ae09aff4df44a7835ce` — a
  separate builder artifact, listed for completeness.)
- **Anchor report tx: `0x9125c1983a1f50402f883521bcfd6b1f848051560a6f98c69fe13ce53ea97330`**
  — status 1, **block 69239836** (txIndex 2), `ReceiptAnchored` log at
  **logIndex 104** (EVM trigger/event index 0 as recorded), emitted ONLY by
  `0xE2d468cd…901D`; **no `DiscrepancyFlagged`** in the receipt (verified via
  `eth_getTransactionReceipt` log filter).
- Result string:
  `"AUDIT OK payment 5: model=k3-256k expected=918 receipt=918 (settled=918) tx=0x9125…7330"`.

### On-chain record (`eth_call` `records(5)`, post-broadcast; also in
`readback.json`)

```
paymentId      = 5
settledAmount  = 918
receiptAmount  = 918
receiptHash    = 0xd0aa997f9981d13ff29d2fb69aff879d54f44cd14cdd1b3ad1eb24f6477c631d
upstreamHost   = api.kimi.com
model          = k3-256k
verdict        = 1 (Match)
anchoredAt     = epoch 1791457930
```

- `receiptHash` equals `keccak256(signature bytes)` of the LIVE
  `GET /receipt/5` response — independently recomputed and matched.
- `isVerified(5) = true`; `totalAnchored() = 1`.
- The post-settle relay snapshot (raw response bytes + full
  `{domain, message, signature}`) was live-HTTP-200 re-fetched and compared
  byte-identically by the reviewed runner replay before the broadcast
  (`finalize.json` top-level `replay_verified: true` — the replay block
  carries `verified_at_unix`).

### Run hygiene

- `--broadcast` was used **exactly once**; no retry, no duplicate anchor
  write (pre-broadcast chain check: `totalAnchored()==0`,
  `isVerified(5)==false`, zero records, zero anchor events, no in-flight
  writes).
- No production workflow deployment, no repeated settle, no Solidity re-deploy,
  no remote service mutations. Private keys/env values were injected in
  process-environment only and never logged; log files are public facts only.
- `receipts_bearer` was a **simulation-only placeholder** (the demo relay's
  `GET /receipt/{id}` is anonymous by design); bearer is NOT authentication —
  the recovered EIP-712 signer is.

---

## 2. Verification inventory at demo completion

| Check | Result |
| --- | --- |
| Workflow unit suite (`bun test tests/`) | **87 tests, 305 assertions, 0 fail** |
| Runner suite (`cre/scripts/run_demo*` gates; `ora42`) | **65 PASS** |
| Consumer suite (`forge test` — receipts/tamper/verdict/readback) | **10 test functions** (compiled + passing in review environment) |
| `tsc --noEmit` (workflow sources) | PASS (0 errors) |
| `cre-compile` / CLI compiled workflow | PASS (`Binary hash da16c4d3…22ae` both runs) |
| Consumer contract on-chain code equality | exact (`readback.json` `code_match: "exact"`) |

---

## 3. Honest limits & provenance notes

1. **Mock anchor mutability**: `records[paymentId]` stores the LATEST record
   (a future report for 5 overwrites); `totalAnchored`/`totalMismatched`
   count delivered reports, not unique payments; `ReceiptAnchored` events do
   NOT repeat the record's model/host fields. The demo pinned a single
   report; multi-report behavior is not exercised on-chain here.
2. **Mock forwarder validates nothing**: report authenticity is conveyed for
   the demo flow only; a production DON deployment would add real
   consensus/signing guarantees absent here.
3. **Crash-evidence provenance is POST-HOC**: `finalize.json` is honest about
   it — `"derived_posthoc": true`, `"presign_gate_recheck_reproduced":
   false`, `"reconciled_from": "on-chain settlement re-verified after the
   broadcast-run evidence-write crash"`. The settle's gates were
   reconstructed from pinned constants during the labeled crash-reconcile;
   the pre-sign recheck was evidenced only in stdout, never persisted. This
   note is a NARROW disclosure, not a full provenance claim.
4. **Bearer placeholder**: the secret injected for simulation is a dummy; the
   run demonstrates local simulation secret-template resolution only; it
   does not prove enclave execution or TEE confidentiality.
   `/receipt` is anonymous (HTTP
   200 for anyone) — NOT an authentication boundary; the sole authenticity
   gate demonstrated is the EIP-712 recovery to the pinned signer.
5. **Pricing scope**: limited to latest-per-call historical arithmetic at
   the trigger block (`3e6/6e6/9e6 → 918`, ±1 tolerance). Nothing here
   claims single-call provenance, cumulative audit, token truth, lock-time
   quote validity, provider resale coverage, or DON trust.
6. **No live MISMATCH broadcast exists** in this demo run (the one broadcast
   was a MATCH); the MISMATCH verdict path is covered by the consumer
   unit tests, not by an on-chain DiscrepancyFlagged tx.

---

## 4. Reproduction (bounded; read p5 state first)

**Do NOT blindly re-run deploy / finalize / broadcast on p5 — the payment is
already Settled and the demo's single broadcast already happened.** Evidence
above is the completed trace; the correct paths, separately gated:

- **Read-only verify of THIS completed demo** (no tx — two stages verify
  different surfaces; the readback is on-chain-only, the finalize replay is
  the receipt comparator):
  ```
  python3 cre/scripts/run_demo.py --stage readback \
    --anchor 0xE2d468cdD917E73C9F02d4Ac629223Ac062D901D \
    --tx 0x9125c1983a1f50402f883521bcfd6b1f848051560a6f98c69fe13ce53ea97330
  python3 cre/scripts/run_demo.py --stage finalize   # read-only replay from the saved snapshot
  ```
- **A fresh demonstration payment** (separately gated; Option B in
  `cre/README.md` only): its own deposit → one call → same-amount settle →
  its own dry-run, then — only after all gates PASS — `--broadcast`. p5
  evidence is never reused for a second payment.
- Any live-HTTP receipt check ALWAYS follows the LIVE-GET-HTTP200 comparator
  rule: raw-body + full `{domain,message,signature}` equality with the
  preserved validated snapshot; any 404/non-200 is an explicit STOP with no
  local replacement or remote recovery.

---

## 5. Evidence index (exact files in the approved temp cre-demo dir)

| File | Content |
| --- | --- |
| `deploy.json` | anchor deploy gates: address, tx `0x211b9094…`, block 69225003, artifact sha `a91504c5…`, `code_match: "exact"`, status 1 |
| `finalize.json` | settle `0x04249c…` receipt (status 1, block 69225090, amounts 918/1499082), `trigger_evm_event_index 0`, `trigger_log_index 4`, post/state + **raw-byte receipt snapshot** + read-only `replay` verdict |
| `sim-dryrun-p5-tx04249c-event0-20261008b.log` | dry-run run: stages, trigger reads, MATCH verdict, dry write, binary/config hashes `da16c4d3…` / `ea77cedb…` |
| `broadcast-p5-tx04249c-event0-20261008.log` | broadcast run: identical binary/config hashes, `ReceiptAnchored … tx=0x9125…7330 verdict=1`, final result string |
| `report-tx-p5-20261008.txt` | persisted report tx hash (written immediately when first observed) |
| `readback-report-tx-9125c198-20261008.log` + `readback.json` | independent read-only verification: forwarder ok, `records` exact, `isVerified true`, `totalAnchored 1`, event `(block 69239836, logIndex 104, args …)` |
| `preflight-readonly-p5-20261008.log` | pre-settle Locked gate correctly refuses post-settle state (fail-closed evidence) |
| `sim-dryrun.log` | earlier ill-fated attempt retained for history (timeout-shape bug, since fixed + unit-`regressed`) |

*(The directory name is a session-local approved scratch path; the durable
public evidence values are exactly the ones pinned in this document — both
kept; nothing secret is in any of them. No temp artifacts or `binary.wasm`
are committed to the repo.)*
