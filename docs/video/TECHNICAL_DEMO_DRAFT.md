# 技术演示视频草稿（TECHNICAL_DEMO_DRAFT）— 全真实流程实拍：新卖家挂单 → 另一买家购买 → 页内真实对话

> 平台硬性上限 **3:00**；本稿时间轴收在 **2:50**。英文口播 **241 词**（机算），100–120 wpm ≈ **2:01–2:25**，其余时间留给真实操作。
> 完整新购买流程实拍：**新卖家 VERIFY + SEAL KEY → SUBMIT TO REGISTRY（新挂单）→ AUTHORIZE DELEGATE → 另一买家 APPROVE + DEPOSIT → 新 LOCK → 页内 SIGN + CALL 真实回复**。不是 receipt-only 走读，也不许用 receipt-only 冒充完整新购买。
> 口播用**短句+常用词**；**本人出镜，摄像头小窗全程常驻**（位置固定，不遮金额/三档价/模型回复/按钮），只按 cue 切页面。UI 动作以现网代码为准：[web/console.html](../../web/console.html)、[web/console.js](../../web/console.js)、[web/config.js](../../web/config.js)、[web/TESTING.md](../../web/TESTING.md)；CRE 历史证据见 [../CRE_DEMO_EVIDENCE.md](../CRE_DEMO_EVIDENCE.md)。Monad 测试网 chain **10143**，Escrow v3.2 + Registry v4 + 共享 Phala TDX 中继。

## 录制安全线（先读，全中文，不上镜）

- **安全门（硬停线）**：MetaMask dapp-scan 对 `tokenshare-web.vercel.app` 域名级 **BLOCK，未证实解除**。连接/签名前必须确认已官方解除；未解除 → **硬停**，只做无连接/无签名的排练，且不得声称画面发生真实调用。**若录制中仍出现告警 → 先 STOP**。绝不建议/执行：关警告、绕过、换钱包、换域名规避；不承诺 localhost 或其它钱包不被标记。**本稿不给任何钱包/交易操作授权。**
- **小窗纪律**：出镜小窗全程不消失、不遮挡关键信息；确认弹窗与成功结果必须完整入镜。
- **p5 已 Settled**：payment 5 不得再 mint、再调用、不得复用；只能只读引用历史证据。
- **密钥卫生**：minted key（`tsk1…`）全文、私钥、上游 key、`.env`、shell history 一律不出镜；上游 key 走密码框（console.html `UPSTREAM API KEY` 为 `type=password`）且输入离屏；MINT 弹窗出 key 瞬间打码或剪除；屏上只出现截断地址，标注 SELLER/BUYER 两个不同角色。
- **限速**：Kimi RPM=3——全片只安排**一次**真实付费调用；重拍两次间隔 ≥20s。
- **两条流不许混**：CALL DEMO 是逐请求钱包签名流（`X-Payment-Id` + `X-Signature`）；MINT API KEY 出的 OpenAI 兼容 bearer key 是**另一条独立流**，可选、不拍也成立。**LOCK 本身不 mint key**，口播/字幕绝不称页内调用消耗了 minted key，也不伪造自动 mint。
- **托管语义**：LOCK = 已注资 USDC 托管，非可转让资产；未用余额按合约 state/TTL 退款（REFUND/withdraw 按合约规则，非即时）；`settlePartial` ≠ 自动最终结算。
- **挂单语义（防伪挂单）**：全新未上架地址 → 单笔 `register`。既有 active listing 的钱包 → 裸 `register` revert（`AlreadyRegistered`）：调价走 `updatePrice`；形状变化走 deactivate → register 两笔并**如实标注 update**，绝不称 "new listing"。
- **未来排练预案（非当前许可）**：用户**未来单独授权**后，可用拍摄同一卖家地址真实排练（VERIFY+SEAL→REGISTER→AUTHORIZE），再 `deactivate()` 下架、**链上确认 inactive** 后重新 register 作为正式录制新挂单。deactivate = active=false，注册历史保留、非删除，也不自动撤销 custody key/settle delegate/退款。**本裁决不是现在执行任何交易的许可**；排练的费用与执行仍需逐项单独授权；纯 UI 无交易排练随时可选；不自动下架、不覆盖在架 listing。封禁未解除前，含连接/签名的排练同样硬停。
- **授权边界**：本稿撰写阶段**未执行任何操作**。新卖家入驻会改动公共 relay；上链 tx、gas、key 上传、真实付费调用——每一项都需用户逐项明确批准；脚本准备不构成操作授权。
- **证据纪律**：只展示实际发生的截断地址、chain 10143、已确认 tx 或 usage；不虚构时间戳/tx/哈希/模型回复。失败即停止/重排，无假兜底。

