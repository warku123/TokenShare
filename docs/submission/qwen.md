# Qwen（通义千问）：开发参与者，与「差一步」的第二上游

> 本文是 TokenShare（Monad 黑客松项目）提交材料的一部分，与 [kimi.md](./kimi.md) 配对阅读。文中严格区分三件事：**已验证的工程事实**、**只有项目负责人亲历的用户陈述**、以及**明确尚未发生的事**。本文不含任何凭证值；所有曾出现在聊天或文档中的密钥都应视为已暴露并立即轮换。

## 摘要

Qwen 在本项目里同样是两个角色，但两个角色的成熟度完全不同：

1. **开发工具**（用户陈述）：据项目负责人确认，Qwen 系列模型作为交互式编程助手参与了本项目的日常开发。
2. **平台第二上游候选**（设计就绪，接入推进中）：中继的请求签名/验签/计价**核心接口与厂商无关、可复用**，接入 Qwen 的架构位置已经留好；经对阿里云 Token Plan 条款的官方研究，当前套餐端点按官方条款**不适用于后端/共享用途**——因此「Qwen 成为共享中继的第二卖家」涉及的真实调用与上架**尚未发生**（零调用、零费用、未上架）。项目负责人已知悉相关套餐限制并愿意承担披露范围内的风险，且已裁决**跳过本地 mock、直接在 Monad 测试网线上推进第二 seller 接入**（保留现有 Kimi 卖家）；真实动作开始前仍需合规的后端按量 key、供应商侧书面许可与费用/凭证安排确认（§三、§五）。我们不为了让文章好看而假装它已经跑通。

## 一、角色一：开发过程中的日常工具（用户陈述）

项目负责人确认：Qwen 系列模型作为对话式编程工具参与了本项目的日常开发（设计讨论、代码草稿、排错等）。与 Kimi 篇相同的边界：本文不指明具体任务对应的模型版本、不统计调用量、不把测试与审计结果归因成模型能力——质量结论属于工程流程（接口 PIN、双端签名互证、独立审查），不属于模型。

## 二、角色二：为什么说「架构就绪」

TokenShare 的中继从第一天起就按「多厂商可插拔」设计：

