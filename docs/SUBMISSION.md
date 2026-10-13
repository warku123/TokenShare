# TokenShare — Submission form fields (paste-ready English + concise CN instructions)

> 使用说明（中文，简明）：
> 1. 表单里逐字段复制下方"代码块"内的英文文本即可粘贴；每个字段右上角标注了实测字符数，均已落在表单限制内。
> 2. Logo 引用相对路径 `docs/assets/tokenshare-logo.png`（资产由 Designer 维护，见 `docs/LOGO.md`）；仓库/演示/视频脚本链接见文末。
> 3. 本文件只做表单文案；不做平台代提交、不虚构视频 URL、不承诺未达成的事项（收入/用户量/生产上线等一律不写）。
> 4. 修改任一字段后，请重新运行仓库内字符统计并更新"实测字符数"表，避免越限。

---

## Project name — paste field

```text
TokenShare
```

## One-line description — paste field

```text
TokenShare: buyers pay per call in USDC via on-chain escrow budgets for sellers' spare official-LLM-plan capacity; calls carry EIP-712 receipts; one demo audit checked the latest vs on-chain prices.
```

## Description — paste field

```text
TokenShare turns spare capacity of official LLM subscription plans into a metered, on-chain-settled service — with receipts a buyer can actually check, not just trust.

THE PROBLEM
Power users of premium LLM plans (Kimi, Qwen, and similar OpenAI-compatible providers) pay for monthly capacity they can rarely consume fully, while agents and prototype teams need pay-per-call access with hard budget control. Directly sharing an upstream API key carries real credential-leak and metering risks, and coordinating informal arrangements between strangers is a genuine burden. TokenShare is the coordination surface for this: a protocol with on-chain budgets, an on-chain price catalog, and signed billing receipts.

THE SOLUTION
TokenShare is an open protocol plus a working reference implementation, deployed on Monad testnet (chain 10143, USDC with 6 decimals):

1. On-chain budgets. A buyer locks a USDC budget in an Escrow contract, scoped to a payment. Funds are released only under the contract's state / TTL / settlement / refund rules; a payment can be split into partial captures and settled at the end (that is how the demo payments ran), and while a payment remains Locked its unconsumed remainder becomes refundable after the TTL — a settled payment is final. The buyer-facing OpenAI-compatible credential is explicitly revocable — separately from the budget lock.
2. On-chain price catalog. The Registry publishes each seller's per-model price row — cached-input / input / output, per 1M tokens — on-chain. It is the shared reference for what a call should cost; it is NOT an independently verified upstream cost, usage record, or lock-time quote, and the receipt's math is reproducible without proving true cost.
3. A shared multi-seller relay on Phala TDX. Sellers enroll their upstream key through encrypted key management; the relay routes requests to official endpoints only and does not require sellers to self-host anything (an optional self-host single-seller mode exists). Requests go through the payment's registered seller (payment.seller), never to an arbitrary wallet.
4. Signer-authenticated billing receipts. Every call produces an EIP-712 signed receipt (paymentId, token tiers, actual amount, upstream host, model). These are signer-authenticated billing assertions about the seller-side record — not independent token-count proof; the chain-side catalog is what a buyer cross-checks them against.
5. Delegation instead of payouts. The seller-approved settle delegate settles on the seller's payment and credits the seller's ledger balance; protocol fees are governed by the Escrow. Revoking a buyer's scoped credential is an explicit, distinct action — disconnecting a session clears local policy consent only and does NOT revoke an issued API key.

WHY MONAD: per-call billing is practical at this scale on an EVM environment designed for low-cost, high-throughput, fast-confirmation transactions — one demo receipt (payment 5) settled for an actual 0.000918 USDC. On Monad testnet, that low-fee design helps keep settlement overhead practical at this payment scale. This is a design-fit statement, not a measured result: upstream model usage and chain gas are two separate real costs, and the project has not published a benchmark of chain-gas share per request.

Usefulness: a buyer gets OpenAI-compatible credentials that cannot spend beyond the locked budget, with a receipt trail they can reconcile against on-chain prices; a seller converts idle plan capacity without sharing raw keys or running their own gateway; both sides see the same numbers.

Demo status (Monad testnet, no real-money claims): real Kimi calls (k3-256k, kimi-for-coding) and a Qwen call (qwen3.8-max) plus partial settlements are recorded; the currently active listings are Kimi (k3-256k, kimi-for-coding-highspeed) and Qwen (qwen3.8-max, qwen3.8-flash — re-listed on a fresh seller wallet after its seller deactivated the original wallet's listing). In ONE completed Chainlink CRE settlement-audit run (local simulation), the LATEST receipt of the demo payment (payment 5) was compared against the exact settle-block Registry prices and the MATCH verdict was anchored on-chain through a permissionless testnet mock forwarder — a workflow-simulation result, NOT a production Chainlink DON deployment; it says nothing about other or future receipts, cumulative usage, lock-time quotes, or independently-proved token usage.

Compliance: upstream sellers remain responsible for complying with their provider's terms; resale authorization is not established by this project.

PRIOR ART AND DIFFERENTIATION
Demand for idle subscription capacity is already proven commercially: DeRouter (derouter.ai, self-described as operating since 2025) pools providers' Claude/Codex subscription accounts behind a centralized relay with custodial prepaid credits, and self-reports 9,000+ users with USDC payouts on Solana; its own whitepaper concedes the operator-absconding risk is not cryptographically prevented, and TEE execution remains a roadmap item. PayPerQ's open-source enclave proxy (ppq-enclave-proxy) runs a similar TEE-proxy pattern in production for its own gateway — but to protect query-content privacy, not to run a capacity marketplace. TokenShare targets the gap between them: a marketplace whose settlement does not require trusting the operator — non-custodial per-call escrow with TTL refunds, seller-consented encrypted custody of keys routed only to official endpoints (no account pooling), and live Phala TDX attestation anchored on-chain.

Links: repo https://github.com/warku123/TokenShare — live demo https://tokenshare-web.vercel.app/ — step-by-step buyer/seller walkthroughs https://tokenshare-web.vercel.app/guide.html — logo assets/tokenshare-logo.png — video script maintained locally (not tracked in the repo; ≤3 minutes, submission requirement).
```

