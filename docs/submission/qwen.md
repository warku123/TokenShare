# Qwen (Tongyi Qianwen): TokenShare's Second Real Seller (Historical Integration; Listing Delisted 2026-10-09)

> This article introduces TokenShare (a Monad hackathon project) to the Qwen / Alibaba Cloud team and related readers, and is meant to be read alongside [kimi.md](./kimi.md). It strictly separates three categories of content: **facts backed by evidence (together with their evidence boundaries)**, **"user statements" known only to the project owner**, and **explicit boundaries**. This article contains no keys or credential values, and makes no assertions unsupported by records. Chinese version: [qwen_ch.md](./qwen_ch.md).

## Summary: Qwen's Two Roles in TokenShare

1. **Development tool** (user statement): Per the project owner's confirmation, Qwen-series models served as the interactive coding assistant in this project's daily development.
2. **The platform's second real seller** (recorded evidence; evidence boundary in §3): The Qwen integration was completed — `qwen3.8-max` has recorded, on-file real API calls and partial on-chain settlement, the Qwen seller's key was escrowed in the shared TEE relay, the listing was once live on the market page, and a settlement delegate was authorized. After the demo was completed, the listing was **voluntarily delisted by the seller on 2026-10-09** (Registry.deactivate, see §1), in order to stop upstream quota consumption; the only currently active listing on the market is Kimi's. This article records that **historical integration**. To state it honestly as well: the tracking repo contains **no** Qwen-specific per-transaction hashes / payment IDs / call counts / itemized amounts; this article neither cites nor invents any such numbers, and does not borrow Kimi's transactions to fill the gap.

## 1. Status at a Glance (Historical Integration + 2026-10-09 Delisting Record)

