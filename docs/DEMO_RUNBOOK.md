# TokenShare 演示 Runbook（T-1h 检查清单）

> 目标：演示开始前 1 小时按本单逐项打勾，确保现场零意外。
> **地址快照 = 2026-10-07 部署（Escrow v3.2，contracts/deployed.monad.json）**。演示前务必与 `web/config.js` 现值核对——重部署后以 config.js 为准。

> ⚠️ **运行时提示（2026-10-07）**：当前在跑的本地 demo relay（PID 44254，端口 8787）是**旧 Escrow v3.1 快照注入的进程**——地址在启动时注入，进程不感知新部署。**现在不要重启/kill 它**。未来获准刷新时：先 `make stop` 再 `make demo`（会按新快照注入地址）；直接 `make demo` 会**复用旧实例**、不刷新地址。Docker 镜像地址为运行时注入，无需 rebuild。

```bash
# ── 公共变量（值取自 web/config.js，如已重部署请同步更新）──
RPC=https://testnet-rpc.monad.xyz          # chainId 10143 · Monad Testnet
ESCROW=0xe4D5Eb0dBDB6DB8063C07ECF7EDFcCdDB9Ad514c   # v3.2，2026-10-07
REGISTRY=0xeD347cDc1761750E20C024459b38dedFb1462254
USDC=0x534b2f3A21130d7a60830c2Df862319e593943A3   # Circle 官方 USDC，6 位小数
SELLER=0x38c26E1782b7E6F656Aa35EbF473e2ff0D718Dd6
BUYER=<买家钱包地址>
```

## ① 余额预检（cast）

门槛：**SELLER MON≥1 ｜ BUYER MON≥1 ｜ BUYER 钱包 USDC≥5 ｜ Escrow 内≥2**

```bash
# MON gas（返回 wei；≥ 1000000000000000000 = ≥1 MON）
cast balance $SELLER --rpc-url $RPC
cast balance $BUYER  --rpc-url $RPC

# BUYER 钱包 USDC（6 位小数；≥ 5000000 = ≥5 USDC）
cast call $USDC "balanceOf(address)(uint256)" $BUYER --rpc-url $RPC

# BUYER 在 Escrow 的可锁余额（6 位小数；≥ 2000000 = ≥2 USDC）
cast call $ESCROW "balances(address)(uint256)" $BUYER --rpc-url $RPC
```

任一不足 → 先补：MON 用 https://faucet.monad.xyz ，USDC 用 https://faucet.circle.com （选 Monad testnet）；
Escrow 不足用 BUYER 钱包在 console 页 DEPOSIT（或 `cast send $ESCROW "deposit(uint256)" <amount-wei> …`）。

## ② Kimi 额度预检 + 换 key

```bash
curl -s http://127.0.0.1:8787/verify-upstream | python3 -m json.tool
# 看 "key_valid": true 即通过；false/额度报错 → 换 key
```

额度不足换 key 三步：

1. 编辑 `.env`：`OPENAI_API_KEY=sk-…`（新 key；`OPENAI_BASE_URL` 保持 `https://api.moonshot.cn`）。
2. 重启 relay（repo 根、.env 已导出）：
   `python3 -m uvicorn relay.app.main:app --host 127.0.0.1 --port 8787`
   （默认 `VERIFY_UPSTREAM_ON_START=1`，启动即验 key，401/403 直接起不来——这就是验证。）
3. 重跑上面的 `curl …/verify-upstream` 确认 `key_valid: true`，且 `accessible_models` 覆盖 listing 模型。

## ③ 预 mint 2 把备用 API key

现场若签名/钱包环节出意外，用预 mint 的 key 直接跑 curl 调用演示。

```bash
cd cli
python3 -m tokenshare_cli mint-key --payment-id <PID> --ttl-override <秒>
```

- 前提：paymentId 处于 **Locked** 且未过期；`--ttl-override` **不能超过 payment 剩余 TTL**（合约事实决定上限）。
- 想拉长有效期 → 先用 BUYER 锁一笔 **大 TTL** 的新 payment，再以 `--ttl-override ≈ 该 TTL` mint。
- 两把 key 全文**存安全处**（密码管理器/私有剪贴板，勿进 git/群聊）；每把记录 paymentId、到期时间。

## ④ 隧道 URL 冻结 → endpoint 重登记 → 市场页复核

1. 冻结当天演示用的隧道公网 URL（不再重启隧道，URL 变了前功尽弃）。
2. 用 `scripts/re-register-endpoint`（infra lane 交付；若文件未到位先找 infra lane 要）把 listing 的 endpoint 重登记为冻结 URL。
3. 打开 http://localhost:8080 （`python3 -m http.server 8080 -d web`）→ 市场页复核：
   - 卖家卡 endpoint 域名 = 冻结 URL；
   - relay 健康点绿色（在线+延迟 ms）；
   - 模型 chips 与三档价正常显示。
4. 跑 `node scripts/smoke-web.mjs` 确认静态冒烟全绿。

## ⑤ 预录视频兜底（T-1h 内录完）

按真实演示动线完整录一遍（ ≤5 分钟）：

1. 市场页：listing 卡 + 模型选择 + 健康点；
2. console：LOCK → MINT API KEY → 出 key；
3. 终端 curl 携带该 key 调 `/v1/chat/completions`，拿到回复；
4. `usage` 端点对账（captured/remaining 与调用一致）;
5. Explorer 打开对应 tx（lock/settle）佐证链上真实。

文件存本地 + 播放快捷方式上桌，现场一键可放。

## ⑥ 演示顺序（重要）

- **REVOKE 放最后**：revoke 集在 relay 内存里，**relay 重启即丢**——中途重启 relay 会让已吊销 key 复活。把 REVOKE 演示排在末尾，之后不再重启 relay。
- **Kimi RPM=3（每分钟 3 次请求）**：现场讲解刻意放慢，两次真实上游调用之间 ≥20s；不要连续点 SIGN+CALL。节奏：讲市场页 → 讲架构 → 再触发下一次调用。

## ⑦ 应急表

| 症状 | 判定 | 处置 |
| --- | --- | --- |
| 调用返回 **502** | Kimi 上游限速/额度 | 停 60s 重试一次；仍失败 → 换 `.env` 备用上游 key 重启 relay；再不行切**预录视频** |
| **lock revert** | Escrow 余额不足 | console BUYER 页 **DEPOSIT** 后重锁（见①余额预检补钱） |
| **RPC 慢/超时** | Monad 公共 RPC 抖动 | 页面 8s 超时降级（市场页琥珀提示、console 静默兜底）**属正常设计**，点 REFRESH 或稍候即恢复，不要当场换链改配置 |
| mint 后调用 401 revoked | 该 key 已吊销（REVOKE 演示后的 key） | 换③备用 key 或重新 mint |
| 市场页 seller 卡灰点 | relay 不可达或 CORS 未开 | 确认 relay 进程在 8787；隧道未重启；浏览器 console 看 CORS 报错 |

## 服务自检（T-1h 最后一步）

```bash
# 静态站（若未起）
python3 -m http.server 8080 -d web &

# 冒烟（应全绿，exit 0）
node scripts/smoke-web.mjs
```