## Go-to-market / user acquisition strategy — paste field

```text
This is a PROPOSED future go-to-market plan, not achieved traction — the numbers below are targets we would measure, not existing metrics.

Who goes first: (1) buyers — AI developers, agent builders, and small prototype teams that already run OpenAI-compatible clients and want metered access to premium models with a hard budget cap; (2) sellers — individuals or teams whose providers permit programmatic access and are willing to list spare plan capacity through the consented, encrypted key-enrollment flow.

How we reach them: start where these users already are — the Monad ecosystem community channels, open-source communities around agent frameworks, and hackathon demos like this submission (the 3-minute video and the runnable demo are the pitch). Then targeted onboarding: a quickstart that maps our flow onto the OpenAI-compatible surface builders already know (mint a scoped credential, point the base URL, cap the budget), SDK examples in the two or three agent stacks we most expect, and a short operator runbook for sellers covering enrollment, consent, and revocation.

The first pilot stays deliberately small: a handful of opt-in sellers (explicit consent, provider-permitted models only) and buyers from the communities above, run on the shared Phala TDX relay with per-call receipts and on-chain reconciliation.

What we would track, in order: activation (first successful paid call per buyer), repeat usage (weekly returning buyers), seller listing fill rate (listed models actually serving traffic), and reconciliation health (receipt-vs-chain price deviation rate, settle/refund error rate).

Proposed production-readiness considerations: provider permissions verified per seller/model, and security readiness (third-party audit of the relay custody path and the escrow/registry contracts). Upstream authorization is not established by this project — any production onboarding would be its own separately gated effort.
```

## Judge access instructions — optional paste field (private: team / judges / organizers)

