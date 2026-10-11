# GitHub Issue 草稿 — metamask/eth-phishing-detect

> **状态：DRAFT，未发布。** 本文件仅为准备稿；不执行任何提交动作（无 `gh issue create/comment`、无网络提交、无仓库改动、无 commit/push）。**发布前须经用户人工批准**，并对照目标仓库实际表单核对。中文内部说明见文末，**不随 issue 发布**。

---

## Publish body (English — paste as issue title + form fields)

**Title:**

```
Request blocklist removal: tokenshare-web.vercel.app
```

### Legitimate domains, IPs, IPFS hashes, or IPNS names

```
tokenshare-web.vercel.app
https://tokenshare-web.vercel.app/
https://tokenshare-web.vercel.app/console.html#seller
```

### Please explain why this content is legitimate

Hi maintainers,

We are the maintainers of TokenShare (public repo: https://github.com/warku123/TokenShare, pinned at commit `141c135a4e6f0d16bc49fd78087dc7d981558ffb`). MetaMask support directed us to this repository regarding a security warning on our domain, so we are submitting this request for review of the current scan verdict.

**Observation (dated 2026-10-09, via the public MetaMask scan endpoint, read-only GET)**

- Hostname `tokenshare-web.vercel.app` was observed with `recommendedAction: BLOCK`, for both `https://tokenshare-web.vercel.app/` and `https://tokenshare-web.vercel.app/console.html#seller`.
- Reproduce with:
  `curl -s "https://dapp-scanning.api.cx.metamask.io/v2/scan?url=https%3A%2F%2Ftokenshare-web.vercel.app"`
- Full scan JSON and UTC time of a fresh re-run: [TO BE PROVIDED — pasted unedited at submission time]
- Wallet-side evidence: warning screenshot [TO BE PROVIDED]; MetaMask version [TO BE PROVIDED]

**About the project**

TokenShare is an escrow-based demo marketplace for metered LLM API quota, deployed on Monad Testnet only (chainId 10143; no mainnet deployment). Buyers escrow a USDC budget on-chain; seller API keys are held under TEE custody. It is a demo — we do not claim it is risk-free, fully noncustodial, or externally audited.

**Auditable wallet interactions** (public source at the pinned commit; configuration in [web/config.js](https://github.com/warku123/TokenShare/blob/141c135a4e6f0d16bc49fd78087dc7d981558ffb/web/config.js), wallet logic in [web/common.js](https://github.com/warku123/TokenShare/blob/141c135a4e6f0d16bc49fd78087dc7d981558ffb/web/common.js) and [web/console.js](https://github.com/warku123/TokenShare/blob/141c135a4e6f0d16bc49fd78087dc7d981558ffb/web/console.js))

- Permission and account prompts are click-driven; session restore uses a silent `eth_accounts` check and never auto-prompts.
- ERC-20 approvals are exact-amount `approve()` calls to the configured escrow address `0xe4D5Eb0dBDB6DB8063C07ECF7EDFcCdDB9Ad514c`, issued only when the existing allowance is insufficient, immediately followed by the `deposit()` they cover.
- Signing consists of five EIP-191 `personal_sign` flows (custody key enroll/revoke, API-key grant mint/revoke, and per-message chat payment signing).
- Reading our own application scripts, we found no `eth_sign`, EIP-2612 `permit`, or `setApprovalForAll`; that review covers `web/*.js` application scripts only, not vendored third-party code, and is not a claim about every bundled dependency.

**Scope limits**

These facts come from limited inspection of our own public source at a pinned commit — not a security audit, and not proof that the deployed bundle matches the repository byte-for-byte. We are not claiming a confirmed false positive, whitelist entitlement, or knowledge of the vendor behind the verdict. We also note that absence from any local list file would not by itself demonstrate that a site is safe, and we do not rely on that argument here.

**Requested action**

Please review the current scan verdict for `tokenshare-web.vercel.app` and let us know what verification details you need (for example, redacted screenshots, or domain-control evidence we could provide in a form agreed with the reviewer). If this repository is not the right route for this request, please point us to the correct one. If the verdict is found to be erroneous, we ask that it be corrected; we understand wallet-side propagation may still be required and we will verify against the wallet UI and the public scan endpoint.

No urgency implied — thank you for your time.

### Is this a duplicate request?

No. On 2026-10-09 we searched this repository's open and closed issues for the hostname (`tokenshare-web.vercel.app`) and for "tokenshare" and found no existing reports (0 results). We will re-run this search immediately before posting and will link any prior or related issue found.

---

## 内部说明（中文 · 不随 issue 发布）

- 本文件仅为草稿，**未发布**；禁止 `gh issue create/comment`、任何提交、push、仓库/应用改动。**用户批准前不得发布。**
- 标题暂用 `Request blocklist removal: tokenshare-web.vercel.app`；发布前再次对照线上模板核对标题和三个字段名：`Legitimate domains, IPs, IPFS hashes, or IPNS names` / `Please explain why this content is legitimate` / `Is this a duplicate request?`，**不得改动措辞**。
- URL 区块每行一个，不要合并或加注释。
- 发布前**重跑** open+closed 搜索（hostname 与 "tokenshare"）；2026-10-09 当日结果为 0，若届时出现重复/相关 issue，先更新「Is this a duplicate request?」答案并附链接。
- 本地清单文件「查无此域名」**不作为安全证明**；正文已显式声明不依赖该论据，勿在评论区追加此类主张。
- 若已取得钱包警告截图与 MetaMask 版本号，替换正文 `[TO BE PROVIDED]`；没有则保留占位，**不得虚构**。
- 发布前：① 重跑当日 scan 并原样粘贴完整 JSON 与 UTC 时间，不改字段值；② 用户人工批准；③ 对照官方模板最终核对。
- 域名控制权证明：仅在审核方明确要求并经用户**单独批准**后，通过**生产主机名**上的挑战文件提供；分支/预览部署不算。英文正文有意省略挑战操作细节。
- `docs/METAMASK_REVIEW_REQUEST.md` 等既有支持材料**保持原状，不得修改**。
- 交付报告：正文字数（目标 400–600 英文词）、文件路径、git diff 检查结果。