- **On-chain**: Monad testnet (chain 10143), Escrow v3.2 + Registry v4 + official Circle USDC (6 decimals) — the same currently active deployment as the Kimi article ([contracts/deployed.monad.json](../../contracts/deployed.monad.json)).
- **Relay**: The shared multi-seller relay runs in a Phala TDX trusted execution environment (Phala Cloud CVM), active in the production-site configuration; **sellers do not need to run any TEE of their own**.
- **Qwen side (historical status + delisting record)**: During the demo, the market page showed a real seller card of "one seller offering Qwen 3.8 Max" (the video script at recording time — maintained locally, not in the repo; then alongside Kimi); the keystore held two seller keys (Kimi and Qwen), both listings' endpoints pointed to this TEE relay, and both sellers had authorized settlement delegates ([web/policy.html](../../web/policy.html)). **Delisting record**: at the seller's request to stop upstream quota consumption, the listing was delisted via Registry.deactivate on **2026-10-09** (tx [`0x6a91bb20…d7131f1b`](https://testnet.monadvision.com/tx/0x6a91bb203751aad1f29003b046dfb56965d901e08d372a4b5070fa9bd7131f1b), block 69498784, Monad testnet 10143); the market page reads the Registry live — **the only currently active listing is Kimi's**. Listings are seller-controlled and can change at any time.
- Note: The "deployed / once active / delisted" statements above are deployment facts as recorded in the submission materials; the Qwen listing's current status is governed by live Registry data, and this article performs no real-time availability checks on live services.

**Why Monad**: TokenShare's per-call accounting depends on an EVM environment oriented toward low-cost, high-throughput, fast-confirmation transactions; the Monad testnet's low-fee design helps keep settlement overhead at the single-call level practically viable. This is a design-fit positioning statement, **not a measured conclusion**: upstream model usage and on-chain gas are two independent kinds of real cost, and this project has published no benchmark measuring "gas share of a single call"; Qwen-specific per-transaction amounts have not been archived (see §3), so this article cites no specific receipt figures. Low fees are a helpful property of the chain environment — they do not constitute proof of cost or revenue, and they do not imply any vendor endorsement.

## 2. Where Qwen Sits in the Architecture

TokenShare's relay was designed as "multi-vendor pluggable" from day one, and the Qwen integration reused exactly the same flow as Kimi's:

- **Identical core flow**: application-layer encrypted escrow of the upstream key (can precede listing) → VERIFY catalog (real fetch of the model list for cross-checking) → select models and publish three-tier per-model prices (cached input / input / output, per million tokens) → explicitly authorize a settlement delegate → buyers are deducted per token.
- **Matching OpenAI-compatible surface**: Alibaba Cloud Model Studio offers an OpenAI-compatible mode (base URL per the [official documentation](https://help.aliyun.com/zh/model-studio/base-url)); the platform's per-model three-tier prices, on-chain price lookup, and USDC 6-decimal per-usage settlement pipeline hold for OpenAI-compatible upstreams.
- **Routing and isolation**: requests are routed by the seller registered on the payment to that seller's key, never to arbitrary wallets; the host whitelist and the model-prefix→vendor mapping are in-code policy ([relay/app/config.py](../../relay/app/config.py)).

## 3. Evidence Status: What Can Be Shown, What Is Missing

**Backed by records (explicitly recorded in the submission materials)**:

- Real API calls of `qwen3.8-max` and **partial on-chain settlement** are recorded as demo facts ([docs/SUBMISSION.md](../SUBMISSION.md), "Demo status" section).
- The Qwen seller's key was in the shared TEE relay's encrypted keystore, the listing was once live on the market page, and the settlement delegate was authorized ([web/policy.html](../../web/policy.html)); the listing was subsequently delisted on 2026-10-09 (see §1), and the demo records here remain unchanged as historical fact.

**Honestly declared evidence gaps**:

- The tracking repo currently contains no Qwen-specific transaction hashes, payment IDs, call counts, or amount details that could be verified item by item. Therefore this article **gives** no Qwen-specific on-chain hashes or numbers; the correspondence between the market page's two seller addresses and "Kimi / Qwen" comes from platform-side records and on-explorer inference, and this article does not identify specific addresses either.
- In other words: **the Qwen integration at the code and documentation level is complete, and the calls really happened**; but "per-transaction, independently clickable Qwen explorer evidence" has not yet been organized and archived. This is a follow-up work item — not a gap this article fills with someone else's data.

## 4. Terms Boundaries (Historical Research from 2026-10-07, Not Re-Verified)

On 2026-10-07, we did a read-only study of the Qwen Token Plan terms; its conclusions are kept as a **historical research record** (this article does not re-verify them in real time):

- Per the official documentation as consulted at the time, Token Plan personal/team endpoints are **limited to interactive tool/agent scenarios**, and the official docs state they do not apply to application backends, automated scripts, or sharing ([personal plan notes](https://platform.qianwenai.com/docs/token-plan/personal/token-plan-personal-overview), [team plan notes](https://platform.qianwenai.com/docs/token-plan/team/token-plan-team-overview)).
- The platform terms of service clearly restrict unauthorized resale/sublicensing ([terms of service](https://terms.alicdn.com/legal-agreement/terms/common_platform_service/20260423145804812/20260423145804812.html) §7.1.12 and §1.4).

**Three things that must be clearly distinguished**:

1. **Demo fact**: the repo records qwen3.8-max calls and partial settlement that really happened. This is a record of accomplished facts at the demo level.
2. **It is not permission**: that calls happened in the demo **does not mean** the demo use was vendor-permitted; the project owner's risk awareness and self-assumption of risk **does not constitute, and cannot substitute for**, vendor authorization.
3. **The production/commercial bar is higher**: the term concerns above still stand at the "production or commercial distribution" level — selling Qwen quota at scale to third parties through TokenShare presupposes obtaining keys suited to backend per-usage billing and explicit vendor-side permission.

## 5. New Positioning: A Metered Sales Channel That Official Vendors Could Also Use (Potential, Not Current)

Today TokenShare's sellers are "individuals/teams with idle quota". But the protocol's capability boundary is wider, and the project owner's one-line positioning is:

> **"They could also take on selling the official APIs — one more sales channel."**

That is: the same infrastructure not only suits putting idle personal subscriptions to work; it also, **technically**, supports the official vendors themselves — or formally authorized distributors/suppliers — using it as a pay-per-usage sales channel: per-model prices are published directly on the on-chain Registry, buyer budgets are hard-constrained by the escrow contract, every call produces a verifiable signed billing receipt, and the shared TEE gateway handles key escrow and routing for the channel party — developers and the agent ecosystem can integrate per token directly, while the channel party need not build any metering or settlement infrastructure.

**A boundary that must be drawn clearly**: this is a description of technical capability and positioning, **not** any actually existing vendor cooperation, authorization, contract, or revenue. This article does not claim Qwen/Alibaba Cloud is an official partner of TokenShare, and there is no written vendor authorization or commercial arrangement of any kind.

## 6. Boundaries and Honest Disclaimers

- **Receipt ≠ token truth**: receipts are issued by the relay; they are a signed certification of a billing statement — not proof of service quality, and not independent evidence of real token consumption.
- **Prices ≠ official prices**: the on-chain three-tier prices are seller-self-reported and are not necessarily equal to the upstream official price list; the Qwen models' usage-field compatibility was verified against real calls during integration, but a comparison of published prices against official prices was not done.
- **TEE reduces risk; it does not eliminate it**: the shared TEE relay confines keys inside an enclave, but the platform operator and governance remain trust surfaces; this is not zero-custody, and it is not trustless.
- **Testnet tokens have no value**: testnet USDC/MON have no real value; but upstream plan quotas and cloud infrastructure are still **real costs**.
- **Refunds are contract-constrained**: refunds are limited by payment state, settlement, and TTL rules — they are not instantly withdrawable at any time; a seller-authorized settlement delegate can still settle existing locks after TTL (funds only ever enter the seller's ledger).
- **Revocation is an explicit action**: revoking a buyer's API key is an explicit operation; disconnecting the wallet does not revoke keys that have already been issued.
- **Compliance responsibility lies with the seller**: upstream sellers are themselves responsible for their accounts' compliance with vendor terms; this project does not constitute distribution authorization, and a disclaimer cannot substitute for authorization.

## 7. Verification Paths

- On-chain deployment facts (shared with the Kimi article): addresses, deployment times, and network are in [`contracts/deployed.monad.json`](../../contracts/deployed.monad.json) (that file contains no version numbers or deployment txs); the Escrow v3.2 deployment tx [`0xc7ca3ac7…523afce4e`](https://testnet.monadvision.com/tx/0xc7ca3ac798e16ace9f4554df51912aed235753f360d7f0a6417b7b0523afce4e) is in the root [README.md](../../README.md) "Networks" section.
- Qwen demo record (real calls and partial settlement): [`docs/SUBMISSION.md`](../SUBMISSION.md), "Demo status" section.
- Historical dual-seller display (recording-time wording; the only currently active listing is Kimi's): video script (maintained locally, not in the repo), [`web/index.html`](../../web/index.html); key escrow and delegate authorization records: [`web/policy.html`](../../web/policy.html), [`web/config.js`](../../web/config.js).
- Relay pluggable architecture and whitelist: [`relay/app/config.py`](../../relay/app/config.py); deployment and images: [`relay/README-docker.md`](../../relay/README-docker.md).
- Qwen official sources (links as of the 2026-10-07 research): [Token Plan personal](https://platform.qianwenai.com/docs/token-plan/personal/token-plan-personal-overview) · [Token Plan team](https://platform.qianwenai.com/docs/token-plan/team/token-plan-team-overview) · [Model Studio base URL](https://help.aliyun.com/zh/model-studio/base-url) · [Terms of service](https://terms.alicdn.com/legal-agreement/terms/common_platform_service/20260423145804812/20260423145804812.html).
- Index: [README.md](./README.md) (directory navigation only; individual status descriptions may lag this article — the primary evidence cited herein prevails).

## Fact Layering

| Layer | Content |
| --- | --- |
| Recorded evidence | §3 documented record of qwen3.8-max real calls and partial settlement, key escrow/delegate authorization, historical dual-seller display; §1 2026-10-09 listing delisting (Registry.deactivate, block 69498784) |
| Evidence gap | §3 Qwen-specific per-tx / payment ID / amount lists not archived; this article cites no such numbers |
| Historical research | §4 2026-10-07 read-only terms study (not re-verified; citations kept) |
| User statement | §Summary / §1 first-person description of using Qwen during development |
| Potential (not fact) | §5 technical support for official/authorized channels as a sales channel; does not represent any cooperation or authorization |
| Boundaries | All items in §6; the positioning boundaries of §1 "Why Monad" (not measured, no proof of cost/revenue, no vendor endorsement) |
