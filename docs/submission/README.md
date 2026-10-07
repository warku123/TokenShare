# docs/submission — 黑客松提交文章（Kimi / Qwen）

本目录收录 TokenShare（Monad 黑客松，Track 04 Trust, Identity & AI Infrastructure）提交用的两篇上游生态文章与索引。定位是**真实的工程复盘**：能给出证据的给证据，只有亲历的标明用户陈述，没做完的明说没做完——不做营销话术包装。

| 文章 | 一句话 |
| --- | --- |
| [kimi.md](./kimi.md) | Kimi：开发工具（用户陈述）+ 平台**第一个完成真实调用与链上结算**的官方上游（有收据、有 tx、有逐 token 对账） |
| [qwen.md](./qwen.md) | Qwen：开发工具（用户陈述）+ 架构就绪的第二上游候选，**接入推进中**（用户已裁决跳过本地 mock、直接线上 Monad 测试网第二 seller，保留 Kimi；Token Plan 条款限制如实保留；真实调用/上架未发生） |

## 事实状态总表（截至 2026-10-07）

| 事项 | 状态 | 证据 / 边界 |
| --- | --- | --- |
| Kimi 真实上游调用与结算 | ✅ 已发生 | 2026-09-23 M5b（`api.kimi.com`、`kimi-k2.6`、730 native）与 2026-09-25 M13 Bearer 按量调用（248 native = 88×2+24×3，usage 与链上 `capturedOf` 三方对账）为**本地工程记录**——发生于**历史部署**（M5b=Escrow 首版+Registry v1；M13=Escrow v2+Registry v4），M5b 无公开 tx 存档、不可远端核对；PROOF 区四笔真实 tx（`web/index.html`）属 M13 历史部署。以上均**非现役 v3.2 的证明**；现役 Escrow v3.2 部署 tx 可经 explorer 公开核对 |
| Kimi / Qwen 参与日常开发 | 🔵 用户陈述 | 项目负责人亲历确认；不指认模型版本/任务/次数，不把测试结果归因成模型能力 |
| Qwen 真实上游调用 / 第二卖家 | ❌ 未发生 | Token Plan 条款限交互式工具、禁后端/脚本/共享、转售需书面许可（官方条款事实保留）；用户已裁决**跳过本地 mock、直接线上 Monad 测试网接入第二 seller（保留 Kimi）**；真实调用、上架、供应商许可均未发生，零费用；前提=合规 key+书面许可+预算确认+凭证安全本地输入（聊天暴露凭证需轮换） |
| `qwen3.8-max` 实际使用 | ❌ 未发生 | 官方模型列表在列 ≠ 我们验证过可访问/兼容/使用过；奖项所需使用证据不存在，不伪造 |
| 共享多租户中继源码 | ✅ 已实现 | 52/52 源码路径独立审计闭合（**内部 AI 辅助 + 独立 source-review 流程 PASS**；**未获正式第三方安全审计**；SOURCE AUDIT PASS ≠ DEPLOYMENT READY）；3 项发现修复复审 closed |
| 测试基线 | ✅ 可复跑 | Forge 130 · relay 345 · CLI 151 · custody web 159 · 离线双卖家 E2E 30 通过 1 跳过（mock + Anvil，**非真实双上游生产证明**） |
| 公开镜像 | ✅ 已发布 | `docker.io/warku123/tokenshare-relay:m15-audited-20261007T015955Z`，index `sha256:5a8c33af444a35fad57cc1922f9d0a32f4327611734e4a10709ef12b044c66e0`（[Docker Hub](https://hub.docker.com/r/warku123/tokenshare-relay)，匿名可拉取） |
| Monad Escrow v3.2 | ✅ 已部署 | testnet 10143，`0xe4D5Eb0dBDB6DB8063C07ECF7EDFcCdDB9Ad514c`，tx [`0xc7ca…afce4e`](https://testnet.monadscan.com/tx/0xc7ca3ac798e16ace9f4554df51912aed235753f360d7f0a6417b7b0523afce4e)，block 68854360，2026-10-07；Registry v4 与官方 USDC 不变 |
| 真实 Phala TEE / DCAP / KMS 重启连续性 | ❌ 未部署未验证 | CVM 未创建；结算委托（delegate）已部署但**尚无卖家授权**（为零地址） |
| Chainlink CRE 结算审计工作流 | ❌ 未部署 | 代码与手册就绪，simulate 未闭合 |
| 本地在线 demo 中继（8787） | ⚠️ 旧快照 | 仍为 v3.1 单租户注入，不代表共享/TEE 形态 |

> 阅读原则：两篇文章正文不把上表 ❌ 写成 ✅；所有「计划」均标注为计划。

## 提交检查（docs-only）

- [ ] 两篇文章 + 本索引经独立审查通过后，**仅本目录三个 Markdown** 以 docs-only 方式提交到 GitHub 仓库（`github.com/warku123/TokenShare`）；不携带任何其他改动。
- [ ] **「published article」公开性要求待主办方确认**：公开 GitHub Markdown 是否满足奖项的「已发表文章」定义尚未确认，需用户登录奖项页面核对全文要求；确认前不把本目录标记为满足资格。
- [ ] 视频与仓库要求按既有已核查结论引用（不再另行调研）：官方规则 v3（2026-09-03）明确 **testnet 部署可接受**（§4.1/§9.2），Track 04 交付物为 working product + GitHub 链接 + 约 3 分钟视频 + 文档；提交截止 2026-10-14 03:59 UTC；**提交前应重查**官方政策接口 `hackathon.monad.xyz/api/v1/policies/current`（主办方保留修改权）。
- [ ] **公开 HEAD 与本地工作树的区分**：docs-only 提交时其他 M15 源码/配置改动尚未 commit——本文相对链接基于**本地工作树**，不代表 GitHub 公开 HEAD 已包含 v3.2/M15 相关文件内容；**链上事实（部署 tx 等）可经公开 explorer 独立核查**，本地工程记录与远端已同步内容需按此区分引用。
- [ ] 敏感面检查通过：三份文件不含任何 API key、私钥或凭证值；曾在聊天中暴露过的凭证均已提示轮换。

## 已知缺口（诚实清单）

1. **开发使用证据**：Kimi/Qwen 参与开发目前只有用户陈述层，无对话记录级证据可公开（聊天含敏感内容，不适合作为提交物）。
2. **公开文章资格**：GitHub Markdown 是否满足「published article」待主办方/奖项全文确认（见上）。
3. **真实 TEE**：共享中继的硬件保障链路（Phala TDX、DCAP 验证、KMS 重启连续性）未部署、未验证——在完成前，任何「硬件级保障」「生产可用」表述都不成立，文章也未使用。
4. **Qwen 真实链路**：用户已裁决直接线上接入第二 seller（不走本地 mock）；真实证据仍需供应商侧解锁（后端按量 key + 第三方使用书面许可）、实际调用预算确认与安全凭证输入后才可产生。
5. **双上游生产证明**：现有双卖家 E2E 是 mock 上游 + 本地 Anvil，不等于真实双上游在线证明。

## 免责

TokenShare 为黑客松原型：仅部署于 testnet、处理测试币、未审计、不处理真实资金。测试币不豁免上游 API 与云基础设施的真实费用。项目与 Kimi/Moonshot、阿里云/Qwen 无任何官方合作或分销授权关系；相关条款边界见两篇文章的合规章节。
