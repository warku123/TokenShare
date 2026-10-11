# docs/submission — 黑客松提交文章（Kimi / Qwen）

本目录收录 TokenShare（Monad 黑客松，Track 04 Trust, Identity & AI Infrastructure）提交用的两篇上游生态文章与索引。定位是**真实的工程复盘**：能给出证据的给证据，只有亲历的标明用户陈述，没做完的明说没做完——不做营销话术包装。

| 文章 | 一句话 |
| --- | --- |
| [kimi.md](./kimi.md)（英文版） | Kimi：开发工具（用户陈述）+ 平台**第一个完成真实调用与链上结算**的官方上游（有收据、有 tx、有逐 token 对账） |
| [qwen.md](./qwen.md)（英文版） | Qwen：开发工具（用户陈述）+ 平台第二位真实上游（有记录的真实调用与部分链上结算）；演示完成后其 listing 已于 2026-10-09 由卖家主动下架，当前活跃 listing 仅剩 Kimi；逐笔 tx/次数/金额未存档，本文不引用、不编造数字；Token Plan 条款限制如实保留 |
| [kimi_ch.md](./kimi_ch.md) | kimi.md 的中文版（与英文版逐段对应） |
| [qwen_ch.md](./qwen_ch.md) | qwen.md 的中文版（与英文版逐段对应） |

## 事实状态总表（截至 2026-10-11）