## 预检清单（录制外完成，任一不过 → 停下排练，不硬拍）

- [ ] 安全门复查：封禁未解除 → 仅排练（无连接/签名/调用）。
- [ ] 卖家地址：全新未上架钱包（`console.html#seller`）；备 MON gas；不把既有 key 复制进另一钱包充新卖家。
- [ ] 上游 key 合法授权：该卖家自己的套餐 key；预检官方 upstream 端点真实可达（relay 以 `upstream/v1/models` 探测 catalog）；BASE URL 粘贴时 UI 自动去尾部 `/v1`。
- [ ] 买家地址：独立钱包，与卖家不同；MON + 足量 6 位小数 USDC。
- [ ] pins 与 explorer：VERIFY 首步部署 pins 与 config 一致；explorer 可打开。
- [ ] 配额：RPM3 → 一次调用；重拍间隔 ≥20s。
- [ ] 干跑二选一：纯 UI 走查（不签名/不广播），或按上面「未来排练预案」经**单独授权**真实排练后下架重挂；2:50 是否装得下以试录为准，本稿不保证。
- [ ] 卖家可录制前离屏预填 BASE URL/key，但 **VERIFY+SEAL、SUBMIT、AUTHORIZE 必须当镜完成**；买家 DEPOSIT/LOCK/对话必须当镜真实发生。

## 时间轴（≤2:50 剪辑版；小窗常驻，按 cue 切页；等待可快剪但确认/结果不删）

| 时间 | 画面/操作（中文） | 口播段 |
| --- | --- | --- |
| 0:00–0:18 | **FULL MARKET 只读**：卖家卡 + 每模型三档价慢滚一遍 | S1 |
| 0:18–0:28 | cue "First, a brand-new seller" → 新卖家开 `console.html#seller`（截断地址入镜）→ **[ CONNECT WALLET ]** → 政策勾选 | S2 起 |
| 0:28–0:48 | **KEY CUSTODY**：粘贴 BASE URL + 密码框 key（输入离屏/预填）→ **[ VERIFY + SEAL KEY ]**：pins 核验 → 一次性 nonce → 浏览器内信封加密（ECDH P-256 → HKDF-SHA256 → AES-256-GCM）→ EIP-191 托管消息签名 → 上传 enclave → relay 实测 catalog（不可服务模型置灰） | S2 |
| 0:48–0:58 | 勾选可服务模型 + 三档价（卖家自报）→ **[ SUBMIT TO REGISTRY ]** → 钱包确认 → **tx 确认结果必须入镜** | S2 |
| 0:58–1:08 | **[ AUTHORIZE DELEGATE ]** → `approveSettleDelegate(已 pin 的 TEE signer)` → 确认结果入镜 | S2 止 |
| 1:08–1:18 | cue "Now a different buyer" → 切独立买家 profile（地址不同）→ console BUYER → **[ CONNECT WALLET ]** → 政策勾选 | S3 起 |
| 1:18–1:30 | **[ APPROVE + DEPOSIT ]**（approval 可已存在）→ escrow 余额入镜 | S3 |
| 1:30–1:46 | **LOCK**：下拉选刚才的新卖家 → MAX AMOUNT → TTL=600s → **[ LOCK ]** → **新 paymentId 大字入镜**（自动带入 CALL DEMO；LOCK 不 mint key） | S3 止 |
| 1:46–1:54 | cue "Now the real test" → **CALL DEMO** 卡：确认 seller/model/paymentId + prompt | S4 起 |
| 1:54–2:06 | **[ SIGN + CALL ]**：逐请求签名 → 中继路由到该卖家 TEE 内 key → **200 OK：真实回复 + usage + `X-Settle-Status` + 验证收据全部入镜** | S4 |
| 2:06–2:22 | 证据位：explorer 当场 lock/settle tx 或 usage 面板；截断地址+角色、chain 10143；（**可选**：SESSION LOCKS 的 [ MINT API KEY ] → [ SIGN + MINT ]，出 key 打码/剪除；跳过也成立） | S4 止 · S5 |
| 2:22–2:50 | cue "Proof you can check" → 收尾卡：GitHub 仓库 + 演示站 | S5 |

**剪辑纪律**：钱包弹窗/RPC/tx 等待可快剪，字幕注明「**等待已缩短**」；确认弹窗、签名动作、成功结果**不得剪掉**；不伪造任何成功画面。

## 英文口播（逐句照读；短句、常用词）

### S1 · 0:00–0:18（32 词）

🎙 "TokenShare turns spare AI plan quota into a small market. Sellers list models on-chain with three prices. Buyers pay per use in USDC, on Monad testnet. Everything next happens live, on camera."

### S2 · 0:18–1:08（65 词）