- **核心接口可复用，策略层需逐厂商适配**：请求签名/验签/计价/收据等核心接口与厂商无关；但 host 白名单与前缀→厂商映射是**代码内的策略**（`relay/app/config.py` 的 `OFFICIAL_UPSTREAM_HOSTS` / `MODEL_PROVIDER_PREFIXES`）——新增 Qwen 意味着一次代码级配置加验证，并非纯配置自动接入。请求级「模型前缀 ↔ 上游 host」一致性校验、收据 `upstreamHost + model` 字段、`/verify-upstream` 的「key 有效 + 已有 listing 时模型 ⊆ 可访问模型」交叉核验在接入后自动生效。
- **计价面天然匹配**：阿里云百炼提供 OpenAI 兼容模式（base URL 说明见官方文档 [help.aliyun.com/zh/model-studio/base-url](https://help.aliyun.com/zh/model-studio/base-url)）；平台的每模型三档价（cachedIn:input:output）与链上 `getPrice` 取价、USDC 6 位小数按量结算的管线对 OpenAI 兼容上游成立——Qwen 侧 usage 字段与缓存标识的**具体兼容性需接入时逐项验证**。
- **共享多租户流程已经定义**：共享 TEE 中继的卖家动线是「托管 key（应用层加密，先于 listing）→ VERIFY catalog（真实拉取 `/v1/models` 交叉核验）→ 选模型 + 三档价 → PUBLISH → 显式 AUTHORIZE 结算委托」。这套流程对 Qwen 卖家与对 Kimi 卖家完全一致，也是离线双卖家端到端测试（mock + Anvil，30 通过 1 跳过）所覆盖的形态。

换句话说：**核心接口可复用，但 Qwen 的 catalog 拉取、usage 字段、参数约束与限速仍需按 §二原则单独适配验证**；真实接入前缺的是合规后端 key、供应商许可与真实费用确认。

## 三、条款边界（本文最重要的一节）

2026-10-07，我们对 Qwen Token Plan 做了官方条款的只读研究。这些是**事实边界**，不因项目方愿意承担风险而改变：

- Token Plan 个人版/团队版端点（`token-plan.maas.qianwenaiapi.com/compatible-mode/v1`）**仅限交互式工具/智能体场景使用**，官方文档明确**禁止用于应用后端、自动脚本及共享**（[个人版说明](https://platform.qianwenai.com/docs/token-plan/personal/token-plan-personal-overview)、[团队版说明](https://platform.qianwenai.com/docs/token-plan/team/token-plan-team-overview)）。
- 平台服务条款对**未经许可向第三方转售/分 sublicense** 有明确限制（[服务条款](https://terms.alicdn.com/legal-agreement/terms/common_platform_service/20260423145804812/20260423145804812.html) §7.1.12 及 §1.4）；第三方转售需另行取得**书面许可**。

项目负责人知悉上述限制、愿意自行承担已披露的套餐使用风险，接入因此**继续推进**，但按以下边界分层执行：

1. **接入方向（用户已裁决）**：跳过本地 mock，直接在 Monad 测试网线上接入第二 seller（保留 Kimi）——这是方向确认，不代表任何真实调用、上架或许可已发生。
2. **真实动作的前提（达成前暂缓）**：适合后端按量计费的 key + 供应商侧书面许可；实际调用预算经项目负责人确认；凭证经**安全本地输入**（聊天中暴露过的凭证需轮换）。此前不发生真实调用、不托管、不上架、不产生费用。开发套餐 key 不用于对外提供服务。
3. `qwen3.8-max` 确实出现在官方模型列表中，但**我们的 key 对它的可访问性、`GET /models` 兼容性、以及任何真实使用均未验证**——与该模型相关的任何奖项所要求的「实际使用证据」目前**不存在**，本文与提交材料都不会伪造这一点。
4. 同样地，Kimi 篇的立场在此重申：我们与任何上游厂商都没有分销授权关系，免责声明不能替代授权。

## 四、明确尚未发生的事

- ❌ Qwen 作为上游的**真实调用**：零次；未产生任何费用。
- ❌ Qwen 卖家 listing：未注册、未上架。
- ❌ `qwen3.8-max` 或任何 Qwen 模型在我们中继上的可访问性验证。
- ❌ 共享多租户中继的真实 TEE 部署（Phala Cloud CVM 尚未创建，DCAP/KMS 跨重启连续性未验证）。
- ⚠️ 本地在线的 demo 中继（`127.0.0.1:8787`）仍是旧的 v3.1 单租户快照，与 Qwen 接入无任何关系。

## 五、后续接入计划（计划 ≠ 已完成）

项目负责人已确认：**跳过本地 mock 路线，直接在 Monad 测试网线上接入第二个 seller（保留现有 Kimi 卖家）**。这是方向裁决，不是已完成事实——真实动作开始前仍有明确前提：

1. **合规前提**：适合后端按量计费的 key + 供应商侧书面许可（Token Plan 套餐 key 不用于对外服务，§三）；
2. **费用与凭证前提**：实际调用预算待项目负责人确认；凭证一律经安全本地输入，聊天中暴露过的凭证需轮换——本文与仓库不含任何凭证值；
3. **基础设施前提**：共享中继的真实 TEE（Phala Cloud CVM）**尚未创建**；任何部署仍需就精确主机/命令/资源/成本获得 fresh 同意，本文所述接入计划不外推该授权。

前提就绪后的流程：共享中继 TEE 部署验证 → Qwen 卖家钱包签名托管 key（应用层加密信封，**可先于 listing**）→ `VERIFY catalog`（真实拉取模型列表；已有 listing 时与链上交叉核验，新卖家随后再 Publish）→ 选模型、按官方价目填三档价、PUBLISH、显式 AUTHORIZE 结算委托 → 买家真实调用 → 收据 `upstreamHost`/`model` 字段落定 → 链上按量结算 → **届时回填本文，把「设计就绪」升级成「已验证」**。

## 六、核对路径

- 上游白名单/前缀映射（可扩展点）：[`relay/app/config.py`](../../relay/app/config.py)
- 共享托管与卖家动线：[`relay/README-docker.md`](../../relay/README-docker.md)、[`web/TESTING.md`](../../web/TESTING.md)
- 链上部署事实（Escrow v3.2，2026-10-07）：[`contracts/deployed.monad.json`](../../contracts/deployed.monad.json)、tx [`0xc7ca3ac798e16ace9f4554df51912aed235753f360d7f0a6417b7b0523afce4e`](https://testnet.monadscan.com/tx/0xc7ca3ac798e16ace9f4554df51912aed235753f360d7f0a6417b7b0523afce4e)
- Qwen 官方来源：[Token Plan 个人版](https://platform.qianwenai.com/docs/token-plan/personal/token-plan-personal-overview) · [Token Plan 团队版](https://platform.qianwenai.com/docs/token-plan/team/token-plan-team-overview) · [百炼 base URL](https://help.aliyun.com/zh/model-studio/base-url) · [服务条款 §7.1.12/§1.4](https://terms.alicdn.com/legal-agreement/terms/common_platform_service/20260423145804812/20260423145804812.html)
- 索引与整体状态表：[README.md](./README.md)

## 事实分层

| 层级 | 内容 |
| --- | --- |
| 工程/官方证据 | §三 条款研究（官方链接可核）、中继可插拔架构代码路径、离线双卖家测试形态、链上部署 tx |
| 用户陈述 | §一 开发过程中使用 Qwen 的亲历描述 |
| 未发生 | §四 全部清单项（真实调用、上架、模型验证、真实 TEE） |