```text
TokenShare judge access — no login, no password, no shared account exist. Use YOUR OWN testnet wallet; never connect real mainnet funds — this deployment is Monad testnet only.

Product: https://tokenshare-web.vercel.app/ — step-by-step walkthroughs: HOW TO USE https://tokenshare-web.vercel.app/guide.html (role switch at the top; opens on the buyer guide; direct links /guide.html#buyer and /guide.html#seller; linked beside BROWSE FULL MARKET on the homepage and from the console intro). No wallet? The market page (https://tokenshare-web.vercel.app/market.html) is browseable anonymously: live on-chain seller listings, per-model three-tier prices read from the Registry contract, relay health, explorer links.

WITH a wallet (Monad testnet, chain 10143, RPC https://testnet-rpc.monad.xyz):
1. Connect via the wallet picker (EIP-6963 — picks up installed wallets; any security prompt is wallet-side, not ours). Tick the Policy & Risks consent box at the top of the console — paid actions stay gated until you do (the full-width [ TICK POLICY CONSENT ↑ ] hints next to the DEPOSIT, LOCK and seller SUBMIT actions point to it). Consent is per-wallet for the session: disconnecting clears it for that wallet, switching wallets keeps each wallet's own; it is a UX gate, not a chain transaction.
2. Fund BOTH tokens: testnet MON for gas (https://faucet.monad.xyz) and testnet USDC — USDC is REQUIRED for DEPOSIT/LOCK, not optional (https://faucet.circle.com — select token USDC, network Monad Testnet).
3. Buyer flow: DEPOSIT USDC into Escrow → pick an active seller/model on the market page → LOCK against it in the console: an amount above the seller's minimum per-call estimate (shown on the LOCK form) + a TTL. A lock is a fixed budget — depositing cannot raise its cap; if it runs short, open a new, funded lock right away (no need to wait for the old one) — the old lock's Locked remainder becomes refundable after its TTL.
4. Call the lock either way: [ MINT API KEY ] — one off-chain wallet signature mints a scoped, revocable credential (tsk1.…, shown once, no per-call popups, revoked explicitly via [ REVOKE ]) for any OpenAI-compatible client — or [ CALL DEMO ] — each request signed fresh in the wallet (the demo never takes the minted key). CALL DEMO returns a real model reply with official usage plus an EIP-712 signed receipt the console verifies, and the settle transaction is viewable on the explorer; API clients get the same signed billing receipt per call — a billing assertion to reconcile against on-chain prices, not independent proof of token usage. [ USAGE ] shows max / captured / remaining; [ API KEYS ] filters ALL / ACTIVE / EXPIRED / REVOKED (six per page) — the status badge is local clock + revocation bookkeeping, not an on-chain proof.
5. Refunds: while a payment is still Locked, the unconsumed remainder becomes refundable after its TTL; a Settled payment is final.
6. Pacing: locks carry a TTL and the shared relay's Kimi upstream is rate-limited to 3 requests/min — wait ~20 seconds between demo calls.

Seller flow (optional): no self-hosting — the shared Phala TDX relay holds the encrypted key. Flow: seal your upstream API key encrypted in the browser → check the served catalog → register the listing on-chain → approve the settle delegate (walkthrough: https://tokenshare-web.vercel.app/guide.html#seller). Judges typically lack an upstream API key — the demo video shows the seller path.

Contracts for reference (chain 10143): Escrow 0xe4D5Eb0dBDB6DB8063C07ECF7EDFcCdDB9Ad514c · Registry 0xeD347cDc1761750E20C024459b38dedFb1462254 · USDC 0x534b2f3A21130d7a60830c2Df862319e593943A3. The relay runs in a Phala TDX TEE; its /sellers/{seller_address}/status endpoint shows each seller's custody-verified catalog match.

Honest scope: the shared relay currently serves the active Kimi listing (k3-256k, kimi-for-coding-highspeed) and the re-listed Qwen seller (qwen3.8-max, qwen3.8-flash); the original Qwen wallet's listing was deactivated by its seller on 2026-10-09 (see the 2026-10-13 addendum at the end of this file). Testnet only — no real funds.
```

## Measured character counts (paste fields above, measured after final edit)

| Field | Limit | Measured |
| --- | --- | --- |
| Project name | ≤120 | 10 |
| One-line description | ≤200 | 198 |
| Description | ≤8000 | 5869 chars / 831 words |
| GTM strategy | none displayed (kept ~200–300 words) | 1941 chars / 279 words |
| Judge access instructions (optional) | ≤8000 | 4095 chars / 625 words |
| 中文摘要（optional） | — | 265 chars |

## 中文摘要（可选，仅说明用途；非表单必填字段）

```text
TokenShare 把官方 LLM 套餐的闲置额度变成可计费服务：买家用 USDC 在链上锁定预算（按合约 state/TTL/结算/退款规则释放）、按调用扣费，买家凭证可显式撤销；卖家通过 Phala TDX 共享中继加密托管 key、无需自建网关；每次调用有 EIP-712 签名账单（签名认证的记账断言，≠独立证明真实 token 用量），与链上卖家按模型价目对账。部署在 Monad 测试网，Kimi/Qwen 有真实调用与部分结算记录；Chainlink CRE 演示为对最新账单的一次本地模拟审计（非生产 DON）。
```

## Form link set (as referenced inside the fields)

- Logo: `docs/assets/tokenshare-logo.png` (see also `docs/LOGO.md`)
- Public repo: https://github.com/warku123/TokenShare
- Live demo: https://tokenshare-web.vercel.app/
- Video script: maintained locally, not tracked in the repo

## 补记 — 2026-10-13 链上与生产状态同步（非表单字段，不随表单粘贴）

> 本节记录 2026-10-13 链上实时读证（chainId 10143，Registry `0xeD347cDc1761750E20C024459b38dedFb1462254`）与生产镜像状态，供评审对账与后续更新。表单字段当日经两次同步：第一轮仅更正「Judge access」内一句过期的"当前在售"表述；第二轮（本次）按新教程页实质重写「Judge access」的买家/卖家步骤（faucet 双币种必取、lock 最小估算+TTL 且不可追加、tsk1. 凭证两种调用路径、USAGE/状态过滤、consent 行为、退款终态语义、教程页入口），并在「Description」同步当前在售目录与退款终态、Links 增加教程链接；全部字段字符数已实测更新（见上表）。下方 ①–⑤ 为第一轮记录，保持原文。

