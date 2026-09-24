# TokenShare Web — 完整功能测试清单

Monad testnet (chainId 10143) · Escrow/Registry/USDC 已部署（见 `web/config.js`)。
纯静态页：本地 `python3 -m http.server 8080 -d web` → http://localhost:8080（控制台 `/console.html`）。若 8080 被占换任意空闲端口。

## 前置条件

1. **钱包**（M11 起任一 EIP-6963 钱包）：MetaMask / OKX Wallet 均可，多装时点 `[ CONNECT WALLET ]` 在自绘选择器里选（详见下节）。连接后若钱包不在 Monad testnet，页面自动 `wallet_addEthereumChain` 引导（chainId `0x279f` = 10143，RPC `https://testnet-rpc.monad.xyz`）。手动切换后页面自动重载。
2. **测试币**（两个账号各一份，卖方+买方）：
   - MON gas：https://faucet.monad.xyz
   - USDC：https://faucet.circle.com （选 Monad testnet）
3. **卖方 relay** 跑在公网可达地址且 **CORS 已开**（`Access-Control-Allow-Origin` 覆盖页面源；响应头需 `Access-Control-Expose-Headers: X-Settle-Status, X-Receipt`），否则浏览器调不通 `/health`、`/verify-upstream`、`/v1/chat/completions`。
4. 卖家地址已列入 `web/config.js` 的 `sellers`（市场页/买家下拉的数据源）。

## 钱包选择器（M11 · EIP-6963 多钱包）

- [ ] **双钱包选 MetaMask 不弹 OKX**：同时装 MetaMask + OKX（OKX 设为 Default Wallet 抢注 `window.ethereum`）→ 点 `[ CONNECT WALLET ]` 只弹**自绘选择器**（终端风 modal，无任何钱包扩展弹窗）；列表两行各带图标+名称+rdns；选 MetaMask 行后**只有 MetaMask** 弹连接授权，OKX 全程静默
- [ ] **唯一弹窗**：从点 CONNECT 到连接成功整流仅一次 `eth_requestAccounts` 弹窗（点选行之后）；Esc / 点遮罩 / `[ CANCEL ]` 关闭选择器时不弹窗、不报错、按钮复原
- [ ] **刷新静默恢复**：连接过 MetaMask 后刷新页面 → 零弹窗，地址/钱包名/USDC 余额/net 徽章自动恢复（localStorage `tokenshare.wallet.rdns` 记忆 → 按 rdns 匹配公告 → 裸 `eth_accounts` 探测；**不**走 getSigner 的隐式弹窗路径）
- [ ] **断开重选**：在钱包扩展里断开本站连接 → 页面立即掉回未连接态（CONNECT 按钮复现）且 rdns 记忆被清，再点 CONNECT 可选另一钱包；钱包内切账户（accountsChanged 非空）→ 同一钱包静默换地址，不弹窗、不重开选择器
- [ ] **事件绑原始 provider + 切换解绑**：连着 MetaMask 时在 OKX 扩展里切账户 → 页面无反应（监听器只绑 MetaMask 的原始 provider）；通过选择器改连 OKX 后，MetaMask 里切账户不再影响页面，OKX 里切账户则静默换地址（旧绑定已 removeListener）
- [ ] **无钱包空态**：无扩展的纯净浏览器 → 点 CONNECT 弹选择器空态（虚线框 + MetaMask/OKX 安装链接），页面其余只读可用，console 无报错
- [ ] **6963 零公告回退**：钱包不支持 6963（仅注入 `window.ethereum`，含 `providers` 数组）→ 选择器列出 legacy 行（按 isMetaMask/isOkxWallet 标注名称），可正常连接；有 6963 公告时 `window.ethereum` 完全不参与（OKX 抢注失效）
- [ ] **图标注入面**：选择器行仅用 `createElement('img')` + `textContent` 渲染（DevTools 检查 DOM 无 innerHTML 注入路径）；伪造恶意 announce（icon 为带脚本的 data-URI SVG、name 含 `<img onerror>`）→ 名称按纯文本显示、无脚本执行

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
- [ ] **DEACTIVATE（MY LISTING 卡一键下线）**：仅 active listing 显示 `[ DEACTIVATE ]`（红系细边框危险按钮，卡底）；未连接 / 未登记 / 已停用 / 读卡期间均不出现
- [ ] 确认弹窗：终端风 alertdialog（红标题 `confirm — registry.deactivate()`），一句后果=市场页 ACTIVE 展示即刻撤下（卡转 INACTIVE 灰态、买家不可再锁单）＋在途 Locked payment 仍可 settle＋可随时 register 恢复；Esc / 点遮罩 / `[ CANCEL ]` 关闭且**不发交易**、按钮复位；默认焦点在 CANCEL
- [ ] 确认后：钱包弹一笔 `deactivate()` 签名；卡内 tx 行 pending(amber)→hash（可点 explorer)→confirmed（绿）；全程按钮 disabled 防重
- [ ] 成功后：MY LISTING STATUS 变 INACTIVE 灰徽章（pulse 停止）+ PRICES 区弱化 + 停用说明行（指引回表单重注册）；买家下拉该 seller 变 `(inactive)` disabled；market.html 刷新后该卡 INACTIVE 灰态、首页 ACTIVE 统计减一（append-only 枚举不删条目）
- [ ] **deactivate → 重注册回环**：下线后表单勾 LISTING ACTIVE → 预览**直走** `register(…)` 单笔（**不**进 deactivate→register 两步）→ 一笔签名 → STATUS 回 ACTIVE、市场页恢复可购；反向 stale（链上已 active 而本地不知）时 AlreadyRegistered 红条 + 一键两步按钮依旧生效
- [ ] deactivate 失败兜底：链上已 inactive 而本地 stale 时点 `[ DEACTIVATE ]` → tx 行红（NotActive revert），卡片自动重读链上真值并收起按钮，表单内容不被清

