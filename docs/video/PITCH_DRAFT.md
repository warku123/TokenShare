# 路演视频草稿（PITCH_DRAFT）— 团队/问题/为什么（TEE 叙事·出镜小窗版）

> 平台硬性上限 **2:00**；本稿时间轴收在 **1:50**。英文口播 **188 词**（机算），按 105–120 wpm 即 **1:34–1:48**。
> 叙事中心是 **TEE**：口播会明确拼读字母 **T-E-E** 并解释 "TEE, a Trusted Execution Environment"。可信差异点：卖家上游 key 加密上传后只在共享 Phala TDX enclave 内处理；买家只拿自己的 limited/revocable 凭证、不经手原始 upstream key；attestation quote 公开可查、锚定所连 relay 的部署身份。
> 口播改用**短句+常用词**（非母语友好）。只讲「是谁（占位符）/问题/我们做了什么/为什么重要」，**不虚构团队规模、用户经历、traction 数据或最新 uptime**；出镜人姓名用 `[USERNAME]` 占位，录制时替换即可。

## 录制安全线（中文，不上镜）

- **画面形态**：摄像头小窗全程保留（出镜人+页面同框），中段只做页面切换、小窗不消失；**不每句换页**，按下表英文 cue 切换。
- **全程只读**：不连接钱包、不签名、不触碰任何交易按钮；MetaMask dapp-scan 域名警告此前被 BLOCK 且未证明解除，**不做任何连接/签名/绕过尝试**。
- **页面只用真实存在内容**：policy 页 04「KEY CUSTODY」面板（平台自述：Phala TDX 共享中继、key 应用层加密后传输、enclave 内处理、静态加密存储、'not zero-custody, not trustless'）；FULL MARKET 卖家列表在线时的紫色 `TEE` 徽标及其 `/attestation` quote 链接（列表数量以实际为准，不预设卡片数）。attestation 按页面原样表述（dcap-qvl 本地验证的 DCAP quote，reportData 绑定 relay 签名者/上传密钥；页面自述为「部署锚定证据，非完整硬件根信任证明」）——**不得**改称「独立硬件验证」「硬件根信任的网页验证」「平台无法访问」「零风险」「trustless」。
- **首页说明**：designer 正在为首页（index.html/styles.css）新增 TEE constraints 区；本稿不涉及该文件，录制前以首页实际上线内容核对开场画面，不预设其文案。
- 所有链上/演示事实引用已记录证据（[../SUBMISSION.md](../SUBMISSION.md)）；不承诺评审结果；Kimi/Qwen 调用为**历史 testnet 记录**；签名收据是 **billing 声明**，与链上预算互补、**≠独立 token 计数证明**；不虚构任何 uptime/新增证明；provider/authorized partners 仅作**潜在**渠道，非已签约。
- 本稿撰写阶段未执行任何新 gas/token 操作。

## 时间轴（≤1:50；小窗全程保留，按 cue 切页）

| 时间 | 画面/操作（中文） | 口播 |
| --- | --- | --- |
| 0:00–0:16 | 真人出镜开场；浏览器显示首页 **OVERVIEW**（`index.html`），小窗+首页同框 | 自我介绍+一句话问题 |
| 0:16–0:33 | **留在首页不切页**；小窗+首页同框讲问题 | 问题展开：共享 key 的风险，引出「key 放进特殊硬件」悬念 |
| 0:33–1:15 | **TEE 核心段**（三个 cue，共 42 秒）：① cue "TokenShare does this with a TEE" → 切 **POLICY 04「KEY CUSTODY」**面板只读滚动；② cue "Anyone can open an attestation quote" → 切 **FULL MARKET**（`market.html`）卖家列表紫色 `TEE` 徽标 + `/attestation` quote 只读一瞥（如未在线显示则停留 policy 面板）；③ cue "Buyers get their own limited, revocable key" → 切 **CONSOLE → BUYER** 面板顶部（FLOW 条 + LOCK 主卡，'ESCROW balances(me)' 余额行与 MAX AMOUNT/TTL 表单**只读展示**，不填、不点 `[ LOCK ]`） | 产品+TEE：拼读 T-E-E、key 加密托管、attestation 身份、买家 scoped 凭证与链上预算 |
| 1:15–1:32 | **回 FULL MARKET** 只读；可叠一行「potential, not a signed deal」字幕 | 延伸定位（潜力，未签约） |
| 1:32–1:50 | **回首页 OVERVIEW**；出镜收尾，叠 GitHub 仓库+演示站结尾卡 | 诚实收束（历史记录+局限） |

## 英文口播（逐句照读；短句、常用词）

### 0:00–0:16（首页 OVERVIEW）

🎙 "Hi, I'm [USERNAME], building TokenShare. Many teams pay each month for AI plans they never use up. But agent projects still need API access with a spending cap."

### 0:16–0:33 问题（留在首页）

🎙 "The usual fix is sharing an API key. That is risky: keys can leak, and bills are hard to check. What if the key lives in special hardware instead?"

### 0:33–1:15 产品与 TEE（核心段；切页 cue 见时间轴）

🎙 "TokenShare does this with a TEE — T, E, E — a Trusted Execution Environment. The seller's key is encrypted first, then processed only inside a shared Phala TDX enclave. Anyone can open an attestation quote that shows which relay is really running — you check the deployment, not a promise. Buyers get their own limited, revocable key — never the raw one — with a spending cap locked on-chain."

### 1:15–1:32 为什么重要（回 FULL MARKET）

🎙 "Why it matters: trust becomes something you can see. The same rails could later give providers — or their authorized partners — a new sales channel. That is potential, not a signed deal."

### 1:32–1:50 收尾（回首页 OVERVIEW）

🎙 "Honest limits: Kimi and Qwen calls are past testnet runs. Signed receipts and on-chain budgets help check bills — they don't prove token counts alone. Repo and demo are linked below. I'm [USERNAME], thanks for watching."

## 口播词数与时长

- 合计 **188 词**（机算规则同技术稿：仅取 🎙 引号口播行 → 去引号 → 剔除独立 `—`/`+` 纯标点 token → 空白分词；`[USERNAME]` 占位出现 2 次、各记 1 词；拼读的 `T,` `E,` `E` 各记 1 词）
- 分段：28 / 29 / 65 / 31 / 35；按 105–120 wpm 即 **1:34–1:48**，留出切页停顿后收在 1:50 时间轴内；TEE 段（0:33–1:15，42 秒）为全片最大时间块，65 词（约 33–37 秒）+ 切页余量
- 超时先删 1:15 段整段（−31 词 → 157 词），仅保留画面字幕「potential, not a signed deal」。
