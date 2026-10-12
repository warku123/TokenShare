# Qwen（通义千问）：TokenShare 的第二位真实卖家（新钱包重新上架；两笔买家支付已在链上结算）

> 本文是 TokenShare（Monad 黑客松项目）面向 Qwen / 阿里云团队及相关读者的介绍文章，与 [kimi.md](./kimi.md) 配对阅读。文中严格区分三类内容：**有证据的事实（含其证据边界）**、**只有项目负责人亲历的「用户陈述」**、以及**明确的边界**。本文不含任何密钥或凭证值，也不做任何未经记录支撑的断言。

## 摘要：Qwen 在 TokenShare 里的两个角色

1. **开发工具**（用户陈述）：据项目负责人确认，Qwen 系列模型作为交互式编程助手参与了本项目的日常开发。
2. **平台第二位真实卖家**（有记录证据，证据边界见 §三）：Qwen 已完成接入——`qwen3.8-max` 有记录在案的真实 API 调用与部分链上结算，Qwen 卖家的 key 托管在共享 TEE 中继、listing 曾上架市场页、并已授权结算委托。原 listing 已于 **2026-10-09 由卖家主动下架**（Registry.deactivate，作为历史记录保留在 §一），目的是停止上游额度消耗；此后该 listing 已由**新卖家钱包重新创建**（Registry active=True），Qwen 并已产生**真实买家支付与链上结算**：两笔 payment 各 Settled、captured 10.000000 USDC，另有一笔已退款，2026-10-12 按 payment ID 从 Escrow v3.2 实时读证（见 §一/§三）。重上架的逐笔交易哈希未在追踪仓库归档——本文按 payment ID 与链上实时状态引用，不编造任何数字，也不借用 Kimi 的交易来充数。

## 一、状态速览（2026-10-09 旧钱包下架 + 新钱包重新上架）

