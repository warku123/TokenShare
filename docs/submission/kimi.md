# Kimi: TokenShare's First Real Upstream (Active)

> This article introduces TokenShare (a Monad hackathon project) to the Kimi / Moonshot AI team and related readers, and is meant to be read alongside [qwen.md](./qwen.md). It strictly separates three categories of content: **facts backed by engineering and on-chain evidence**, **"user statements" known only to the project owner**, and **explicit boundaries**. Every on-chain hash can be pasted into any Monad testnet explorer for verification; in-repo evidence is given as relative paths. This article contains no keys or credential values. Chinese version: [kimi_ch.md](./kimi_ch.md).

## Summary: Kimi's Two Roles in TokenShare

1. **Development tool** (user statement): Per the project owner's confirmation, Kimi-series models served as the interactive coding assistant throughout the project's daily development, from smart contracts to the relay service to the frontend pages.
2. **The platform's first real upstream** (facts with engineering and on-chain evidence): TokenShare is an open protocol that turns official LLM plan quotas into a "token-metered, on-chain escrow-settled" service. Kimi (Moonshot AI, OpenAI-compatible API) was the first upstream to complete the full loop of **real registration, real calls, and real on-chain settlement**; the latest demo (payment 5, `k3-256k`) has a publicly verifiable transaction covering the call, the metered settlement, and the refund, plus an on-chain-anchored settlement reconciliation demo (§4).

## 1. Current Status at a Glance (as recorded in submission materials, 2026-10)

- **On-chain**: Monad testnet (chain 10143), Escrow v3.2 `0xe4D5…514c` + Registry v4 `0xeD34…2254`, with official Circle USDC (6 decimals) for escrow and per-usage settlement ([contracts/deployed.monad.json](../../contracts/deployed.monad.json)).
- **Relay**: The shared multi-seller relay is deployed in a Phala TDX trusted execution environment (Phala Cloud CVM) and is active in the production-site configuration; **sellers do not need to run any TEE of their own** — application-layer encrypted escrow of the upstream key is enough (the self-hosted single-seller mode remains an option). The market page reads the Registry live: **the currently active listings are Kimi's** (`k3-256k` and `kimi-for-coding-highspeed`); a Qwen listing once existed and was delisted by its seller on 2026-10-09 after its demo was completed (listings are seller-controlled and can change at any time) ([web/config.js](../../web/config.js); the dual-seller wording at recording time is in the locally maintained video script, not in the repo).
- **Kimi side**: Both `k3-256k` and `kimi-for-coding` have recorded, on-file real calls; the latest complete loop is in §3.
- Note: The "deployed/active" statements above are deployment facts as recorded in the submission materials (including configuration and verification records); this article performs no real-time availability checks on live services.

**Why Monad**: TokenShare's per-call accounting depends on an EVM environment oriented toward low-cost, high-throughput, fast-confirmation transactions — the actual receipt for a single model call can be as low as 0.000918 USDC (the actual charge of payment 5 in §3), and the Monad testnet's low-fee design helps keep settlement overhead at this payment scale practically viable. This is a design-fit positioning statement, **not a measured conclusion**: upstream model usage and on-chain gas are two independent kinds of real cost, and this project has published no benchmark measuring "gas share of a single call"; low fees are a helpful property of the chain environment — they do not constitute proof of cost or revenue, and they do not imply any vendor endorsement.

## 2. Where Kimi Sits in the Architecture

TokenShare's product form is an "on-chain, pay-per-usage rental market for official plan quotas". The full path of a single request is:

- **Seller flow**: wallet identity verification → application-layer encrypted escrow of the upstream key (can precede listing) → VERIFY catalog (real fetch of the model list for cross-checking) → select models and publish three-tier per-model prices (cached input / input / output, per million tokens) → explicitly authorize a settlement delegate.
- **Buyer flow**: a wallet signature locks a USDC budget → a standard OpenAI-compatible API key is minted → deductions are metered per token; one lock supports multiple calls, and after the lock expires (TTL) only the unconsumed balance is refundable.
- **Request routing**: the relay only allows requests to official endpoints (host whitelist + model-prefix→vendor mapping + per-request consistency check, see [relay/app/config.py](../../relay/app/config.py)); requests are routed by the seller registered on the payment to **that seller's key**, never to arbitrary wallets.
- **Pricing and billing**: the on-chain `getPrice` fetches the seller-published three-tier price per model; the billing formula is `(cached×cachedIn + (prompt−cached)×input + completion×output)//1e6`. Note that these are **seller-self-published prices** — they are not the upstream official price list, and they are not independent proof of real cost.
- **Settlement**: a seller-authorized settlement delegate posts settlement on-chain on the seller's behalf; funds enter the seller's on-chain ledger balance; whether the protocol charges a fee is governed by Escrow.

