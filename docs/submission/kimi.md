# Kimi：从日常开发工具，到 TokenShare 平台的第一个真实上游

> 本文是 TokenShare（Monad 黑客松项目）提交材料的一部分。文中所有链上事实、测试计数与代码路径均可通过仓库内文件与公开区块浏览器核对；凡是只有项目负责人亲历、无法给出工程证据的内容，本文明确标注为「用户陈述」，不做任何夸大。

## 摘要

Kimi 在本项目里扮演了两个角色：

1. **开发工具**（用户陈述）：据项目负责人确认，Kimi 系列模型作为交互式编程助手参与了本项目从智能合约、中继服务到前端页面的日常开发。
2. **平台第一个真实上游**（有工程与链上证据）：TokenShare 是一个「把官方 LLM 套餐额度代管出租给买家」的协议。Kimi（Moonshot AI，OpenAI 兼容接口）是我们第一个完成**真实注册、真实调用、真实链上结算**的官方上游，全过程有 EIP-712 签名收据、链上交易与三方金额对账可查。

## 一、角色一：开发过程中的日常工具（用户陈述）

项目负责人确认：项目的合约（Solidity/Foundry）、中继（Python/FastAPI）、前端（原生 JS）以及 CLI 的日常开发中，Kimi 系列模型作为对话式编程工具被持续使用——用于设计讨论、代码草稿、排错与文案。

**本文不做的归因**：不指明具体任务由哪个模型版本完成，不统计调用次数，不宣称「节省了多少工时」，也不把「测试全绿」归因成模型能力——仓库最终的质量由 130 项合约测试、345 项中继测试、151 项 CLI 测试、159 项托管面静态检查以及 52 个源码路径的独立审计背书，那是**工程流程**（接口 PIN 逐字对齐、双端签名互证、独立 oracle 审查）的结果，而不是任何模型的性能结论。

## 二、角色二：平台第一个真实上游（工程证据）

### 2.1 架构里 Kimi 处在哪一环

TokenShare 的产品叙事是「官方套餐额度出租」：卖家把自己在官方平台的 API key 托管给中继，中继**只允许请求官方端点**（防投毒：`OFFICIAL_UPSTREAM_HOSTS` 白名单 + 模型前缀→厂商映射 + 请求级一致性校验，见 `relay/app/config.py`）；买家用钱包签名按量付费，费用以 USDC（6 位小数）在 Monad 链上锁定与结算。Kimi 是这套链路里第一个完成**真实调用 + 测试链按量结算闭环**的上游：

- API 端点：`https://api.moonshot.cn`（国内）/ `https://api.moonshot.ai`（国际）/ `https://api.kimi.com/coding`（Coding 套餐），OpenAI 兼容 Bearer 鉴权；
- 测试模型：`kimi-k2.6`、`kimi-for-coding` 等（价格三档：缓存命中输入 / 非缓存输入 / 输出）。

### 2.2 真实调用与链上结算证据

- **2026-09-23（M5b，Monad testnet 首次全链路；**本地工程记录**——当时部署为 Escrow 首版 `0x654c…` + Registry v1 `0x3a44…`，该阶段无公开 tx 存档，不可远端核对）**：卖家注册 listing → 买家锁定 10 USDC → **真实调用 Kimi** → 收据字段 `upstreamHost=api.kimi.com`、`model=kimi-k2.6`、`actualAmount=730`（native）→ 链上 `settle` 金额精确一致 → TTL 后 `refund` 闭环 → `E2E PASSED`。这是这个协议第一次用链上测试币（testnet USDC，**无真实价值**）按 token 计价完成真实的 Kimi 调用与测试链按量结算闭环；该次调用的**真实成本是卖家的上游套餐额度**。
- **2026-09-25（M13，按量多次调用闭环；当时部署为 Escrow v2 `0x31F9…` + Registry v4，与今日现役 Escrow v3.2 是**不同代际的部署**）**：买家锁定 2 USDC 后 mint 出 OpenAI 兼容 API key，用 `curl` 携带 Bearer key **真实调用 Kimi**（`kimi-for-coding`，prompt 88 / completion 24 tokens）→ 计费 `248 native = 88×2 + 24×3`，与套餐单价逐 token 精确一致 → `GET /payment/1/usage` 返回 `captured=248` → **链上 `capturedOf(1)==248` 三方对账一致**。这次调用证明了「一次锁定、多次调用、按量累计扣款」的 agent 计费模型真的能跑。
- **PROOF 区四笔真实交易**（deposit → lock → settlePartial → settle）连同 EIP-712 收据本地验签一起收录在市场页（`web/index.html` 的 PROOF 区块）。注意：这四笔是 M13 时期（Escrow v2）的**历史 proof**，不是现役 Escrow v3.2 的证明；现役部署事实见 §四（公开 tx 可经 explorer 核对）。

### 2.3 对接 Kimi 时真实踩过的坑（都有对应工程处置）

