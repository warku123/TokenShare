# TokenShare 提交视频脚本（≤3 分钟）

> 提交硬性要求（Metropolis rules v3）：**≤3 分钟**、公开放 YouTube/Loom/Vimeo、
> **必须展示 Monad 集成**（钱包网络 + 浏览器交易证据）。
> 现场事实基线：Vercel 生产站 + Phala TEE 共享 relay（双卖家）+ Escrow v3.2。
> 录前 checklist 与应急表见 `docs/DEMO_RUNBOOK.md`（①余额 ③备用 key ⑥节奏）。

## 录前 30 分钟 checklist

- [ ] BUYER 钱包：MON ≥1、USDC ≥5、Escrow 余额 ≥2（不足走两个 faucet + console DEPOSIT）
- [ ] TEE relay 健康：`curl -s https://c30652f4833465adda9bdc7ac557e8b45e8e66c6-8787.dstack-pha-prod5.phala.network/health` 返回 ok
- [ ] `/verify-upstream` `key_valid:true`（Kimi 额度不足先换 key，见 runbook ②）
- [ ] 预 mint 1-2 把备用 tsk1 key（防现场签名环节意外，存密码管理器）
- [ ] 浏览器预备好三个标签：市场页 / console 页 / explorer（提前打开 lock 交易待用）
- [ ] 钱包网络切 **Monad Testnet (10143)**；录屏 1920×1080；开勿扰模式
- [ ] 记住节奏：Kimi RPM=3，两次真实调用间隔 ≥20s；**REVOKE 演示（若有）放最后**

## 脚本正文（中文台词 + 画面动作）

### 0:00–0:15 开场（画面：市场页全景）

> 「这是 TokenShare——LLM API 额度的链上租赁市场。卖家把闲置的大模型额度变成收入，
> 买家按 token 付 USDC，双方无需互信：托管合约 + TEE 可信执行环境兜底。
> 全部跑在 Monad 测试网。」

### 0:15–0:45 市场页 = 链上真实数据（滚动两个卖家卡）

> 「市场页每一行都来自链上 Registry 的实时数据——不是硬编码。现在有两个真实卖家在线：
> 一个卖 Kimi 系列，一个卖 Qwen 3.8 Max。绿点是 relay 健康检查。
> 每个模型三档价格：缓存输入 / 普通输入 / 输出，全部由卖家自己在链上登记。」

（鼠标指 relay 域名）

> 「两个卖家共享同一个 relay——它跑在 Phala TEE 里，卖家托管的 API key 从不出 enclave，
> attestation 公开可验。」

### 0:45–1:20 买家下单（console 页：连接 → 充值 → 锁预算 → mint key）

> 「切到买家视角。连接钱包——注意这里的政策勾选：付费操作必须先完成风险告知确认。
> 先把 USDC 充进托管合约，然后选模型、锁一笔预算。」

（签名 lock 交易，等 1 个确认）

> 「预算上链锁定后，页面 mint 出一把临时 API key——额度和有效期都受这笔链上 payment 约束。」

### 1:20–1:55 真实调用（终端 curl，标准 OpenAI 兼容接口）

（贴入 curl，`tsk1…` key 可见）

> 「拿这把 key 直接调任何 OpenAI 兼容客户端。」

（回车，回复流出）

> 「真实的 Kimi 模型回复。计费按 token 精确累计；预算耗尽或 key 过期，下一次调用立即被拒。」

### 1:55–2:35 链上结算（切 explorer：lock tx + settle tx）

> 「调用完成，TEE relay 用受托签名代卖家触发结算——Escrow 的 settleDelegateOf：
> seller 事先一次性授权，资金只能进卖家自己的链上余额，delegate 改不了去向。
> 浏览器里 lock 和 settle 都是真实的 Monad 交易，卖家的已结算余额随之增加。」

（指着 settle tx hash 停 2-3 秒）

### 2:35–2:55 信任模型收尾（回 console）

> 「买家侧保护同样硬：payment 到期，未消费的预算买家可取回；key 支持一键吊销、即时生效。
> 卖家的上游 key 全程密封在 TEE。没有托管跑路，没有 key 泄露。」

> 「TokenShare——让闲置的 AI 算力流动起来。仓库地址见简介。」

### 2:55–3:00 结尾卡

（黑底白字：`TokenShare · github.com/warku123/TokenShare · Monad Testnet 10143`）

## 录制备注

- 指哪讲哪，别逐字念稿；每段录完停 2s 方便剪辑。
- 建议上传英文字幕（YouTube 自动翻译或手写 .srt）——评审是国际评委。
- 上游限速（502）应急：停 60s 重试一次；仍失败用预录的调用段替换 1:20–1:55。
- 若加 REVOKE 演示：放在 2:35 之后 5 秒带过（吊销态在 relay 内存，之后**不要再动 relay**）。
- 时长红线：成片 ≤3:00，宁可在 0:45/1:55 段加速也别砍 Monad 证据（1:55–2:35 不可删）。