The Kimi integration was the first to walk through this entire flow end to end: the API endpoints cover `api.moonshot.cn` (China) / `api.moonshot.ai` (international) / `api.kimi.com` (Coding plans), all with OpenAI-compatible Bearer authentication (endpoint records in [.env.example](../../.env.example)).

Upstream behavior differences that surfaced one by one during real integration and were written into the code are preserved below:

| Kimi upstream behavior | Our handling |
| --- | --- |
| Any value for `temperature`/`top_p`/`n`/`penalties` (including 0) returns 400 | Relay zero-injection: no default sampling parameters are added on the buyer's behalf; requests are passed through as-is |
| In streaming responses, usage appears in some chunk before `[DONE]`, possibly inside empty `choices` | Take the last non-null usage, with a fallback for the non-standard top-level `usage.cached_tokens` |
| Low-tier plans are strictly rate-limited (concurrency 1 / RPM 3) | The demo flow is paced accordingly; on the product side, "accumulate + threshold-based batch on-chain writes" reduce the number of on-chain transactions |
| Cached-input tokens have a separate unit price | Platform pricing directly uses the per-model three-tier prices, fetched on-chain per model |
| China / international / Coding keys are not interchangeable | Host normalization + the whitelist includes all three official hosts |

## 3. Real Call and On-Chain Settlement Evidence (payment 5, k3-256k)

This is a publicly verifiable, complete loop on the currently active Escrow v3.2 generation (details in [docs/CRE_DEMO_EVIDENCE.md](../CRE_DEMO_EVIDENCE.md)):

