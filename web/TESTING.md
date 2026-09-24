# TokenShare Web — 完整功能测试清单

Monad testnet (chainId 10143) · Escrow/Registry/USDC 已部署（见 `web/config.js`)。
纯静态页：本地 `python3 -m http.server 8080 -d web` → http://localhost:8080（控制台 `/console.html`）。若 8080 被占换任意空闲端口。

## 前置条件

1. **MetaMask** 已装。打开 console 页点 `[ CONNECT WALLET]`；若钱包不在 Monad testnet，页面自动 `wallet_addEthereumChain` 引导（chainId `0x279f` = 10143，RPC `https://testnet-rpc.monad.xyz`）。手动切换后页面自动重载。
2. **测试币**（两个账号各一份，卖方+买方）：
   - MON gas：https://faucet.monad.xyz
   - USDC：https://faucet.circle.com （选 Monad testnet）
3. **卖方 relay** 跑在公网可达地址且 **CORS 已开**（`Access-Control-Allow-Origin` 覆盖页面源；响应头需 `Access-Control-Expose-Headers: X-Settle-Status, X-Receipt`），否则浏览器调不通 `/health`、`/verify-upstream`、`/v1/chat/completions`。
4. 卖家地址已列入 `web/config.js` 的 `sellers`（市场页/买家下拉的数据源）。

## 市场页（只读，无需钱包）

> M9 起首页 `index.html` 仅保留 ≤2 卡预览 + FULL MARKET 入口面板；完整市场在 `market.html`（导航三页互链）。

- [ ] **首页预览**：市场区数据源=Registry 枚举（sellerCount+getSellers）→ getListing 富化，渲染前 2 张卡（v2 卡同规格）；RPC 失败降级 config.js sellers；统计条 LISTINGS/ACTIVE/RELAYS ONLINE 按全量枚举计数（非 2 卡截断）；入口面板无 JS 也可见，点击进 `market.html`
- [ ] listings 卡渲染：operator（截断，点击=复制地址）、endpoint 域名、models 标签、ACTIVE 徽章、explorer 链接
- [ ] **模型选择器（v2）**：模型 chips 可点（button + aria-pressed），默认选中首模型并高亮；点选后三档价格框（CACHED IN $ · INPUT $ · OUTPUT $ + 原生 6dp units 行）切换为该模型价；30s 自刷/手动 REFRESH 后选中态保留；键盘 Tab 聚焦 + Enter 可切
- [ ] 每模型独立价：同一 listing 多模型不同价时逐 chip 核对与链上 `getPrice(operator, model)` 一致（数据源 = getListing 的 prices[] 平行数组）
- [ ] 空模型 listing：价格区显示空态虚线框（no models listed），无三档框；chips 区显 `—`
- [ ] 窄视口（≤360px 卡宽）：三档框等宽不破，`$3.000000` 与 units 行不溢出、不贴右框线；chips 换行均衡（每行拉满，无参差孤行）
- [ ] relay 健康点：relay 在线=绿点+延迟 ms；离线/CORS 未开=灰点（hover 有说明）
- [ ] relay `/info` 在线时：TEE 模式显紫色 `TEE` 徽标（点击开 `/attestation` quote JSON)；meta 行显 upstream host + official/CUSTOM；relay 不可达时全部静默不显示
- [ ] 统计条 LISTINGS/ACTIVE/RELAYS ONLINE 数字一致；`[ REFRESH ]` 与 30s 自刷生效

## 市场子页 market.html（Registry v3 链上枚举 + 分页/筛选/搜索/排序）

- [ ] **枚举发现（M10）**：数据源=`Registry.sellerCount()` + `getSellers()` 分页 getter（每页 100，>100 卖家多次调用拼接）→ 逐 seller `getListing` 富化；不依赖 config sellers；active/价格以 getListing 为准（deactivate→re-register 不重复收录）
- [ ] **秒开节奏**：无扫描/进度条；首屏 6 张 skeleton 脉冲卡占位，枚举 eth_calls 返回后立即换真卡；计数（f-count）即时；来源行「N sellers · read directly from the Registry on-chain · prices live via getListing」（技术细节 hover tooltip）
- [ ] **REFRESH**：重新走 sellerCount+getSellers+getListing 全量（无缓存语义），按钮 pending 期间禁用
- [ ] **降级**：RPC 不可达或 Registry 无枚举函数（旧 v2 合约）→ 来源行红字「couldn't read the Registry — showing N sellers from config.js」+ tooltip 原因，列表降级 config sellers；页面不崩
- [ ] **分页**：>12 listing 时 12/页，PREV/NEXT 边界禁用，SHOWING a–b OF n · PAGE x/y 与实际一致；换页仅探测当前页卡片
- [ ] **模型筛选**：filter chips = 已加载 listings 的 models 并集（含计数徽标），ALL 默认选中；点选过滤列表并重置到第 1 页；筛选后无结果给 RESET 提示
- [ ] **搜索**：operator 地址子串（大小写不敏感）或模型名子串命中；无命中空态+RESET；输入 200ms 防抖；搜索框/排序下拉为暗色主题控件（mono 字体、`--bg-2` 底、细边框、绿焦点环、无原生圆角/取消按钮），与 console 表单一致
- [ ] **排序**：NEWEST REGISTERED（默认，按事件块高）· 首模型 INPUT 价 ↑/↓（无价 listing 排尾/首）· OPERATOR A→Z/Z→A · 模型数 ↑/↓，切换后立即重排
- [ ] 卡片行为同首页：模型 chip 选择器/每模型价/健康点/TEE 徽标/复制地址全部生效
- [ ] `config.js` 键核对：M10 后**无** `registryFromBlock`/`scanDepthBlocks`（扫描下线已删）；既有键（escrowAddr/registryAddr/usdcAddr/sellers 等）未被改动；v3 部署后 orchestrator 刷新 `registryAddr` 即生效

