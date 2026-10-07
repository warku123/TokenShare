# TokenShare Web — 完整功能测试清单

Monad testnet (chainId 10143) · Escrow/Registry/USDC 已部署（见 `web/config.js`）。
> **2026-10-07：Escrow v3.2 重部署**（`0xe4D5Eb0dBDB6DB8063C07ECF7EDFcCdDB9Ad514c`），`config.js` 的 `escrowAddr` 已同步刷新。此后若再重部署：**先改 `config.js` 再跑本清单**；本地 demo relay 进程（PID 44254/8787）地址为启动时注入，未按 `make stop && make demo` 重启前仍指向旧 v3.1。
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

## 登出 / 换账户（lib-6 · DISCONNECT 真登出 + SWITCH ACCOUNT）

- [ ] **DISCONNECT 真登出（路径 1）**：连接态钱包条出现并列 `[ SWITCH ACCOUNT ]` + `[ DISCONNECT ]`（ghost 小按钮）；点 DISCONNECT → best-effort `wallet_revokePermissions({eth_accounts:{}})`（≤300ms race 吞错）+ 写持久 flag `tokenshare.wallet.logout:<rdns>` + 清本地连接态（地址/余额/MY LISTING 回未连接空态，CONNECT 按钮复现）；MetaMask 场景钱包内「已连接站点」列表本站消失（真撤销）
- [ ] **登出后刷新不自动连回（路径 2）**：DISCONNECT 后刷新页面 → 零弹窗、保持未连接（resume 先查 `logout:<rdns>` flag，有则跳过 eth_accounts 恢复）；再显式点 CONNECT 选同一钱包成功连接（remember 清 flag）→ 之后刷新恢复静默连接（flag 已清）
- [ ] **OKX/Coinbase 手动断开提示（路径 3）**：OKX/Coinbase 下点 DISCONNECT → 钱包条出现琥珀色提示「logged out locally — also disconnect this site inside your wallet (it ignores programmatic revoke)… refresh will not auto-reconnect」+ 附注「on-chain USDC approvals (approve) are separate and unaffected」（登出≠撤销 USDC 代币授权）；提示数秒后自动消失
- [ ] **SWITCH ACCOUNT（路径 4）**：点 `[ SWITCH ACCOUNT ]` → `wallet_requestPermissions({eth_accounts:{}})`——MetaMask 已连接也弹选号 UI；4001 用户取消 → 一切保持现状（地址/余额不变，无报错）；不支持该方法的钱包（OKX/Coinbase 等）自动降级 `eth_requestAccounts` 经典弹窗；成功后裸 `eth_accounts` 校准当前账户 + 清 logout flag → 地址/余额/MY LISTING 静默刷新（全程不调 ethers getSigner 的隐式弹窗路径）
- [ ] **accountsChanged 事件语义（路径 5）**：钱包扩展内手动断开本站（accountsChanged=[]）→ 页面清态（CONNECT 复现）且**不写** logout flag（localStorage 无 `logout:` 键）、rdns 记忆清除；钱包内切账户（accountsChanged 非空）→ 同一钱包静默换地址（不弹窗、不重开选择器）；显式 DISCONNECT 与钱包侧断开在刷新后都不自动连回，但前者靠 logout flag、后者靠 rdns 记忆清除

## UI 文案语言（M13）

- [ ] **用户可见串零中文**：`grep -n "[一-鿿]" web/*.html web/*.js` 仅剩代码注释命中（config.js/common.js/console.js 注释）；页面按钮/标签/错误提示/确认 dialog/tooltip/状态文案全部英文（TESTING.md 本身保留中文）


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

> **M15 共享 relay 托管模式**：`web/config.js` 现默认携带 **ACTIVE** `m15` 块（2026-10-08 激活，六项身份 pin 全值，B9 检查守护）→ SELLER 页顶部出现 KEY CUSTODY 卡并接管表单（详见下节「M15 共享托管」）；移除该块即回退单卖方经典表单（手填 RELAY ENDPOINT + `[ ↺ LOAD FROM RELAY ]`）。

