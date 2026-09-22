# TokenShare P0 构建规格（Coding Agent 执行用）v1.1

> 背景与推理见《TokenShare 原型设计文档 V1.0》（PDF）。本文档是**执行依据**：范围、顺序、验收标准以本文为准；
> 设计动机（为什么不上 TEE、为什么不托管 key 等）有疑问时查 PDF 对应章节，不要自行推翻架构决策。
>
> v1.1 变更：目标链改为双网络——开发迭代用 Base Sepolia，参赛交付部署 Monad testnet（Monad Metropolis
> 黑客松要求作品构建在 Monad 上；Monad 为 EVM 等价链，合约零改动迁移）。新增部署前置 checklist（§9）。

## 1. 项目一句话

闲置大模型 API 额度的按次出租市场：卖方跑一个转发中继（持有自己的 API key），买方以小额 USDC 按次付费，
链上合约负责托管资金与结算，中继凭链上付款凭证放行请求，按官方 usage 分档计价结算。
参赛叙事：为 x402/agentic 支付轨道供给"无需信任批发货源"的 API 市场（卖方额度回血 + 买方低价按次调用）。

## 2. 核心约束（不可违背）

1. **绝不接触卖方 key 之外的任何托管资产**：协议不托管 key、不托管明文、合约不可升级（no proxy）。
2. **中继必须先验证链上付款凭证、再转发请求**；未付款请求零成本拒绝。
3. **key 只存在于卖方中继的环境变量 / 本地密钥文件**，禁止写入合约、禁止上传任何远端。
4. 结算金额以中继上报的 usage 为准（原型阶段接受，链上不验证用量明细）。
5. **链无关代码**：合约与脚本不得出现任何链专属硬编码（USDC 地址、chainId 等一律走配置/环境变量），
   保证 Base Sepolia 与 Monad testnet 共用同一份字节码。

## 3. P0 范围

**做（In scope）：**

| # | 模块 | 内容 |
|---|------|------|
| M1 | Escrow 合约 | deposit / lock(seller, maxAmount, ttl) → paymentId / settle(paymentId, actual) 仅卖方 / refund(paymentId) 期满 / withdraw；view: isValid(paymentId, seller, minAmount)、balances(address) |
| M2 | Registry 合约 | Listing{operator, endpoint, models, priceCachedIn, priceInput, priceOutput, active}；register / updatePrice / deactivate / getListing；事件齐全 |
| M3 | 卖方中继（FastAPI） | OpenAI 兼容 `POST /v1/chat/completions`（支持 stream）；流程：验签 → 读链验 paymentId → 用 `OPENAI_API_KEY` 转发 → 读 usage → 分档计价 → 调 settle → 响应头附 EIP-712 签名收据；另暴露 `GET /health` 与 `GET /receipt/{paymentId}` |
| M4 | 买方 CLI | `deposit` / `lock --seller --max` / `call "prompt..."`（自动 lock→请求→验收据）/ `balance` / `refund` |
| M5 | 端到端集成 + 双链部署 | anvil 本地 fork Base Sepolia → 全链路脚本跑通；随后同一份字节码部署 Monad testnet 并复跑 e2e；README 含两条链的部署与演示命令 |

**不做（Out of scope，明确禁止混入 P0）：** TEE / attestation、心跳与质押（LivenessStaking）、facilitator 批量结算、Reputation 合约、支付通道、前端网页、多卖方聚合路由、缓存命中折扣的真实定价优化（接口留好三档字段，MVP 允许三档同价）。

## 4. 技术栈与网络配置

- 合约：**Foundry**（Solidity ^0.8.24）
- 中继：**Python 3.11+ / FastAPI / web3.py / openai-python**，端口 8787
- 买方 CLI：**Python / Typer / web3.py**
- 本地链：anvil（`anvil --fork-url $BASE_SEPOLIA_RPC`）

**双网络配置（foundry.toml + .env，禁止硬编码进源码）：**

| 网络 | 用途 | chainId | RPC | 结算币 |
|------|------|---------|-----|--------|
| Base Sepolia | 日常开发 / CI | 84532 | `https://sepolia.base.org` | USDC `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` |
| Monad testnet | **参赛交付** | 10143 | `https://testnet-rpc.monad.xyz` | USDC 地址见 §9 checklist 核实结果 |

## 5. 仓库结构（agent 按此创建）