**① Qwen 重上架（Registry sellerCount=3）**

- 旧 Qwen 钱包 `0x1F1A8C5c1E56eD339C8021a0396095a724aca687` 已于 2026-10-09 由卖家主动下架（`Registry.deactivate`，tx [`0x6a91bb20…d7131f1b`](https://testnet.monadvision.com/tx/0x6a91bb203751aad1f29003b046dfb56965d901e08d372a4b5070fa9bd7131f1b)），链上 listing 数据保留、active=false，**非在售**。
- 新 Qwen 钱包 `0xD6172dC960D1533e49A367253FF9Db01AE83Ba39` active=true，endpoint = 共享 TEE relay（与 Kimi 同一入口）；在售模型与现价（cached-input / input / output，USDC per 1M tokens，6dp native）：`qwen3.8-max` 2/4/36、`qwen3.8-flash` 1/2/3（raw：2000000/4000000/36000000 · 1000000/2000000/3000000）。
- `settleDelegateOf(0xD617…Ba39)` = `0x49CA3eADB3b8F23623f735918A6F2b945F60fb2D`（共享 TEE relay 的 pinned signer，与 Kimi 卖家相同）。
- Registry `sellerCount = 3` = Kimi（active）+ Qwen 旧钱包（inactive，数据保留）+ Qwen 新钱包（active）。

**② 买家支付 payment 6/7/8（Escrow v3.2 实时读证）**

- p6 = **Refunded**（captured 0）；p7 / p8 = **Settled**。
- 语义注意：`capturedOf(7/8) = 10,000,000`（10 USDC，6dp）是 `settle()` 的**终态标记值，≠ 实收金额**；`/receipt/7`、`/receipt/8` 公开匿名可读，最后实际结算 `actualAmount` = **4510 / 5084**（6dp native，≈ 0.0045 / 0.0051 USDC；p8 与链上价目自洽）。

**③ G5-1 生产镜像**

- `warku123/tokenshare-relay:tee-prod-20261013`（digest `sha256:c600e16910c7…`，源码 = 本仓库 commit `4e4f10c`，含 F1–F5 加固）已在 CVM `tokenshare-m15-demo` 运行；signer / derivedAddress 不变 `0x49CA…fb2D`；public-logs / sysinfo / tcbinfo 全关。

**④ relay /health `keystoreEntries=3`（已查明，正常）**

- 三条 = Kimi + Qwen（旧钱包，已 deactivate 但 key 封套仍留存 keystore）+ Qwen（新钱包）；第三条来源即 ① 的新钱包注册。

**⑤ 表单字段时效核对**

- 文中无「视频待提交」类表述（视频脚本仍为本地维护、不入库，与现状一致）。
- 「Judge access」字段内 testnet 合约地址表与本快照一致（Escrow `0xe4D5…514c` / Registry `0xeD34…2254` / USDC `0x534b…43A3` 未变）。
- Kimi 卖家链上现价已调为 payment 5 结算块价的 10 倍（`k3-256k` = 30/60/90、`kimi-for-coding-highspeed` = 15/30/45，2026-10-13 口径）；at-block 数据保留为历史，见 [docs/submission/kimi.md](./submission/kimi.md) §3 脚注。

**⑥ 前端教程页（同版本发布说明）**

- 新增静态页 `web/guide.html`（无钱包连接、无链上读数）：顶部 BUYER/SELLER 角色切换，深链 `guide.html#buyer` / `guide.html#seller`，默认 buyer、单面板可见（无 JS 时两段全渲染）；buyer 路径 DEPOSIT → 选卖家 LOCK（最小每调估算 + TTL）→ MINT API KEY（`tsk1.…`，一次链下签名、仅展示一次、无逐调用弹窗、可显式撤销）或 CALL DEMO（逐调用钱包签名，不取 minted key）；USAGE 与 API KEYS 状态过滤（ALL/ACTIVE/EXPIRED/REVOKED，每页 6 条；badge 为本地时钟 + 撤销记账，非链上证明）；402 归因与排查（余额低于最小估算 / payment 过期或非 Locked / 卖家或买家不符）；seller 路径加密托管 key → catalog 核对 → 链上注册 → 授权 settle delegate（共享 TEE，无需自建）。
- 入口：首页 BROWSE FULL MARKET 按钮旁与 console 简介处均链接教程页。
- 发布状态：该页与本次提交材料**同版本发布**；成文时尚未独立复核其线上状态——此处仅描述能力，不作为已公开验证的证据，不附 commit/验证输出。