| Kimi 上游行为 | 我们的处理 |
| --- | --- |
| 对 `temperature`/`top_p`/`n`/`penalties` 传任何值（含 0）一律 400 | 中继**零注入**：不替买家补默认采样参数，买家自带被拒属上游行为，如实透传 |
| 流式响应的 usage 出现在 `[DONE]` 前的某个 chunk，可能在空 choices 里 | 取**最后一个非 null usage**，并对非标准顶层 `usage.cached_tokens` 做兜底 |
| Tier0 限速并发 1 / RPM 3 | 演示动线按 RPM 3 设计节奏；产品侧用「阈值批量结算」（`settlePartial` 累计 + 达阈值/临期落链）减少上链次数 |
| 缓存命中输入有独立单价（与普通输入/输出价分离） | 平台计价直接采用**每模型三档价**（cachedIn:input:output），链上 `getPrice` 按模型取价，账单公式 `(cached×cachedIn + (prompt−cached)×input + completion×output)//1e6` |
| 国内/国际两站 key 不互通；平台域名已 Kimi 化但 API host 未变 | host 归一化 + 白名单同时收录 `api.moonshot.cn`/`api.moonshot.ai`/`api.kimi.com` |

这些适配不是包装出来的卖点——它们是接入真实上游时逐条暴露、逐条写进代码与测试的行为差异。

## 三、作为上游方向的产品价值

- **买家**：钱包签名鉴权（EIP-191 请求签名 / 一次签名 mint 出标准 OpenAI 兼容 key），按 token 三档计价，USDC 6 位小数按量扣款；一次锁定可多次调用（`settlePartial`），TTL 后只退**未消费**余额。
- **卖家**：结算资金进入 `balances[seller]`，由卖家自行提现——当前部署 `feeBps = 0`，即卖方全额入账；协议费率由合约 owner 可调（0–10000 bps，调整经 `FeeTaken` 事件留痕）。上游 key 托管走应用层加密（ECDH-P256 信封 + KEK 包裹落盘），动线是**钱包身份与可信 TEE 身份 pin 核验 → 应用层加密托管（可先于 listing）→ Verify catalog → Publish**，并不要求先有 listing。
- **可替换上游**：中继的请求签名/验签/计价**核心接口与厂商无关、可复用**；但 host 白名单与前缀→厂商映射是**代码内的策略**，接入新厂商需要一次代码级配置——并非改配置即自动支持，且每个 provider 的 catalog 拉取、usage 字段、参数约束与限速都需**单独适配验证**。Kimi 是第一个完成这套适配的成员；第二个候选（Qwen/通义千问）的接入计划与条款边界见 [qwen.md](./qwen.md)。

**边界（如实声明）**：收据由中继签发，是 usage 上报而非服务质量的证明；testnet 测试币不豁免上游与云的真实费用；我们与 Kimi/Moonshot 无任何官方分销授权关系，不宣称「官方合作」；当前本地在线的 demo 中继仍是旧的 v3.1 单租户快照，共享多租户形态尚未部署到 TEE。

## 四、核对路径

- 合约与部署事实：[`contracts/deployed.monad.json`](../../contracts/deployed.monad.json)（Escrow v3.2 `0xe4D5Eb0dBDB6DB8063C07ECF7EDFcCdDB9Ad514c`，2026-10-07 部署，tx [`0xc7ca3ac798e16ace9f4554df51912aed235753f360d7f0a6417b7b0523afce4e`](https://testnet.monadscan.com/tx/0xc7ca3ac798e16ace9f4554df51912aed235753f360d7f0a6417b7b0523afce4e)，block 68854360）；Registry v4 与官方 USDC 地址未变。
- 上游白名单与前缀映射：[`relay/app/config.py`](../../relay/app/config.py)；中继部署与镜像：[`relay/README-docker.md`](../../relay/README-docker.md)（公开镜像 `docker.io/warku123/tokenshare-relay:m15-audited-20261007T015955Z`，index digest `sha256:5a8c33af444a35fad57cc1922f9d0a32f4327611734e4a10709ef12b044c66e0`）。
- Kimi 官方 API 端点记录：[`.env.example`](../../.env.example)（`api.moonshot.cn` / `api.moonshot.ai` / `api.kimi.com/coding`）。
- 演示动线与限速节奏：[`docs/DEMO_RUNBOOK.md`](../DEMO_RUNBOOK.md)。
- 索引与整体状态表：[README.md](./README.md)。

## 事实分层

| 层级 | 内容 |
| --- | --- |
| 工程/链上证据 | §2.2 三段真实调用与结算、§2.3 适配表、白名单/计价代码路径、Escrow v3.2 部署 tx |
| 用户陈述 | §一 开发过程中使用 Kimi 的亲历描述 |
| 未发生 | Qwen 真实上游调用、真实 TEE 部署、任何官方授权关系 |
