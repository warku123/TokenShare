# MetaMask 站点警告申诉材料（METAMASK REVIEW REQUEST）

> 状态：**待提交准备稿**。本文件只是申诉前的准备与操作手册，**本仓库内不执行任何提交动作**。
> 日期：2026-10-09 · 观察对象：`https://tokenshare-web.vercel.app`（MetaMask 钱包内打开时出现安全警告，阻断演示）。

---

## 0. 基本立场（先读）

- 2026-10-09 通过 MetaMask 公开扫描接口观察到：该主机名 `tokenshare-web.vercel.app` 的扫描结果 `recommendedAction` 为 **BLOCK**；首页与 `https://tokenshare-web.vercel.app/console.html#seller` 两个 URL 均为 BLOCK。
- **判定原因与判定供应商目前未知**。Blockaid 是 MetaMask 文档化的申诉/复核渠道之一，但**并非已确认**是本站判定结果的供应商。
- 本文件**不断言「误报已成立」**，只提供可核查的事实与申诉渠道。英文正文见 [§4 REQUEST FOR REVIEW](#4-request-for-review-英文可直接粘贴)。
- **禁止事项（适用于所有操作）**：不关闭/绕过警告、不换域名/换源规避、不诱导评审者禁用安全特性、不代任何人提交表单或发 issue、不发任何链上交易。

---

## 1. 可核查的项目事实

| 项 | 值 | 出处 |
| --- | --- | --- |
| 公开仓库 | https://github.com/warku123/TokenShare | docs/SUBMISSION.md |
| 公开 HEAD | `141c135`（`141c135a4e6f0d16bc49fd78087dc7d981558ffb`） | git log；本文件行号引用与之对齐 |
| 线上站点 | https://tokenshare-web.vercel.app/ | docs/SUBMISSION.md |
| 钱包控制台页 | https://tokenshare-web.vercel.app/console.html#seller | web/console.html |
| 链 | **Monad Testnet，chainId `10143`（仅测试网）** | web/config.js |
| Escrow | `0xe4D5Eb0dBDB6DB8063C07ECF7EDFcCdDB9Ad514c` | web/config.js |
| Registry | `0xeD347cDc1761750E20C024459b38dedFb1462254` | web/config.js |
| USDC（官方 Circle 测试网 USDC） | `0x534b2f3A21130d7a60830c2Df862319e593943A3` | web/config.js |

> 边界：我们**不声称**「线上部署字节与仓库 HEAD 逐字节等同」（部署产物未做逐字节比对），仅说明站点按项目记录从 `141c135` 这条开发线部署、仓库公开 HEAD 为 `141c135`。

### 1.1 钱包交互事实（源码支撑，行号对应公开 HEAD `141c135`）

- **连接/切换钱包**：所有权限与账户弹窗均由**用户主动点击**触发。初次选择钱包调用 `eth_requestAccounts`（`web/common.js` L1546-1554，`connectInteractive`）；SWITCH ACCOUNT 先调用 `wallet_requestPermissions({eth_accounts:{}})`（L1594-1611，`switchAccount`），用户取消（4001/ACTION_REJECTED）即中止、不降级；仅当钱包不支持该方法等**非取消类错误**时才降级为 `eth_requestAccounts`。
- **会话恢复**：静默 `eth_accounts`（`web/common.js` L1621-1634，`resume`），不会在未授权时自动弹窗。
- **ERC-20 授权**：仅**精确金额** `approve` 到配置的 Escrow 地址，且仅当现有 allowance 不足时发起，随后 `deposit`（`web/console.js` L2076-2091）。**应用脚本中未发现** `setApprovalForAll`、EIP-2612 `permit`、`eth_sign`（已核查 `web/*.js` 应用脚本；`web/vendor/` 第三方库与 `contracts/lib/` 依赖测试不在应用行为范围内）。
- **签名操作**：全部为用户逐次确认的 EIP-191 `personal_sign`（经 ethers `signer.signMessage`），共 5 个调用点，覆盖 5 类动作：
  1. 托管密钥注册（custody enrollment）— `web/console.js` L1597
  2. 托管密钥撤销（custody revoke）— `web/console.js` L1805
  3. API key 铸造/授予（grant/mint）— `web/console.js` L2216
  4. API key 撤销（grant revoke）— `web/console.js` L2475
  5. 聊天逐条支付消息签名 — `web/console.js` L2631
- **依赖**：web 端 ethers v6.13.5 以本地 vendored 文件随站点发布（`web/vendor/ethers.umd.min.js`），无运行时第三方脚本注入面。
- 配置唯一入口：`web/config.js`（链与合约地址如上表）。

### 1.2 明确不声称的事（防夸大）

- 不声称 drainer / 恶意行为「不可能」；
- 不声称「所有交易均为模拟」；
- 不声称所有依赖已通过外部验证；
- 不声称源码可证明线上部署字节等同；
- 不声称中继（relay）当前在线率/可用性有任何保证；
- **TEE 不是全面安全证明**：结算委托（delegate）按合约规则可在 seller 锁定上限内结算 seller 的 Locked 支付（不能重定向已入账 seller 资金）；买家在测试网上仍承担**真实但非零**的风险；
- 以上仅为**有限代码侦察**结论，**不是形式化安全审计**。

---

## 2. 观察证据：MetaMask 公开扫描接口（2026-10-09）

只读 GET，可随时自行复核（无副作用、非提交）：

```bash
# 根域名（2026-10-09 观察到 recommendedAction: BLOCK）
curl -s "https://dapp-scanning.api.cx.metamask.io/v2/scan?url=https%3A%2F%2Ftokenshare-web.vercel.app"

# 卖家控制台页（2026-10-09 观察到 recommendedAction: BLOCK）
curl -s "https://dapp-scanning.api.cx.metamask.io/v2/scan?url=https%3A%2F%2Ftokenshare-web.vercel.app%2Fconsole.html%23seller"
```

**观察记录模板**（提交前重跑一次，粘贴完整 JSON 并填 UTC 时间；不要手改字段值）：

```
Observed at (UTC): [YYYY-MM-DD HH:MM:SS UTC — 填写实际执行时间]
Scan URL (root):            https://dapp-scanning.api.cx.metamask.io/v2/scan?url=https%3A%2F%2Ftokenshare-web.vercel.app
Scan URL (console#seller):  https://dapp-scanning.api.cx.metamask.io/v2/scan?url=https%3A%2F%2Ftokenshare-web.vercel.app%2Fconsole.html%23seller
Observed (2026-10-09): hostname=tokenshare-web.vercel.app, recommendedAction=BLOCK (both URLs)

Root scan JSON:
[PASTE_FULL_SCAN_JSON_ROOT_HERE]

Console scan JSON:
[PASTE_FULL_SCAN_JSON_CONSOLE_HERE]
```

> 供应商侧判定随时可能变化；若供应商更新判定，可能还需 MetaMask 侧传播后才反映到钱包。申诉前**务必重跑**以上命令，以当日 JSON 为准。

### 2.1 钱包端证据收集（截图 + 版本 + UTC 时间）

- [ ] MetaMask 浏览警告页完整截图（含完整 URL 栏）：`[ATTACH_SCREENSHOT_WALLET_WARNING]`
- [ ] MetaMask 版本号（设置 → 关于）：`[METAMASK_VERSION]`
- [ ] 复现时间（UTC）：`[YYYY-MM-DD HH:MM:SS UTC]`
- [ ] 复现路径记录：哪个入口（首页 / `console.html#seller`）、哪个动作触发警告、钱包扩展 or 移动端
- [ ] 截图中如含个人地址/余额，先打码再存档

---

## 3. 中文操作手册（人工步骤）

### 步骤 1 — 重跑扫描，固化当日证据
按 §2 命令重跑两个 scan URL，把完整 JSON 原样粘贴进 §2 模板（含 UTC 时间）。不要编辑 JSON 字段。

### 步骤 2 — MetaMask 官方支持（Support）
1. 打开 https://support.metamask.io/ ，进入在线聊天（右下角 chat 入口）。
2. 粘贴 [§4](#4-request-for-review-英文可直接粘贴) 的 **REQUEST FOR REVIEW** 全文（含占位符已填好的扫描 JSON、截图、MetaMask 版本、UTC 时间）。
3. 如客服要求验证域名控制权，按 §3.5 准备证明；**只提供脱敏材料**。
4. 记录：聊天开始/结束 UTC 时间、客服工单号（如有）。**没有 SLA 承诺，不要催促式施压，也不要预期保证解除时限。**

### 步骤 3 — Blockaid 申诉/复核渠道（文档化渠道，非已确认判定方）
1. 打开 https://report.blockaid.io/mistake （“报告误报/复核”表单）。**页面含人工 captcha，需真人完成**，本仓库不会也不应代提交。
2. 表单要点（照 [§4](#4-request-for-review-英文可直接粘贴) 填写）：URL、观察日期、扫描 JSON、钱包行为摘要（§1.1）、仓库与 HEAD。
3. 若表单提供项目认证入口（`/verifiedProject` 路径下的 Project Verification），同样需真人 captcha；**是否申请认证由项目负责人决定**，本文件只记录渠道。
4. 保存提交回执/编号：`[BLOCKAID_REPORT_ID — 提交后填写]`。

### 步骤 4 —（可选，仅记录渠道，不在本仓库内执行）eth-phishing-detect
- 渠道：https://github.com/MetaMask/eth-phishing-detect （MetaMask 维护的钓鱼域名清单仓库，可按其 README 以 issue 形式反馈误收域名）。
- 注意：这是**独立于**扫描接口的另一条清单机制；是否适用本例（vercel.app 子域）由该仓库维护规则决定。**本项目内只记录 URL，不代发 issue**；如需反馈由项目负责人人工进行。

### 步骤 5 — 域名控制权证明（仅在对方要求时提供）
- `tokenshare-web.vercel.app` 是 Vercel 项目子域，控制权通过 **Vercel 项目所有权**体现。可选证明方式：
  1. 若审核方要求在站点放置一次性挑战文件（如 `https://tokenshare-web.vercel.app/<challenge>.txt`）：该文件必须从**生产主机名本身**可访问。分支/预览部署的 URL 属不同主机名，**不足以**证明对本域名的控制；任何向该生产主机名的实际部署都是**需单独批准的独立动作**，本文件不执行、也不代替批准。
  2. Vercel 项目 Dashboard 所有权截图（**遮蔽邮箱、team 名、任何 token**）；
  3. 在公开仓库 HEAD 提交审核方指定的一次性标记（审核方确认后可移除）——此法证明的是**仓库控制权**，域名控制仍以生产主机名上的验证为准。
- **红线**：任何私钥、API key、session token、`.env` 内容**永远不出现在截图或文本里**；不确定就再遮一层。**本文件不预设任何控制权证据**——在完成验证前，`[DOMAIN_CONTROL_PROOF]` 一律留空占位。

### 步骤 6 — 禁止动作清单（自查）
- [ ] 未建议任何人「无视警告继续操作」「关闭安全检测」「信任此站点标记」
- [ ] 未使用任何替代域名/镜像/IP 直连规避封禁
- [ ] 未使用非 MetaMask 钱包来「绕过演示阻断」
- [ ] 未以项目/仓库身份代发任何表单、工单、issue
- [ ] 未发起任何链上交易作为「证明」

### 期望结果与后续
- 任何渠道**均无 SLA/保证**；结果可能为维持、解除或要求补充材料。
- 若供应商侧更正判定，钱包端生效可能仍需 MetaMask 侧同步/传播——解除确认以**钱包端实测**（新会话打开站点不再出现警告）+ 当日 scan JSON 为准，而非口头答复。
- 演示门禁：解除前遵循 [docs/video/TECHNICAL_DEMO_DRAFT.md](./video/TECHNICAL_DEMO_DRAFT.md) 的硬停线规则（未解除 → 不做真实连接/签名的演示）。

---

## 4. REQUEST FOR REVIEW（英文，可直接粘贴）

> 使用说明：填好所有 `[PLACEHOLDER]` 后整段复制到 Support 聊天或 Blockaid 表单。**不要虚构联系邮箱或控制权证据**——没有就写 "to be provided after verification"。本信为中性复核请求，**不主张误报已成立**。

```text
REQUEST FOR REVIEW — security warning on https://tokenshare-web.vercel.app

1) WHAT WE ARE ASKING
We are the maintainers of TokenShare, an escrow-based demo marketplace
for metered LLM API access, deployed on Monad TESTNET only (chainId
10143). Buyer budgets are escrowed on-chain (exact-amount USDC approvals
to a fixed escrow contract); seller API keys are sealed in a shared TEE
relay — our public policy page states plainly this is NOT a zero-custody
design (web/policy.html).
MetaMask currently shows a security warning (BLOCK) for our domain. We are
NOT claiming the verdict is a false positive — we do not know the reason or
the vendor behind it. We are requesting a review of the current verdict and
offering verifiable facts about the app's wallet interactions to support it.

2) IDENTIFICATION
- Site:            https://tokenshare-web.vercel.app/
- Flagged paths:   https://tokenshare-web.vercel.app/
                   https://tokenshare-web.vercel.app/console.html#seller
- Public repo:     https://github.com/warku123/TokenShare
- Public HEAD:     141c135 (line numbers below refer to this commit)
- Chain:           Monad Testnet, chainId 10143 ONLY (no mainnet deployment)
- Escrow:          0xe4D5Eb0dBDB6DB8063C07ECF7EDFcCdDB9Ad514c
- Registry:        0xeD347cDc1761750E20C024459b38dedFb1462254
- USDC (testnet):  0x534b2f3A21130d7a60830c2Df862319e593943A3
- Contact:         [YOUR_CONTACT_EMAIL — fill only with a real, monitored address; NEVER invent]

3) OBSERVATION (dated evidence)
- Observed on 2026-10-09 via the public scan endpoint:
  https://dapp-scanning.api.cx.metamask.io/v2/scan?url=https%3A%2F%2Ftokenshare-web.vercel.app
  → hostname "tokenshare-web.vercel.app", recommendedAction "BLOCK"
  (same BLOCK observed for /console.html#seller)
- Full scan JSON at time of submission:
[PASTE_FULL_SCAN_JSON_HERE]
- Wallet warning reproduced at (UTC): [YYYY-MM-DD HH:MM:SS UTC]
- MetaMask version: [METAMASK_VERSION]
- Screenshot: [ATTACH_SCREENSHOT]

4) WALLET-INTERACTION FACTS (auditable in the public repo at HEAD 141c135)
- Connect / switch account: every permission or account prompt is
  triggered by an explicit user click. Initial wallet selection calls
  eth_requestAccounts (web/common.js L1546-1554). SWITCH ACCOUNT calls
  wallet_requestPermissions({eth_accounts:{}}) first (L1594-1611);
  a user cancellation (4001) stops there, and only a non-cancellation
  error — such as the wallet not supporting that method — degrades to
  an eth_requestAccounts fallback.
- Session restore: silent eth_accounts on reload; never auto-prompts when
  unauthorized (web/common.js L1621-1634).
- Token approvals: only exact-amount ERC-20 approve() to the configured
  escrow address, issued only when the current allowance is insufficient,
  immediately followed by the deposit() it is meant to cover
  (web/console.js L2076-2091). No setApprovalForAll, no EIP-2612 permit,
  and no eth_sign anywhere in the application scripts (web/*.js,
  excluding third-party vendor/library code).
- Signatures: every signing request is a user-confirmed EIP-191
  personal_sign (ethers signer.signMessage) at five call sites:
  custody key enrollment (web/console.js L1597), custody key revocation
  (L1805), API-key mint/grant (L2216), API-key revocation (L2475), and
  per-message chat payment signing (L2631).
- Frontend dependencies: ethers v6.13.5 is vendored locally with the site
  (web/vendor/ethers.umd.min.js); no runtime third-party script injection.
- Testnet disclaimer: all funds on chainId 10143 are test assets; we do not
  claim zero risk for users, and the project is not a formal security audit.

5) SCOPE LIMITS (stated explicitly so the reviewer is not misled)
- We do not know why the site is flagged or which vendor produced the
  verdict; Blockaid's report channels are documented review routes, not a
  confirmed attribution for this verdict.
- Facts above come from reading our own public source at a pinned commit;
  they are not an external audit, and we do not claim byte-level equality
  between the repo HEAD and the deployed bundle.

6) WHAT WE WILL PROVIDE ON REQUEST
- Proof of domain control via the Vercel project that serves this
  hostname (dashboard/ownership evidence), and/or a reviewer-chosen
  challenge file. Note: a challenge is only meaningful if served from
  https://tokenshare-web.vercel.app itself — preview/branch deployments
  on different hostnames are NOT sufficient, and any actual production
  deployment for a challenge would be a separately approved action that
  has not been performed as part of this request. Any evidence shared
  will have secrets redacted.
  [DOMAIN_CONTROL_PROOF — left blank until verification]
- Point-in-time access to a maintainer for follow-up questions.

7) REQUESTED OUTCOME
Please review the current verdict for tokenshare-web.vercel.app and advise
what additional information you need. If the verdict is corrected on the
vendor side, we understand propagation on the MetaMask side may still be
required and we will verify against the wallet UI and the public scan
endpoint before considering the issue resolved.
```

---

## 5. 内部附注（不随申诉外发；非封禁原因声明）

- 首页「platform TEE relay」等表述为**项目自述**（badge/文案性质），对外材料不应将其当作经第三方验证的安全声明；后续可收敛措辞或补充可核查引用。
- 站点页面**未观察到** Content-Security-Policy 头（`web/*.html` 无 CSP meta，部署层未见配置）——内部加固项，**本文件不断言它是本次 BLOCK 的原因**。
- 两项均为内部待办，与 §0 立场一致：原因未查明前，不做归因。

---

## 6. 相关文档（相对链接，均保持原状未被本文件修改）

- [docs/SUBMISSION.md](./SUBMISSION.md) — 提交口径与公开链接
- [docs/CRE_DEMO_EVIDENCE.md](./CRE_DEMO_EVIDENCE.md) — CRE 结算证据
- [docs/DEMO_RUNBOOK.md](./DEMO_RUNBOOK.md) — 演示手册
- 视频脚本 — 本地维护、不在仓库
- [docs/video/TECHNICAL_DEMO_DRAFT.md](./video/TECHNICAL_DEMO_DRAFT.md) — 演示安全门（硬停线）
- [docs/submission/README.md](./submission/README.md) — 提交文章索引与事实状态总表
- [web/config.js](../web/config.js) — 链/合约配置唯一入口
- [web/common.js](../web/common.js) · [web/console.js](../web/console.js) — 钱包交互源码（行号见 §1.1）
- [web/policy.html](../web/policy.html) — 面向用户的产品说明页
- [web/TESTING.md](../web/TESTING.md) — 测试基线