- [ ] 连接卖家钱包 → MY LISTING 显示链上 listing（未登记→空态引导）；PRICES /1M 按模型分组逐行（model + cached/in/out）；已登记且 relay 在线时追加 TEE/UPSTREAM 两行
- [ ] MODELS 硬化：无自由文本输入；`[ LOAD FROM RELAY ]` 预检 `verify-upstream` → `accessible_models` 渲染为可勾选 chips（默认全选，已有 listing 时预勾 listed∩accessible）；只能勾选 relay 实测模型
  - 设计决定（Gate J M1，用户已认可）：模型探测用 `GET /verify-upstream`（key 留在 relay、不过浏览器）而非 `POST /preview-models`（key 浏览器输入流）。核心约束「模型仅从 relay 实测面勾选」两种流都满足；现流更安全。`/preview-models` 保留为独立端点（PIN 已达标），生产可另作无 relay 配置场景的消费面。
- [ ] **每模型三档价（v2）**：勾选模型展开独立价格行（默认继承 BASE 三档）；改某格=该模型覆盖价；改 BASE 时未动过的格子跟随、已覆盖的保留；取消勾选收起该行，重勾恢复继承 BASE；已有 listing 时预填链上各模型价（BASE=首模型价）
- [ ] 提交门控：未预检 / key 无效 / relay 不可达 / endpoint 改后未复验 → 禁提交并在预览框+红条给明确原因；models 零选 → 红条拒绝；**任一模型**价格非数或 >6 位小数 → 预览框琥珀行列出模型名 + 提交红条拒绝
- [ ] 预览框实时反映将发的交易：`register(endpoint, models, prices[])`（prices 平行数组逐模型 `(c,i,o)`，**一笔原子 tx**）/ `updateModelPrice(model, (c,i,o))` 逐变更模型多行 / `deactivate()+register` 两笔 / `deactivate()`；按钮文案随路径切换（SUBMIT / UPDATE PRICE·N TX / DEACTIVATE → RE-REGISTER · 2 TX / DEACTIVATE）；价格全与链上一致 → 按钮变 PRICES UNCHANGED，点击仅红条提示不发交易
- [ ] **逐模型改价**：shape 不变仅改价 → 每个变更模型一笔 `updateModelPrice`，逐笔钱包签名 + tx 行序号 i/N；中途某笔失败 → 该行红、其余标记 skipped（灰虚线），修复后重新提交剩余；staticCall 预演失败（NotActive/ModelNotFound）→ 人话红条且不弹钱包
- [ ] **UPSTREAM PRECHECK 已并入 register 表单（M13 合并）**：无独立 precheck 卡；表单 MODELS 区即预检步骤——填 endpoint → `[ ↺ LOAD FROM RELAY ]` → 成功 `accessible_models` 直接渲染为可勾选 chips 进入每模型价格表；key 无效/relay 不可达/HTTP 非 200 等失败状态内嵌表单区显示（红条 + 诊断行 KEY/UPSTREAM/ACCESSIBLE/LISTING ON-CHAIN/LISTED/MISMATCHES/FORM vs KEY + RETRY 按钮）；成功后诊断行同样内嵌渲染；预检中 `[ ↺ LOAD FROM RELAY ]` 禁用
- [ ] 提交：MetaMask 弹窗前先 `staticCall` 预演，revert 人话化（AlreadyRegistered→提示 deactivate→register 唯一路径并给一键两步按钮；NotActive/ModelNotFound/EmptyModels/LengthMismatch 各有文案）；tx 行 pending(amber)→hash（可点 explorer)→confirmed（绿）/reverted（红）；成功后 MY LISTING 刷新 + 预检自动复验
- [ ] 按钮防重：交易 pending / 预检进行中期间按钮 disabled
- [ ] **DEACTIVATE（MY LISTING 卡一键整站下线，M13 改名）**：仅 active listing 显示 `[ DEACTIVATE ALL ]`（红系细边框危险按钮，卡底；行为不变=listing 级 deactivate，与行级 [ REMOVE ] 形成层级对比）；未连接 / 未登记 / 已停用 / 读卡期间均不出现
- [ ] 确认弹窗：终端风 alertdialog（红标题 `confirm — registry.deactivate()`），一句后果=市场页 ACTIVE 展示即刻撤下（卡转 INACTIVE 灰态、买家不可再锁单）＋在途 Locked payment 仍可 settle＋可随时 register 恢复；Esc / 点遮罩 / `[ CANCEL ]` 关闭且**不发交易**、按钮复位；默认焦点在 CANCEL
- [ ] 确认后：钱包弹一笔 `deactivate()` 签名；卡内 tx 行 pending(amber)→hash（可点 explorer)→confirmed（绿）；全程按钮 disabled 防重
- [ ] 成功后：MY LISTING STATUS 变 INACTIVE 灰徽章（pulse 停止）+ PRICES 区弱化 + 停用说明行（指引回表单重注册）；买家下拉该 seller 变 `(inactive)` disabled；market.html 刷新后该卡 INACTIVE 灰态、首页 ACTIVE 统计减一（append-only 枚举不删条目）
- [ ] **deactivate → 重注册回环**：下线后表单勾 LISTING ACTIVE → 预览**直走** `register(…)` 单笔（**不**进 deactivate→register 两步）→ 一笔签名 → STATUS 回 ACTIVE、市场页恢复可购；反向 stale（链上已 active 而本地不知）时 AlreadyRegistered 红条 + 一键两步按钮依旧生效
- [ ] deactivate 失败兜底：链上已 inactive 而本地 stale 时点 `[ DEACTIVATE ]` → tx 行红（NotActive revert），卡片自动重读链上真值并收起按钮，表单内容不被清
- [ ] **模型级下线（M12 · Registry v4 removeModel）**：MY LISTING 的 PRICES /1M 区每模型行末尾有小型 `[ REMOVE ]` 链接（红系弱样式，视觉档位低于卡底 `[ DEACTIVATE ALL ]` 大按钮）；active 与 inactive listing 均显示（合约无 active 要求）
- [ ] 确认弹窗：终端风 alertdialog（红标题 `confirm — registry.removeModel()`），文案三要素齐全=①该模型**即刻停止服务**（getPrice revert → relay 对新调用 400）②**在途（Locked）payment 将 settle-failed，买家 ttl 后 refund 收回全款**③下架后可 register 加回；Esc / 点遮罩 / `[ CANCEL ]` 关闭且**不发交易**；默认焦点在 CANCEL
- [ ] **单 tx**：确认后先 `staticCall` 预演（revert 人话化、不弹钱包）→ 钱包仅弹**一笔** `removeModel(model)` 签名 → tx 行 pending(amber)→hash（可点 explorer)→confirmed（绿）；操作期间全部行级按钮 disabled 防重，结束/取消后恢复正确态
- [ ] **行消失+联动**：成功后 MY LISTING 重读 getListing，该模型行消失；买家 LOCK 卖家信息、CALL DEMO 模型下拉同步少一模型（同源 getListing）；market.html 下次刷新该卡少一模型 chip；表单内未提交的编辑不被清（prefill 跳过）
- [ ] **末模型守卫（禁点方案，M13 引导改）**：仅剩 1 个模型时其 `[ REMOVE ]` 为 disabled 灰态，title 提示「last model — use [ DEACTIVATE ALL ] below · contract guards RemoveLastModel」（引导用大按钮）；竞争兜底=卡片 stale（另一窗口已删到只剩一个）时点下线 → staticCall 捕 `RemoveLastModel` → 人话红条「the last model cannot be removed … use [ DEACTIVATE ALL ] at the card bottom」且不弹钱包
- [ ] stale 行兜底：另一窗口已移除该模型后本地仍显示 → 点 `[ REMOVE ]` → staticCall 捕 `ModelNotFound` → 人话红条 + 卡片自动重读（行消失），不弹钱包
- [ ] **重注册回环**：移除某模型后，右侧表单重新勾选该模型并提交 → 走 deactivate→register 两步流 → 该模型行带价复活（价随模型走：prices ∥ models 同索引）；`ModelRemoved(operator, model)` 事件可在 explorer tx 日志核对