```
tokenshare/
├── BUILD_SPEC.md            # 本文件
├── contracts/               # Foundry
│   ├── foundry.toml         # [rpc_endpoints] base_sepolia / monad_testnet；USDC 地址用 env 注入
│   ├── script/Deploy.s.sol  # 单一部署脚本，--sig run(string) 接收网络名，输出 deployed.json
│   ├── src/Escrow.sol
│   ├── src/Registry.sol
│   └── test/                # 每个函数至少一个 happy path + 一个 revert case（anvil 上跑，不依赖 fork）
├── relay/                   # 卖方中继（FastAPI）
│   ├── app/main.py          # 路由与凭证校验
│   ├── app/pricing.py       # 三档计价（读 Registry 单价）
│   ├── app/receipt.py       # EIP-712 签名收据
│   └── requirements.txt
├── cli/                     # 买方 CLI
│   ├── tokenshare_cli/
│   └── requirements.txt
├── e2e/                     # M5 集成脚本（python）：--network base_sepolia|monad_testnet
└── .env.example             # SELLER_PRIVATE_KEY / OPENAI_API_KEY / BUYER_PRIVATE_KEY / RPC_URL / ESCROW_ADDR / REGISTRY_ADDR / USDC_ADDR / CHAIN_ID
```

## 6. 关键行为规格

### 6.1 Escrow 状态机（M1）
`—deposit→Deposited —lock→Locked —settle(卖方)→Settled`；`Locked —refund(买方,过ttl)→Refunded`；
`Locked —ttl届满(任何人可触发清理)→Refunded`。lock 的 ttl 默认 600 秒。
settle 金额 ≤ lock 的 maxAmount，差额退回买方余额。所有余额变动先记账后转移（checks-effects-interactions），防重入。

### 6.2 中继请求处理（M3）
1. 校验请求头 `X-Payment-Id` + `X-Signature`（签名 = 买方对 `method + path + body哈希 + paymentId` 的 EIP-191 签名）；
2. `Escrow.isValid(paymentId, seller=self, minAmount=本次预估上限)` 为 false → 402 拒绝，**不得转发**；
3. 转发 OpenAI（`stream=true` 时边收边传 SSE）；
4. 从流末 usage 字段取 `prompt_tokens / prompt_tokens_details.cached_tokens / completion_tokens`；
5. `actual = Σ(各档用量 × Registry 单价)`；调 `settle(paymentId, actual)`；
6. 响应头 `X-Receipt: <EIP-712 签名>`，负载含 {paymentId, 三档用量, actualAmount, seller}；
7. 任何一步失败（OpenAI 报错/超时）→ 不 settle，等待买方 refund。

### 6.3 收据验签（M4 必做）
CLI 收到响应后用 Registry 中的卖方地址 ecrecover 验签，失败则打印警告并把 paymentId 标记为争议。

## 7. 验收标准（每里程碑必须通过才能进下一个）

- M1/M2：`forge test` 全绿（anvil）；状态机迁移与 6.1 逐条对应。
- M3：单元测试覆盖「未付款 402」「签名错误 401」「settle 金额=分档计价结果」；`OPENAI_API_KEY` 缺省时中继启动失败而非降级。
- M4：单笔调用命令行可完成 deposit→lock→call→验收据全流程。
- M5：`python e2e/run.py --network base_sepolia` 输出 `E2E PASSED`；
  **`python e2e/run.py --network monad_testnet` 同样输出 `E2E PASSED`**（同一代码、仅配置切换）；
  README 含两链部署命令与演示截图位。

## 8. 工作方式要求

1. 严格按 M1→M5 顺序，**每完成一个里程碑停下汇报**，不要提前做后续模块；
2. 所有金额单位在合约内用 USDC 的 6 位小数原生表示，注释标清；
3. 不改架构决策（需要改时先停下来说明理由并等待确认）；
4. 测试必须真跑，失败就修到绿，不要跳过；
5. 最终交付：`forge test` 全绿 + 两链 e2e 均输出 `E2E PASSED` + README 完整。

## 9. 部署前置 checklist（M5 启动前逐项核实，结果回填本文）

- [ ] **Monad testnet USDC 合约地址**：查 Monad 官方文档 / 水龙头确认原生 USDC 地址；若无官方 USDC，
      用标准 ERC-20 mock（OpenZeppelin）部署到 Monad testnet 并在 .env 记录，README 注明"测试代币"；
- [ ] **MON 测试币水龙头**：确认 `https://testnet-rpc.monad.xyz` 对应的水龙头入口（官方 Discord / 文档），
      部署与 e2e 各需 1–2 MON；
- [ ] **Monad testnet 稳定出块与确认时间**：e2e 脚本轮询 confirmation 的超时按实测调整；
- [ ] **facilitator 确认**：CDP 不支持 Monad → demo 采用买方自付 gas 的逐笔 lock/settle（Monad gas 极低，可接受），
      代码中预留 `FACILITATOR_URL` 环境变量但 P0 不实现；
- [ ] **etherscan 类浏览器**：确认 Monad testnet 区块浏览器地址，把 e2e 输出的 tx hash 拼成可点击链接写进 README。