## Seller tab

- [ ] 连接卖家钱包 → MY LISTING 显示链上 listing（未登记→空态引导）；PRICES /1M 按模型分组逐行（model + cached/in/out）；已登记且 relay 在线时追加 TEE/UPSTREAM 两行
- [ ] MODELS 硬化：无自由文本输入；`[ LOAD FROM RELAY ]` 预检 `verify-upstream` → `accessible_models` 渲染为可勾选 chips（默认全选，已有 listing 时预勾 listed∩accessible）；只能勾选 relay 实测模型
  - 设计决定（Gate J M1，用户已认可）：模型探测用 `GET /verify-upstream`（key 留在 relay、不过浏览器）而非 `POST /preview-models`（key 浏览器输入流）。核心约束「模型仅从 relay 实测面勾选」两种流都满足；现流更安全。`/preview-models` 保留为独立端点（PIN 已达标），生产可另作无 relay 配置场景的消费面。
- [ ] **每模型三档价（v2）**：勾选模型展开独立价格行（默认继承 BASE 三档）；改某格=该模型覆盖价；改 BASE 时未动过的格子跟随、已覆盖的保留；取消勾选收起该行，重勾恢复继承 BASE；已有 listing 时预填链上各模型价（BASE=首模型价）
- [ ] 提交门控：未预检 / key 无效 / relay 不可达 / endpoint 改后未复验 → 禁提交并在预览框+红条给明确原因；models 零选 → 红条拒绝；**任一模型**价格非数或 >6 位小数 → 预览框琥珀行列出模型名 + 提交红条拒绝
- [ ] 预览框实时反映将发的交易：`register(endpoint, models, prices[])`（prices 平行数组逐模型 `(c,i,o)`，**一笔原子 tx**）/ `updateModelPrice(model, (c,i,o))` 逐变更模型多行 / `deactivate()+register` 两笔 / `deactivate()`；按钮文案随路径切换（SUBMIT / UPDATE PRICE·N TX / DEACTIVATE → RE-REGISTER · 2 TX / DEACTIVATE）；价格全与链上一致 → 按钮变 PRICES UNCHANGED，点击仅红条提示不发交易
- [ ] **逐模型改价**：shape 不变仅改价 → 每个变更模型一笔 `updateModelPrice`，逐笔钱包签名 + tx 行序号 i/N；中途某笔失败 → 该行红、其余标记 skipped（灰虚线），修复后重新提交剩余；staticCall 预演失败（NotActive/ModelNotFound）→ 人话红条且不弹钱包
- [ ] UPSTREAM PRECHECK 与表单联动：表单侧预检会回填 `p-base` 并渲染结果卡；卡侧 RUN 成功同样刷新表单 chips；渲染 key_valid、upstream_host、accessible_models、listed_models、mismatches（逐条 model+原因）；relay 不可达→RETRY 提示+表单侧同步失败态
- [ ] 提交：MetaMask 弹窗前先 `staticCall` 预演，revert 人话化（AlreadyRegistered→提示 deactivate→register 唯一路径并给一键两步按钮；NotActive/ModelNotFound/EmptyModels/LengthMismatch 各有文案）；tx 行 pending(amber)→hash（可点 explorer)→confirmed（绿）/reverted（红）；成功后 MY LISTING 刷新 + 预检自动复验
- [ ] 按钮防重：交易 pending / 预检进行中期间按钮 disabled

## Buyer tab

- [ ] 余额卡：钱包 USDC + Escrow `balances(me)`；WITHDRAW 输入金额→tx 流→余额刷新
- [ ] DEPOSIT： allowance 不足时 `approve → deposit` 两笔步进；充足时 approve 自动跳过
- [ ] LOCK：选卖家（下拉=市场同源数据）→ 按模型逐行显示三档价 + 各模型 minAmount 估计（in×200k+out×32k caps)；hint 取最贵模型估计，低于它给黄色提醒但仍可发；成功→大字 paymentId（点击复制）+ 自动带入 CALL DEMO
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