## Buyer tab

- [ ] 余额卡：钱包 USDC + Escrow `balances(me)`；WITHDRAW 输入金额→tx 流→余额刷新
- [ ] DEPOSIT： allowance 不足时 `approve → deposit` 两笔步进；充足时 approve 自动跳过
- [ ] LOCK：选卖家（可搜索下拉=Registry 链上枚举，RPC 失败降级 config sellers，与市场页同源）→ 按模型逐行显示三档价 + 各模型 minAmount 估计（in×200k+out×32k caps)；hint 取最贵模型估计，低于它给黄色提醒但仍可发；成功→大字 paymentId（点击复制）+ 自动带入 CALL DEMO
- [ ] LOCK 大卡布局：BUYER 页首卡=全宽 LOCK hero（左列 seller 选择器+模型价目 mini-info，右列 live 余额+MAX AMOUNT/TTL+按钮+paymentId），CALL DEMO 次卡全宽，BALANCES/DEPOSIT/REFUND 资金组三等分轻卡，DISPUTES 殿后全宽；≤1020px 时 hero 左右列塌成纵向（分隔线转顶部虚线）
- [ ] seller 可搜索下拉（LOCK+CALL 同组件）：聚焦弹全量列表（截断地址+host+ACTIVE/INACTIVE 徽章+≤3 模型 chips+首模型三档价/min 一行+健康延迟点懒探测）；输入按地址/host/模型名子串过滤；↑/↓ 移动高亮、Enter 选中、Esc 恢复并关闭、失焦/点击外部恢复；INACTIVE 行灰色不可选；无匹配显示提示、链上无 seller（或 RPC 不可达）显示降级空态；LOCK 选中后 CALL 处自动带入（silent，不联动循环）；10+ seller 列表滚动可用
- [ ] MINT API KEY（M13）：LOCK 成功 pid-box 出 `[ MINT API KEY ]` + SESSION LOCKS 每行同入口 → modal 显示链上 getPayment 摘要（state/max/expires，非 Locked 或已过期则禁签并红字说明）→ `[ SIGN + MINT ]` 弹钱包签 `TokenShare API key grant|paymentId=…|expiry=…|maxAmount=…` → 结果区=key 全文（点击复制）+BASE URL（可复制）+curl 示例（占位符 `$BASE_URL`/`$API_KEY`，赋值行单引号转义，整块点击复制）+琥珀色安全提示（泄露最多亏 maxAmount/可在 API KEYS 面板 [ REVOKE ] 即时吊销/TTL 到期失效/key 本体不存储仅元数据落面板）；Esc/遮罩关闭；key 与 CLI `mint-key` 同参数字节一致
  - **恶意链上字符串（rev-6 C1）**：链上 register 含引号/`;`/`$()`/反引号/换行的 endpoint 或模型名 → mint 结果 curl 块显示为单引号转义赋值（如 `BASE_URL='https://evil.com/$(x)'`，原样可见但 shell 惰性），复制照粘不执行任何注入命令
  - **伪造 locks 行（rev-6 C2）**：localStorage `tokenshare.locks` 行改 seller 为他人地址 → MINT modal/USAGE 面板顶部红字警告「row seller ≠ on-chain」且 endpoint/示例一律按链上 getPayment seller 解析（链读失败才回退行值并给 dim 提示）