🎙 "First, a brand-new seller. A fresh wallet connects. Policy tick. The seller pastes the official base URL, and types the API key off camera. Then VERIFY + SEAL: the page checks the pins, and seals the key inside the browser — it goes up encrypted, for the TEE. Next, three prices per model, SUBMIT TO REGISTRY — one transaction, confirmed. Then AUTHORIZE DELEGATE — confirmed. The seller is live."

### S3 · 1:08–1:46（54 词）

🎙 "Now a different buyer — a separate wallet, a different address. Connect, policy tick, then APPROVE + DEPOSIT to fund escrow. LOCK: pick that new seller, set the cap, TTL six hundred seconds. One confirmed transaction makes the payment. This lock is funded USDC escrow — not an asset; what is unused comes back by contract rules."

### S4 · 1:46–2:22（54 词）

🎙 "Now the real test. CALL DEMO is pre-filled: this seller, the model, the fresh payment ID. One short prompt. SIGN + CALL — the buyer signs this one request; the relay uses the seller's sealed key inside the TEE. Seconds later: a real reply, live usage, the settle status, and a signed receipt. All on camera."

### S5 · 2:22–2:50（36 词；MINT 版）

🎙 "Proof you can check: the settle transaction on the explorer — chain ten-one-forty-three. MINT API KEY is a separate flow for agent keys; the in-page call never touches it. Budgets you can set. Bills you can check."

### S5 变体 B（无 MINT 素材时，31 词，整段 236 词）

🎙 "Proof you can check: the settle transaction on the explorer — chain ten-one-forty-three. A CRE pass once re-checked the receipt math — historical, clearly labeled. Budgets you can set. Bills you can check."

> CRE 对账是**历史 mock 演示**（2026-10-08，本地模拟 + MockKeystoneForwarder 广播），可选、非必需；分镜细节原归 Chainlink 演示草稿（提交前出于隐私清理已从仓库移除）。

## 词数与时长估算

- **主版本（含 MINT 句）**：**241 词** ≈ 100–120 wpm 下 **2:01–2:25**；S5 MINT 版 = 36 词。
- **变体 B（CRE 替换 MINT 句）**：**236 词** ≈ **1:58–2:22**。
- **机算规则（三稿统一）**：仅取 🎙 引号口播行 → 去引号 → 剔除独立 `—`/`+` 纯标点 token → 空白分词；连字符复合词（EIP-191、ten-one-forty-three 等）各记 1；本稿口播无占位符。
- 超时先整段换 **S5 变体 B**（净差 −5 词：36 → 31）→ 仍超时再剪 S1 末句 "Everything next happens live, on camera."（−6 词）。
- 操作窗口合计 **2:50**：钱包签名/tx 确认/VERIFY catalog 探测是主要耗时；等待可快剪（注明「等待已缩短」），确认与结果不删。**2:50 能否容纳全部真实操作，只有试录后才知道——本稿不作保证。**

## 中文排练清单（开拍前逐项打勾）

1. 安全门已复查；未解除 → 仅排练模式，不标「真实调用」；录制中见告警 → STOP。
2. 两个钱包就位：卖家=全新未上架（有 MON gas）；买家=另一地址（MON+USDC）；截断地址写在提词卡。
3. 上游端点可达（含 `/v1` 面）、key 为该卖家合法授权、BASE URL 文本已备（离屏粘贴）。
4. 干跑二选一：纯 UI 走查，或经单独授权真实排练后 deactivate → 链上确认 inactive → 再 register 重挂；DEPOSIT→LOCK→CALL DEMO 全链路只在正式录制执行；重排时间轴。
5. 限速：全片一次真实付费调用；重拍间隔 ≥20s。
6. 屏幕卫生：密码框离屏输入；不出镜清单（tsk1 全文/私钥/上游 key/.env/history）贴在显示器边。
7. explorer 分页预开；chain 10143 与截断地址可入镜；小窗位置试录确认不遮金额/价目/回复/按钮。
8. 若只能用既有钱包：按 **update** 口径（updatePrice 或 deactivate→register 两笔），不称 new listing。
9. 任何环节失败 → 停止/重排，不声称真实调用，无假兜底；p5 永不复用。

## 边界备忘（中文，不上镜）

收据=中继签名的账单陈述，非 token 真相；Registry 三档价=卖家自报，非上游官方价；settle delegate 只能结算**该卖家的** Locked 支付（含 TTL 后），动不了卖家 escrow 余额；退款/withdraw 受合约 state/TTL 约束，非即时；断开钱包只清页面政策同意，不吊销 key；testnet 代币无价值，上游套餐与云成本是真实成本；不承诺任何评审结果、团队规模或 traction。