## Buyer tab

- [ ] 余额卡：钱包 USDC + Escrow `balances(me)`；WITHDRAW 输入金额→tx 流→余额刷新
- [ ] DEPOSIT： allowance 不足时 `approve → deposit` 两笔步进；充足时 approve 自动跳过
- [ ] LOCK：选卖家（下拉=Registry 链上枚举，RPC 失败降级 config sellers，与市场页同源）→ 按模型逐行显示三档价 + 各模型 minAmount 估计（in×200k+out×32k caps)；hint 取最贵模型估计，低于它给黄色提醒但仍可发；成功→大字 paymentId（点击复制）+ 自动带入 CALL DEMO
- [ ] CALL DEMO:model 下拉=所选卖家 listing models；prompt → `[ SIGN + CALL ]`
  - 终端 trace:EIP-191 msg 明文（`POST|/v1/chat/completions|<64hex>|<paymentId>`)→ MetaMask personal_sign → POST
  - **恶意 relay usage 注入（rev-4 C1）**：mock relay 返回 `"usage": {"prompt_tokens": "<img onerror=alert(1)>", "cached_tokens": "<script>...", "completion_tokens": {"x":1}}` → trace 中三字段按纯文本转义显示（可见 `<img …>` 原文），无脚本执行、无节点注入、正常数值显示与之前逐字符一致
  - **relay 挂起超时（rev-4 L3）**：mock relay 收 POST 后 20s 不回 → 15s 后 trace 显「relay 请求超时（15s）」，按钮解锁可重试
  - **localStorage 篡改（rev-4 C2）**：`tokenshare.locks` 写入畸形条目（缺 `maxAmount`、`"maxAmount": "abc"`、`paymentId` 为对象）→ 刷新 console 页面完整启动（DISPUTES/下拉/卡片全部渲染），畸形行降级显示（max ?）或整行跳过，无 console 报错
  - 200：回复文本 + usage 三档 + X-Settle-Status；收据面板绿「✓ 收据验签通过」+ actualAmount/upstreamHost/model/tokens
  - 验签失败：红条 + 自动记入 DISPUTES(localStorage)
  - 402 → 提示「maxAmount 低于卖家 minAmount 估计，调高金额」;401/409/400/502 各有对应文案；relay 不可达→CORS 提示
- [ ] REFUND：输入 paymentId（datalist 含本会话锁单）→ 先 getPayment 显示 state/max/expires，非 Locked 拒绝；Locked 且过期→refund tx→余额刷新
- [ ] DISPUTES：列表显示 paymentId/原因/时间；连接钱包后按当前地址过滤；`[ CLEAR ]` 清空

## 网络/断线走查

- [ ] 钱包切到非 10143：net 徽章变红 + 琥珀色 banner + `[ SWITCH / ADD NETWORK ]` 按钮（徽章本体也可点）
- [ ] 无钱包：CONNECT 点开为选择器空态（含 MetaMask/OKX 安装链接）—— 详见「钱包选择器」节；页面其余只读可用
- [ ] RPC 不可达：市场页显示琥珀色「RPC 不可达」提示而非崩溃；console 静默兜底
- [ ] 未装钱包时刷新页面无 console 报错；已授权钱包刷新后静默恢复连接（不弹窗，走 rdns + eth_accounts）