- [ ] USAGE：SESSION LOCKS 行 `[ USAGE ]` → 展开 GET {relay}/payment/{id}/usage → captured/remaining/max + 进度条 + 时间戳 + `[ REFRESH ]` 重拉；再点收起；relay 不可达/HTTP 错显示红字+REFRESH 可重试；卖家不在当前 listings（RPC 降级）给提示不报错
- [ ] API KEYS 面板（M13 补件，LOCK 卡之后）：mint modal 成功（SIGN+MINT 出 key）后 Esc/遮罩/CLOSE 关闭 → 该 key 元数据行自动出现在面板（paymentId/model/max $/minted 时间/ACTIVE 绿徽章 + expiry 倒计时 1s 走秒 + captured·remaining 用量条 + `[ COPY BASE URL ]` + `[ REVOKE ]`）；registry=localStorage `tokenshare.apikeys` 仅存 `{p,e,m,model,createdAt,revokedAt}` 元数据（e/m=mint 时链上 getPayment 真值），**key 本体面板永不再显示**（仅 mint 结果一次性展示）
  - **REVOKE 流**：`[ REVOKE ]`（仅 ACTIVE 行）→ dangerConfirm（文案：吊销后该 key 立即失效不可恢复直到 payment TTL 结束；链上已 capture 金额不受影响，未用余额 TTL 后照常 refund）→ 钱包签 `TokenShare API key revoke|paymentId=…|expiry=…`（EIP-191 signMessage）→ POST {relay}/payment/{id}/revoke `{"message","signature":"0x…"}` → 200 `{"revoked":true}`（重复吊销幂等 200）→ 行转 REVOKED 红点徽章；之后拿该 key 打 relay → 401 detail revoked
  - **非 buyer 签名**：连着非 buyer 钱包点 REVOKE → relay 401 → 行内红字 detail，状态不变可换账户重试；签名拒签/网络超时/relay 不可达各有红字且 key 仍 live
  - **EXPIRED 本地判定**：now ≥ e 倒计时归零 → 行自动翻 EXPIRED 灰徽章（不再拉用量、无 REVOKE 钮；COPY BASE URL 仍可按链上 seller 解析）
  - **持久化**：重启浏览器面板仍在（localStorage），REVOKED 状态不丢；`tokenshare.apikeys` 写畸形条目（缺 e、`"p": {}`、m 非数字）→ 页面正常启动、畸形行跳过不炸
  - **COPY BASE URL**：endpoint 按链上 `getPayment(p).seller` → 当前 listings 解析（rev-6 C2 同模式——registry 行不存 seller，localStorage 不可信）；seller delisted/链读失败 → 钮禁用 + title 说明；复制值不经 HTML 解析（dataset 属性赋值）
