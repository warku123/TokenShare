# TokenShare Web — 完整功能测试清单

Monad testnet (chainId 10143) · Escrow/Registry/USDC 已部署（见 `web/config.js`)。
纯静态页：本地 `python3 -m http.server -d web` → http://localhost:8000（控制台 `/console.html`）。

## 前置条件

1. **MetaMask** 已装。打开 console 页点 `[ CONNECT WALLET]`；若钱包不在 Monad testnet，页面自动 `wallet_addEthereumChain` 引导（chainId `0x279f` = 10143，RPC `https://testnet-rpc.monad.xyz`）。手动切换后页面自动重载。
2. **测试币**（两个账号各一份，卖方+买方）：
   - MON gas：https://faucet.monad.xyz
   - USDC：https://faucet.circle.com （选 Monad testnet）
3. **卖方 relay** 跑在公网可达地址且 **CORS 已开**（`Access-Control-Allow-Origin` 覆盖页面源；响应头需 `Access-Control-Expose-Headers: X-Settle-Status, X-Receipt`），否则浏览器调不通 `/health`、`/verify-upstream`、`/v1/chat/completions`。
4. 卖家地址已列入 `web/config.js` 的 `sellers`（市场页/买家下拉的数据源）。

## 市场页（只读，无需钱包）

- [ ] listings 卡渲染：operator（截断，点击=复制地址）、endpoint 域名、models 标签、三档价（USDC/1M + 原生 6dp 整数）、ACTIVE 徽章、explorer 链接
- [ ] relay 健康点：relay 在线=绿点+延迟 ms；离线/CORS 未开=灰点（hover 有说明）
- [ ] relay `/info` 在线时：TEE 模式显紫色 `TEE` 徽标（点击开 `/attestation` quote JSON)；meta 行显 upstream host + official/CUSTOM；relay 不可达时全部静默不显示
- [ ] 统计条 LISTINGS/ACTIVE/RELAYS ONLINE 数字一致；`[ REFRESH ]` 与 30s 自刷生效

## Seller tab

- [ ] 连接卖家钱包 → MY LISTING 显示链上 listing（未登记→空态引导）；已登记且 relay 在线时追加 TEE/UPSTREAM 两行
- [ ] 表单校验：endpoint 空 / models 零选 / 价格非数或 >6 位小数 → 红条拒绝，不发交易
- [ ] 预览框实时反映将发的交易：`register(...)` / `updatePrice(...)` / `deactivate()+register` 两笔 / `deactivate()`
- [ ] UPSTREAM PRECHECK：填 relay base URL → RUN → 渲染 key_valid、upstream_host、accessible_models、listed_models、mismatches（逐条 model+原因）、表单勾选 vs 可服务集差异；relay 不可达→RETRY 提示
- [ ] 提交：MetaMask 弹窗 → tx 行 pending(amber)→hash（可点 explorer)→confirmed（绿）/reverted（红）；成功后 MY LISTING 刷新
- [ ] 按钮防重：交易 pending 期间按钮 disabled

## Buyer tab

- [ ] 余额卡：钱包 USDC + Escrow `balances(me)`；WITHDRAW 输入金额→tx 流→余额刷新
- [ ] DEPOSIT： allowance 不足时 `approve → deposit` 两笔步进；充足时 approve 自动跳过
- [ ] LOCK：选卖家（下拉=市场同源数据）→ 显示三档价 + minAmount 估计（in×200k+out×32k caps)；低于估计给黄色提醒但仍可发；成功→大字 paymentId（点击复制）+ 自动带入 CALL DEMO
- [ ] CALL DEMO:model 下拉=所选卖家 listing models；prompt → `[ SIGN + CALL ]`
  - 终端 trace:EIP-191 msg 明文（`POST|/v1/chat/completions|<64hex>|<paymentId>`)→ MetaMask personal_sign → POST
  - 200：回复文本 + usage 三档 + X-Settle-Status；收据面板绿「✓ 收据验签通过」+ actualAmount/upstreamHost/model/tokens
  - 验签失败：红条 + 自动记入 DISPUTES(localStorage)
  - 402 → 提示「maxAmount 低于卖家 minAmount 估计，调高金额」;401/409/400/502 各有对应文案；relay 不可达→CORS 提示
- [ ] REFUND：输入 paymentId（datalist 含本会话锁单）→ 先 getPayment 显示 state/max/expires，非 Locked 拒绝；Locked 且过期→refund tx→余额刷新
- [ ] DISPUTES：列表显示 paymentId/原因/时间；连接钱包后按当前地址过滤；`[ CLEAR ]` 清空

## 网络/断线走查

- [ ] 钱包切到非 10143：net 徽章变红 + 琥珀色 banner + `[ SWITCH / ADD NETWORK ]` 按钮（徽章本体也可点）
- [ ] 无 MetaMask：connect 按钮变为 `NO WALLET — INSTALL METAMASK`，页面其余只读可用
- [ ] RPC 不可达：市场页显示琥珀色「RPC 不可达」提示而非崩溃；console 静默兜底
- [ ] 未装钱包时刷新页面无 console 报错；已授权钱包刷新后静默恢复连接（不弹窗）