- **链上**：Monad 测试网（chain 10143），Escrow v3.2 + Registry v4 + 官方 Circle USDC（6 位小数），与 Kimi 篇同一套现役部署（[contracts/deployed.monad.json](../../contracts/deployed.monad.json)）。
- **中继**：共享多卖家中继运行在 Phala TDX 可信执行环境（Phala Cloud CVM）中，处于生产站配置的活跃状态；**卖家不需要自建任何 TEE**。
- **Qwen 侧（旧钱包下架记录 + 新钱包重新上架）**：演示期间市场页曾展示「一个卖 Qwen 3.8 Max」的真实卖家卡片（录制当时的视频脚本——本地维护、不在仓库，当时与 Kimi 并列）；keystore 中持有两把卖家 key（Kimi 与 Qwen），两个 listing 的端点都指向这台 TEE 中继，且两位卖家都曾授权结算委托（[web/policy.html](../../web/policy.html)）。**下架记录（历史）**：应卖家要求停止上游额度消耗，旧卖家钱包 `0x1F1A…a687` 已于 **2026-10-09** 通过 Registry.deactivate 下架（tx [`0x6a91bb20…d7131f1b`](https://testnet.monadvision.com/tx/0x6a91bb203751aad1f29003b046dfb56965d901e08d372a4b5070fa9bd7131f1b)，block 69498784，Monad 测试网 10143）。**重新上架（现状）**：新卖家钱包 `0xD6172dC960D1533e49A367253FF9Db01AE83Ba39` 在 Registry 上 active=True，listing 端点指向与 [web/config.js](../../web/config.js) `relayOrigin` 相同的共享 Phala TEE 中继（relay `/info`：`mode=shared`、`tee.enabled=true`、keystoreEntries=3）；发布模型为 `qwen3.8-max`（缓存输入 2 / 输入 4 / 输出 36 USDC 每百万 token）与 `qwen3.8-flash`（1 / 2 / 3），且 Escrow v3.2 对新钱包的 `settleDelegateOf` 返回与 Kimi 卖家相同的委托地址（`0x49CA…fb2D`，共享 TEE 中继的 pinned signer）。市场页实时读取 Registry 并隐藏非活跃 listing（过滤逻辑见 commit 73bf921）：**当前展示两个活跃 listing——Kimi 与重新上架的 Qwen**，旧钱包已下架的 listing 被隐藏。listing 由卖家自主控制、随时可变。
- 说明：以上「已下架 / 重新上架 / 活跃」均以所引用的链上记录为准（2026-10-09 的下架 tx 与 2026-10-12 的 Registry/Escrow 实时读取）；Qwen listing 的当前状态以 Registry 实时数据为准，本文除所引读证外不对线上服务做实时可用性检查。

**为什么是 Monad**：TokenShare 的按调用记账依赖一个面向低成本、高吞吐、快确认交易的 EVM 环境；Monad 测试网的低费用设计有助于让单次调用级别的结算开销保持在实际可行的范围。这是设计适配性的定位陈述，**不是实测结论**：上游模型用量与链上 gas 是两类互相独立的真实成本，本项目未发布过任何「gas 占单笔比例」的基准测量；Qwen 专属的逐笔调用收据金额仍未归档（见 §三），本文引用的支付级 captured 金额是链上读到的 payment 预算数，不是逐笔调用收据数字。低费用是链环境特性带来的帮助，不构成对成本或收益的证明，也不代表任何厂商授权。

## 二、架构里 Qwen 处在哪一环

TokenShare 的中继从第一天起按「多厂商可插拔」设计，Qwen 的接入复用了与 Kimi 完全相同的动线：

- **核心流程一致**：upstream key 应用层加密托管（可先于上架）→ VERIFY catalog（真实拉取模型列表交叉核验）→ 选模型、发布每模型三档价（缓存输入/输入/输出，每百万 token）→ 显式授权结算委托 → 买家按 token 计量扣款。
- **OpenAI 兼容面匹配**：阿里云百炼提供 OpenAI 兼容模式（base URL 说明见[官方文档](https://help.aliyun.com/zh/model-studio/base-url)），平台的每模型三档价、链上取价与 USDC 6 位小数按量结算管线对 OpenAI 兼容上游成立。
- **路由与隔离**：请求按 payment 上登记的 seller 路由到对应卖家的 key，绝不发往任意钱包；host 白名单与模型前缀→厂商映射是代码内策略（[relay/app/config.py](../../relay/app/config.py)）。

## 三、证据现状：能证明什么，还缺什么

**有记录支撑的（提交材料明确记录）**：

- `qwen3.8-max` 的**真实 API 调用**与**部分链上结算**已被记录为演示事实（[docs/SUBMISSION.md](../SUBMISSION.md)「Demo status」段）。
- Qwen 卖家的 key 在共享 TEE 中继的加密 keystore 中，listing 曾在市场页上线，结算委托已授权（[web/policy.html](../../web/policy.html)）；旧钱包 listing 后于 2026-10-09 下架（见 §一），此处的演示记录作为历史事实保持不变。
- **重新上架钱包的真实买家支付（Escrow 实时读证，2026-10-12）**：实时读取 Escrow v3.2 `getPayment`——payment 6（预算 5 USDC）为 **Refunded**、captured 0；payment 7（预算 10 USDC）为 **Settled**、**captured 10.000000 USDC**；payment 8（预算 10 USDC）为 **Settled**、**captured 10.000000 USDC**（nextPaymentId = 9；买家 `0xA0ff…EFeF7`）。这些是 payment ID 级的链上状态，可对照链上合约独立核查。

**诚实声明的证据缺口**：

- 重上架部分，追踪仓库内没有 Qwen 专属的逐笔交易哈希、逐笔调用收据金额或调用次数可归档。因此本文按 **payment ID + 链上实时状态**（上文）引用重上架，而不是逐笔 explorer 链接；payment ID 及其 Settled/Refunded 状态是链上事实，但逐笔交易哈希清单尚未整理归档。新卖家钱包与「Qwen」的对应关系基于链上读证：该钱包 Registry active、其结算委托地址等于共享 TEE 中继的 pinned signer、其发布模型为 Qwen 前缀——本文不在此之外做更多归因。
- 换句话说：**代码与文档层面的 Qwen 接入是完整的、调用是真实发生过的**；但「逐笔、可独立点开验证的 Qwen explorer 证据」尚未整理归档。这是后续补齐的工作项，不是本文用别家数据填补的空缺。

## 四、条款边界（2026-10-07 的历史研究，非重新核实）

2026-10-07，我们对 Qwen Token Plan 条款做过一次只读研究，结论作为**历史研究记录**保留（本文不对其做实时复核）：

- Token Plan 个人版/团队版端点按当时查阅的官方说明**仅限交互式工具/智能体场景**，官方文档写明不适用于应用后端、自动脚本及共享（[个人版说明](https://platform.qianwenai.com/docs/token-plan/personal/token-plan-personal-overview)、[团队版说明](https://platform.qianwenai.com/docs/token-plan/team/token-plan-team-overview)）。
- 平台服务条款对未经许可的转售/分许可有明确限制（[服务条款](https://terms.alicdn.com/legal-agreement/terms/common_platform_service/20260423145804812/20260423145804812.html) §7.1.12 及 §1.4）。

**必须区分清楚的三件事**：

1. **演示事实**：仓库记录了真实发生过的 qwen3.8-max 调用与部分结算。这是演示层面的既成事实记录。
2. **它不等于许可**：演示中发生过调用，**不意味着**演示使用获得了供应商许可；项目负责人的风险知悉与自担也**不构成、不能替代**厂商授权。
3. **生产/商用门槛更高**：上述条款关注点在「生产或商用分销」层面依然成立——把 Qwen 额度通过 TokenShare 面向第三方做规模化售卖，前提是取得适合后端按量计费的 key 与供应商侧的明确许可。

## 五、新定位：官方也可以使用的计量售卖渠道（潜力，非现状）

今天 TokenShare 的卖家是「有闲置额度的个人/团队」。但协议本身的能力边界更宽，项目负责人的一句话定位是：

> **“他们也可以承接官方的API的售卖，多一个相应的售卖渠道。”**

也就是说，同一套基础设施不仅适合盘活个人闲置订阅，也同样**在技术上支持官方厂商自身、或获得正式授权的分销商/供应商**把它作为一条按量售卖渠道使用：每模型价目直接发布在链上 Registry，买家预算由托管合约硬性约束，每次调用产生可验证签名的计费收据，共享 TEE 网关替渠道方承担密钥托管与路由——开发者与 agent 生态可以按 token 直接接入，而渠道方无需自建任何计量与结算设施。

**必须划清的边界**：这是技术能力与定位描述，**不是**任何实际存在的厂商合作、授权、签约或收入。本文不声称 Qwen/阿里云是 TokenShare 的官方伙伴，也没有任何来自厂商的书面授权或商业安排。

## 六、边界与如实声明

- **收据 ≠ token 真相**：收据由中继签发，是账单陈述的签名认证，不是服务质量的证明，也不是真实 token 消耗的独立证据。
- **价目 ≠ 官方价**：链上三档价是卖家自报的，不必然等于上游官方价目；Qwen 模型的 usage 字段兼容性在接入时已按实际调用验证，但发布价与官方价的比对未做。
- **TEE 降低而非消除风险**：共享 TEE 中继把密钥约束进 enclave，但平台运营方与治理仍是信任面，这不是零托管、也不是 trustless。
- **测试网代币没有价值**：testnet USDC/MON 无真实价值；但上游套餐额度与云基础设施仍是**真实成本**。
- **退款受合约约束**：退款受支付状态、结算与 TTL 规则限制，不是随时即时可取；卖家授权的结算委托在 TTL 后仍可结算既有锁定（资金只会进入卖家账本）。
- **吊销是显式动作**：吊销买家的 API key 是一项显式操作，断开钱包连接不等于吊销已发出的 key。
- **合规责任在卖家**：上游卖家自行对其账号遵守厂商条款负责；本项目不构成分销授权，免责声明不能替代授权。

## 七、核对路径

- 链上部署事实（与 Kimi 篇共享）：地址、部署时间与网络见 [`contracts/deployed.monad.json`](../../contracts/deployed.monad.json)（该文件不含版本号与部署 tx）；Escrow v3.2 部署 tx [`0xc7ca3ac7…523afce4e`](https://testnet.monadvision.com/tx/0xc7ca3ac798e16ace9f4554df51912aed235753f360d7f0a6417b7b0523afce4e) 见根目录 [README.md](../../README.md)「Networks」段。
- Qwen 演示记录（真实调用与部分结算）：[`docs/SUBMISSION.md`](../SUBMISSION.md)「Demo status」段。
- 历史双卖家展示（录制当时口径；市场现展示两个活跃 listing——Kimi 与重新上架的 Qwen）：视频脚本（本地维护、不在仓库）、[`web/index.html`](../../web/index.html)；密钥托管与委托授权记录：[`web/policy.html`](../../web/policy.html)、[`web/config.js`](../../web/config.js)。
- 中继可插拔架构与白名单：[`relay/app/config.py`](../../relay/app/config.py)、部署与镜像：本地维护、不在仓库。
- Qwen 官方来源（2026-10-07 研究时的链接）：[Token Plan 个人版](https://platform.qianwenai.com/docs/token-plan/personal/token-plan-personal-overview) · [Token Plan 团队版](https://platform.qianwenai.com/docs/token-plan/team/token-plan-team-overview) · [百炼 base URL](https://help.aliyun.com/zh/model-studio/base-url) · [服务条款](https://terms.alicdn.com/legal-agreement/terms/common_platform_service/20260423145804812/20260423145804812.html)。
- 索引：[README.md](./README.md)（仅作目录导航；个别状态描述可能滞后于本文，以本文引用的一手证据为准）。

## 事实分层

| 层级 | 内容 |
| --- | --- |
| 记录证据 | §三 Qwen3.8-max 真实调用与部分结算的文档记录、key 托管/委托授权、历史双卖家展示；§一 2026-10-09 旧钱包下架（Registry.deactivate，block 69498784）；§三 新钱包重新上架（Registry active=True）及按 payment ID 实时读证的买家支付（2026-10-12）——payment 7 与 payment 8 各 Settled、captured 10.000000 USDC，payment 6 Refunded（captured 0） |
| 证据缺口 | §三 重上架的逐笔交易哈希 / 逐笔调用收据金额 / 调用次数未归档，改按 payment ID + 链上实时状态引用，不引用逐笔调用金额 |
| 历史研究 | §四 2026-10-07 条款只读研究（未重新核实，引用保留） |
| 用户陈述 | §摘要/§一 开发过程中使用 Qwen 的亲历描述 |
| 潜力（非事实） | §五 官方/授权渠道作为售卖渠道的技术支持，不代表任何合作或授权 |
| 边界 | §六 全部条目；§一「为什么是 Monad」的定位边界（非实测、不构成成本/收益证明、不代表厂商授权） |