- [ ] LOCK 余额预校验：LOCK 卡内嵌实时 `ESCROW balances(me)`（连接即拉取，deposit/withdraw/lock/refund 成功后刷新）；MAX AMOUNT > 余额时 `[ LOCK ]` 禁点 + 红字「insufficient escrow balance — deposit $N more (escrow $X < lock $Y)」；金额降回余额内即自动解禁（实测：余额 2.999674、填 10 → 禁点 + 提示 deposit $7.000326 more）
- [ ] Escrow revert 人话：绕过预校验造成链上 revert（如另一窗口先 lock 把钱占走再发，或未到期 refund 触发 TtlNotElapsed）→ tx 行显示带参数的人话（如「insufficient escrow balance — escrow $2.999674 < requested $10; deposit $7.000326 more first (InsufficientBalance)」），不再出现 unknown custom error
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

## M15 共享托管（shared relay custody · 仅当 config.js 含完整 m15 块）

> 协议冻结 v1-e1。托管面只在 `m15` 块**完整且合规**时出现；缺字段/畸形 → 红字 fail-closed（VERIFY 禁用），经典表单照旧。身份 pin 一律来自 config（部署记录+运营者离线验证），**绝不**用 relay 自报的 /info 反向 pin（no TOFU）。

- [ ] **共享表单变换**：m15 生效后 RELAY ENDPOINT 输入框消失（托管卡顶部显固定 RELAY ORIGIN，不可手改）；REGISTER 卡 MODELS 区不再显示 `[ ↺ LOAD FROM RELAY ]`；唯一输入 = UPSTREAM BASE URL + UPSTREAM API KEY（password 遮罩）
- [ ] **VERIFY 全流**：连接钱包即自动跑 live pin 检查（/info + /attestation ↔ config：origin / signer / upload key sha256 / appId / derivedAddress / reportData=signer‖zeros‖keccak(pub) 六项全绿行）；点 `[ VERIFY + SEAL KEY ]` → nonce（签发值逐项对照 config，篡改拒签）→ 浏览器内封套（ECDH P-256→HKDF-SHA256→AES-256-GCM）→ 钱包签 EIP-191 custody 消息 → POST /sellers/keys → catalog 渲染为 chips
- [ ] **DEPLOYMENT MISMATCH**：任一 pin 不符 → 红字硬失败 + 托管禁用（VERIFY/PUBLISH 全锁）；quote 仅自报——卡内明示运营者验证器/日期/digest + 「本页只复查 live binding，不重跑 DCAP」的诚实信任边界
- [ ] **catalog 语义**：servable=false 模型灰显虚线不可勾（title 带 relay 原因）；零可服务模型 → PUBLISH 禁用；编辑 URL 或 key → catalog 转 stale + PUBLISH 锁，须重新 VERIFY；VERIFY 成功后 key 输入框被程序化清空（不触发 stale；手动重输才 stale）；钱包切账户 → 全部托管态重置
- [ ] **PUBLISH**：register endpoint 自动 = pinned relay origin（预览框可见，绝不是 upstream URL）；AlreadyRegistered → 沿用 deactivate→register 两步显式流，无隐藏交易
- [ ] **AUTHORIZE（独立按钮）**：`[ AUTHORIZE DELEGATE ]` → 确认弹窗含完整风险文案（delegate 可对**任意** Locked payment 按全额 maxAmount settle、无需服务证明、含旧锁/过期锁；买家仅可退未消费部分；delegate 不能提现/转走余额）→ 一笔 `approveSettleDelegate(pinnedSigner)` 链上交易；DELEGATE 行显示链上真值（settleDelegateOf）；已授权给**非** pin 地址 → 红字警告 + 可 REVOKE
- [ ] **REVOKE**：`[ REVOKE DELEGATE ]`（approve 零地址，独立显式交易）与 `[ REVOKE STORED KEY ]`（DELETE /sellers/keys，新 nonce + action=revoke 签名，无链上交易）分离；吊销 key 后 listing 仍在链上但 PUBLISH 锁回 VERIFY
- [ ] **resume（C1 修复后语义）**：刷新/重连后 relay /status 有 key → catalog 可复用，但仅当**本连接 session 完全未被触碰**（无 anchor、URL/key 无编辑——gen 仍等于 `sessionStartGen` 基线、本 session 无失败 VERIFY）且 pins 当场重验过，且 /status 应答落在发起时的 generation+钱包上（gen 单调递增 ⇒ 旧账户/同地址断开重连的晚到应答**绝不**复活旧 catalog/pins/anchor）；资格判定 = `T.custody.canResumeCatalog` 谓词（M 段正/负矩阵驱动）——任何 URL/key 编辑或失败 VERIFY 立即使复用失效，status 只是展示面，不是身份权威
- [ ] **买家侧**：listing endpoint 严格匹配 pinned relay origin（https·根路径·无 userinfo/query/fragment·有效端口一致）时，receipt 验签对象换成 pinned relay signer（message.seller 仍须=链上 listing operator）；其它 seller 一律沿用单卖方旧规则；不匹配即不施加共享 pin
- [ ] **跨语言测试边界**：`scripts/check-custody-web.mjs`（node，零新依赖：vendor ethers + Node WebCrypto）覆盖 URL 归一化/m15 校验/冻结格式字节级断言/封套 roundtrip+篡改/pin 矩阵/nonce 矩阵/单+共享 receipt/delegate calldata/表单门控谓词/catalog 恢复资格谓词（M 段：首次连接/刷新/重连正向 + 编辑/VERIFY 失败/pins/在途竞态/旧账户晚返回/同地址断开重连晚返回负向 + console.js 静态接线证据）/custody 请求头纪律；**浏览器内不重验 DCAP quote**；Python relay ↔ 本页的字节级跨语言向量由 relay lane 的 `relay/tests/vectors/custody_vector.json` 持有 —— 本套件 L 段在该文件存在时**只读**复现它（AAD 逐字节、canonical body 对独立构造的字面量、本地自算 sha256、消息结构逐字节、固定 eph/iv 重加密 ct 逐字节、接收方解密回环）。**绝不**从 fixture 消息里抽取 body_sha256 回喂作输入（无循环自证）；fixture 未提供的字段（raw_body/body_sha256 真实哈希、synthetic EOA 签名/signer —— 待 relay fix46）报 **[PEND]** 并单列，计入摘要但**不计 PASS**，文件缺席则整段跳过不失败（自包含性保持）