| 事项 | 状态 | 证据 / 边界 |
| --- | --- | --- |
| Kimi 真实上游调用与结算 | ✅ 已发生 | 最新完整闭环：payment 5（`api.kimi.com` / `k3-256k`）真实调用，按量结算 **918 micro-USDC**（0.000918）、退款 1,499,082，结算 tx [`0x04249c26…c07d1785`](https://testnet.monadvision.com/tx/0x04249c263cdc64661141bc3a0911ddec2b2c05e57f79d21b5fd0f682c07d1785)（block 69225090），公开可查；逐 token 对账与链上锚定的结算对账演示见 [kimi.md](./kimi.md) §三/§四。更早的 M5b / M13 记录（730 / 248 native）属历史部署的本地工程记录，M5b 无公开 tx 存档；PROOF 区四笔真实 tx（`web/index.html`）属 M13 历史部署 |
| Kimi / Qwen 参与日常开发 | 🔵 用户陈述 | 项目负责人亲历确认；不指认模型版本/任务/次数，不把测试结果归因成模型能力 |
| Qwen 真实上游调用 / 第二卖家 | ✅ 已发生（历史集成，边界如下） | `qwen3.8-max` 有记录在案的真实 API 调用与**部分链上结算**（[qwen.md](./qwen.md)）；演示完成后该 listing 已于 **2026-10-09 由卖家主动下架**（Registry.deactivate，tx [`0x6a91bb20…d7131f1b`](https://testnet.monadvision.com/tx/0x6a91bb203751aad1f29003b046dfb56965d901e08d372a4b5070fa9bd7131f1b)，block 69498784），当前活跃 listing 仅剩 Kimi。**逐笔 tx / payment ID / 调用次数 / 具体金额未存档**——不引用、不编造任何这类数字；Token Plan 条款限制（限交互式工具、转售需书面许可）如实保留（[qwen.md](./qwen.md) 合规章节） |
| `qwen3.8-max` 实际使用 | ✅ 已发生（演示层面） | 有记录在案的真实调用与部分链上结算（[qwen.md](./qwen.md)、[docs/SUBMISSION.md](../SUBMISSION.md)「Demo status」段）；逐笔数字未存档为已知边界，不做超出记录的归因 |
| 共享多租户中继源码 | ✅ 已实现 | 52/52 源码路径独立审计闭合（**内部 AI 辅助 + 独立 source-review 流程 PASS**；**未获正式第三方安全审计**；SOURCE AUDIT PASS ≠ DEPLOYMENT READY）；3 项发现修复复审 closed |
| 测试基线 | ✅ 可复跑 | Forge 130 · relay 345 · CLI 151 · custody web 159 · 离线双卖家 E2E 30 通过 1 跳过（mock + Anvil，**非真实双上游生产证明**） |
| 公开镜像 | ✅ 已发布 | `docker.io/warku123/tokenshare-relay:m15-audited-20261007T015955Z`，index `sha256:5a8c33af444a35fad57cc1922f9d0a32f4327611734e4a10709ef12b044c66e0`（[Docker Hub](https://hub.docker.com/r/warku123/tokenshare-relay)，匿名可拉取） |
| Monad Escrow v3.2 | ✅ 已部署 | testnet 10143，`0xe4D5Eb0dBDB6DB8063C07ECF7EDFcCdDB9Ad514c`，tx [`0xc7ca…afce4e`](https://testnet.monadscan.com/tx/0xc7ca3ac798e16ace9f4554df51912aed235753f360d7f0a6417b7b0523afce4e)，block 68854360，2026-10-07；Registry v4 与官方 USDC 不变 |
| 真实 Phala TEE 部署（M15 共享多卖家中继） | ✅ 已部署 | 共享多卖家中继运行于 Phala TDX CVM（Phala Cloud，生产站配置）：`https://c30652f4833465adda9bdc7ac557e8b45e8e66c6-8787.dstack-pha-prod5.phala.network`，pinned signer `0x49CA3eADB3b8F23623f735918A6F2b945F60fb2D`（部署细节：[web/config.js](../../web/config.js) m15 relayOrigin + signer pins、[cre/README.md](../../cre/README.md)、[docs/CRE_DEMO_EVIDENCE.md](../CRE_DEMO_EVIDENCE.md)；relay/README-docker.md 的状态横幅早于 10-07 CVM 部署，不作引用）。卖家无需自建 TEE，应用层加密托管 upstream key；**未获正式第三方安全审计**（源码审计 PASS ≠ 部署就绪） |
| 卖家授权结算委托（Escrow v3.2 `settleDelegateOf`） | ✅ 两位卖家均已授权（历史） | Kimi 与 Qwen 两位卖家都曾完成 settle delegate 授权（[web/policy.html](../../web/policy.html)、[qwen.md](./qwen.md)）；授权非零地址、长期有效，listing 状态由卖家自主控制（当前活跃 listing 仅剩 Kimi） |
| Chainlink CRE 结算审计工作流 | ⚠️ 本地演示已闭环，非生产 DON | 本地 CLI 模拟（dry-run + broadcast）在结算块重算账单公式、验证中继 EIP-712 收据签名（918 = 918 Match），报告经**免许可的测试网 mock forwarder** 锚定到演示 ReceiptAnchor 实例：report tx [`0x9125c198…53ea97330`](https://testnet.monadvision.com/tx/0x9125c1983a1f50402f883521bcfd6b1f848051560a6f98c69fe13ce53ea97330)，`isVerified(5)=true`（[docs/CRE_DEMO_EVIDENCE.md](../CRE_DEMO_EVIDENCE.md)）。**非 Chainlink 生产 DON 运行**，不得表述为生产级 CRE 集成 |
| 本地在线 demo 中继（本机 8787） | ⚠️ 旧快照 | 仅指本地机器上的演示实例，仍为 v3.1 单租户注入；**不代表**已部署的共享/TEE 形态（见上行 Phala TDX 部署） |

> 阅读原则：两篇文章正文不把上表的 ⚠️ 限制与「边界」栏写成 ✅；所有「计划」均标注为计划。

## 提交检查（docs-only）

- [ ] 两篇文章 + 本索引经独立审查通过后，**仅本目录三个 Markdown** 以 docs-only 方式提交到 GitHub 仓库（`github.com/warku123/TokenShare`）；不携带任何其他改动。
- [ ] **「published article」公开性要求待主办方确认**：公开 GitHub Markdown 是否满足奖项的「已发表文章」定义尚未确认，需用户登录奖项页面核对全文要求；确认前不把本目录标记为满足资格。
- [ ] 视频与仓库要求按既有已核查结论引用（不再另行调研）：官方规则 v3（2026-09-03）明确 **testnet 部署可接受**（§4.1/§9.2），Track 04 交付物为 working product + GitHub 链接 + 约 3 分钟视频 + 文档；提交截止 2026-10-14 03:59 UTC；**提交前应重查**官方政策接口 `hackathon.monad.xyz/api/v1/policies/current`（主办方保留修改权）。
- [ ] **公开 HEAD 与本地工作树的区分**：docs-only 提交时其他 M15 源码/配置改动尚未 commit——本文相对链接基于**本地工作树**，不代表 GitHub 公开 HEAD 已包含 v3.2/M15 相关文件内容；**链上事实（部署 tx 等）可经公开 explorer 独立核查**，本地工程记录与远端已同步内容需按此区分引用。
- [ ] 敏感面检查通过：三份文件不含任何 API key、私钥或凭证值；曾在聊天中暴露过的凭证均已提示轮换。

## 已知缺口（诚实清单）

1. **开发使用证据**：Kimi/Qwen 参与开发目前只有用户陈述层，无对话记录级证据可公开（聊天含敏感内容，不适合作为提交物）。
2. **公开文章资格**：GitHub Markdown 是否满足「published article」待主办方/奖项全文确认（见上）。
3. **第三方安全审计**：共享中继已部署于 Phala TDX CVM（见上表），但未获正式第三方安全审计；部署不等于审计通过，任何「硬件级保障 = 安全保证」的表述都不成立，文章也未使用。
4. **Qwen 证据边界**：`qwen3.8-max` 的真实调用与部分链上结算已记录为演示事实，但**逐笔交易哈希 / payment ID / 调用次数 / 具体金额未存档**（不编造数字）；该 listing 已于 2026-10-09 由卖家下架；Token Plan 条款边界如实保留（[qwen.md](./qwen.md)）。
5. **双上游存档不对称**：离线双卖家 E2E 仍是 mock 上游 + 本地 Anvil；真实双上游调用均已发生，但仅 Kimi 侧有完整公开链上闭环（结算 tx + 对账），Qwen 侧为部分链上结算、无逐笔存档——两篇正文按此不对称如实表述。

## 免责

TokenShare 为黑客松原型：仅部署于 testnet、处理测试币、未审计、不处理真实资金。测试币不豁免上游 API 与云基础设施的真实费用。项目与 Kimi/Moonshot、阿里云/Qwen 无任何官方合作或分销授权关系；相关条款边界见两篇文章的合规章节。