| Item | Value |
| --- | --- |
| Upstream / model | `api.kimi.com` / `k3-256k` |
| Token usage | prompt 93 · cached 0 · completion 40 |
| Actual charge | 918 (= 93×6 + 40×9, testnet USDC 6 decimals, i.e. 0.000918) |
| Budget cap / refund | 1,500,000 / refund 1,499,082 |
| Settlement tx | [`0x04249c26…c07d1785`](https://testnet.monadvision.com/tx/0x04249c263cdc64661141bc3a0911ddec2b2c05e57f79d21b5fd0f682c07d1785) (block 69225090) |

Recomputing token by token from the Registry's at-block three-tier prices (cachedIn 3 / input 6 / output 9, per million tokens), the result matches the settlement amount and the refund amount — all three sides agree. The earlier 2026-09 records (the first run and the multi-call metered loop under the then-deployed generation) belong to a **historical deployment phase**; their contract versions and snapshot details are not repeated in this article, to avoid confusion with the current-generation evidence; for current-generation evidence, this section prevails.

## 4. Settlement Reconciliation Demo: What It Proves and What It Doesn't

On 2026-10-08, we used a Chainlink CRE workflow to run a settlement reconciliation on this payment and anchored the result on-chain:

- **What was done**: the local CRE simulation ran twice (one dry-run, one broadcast), recomputing the billing formula at the settlement block height, retrieving the relay-signed EIP-712 receipt and verifying its signature; the comparison result was Match (918 = 918), and the report was anchored through a **permissionless testnet mock forwarder** to the ReceiptAnchor contract of the demo instance, in anchor tx [`0x9125c198…53ea97330`](https://testnet.monadvision.com/tx/0x9125c1983a1f50402f883521bcfd6b1f848051560a6f98c69fe13ce53ea97330) (block 69239836).
- **What it proves**: the receipt's amount is consistent with the on-chain prices and the on-chain settled amount at the settlement block height, and the anchoring action itself left a public transaction.
- **What it does not prove**: this is not a production Chainlink DON deployment (the mock forwarder performs no verification), nor does it constitute independent proof of the true origin of a single call, cumulative usage, or "real token consumption". The signed receipt certifies **the relay's billing statement**, not token truth — what a buyer can do with it is "reconcile against on-chain prices", not "prove the upstream actually produced these tokens".

## 5. Development-Tool Role (User Statement)

The project owner confirms: during the daily development of the contracts (Solidity/Foundry), the relay (Python/FastAPI), the frontend (vanilla JS), and the CLI, Kimi-series models were used continuously as conversational coding tools — for design discussions, code drafts, debugging, and copywriting.

**This article makes no attribution**: it does not identify which model version completed which task, does not count calls, does not claim saved engineering hours, and does not attribute test or audit results to model capability — the repo's final quality is the result of the engineering process (verbatim interface alignment, two-sided signature cross-verification, independent review), not of model performance.

## 6. New Positioning: A Metered Sales Channel That Official Vendors Could Also Use (Potential, Not Current)

Today TokenShare's sellers are "individuals/teams with idle quota". But the protocol's capability boundary is wider, and the project owner's one-line positioning is:

> **"They could also take on selling the official APIs — one more sales channel."**

That is: the same infrastructure not only suits putting idle personal subscriptions to work; it also, **technically**, supports the official vendors themselves — or formally authorized distributors/suppliers — using it as a pay-per-usage sales channel: per-model prices are published directly on the on-chain Registry, buyer budgets are hard-constrained by the escrow contract, every call produces a verifiable signed billing receipt, and the shared TEE gateway handles key escrow and routing for the channel party — developers and the agent ecosystem can integrate per token directly, while the channel party need not build any metering or settlement infrastructure.

**A boundary that must be drawn clearly**: this is a description of technical capability and positioning, **not** any actually existing vendor cooperation, authorization, contract, or revenue. This article does not claim Kimi/Moonshot is an official partner of TokenShare, and there is no written vendor authorization or commercial arrangement of any kind.

## 7. Boundaries and Honest Disclaimers

- **Receipt ≠ token truth**: receipts are issued by the relay; they are a signed certification of a billing statement — not proof of service quality, and not independent evidence of real token consumption.
- **Prices ≠ official prices**: the on-chain three-tier prices are seller-self-reported and are not necessarily equal to the upstream official price list.
- **TEE reduces risk; it does not eliminate it**: the shared TEE relay confines keys inside an enclave, but the platform operator and governance remain trust surfaces; this is not zero-custody, and it is not trustless.
- **Testnet tokens have no value**: testnet USDC/MON have no real value; but upstream plan quotas and cloud infrastructure are still **real costs**.
- **Refunds are contract-constrained**: refunds are limited by payment state, settlement, and TTL rules — they are not instantly withdrawable at any time; a seller-authorized settlement delegate can still settle existing locks after TTL (funds only ever enter the seller's ledger).
- **Revocation is an explicit action**: revoking a buyer's API key is an explicit operation; disconnecting the wallet does not revoke keys that have already been issued.
- **Compliance responsibility lies with the seller**: upstream sellers are themselves responsible for their accounts' compliance with vendor terms; this project does not constitute distribution authorization, and a disclaimer cannot substitute for authorization.

## 8. Verification Paths

- Current deployment facts: addresses, deployment times, and network are in [`contracts/deployed.monad.json`](../../contracts/deployed.monad.json) (that file contains no version numbers or deployment txs); Escrow v3.2 `0xe4D5…514c` (deployed 2026-10-07) and tx [`0xc7ca3ac7…523afce4e`](https://testnet.monadvision.com/tx/0xc7ca3ac798e16ace9f4554df51912aed235753f360d7f0a6417b7b0523afce4e) are in the root [README.md](../../README.md) "Networks" section; Registry v4 and the official USDC address are in the same JSON file.
- Full payment 5 evidence: [`docs/CRE_DEMO_EVIDENCE.md`](../CRE_DEMO_EVIDENCE.md) (settlement tx, CRE simulation and anchor tx, on-chain `records(5)` readback).
- Submission status and demo scope: [`docs/SUBMISSION.md`](../SUBMISSION.md); demo runbook: [`docs/DEMO_RUNBOOK.md`](../DEMO_RUNBOOK.md); video script: maintained locally, not in the repo.
- Relay whitelist and prefix mapping: [`relay/app/config.py`](../../relay/app/config.py); deployment and images: [`relay/README-docker.md`](../../relay/README-docker.md).
- Key escrow and policy: [`web/policy.html`](../../web/policy.html); market page configuration: [`web/config.js`](../../web/config.js).
- Kimi official endpoint records: [`.env.example`](../../.env.example) (`api.moonshot.cn` / `api.moonshot.ai` / `api.kimi.com/coding`).
- Index: [README.md](./README.md) (directory navigation only; individual status descriptions may lag this article — the primary evidence cited herein prevails).

## Fact Layering

| Layer | Content |
| --- | --- |
| Engineering / on-chain evidence | §3 payment 5 real call and settlement, §4 CRE reconciliation demo, §2 architecture and fit table, Escrow v3.2 deployment |
| User statement | §5 first-person description of using Kimi during development |
| Potential (not fact) | §6 technical support for official/authorized channels as a sales channel; does not represent any cooperation or authorization |
| Boundaries | All items in §7; the positioning boundaries of §1 "Why Monad" (not measured, no proof of cost/revenue, no vendor endorsement) |