## Policy 阅读同意门禁（POLICY_VERSION 2026-10-08）

> 存储绑定：`web/common.js` `T.policyConsent` —— sessionStorage 键 `tokenshare.policyConsent.v1`，值 `{"version":"2026-10-08","wallet":"0x…","ts":…}`；`isValid(version, wallet)` 双条件校验。consent 本身**不需要任何钱包签名**，纯 UX 门禁。console 顶部 consent 条 = 单一事实面（`#policy-consent` + `#policy-agree`）。

- [ ] **条存在**：console 页顶部有一条「I have read and agree to the Platform Policy & Risk Notice」（链接 policy.html 新窗口；policy 页首 chip + 页脚均标注版本 2026-10-08）
- [ ] **未勾选全禁**：`s-submit` / `s-verify-btn` / `sc-authorize` / `b-dep-btn` / `b-lock-btn` / `c-send` 均 disabled 且 hover title 说明；SESSION LOCKS 行内 `[ MINT API KEY ]` 同样禁用；未连接钱包时 checkbox 本身禁用
- [ ] **勾选生效**：连接钱包后勾选 → 上述按钮全部启用；DevTools → sessionStorage 可见 `tokenshare.policyConsent.v1 = {"version":"2026-10-08","wallet":…,"ts":…}`
- [ ] **handler guard（双层阻断第二层）**：DevTools 手动移除按钮 disabled 后点击付费动作 → 抛 `PolicyConsentRequired`（console 见 POLICY_CONSENT_REQUIRED），**无任何 tx / 签名弹窗**，consent 条闪烁定位
- [ ] **切账户/断开失效**：勾选后 DISCONNECT（或钱包内切账户）→ 记录删除、checkbox 复位禁用、按钮回到禁用（重连后需重勾）；刷新页面 sessionStorage 存活 → 同钱包静默恢复勾选态
- [ ] **匿名只读放行**：不连接钱包浏览 index / market / console 全部可见可用（市场卡、卖方表单预览、只读探测按钮），无 consent 拦截
- [ ] **退出路径放行**：未勾选时 refund / withdraw / deactivate / removeModel（行级 `[ REMOVE ]`）/ 买家 key `[ REVOKE ]` / 卖家 `[ REVOKE DELEGATE ]` / `[ REVOKE STORED KEY ]` / DISCONNECT / SWITCH ACCOUNT 照常可点（这些按钮从不进 POLICY_GATED_BTNS）
- [ ] **custody 流零破坏**：VERIFY / keys POST / pin 检查逻辑未改动（`scripts/check-custody-web.mjs` 全绿），gate 只加在 `s-verify-btn` / `sc-authorize` 动作入口
- [ ] **静态接线**：`node scripts/smoke-web.mjs` 的 `consent *` 8 项全 PASS（bar id、POLICY_VERSION 与 policy.html 一致、guardPaid 六按钮、退出路径仍为普通 guard）

## 网络/断线走查

- [ ] 钱包切到非 10143：net 徽章变红 + 琥珀色 banner + `[ SWITCH / ADD NETWORK ]` 按钮（徽章本体也可点）
- [ ] 无钱包：CONNECT 点开为选择器空态（含 MetaMask/OKX 安装链接）—— 详见「钱包选择器」节；页面其余只读可用
- [ ] RPC 不可达：市场页显示琥珀色「RPC 不可达」提示而非崩溃；console 静默兜底
- [ ] 未装钱包时刷新页面无 console 报错；已授权钱包刷新后静默恢复连接（不弹窗，走 rdns + eth_accounts）
