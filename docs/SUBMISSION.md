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

1. On-chain budgets. A buyer locks a USDC budget in an Escrow contract, scoped to a payment. Funds are released only under the contract's state / TTL / settlement / refund rules; a payment can be split into partial captures and settled at the end (that is how the demo payments ran). The buyer-facing OpenAI-compatible credential is explicitly revocable — separately from the budget lock.
2. On-chain price catalog. The Registry publishes each seller's per-model price row — cached-input / input / output, per 1M tokens — on-chain. It is the shared reference for what a call should cost; it is NOT an independently verified upstream cost, usage record, or lock-time quote, and the receipt's math is reproducible without proving true cost.
3. A shared multi-seller relay on Phala TDX. Sellers enroll their upstream key through encrypted key management; the relay routes requests to official endpoints only and does not require sellers to self-host anything (an optional self-host single-seller mode exists). Requests go through the payment's registered seller (payment.seller), never to an arbitrary wallet.
4. Signer-authenticated billing receipts. Every call produces an EIP-712 signed receipt (paymentId, token tiers, actual amount, upstream host, model). These are signer-authenticated billing assertions about the seller-side record — not independent token-count proof; the chain-side catalog is what a buyer cross-checks them against.
5. Delegation instead of payouts. The seller-approved settle delegate settles on the seller's payment and credits the seller's ledger balance; protocol fees are governed by the Escrow. Revoking a buyer's scoped credential is an explicit, distinct action — disconnecting a session clears local policy consent only and does NOT revoke an issued API key.

Usefulness: a buyer gets OpenAI-compatible credentials that cannot spend beyond the locked budget, with a receipt trail they can reconcile against on-chain prices; a seller converts idle plan capacity without sharing raw keys or running their own gateway; both sides see the same numbers.

Demo status (Monad testnet, no real-money claims): real Kimi calls (k3-256k, kimi-for-coding) and a Qwen call (qwen3.8-max) plus partial settlements are recorded. In ONE completed Chainlink CRE settlement-audit run (local simulation), the LATEST receipt of the demo payment (payment 5) was compared against the exact settle-block Registry prices and the MATCH verdict was anchored on-chain through a permissionless testnet mock forwarder — a workflow-simulation result, NOT a production Chainlink DON deployment; it says nothing about other or future receipts, cumulative usage, lock-time quotes, or independently-proved token usage.

Compliance: upstream sellers remain responsible for complying with their provider's terms; resale authorization is not established by this project.

Links: repo https://github.com/warku123/TokenShare — live demo https://tokenshare-web.vercel.app/ — logo assets/tokenshare-logo.png — video script docs/VIDEO_SCRIPT.md (≤3 minutes, submission requirement).
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

## Measured character counts (paste fields above, measured after final edit)

| Field | Limit | Measured |
| --- | --- | --- |
| Project name | ≤120 | 10 |
| One-line description | ≤200 | 198 |
| Description | ≤8000 | 3933 |
| GTM strategy | none displayed (kept ~200–300 words) | 1941 chars / 279 words |
| 中文摘要（optional） | — | 265 chars |

## 中文摘要（可选，仅说明用途；非表单必填字段）

```text
TokenShare 把官方 LLM 套餐的闲置额度变成可计费服务：买家用 USDC 在链上锁定预算（按合约 state/TTL/结算/退款规则释放）、按调用扣费，买家凭证可显式撤销；卖家通过 Phala TDX 共享中继加密托管 key、无需自建网关；每次调用有 EIP-712 签名账单（签名认证的记账断言，≠独立证明真实 token 用量），与链上卖家按模型价目对账。部署在 Monad 测试网，Kimi/Qwen 有真实调用与部分结算记录；Chainlink CRE 演示为对最新账单的一次本地模拟审计（非生产 DON）。
```

## Form link set (as referenced inside the fields)

- Logo: `docs/assets/tokenshare-logo.png` (see also `docs/LOGO.md`)
- Public repo: https://github.com/warku123/TokenShare
- Live demo: https://tokenshare-web.vercel.app/
- Video script: `docs/VIDEO_SCRIPT.md`
