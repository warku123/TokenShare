# Chainlink CRE 演示录制 Runbook（CHAINLINK_DEMO_DRAFT）— 单次真实执行 + 如实缩短等待 实操脚本

> 平台硬性上限 **2:00**；**1:40–1:50 为剪辑后成片目标**——现场单次真实执行的原始时长可能更长，彩排时掐表；成片 ≤2:00，编译/查询等待快剪并打「等待已缩短」字幕，**不伪造任何输出**。英文口播 **126 词**（机算，规则见 §6），100–120 wpm → 口播净时长 **1:03–1:16**。
> 展示内容：**现场 fresh DRY run（无 `--broadcast`，今日零新交易）** + **2026-10-08 历史已广播报告的链上 readback**。两件事全程分开讲。不是生产 DON 部署。
> **⚠ 工作目录勘误（以本稿为准）**：CLI 的 canonical cwd 是 **`/Users/zhangpinge/others/TokenShare/cre`**（含 `project.yaml`），命令里 workflow 路径写 `./settlement-audit`。`cre/README.md` 中 "cd cre/settlement-audit" 的说法已过时——**本稿不改 README（超出范围），录制时照本稿命令块执行**。出处：[../../cre/README.md](../../cre/README.md)、[../CRE_DEMO_EVIDENCE.md](../CRE_DEMO_EVIDENCE.md)、[chainlink.md](../submission/chainlink.md)。
> 小窗纪律：**本人出镜、摄像头小窗全程常驻**（固定角落，不遮命令/输出/金额）。这是**个人出镜偏好，不是赞助方/平台规则**，只为可信度。

## 0. 录前检查单（中文，不上镜，开录前全部通过）

```zsh
# 逐行跑；任何一行失败 → 先修复再录
~/.cre/bin/cre --version                      # CLI 在位
bun --version                                  # Bun 在位
test -d /Users/zhangpinge/others/TokenShare/cre/settlement-audit/node_modules \
  && echo "node_modules OK" \
  || echo "缺依赖：先 cd /Users/zhangpinge/others/TokenShare/cre/settlement-audit && bun install"
test -x /Users/zhangpinge/others/tokenshare-relay-venv/bin/python && echo "venv OK"
```

1. **登录态只在需要时处理**：如 CLI 报鉴权/登录错误，先 `~/.cre/bin/cre whoami` 确认；未登录才执行 `~/.cre/bin/cre login` 按浏览器提示完成。**登录凭据文件、token、任何鉴权内容不出镜、不打印、不粘贴。**
2. **彩排**：完整跑一遍 §1 命令块并掐表，记录编译等待时长；正式录制时等待段快剪并打「等待已缩短」字幕（见 §2 剪辑纪律）。
3. **今日动作边界**：p5 已 Settled、报告已广播过一次。现场**只有**：只读闸门 curl + dry-run simulate + 只读 readback。**禁止** `--broadcast`、`--stage deploy/finalize`、re-settle、新 payment、新调用、新交易。dry run **不需要** `CRE_ETH_PRIVATE_KEY`。
4. **密钥卫生**：不 source `.env`；`.env`、`cre/secrets.yaml`、私钥、上游 API key、shell history、AI 对话上下文一律不出镜。屏上允许出现的唯一"敏感样式"字符串是占位符 `dummy-bearer-token`（非机密，见 §1）。
5. 录屏前关掉通知（`mimetypes` 弹窗、iMessage、日历提醒），清掉无关 tab。

## 1. 一条完整可复制的录制命令序列（子 shell + `set -e`，逐字照贴）

整块复制到**新的 zsh 终端**一次执行。子 shell `( … )` 隔离：失败即停、不污染你的 shell；`set -e` 强制闸门——**任何一步失败，后面步骤不会执行**。

```zsh
(
set -e
REPO=/Users/zhangpinge/others/TokenShare

# ---- STEP 1: 硬闸门 —— /receipt/5 必须实时 HTTP 200（15 秒上限）----
echo "== STEP 1: receipt gate /receipt/5 =="
code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 \
  https://c30652f4833465adda9bdc7ac557e8b45e8e66c6-8787.dstack-pha-prod5.phala.network/receipt/5)" || true
if [ "$code" != "200" ]; then
  echo "STOP: /receipt/5 returned HTTP ${code:-no-response}."
  echo "Do NOT record. No local snapshots, no relay restart, no blind retries."
  exit 1
fi
echo "GATE OK: HTTP 200"

# ---- STEP 2: 非机密占位符（GET /receipt 匿名路由的模板占位；不是鉴权凭据）----
export RECEIPTS_BEARER_ALL=dummy-bearer-token

# ---- STEP 3: 现场 dry run（cwd 勘误：cre/ 下有 project.yaml）----
echo "== STEP 3: live dry-run simulate =="
cd "$REPO/cre"
~/.cre/bin/cre workflow simulate ./settlement-audit \
  --target staging-settings --non-interactive \
  --trigger-index 0 \
  --evm-tx-hash 0x04249c263cdc64661141bc3a0911ddec2b2c05e57f79d21b5fd0f682c07d1785 \
  --evm-event-index 0
echo "== SIMULATE DONE =="

# ---- STEP 4: 链上 readback（只读，查的是 2026-10-08 历史报告，不发交易）----
echo "== STEP 4: read-only readback =="
cd "$REPO"
/Users/zhangpinge/others/tokenshare-relay-venv/bin/python cre/scripts/run_demo.py \
  --stage readback \
  --anchor 0xE2d468cdD917E73C9F02d4Ac629223Ac062D901D \
  --tx 0x9125c1983a1f50402f883521bcfd6b1f848051560a6f98c69fe13ce53ea97330
echo "== READBACK DONE =="
)
```

执行要点（中文）：

- **闸门语义**：只打印状态码**不算过闸**——`if` 分支才是闸门；非 200（含 404/超时/无响应）→ 打印 STOP、`exit 1`，**录制终止**。不得改用本地快照、不得重启/远端补救 relay、不得盲目重试——改期或改走「历史证据走读」并如实标注。
- **bearer 占位符**：`RECEIPTS_BEARER_ALL=dummy-bearer-token` 只是 secret 模板解析用的**非机密占位**；该收据路由**匿名、无需鉴权**（匿名 ≠ 恒 200：**收据缺失时返回 404**，所以 STEP 1 的**实时 HTTP 200 是强制闸门**），**bearer 不是鉴权声明**——真正的真实性闸门是 EIP-712 签名恢复到 pin。不 source `.env`，无需 `CRE_ETH_PRIVATE_KEY`。
- **停顿/剪切点**：STEP 3 的编译等待可快剪；STEP 3 输出完毕、STEP 4 开始前可自然停顿切换画面（explorer）。
- **macOS zsh**：整块无需 source 任何文件，无后台进程残留；子 shell 结束后 `RECEIPTS_BEARER_ALL` 不残留于你的 shell。

## 2. 预期输出对照（⚠ 均为 2026-10-08 已录得的**证据示例**，非本次承诺输出、不代表当前可用性）

**STEP 3 dry run 历史示例措辞**（出现顺序近似）：

- `Receipt auth: signature recovered … == trusted …`（签名恢复 == pin）
- `Trigger block: 69225090` —— 所有读取都在这一确切高度
- `Compare: model=k3-256k expected=918 receipt=918 (onchain settled=918) → MATCH`
- `AUDIT OK payment 5 … tx=0x (dry-run — add --broadcast)`
- `Simulation complete!`

**STEP 4 readback 历史示例措辞**：`readback forwarder OK`；`records(5) settled=918 receipt=918 verdict=Match`；`isVerified=True`；`totalAnchored=1`；末行 **`READBACK PASSED (read-only)`**。

规则：

- 输出里打印的 `add --broadcast` 提示**只是 dry-run 的文案，绝不照抄执行**；今日全程无广播。
- fresh dry-run 的 `tx=0x` = **今天不产生任何交易**；与 **2026-10-08 那次已上链广播 `0x9125c198…97330`**（block 69239836）是两回事，口播分开讲。
- readback 是**纯链上历史报告查询**（验证 Oct-8 那次广播的锚定记录），**不是** live relay 原始收据比对器——那个比对属于 finalize replay，今日不做、不提混。
- `--evm-event-index 0` **照抄已成功执行的记录**（finalize.json 记录的触发约定）；它与该交易回执的全局 `logIndex 4` 是两个不同数值——**不从 logIndex 推导本参数，也不据此断言原始日志条数**。
- **剪辑纪律**：命令行、`MATCH`、`READBACK PASSED (read-only)`、`tx=0x (dry-run)` 输出**不得剪掉**；编译/查询等待可快剪但必须打「等待已缩短」字幕；**不伪造、不重演任何输出截图**；与历史哈希不一致时如实说「本地重编译产物哈希可能不同」，只引用已记录结论。

## 3. 数字卡（照屏可读，不口述哈希）

- 触发交易：`0x04249c263cdc64661141bc3a0911ddec2b2c05e57f79d21b5fd0f682c07d1785`（Settled @ block 69225090）；事件下标 `0`（触发数组下标，非全局 logIndex）
- 计费模型：`k3-256k` @ `api.kimi.com`；prompt 93 / cached 0 / completion 40；当块 Registry 价 cached 3,000,000 / input 6,000,000 / output 9,000,000 = **3 / 6 / 9 USDC 每百万 token**（即 3m / 6m / 9m micro-USDC per million tokens）；计费 =（0·cached + 93·input + 40·output）÷ 1e6 → **918 micro-USDC = 0.000918 USDC**；预算 1,500,000，退款 1,499,082
- 签名人 pin：`0x49CA3eADB3b8F23623f735918A6F2b945F60fb2D`（≠ 经济卖家）
- 历史报告交易：`0x9125c1983a1f50402f883521bcfd6b1f848051560a6f98c69fe13ce53ea97330`（block 69239836）；`records(5)` 918/918/Match、`isVerified(5)=true`、`totalAnchored=1` —— **均为 2026-10-08 录得验证值，不声称 fresh state**

## 4. 时间轴（**剪辑后成片目标 1:40–1:50**；原始执行可更长，彩排掐表；小窗全程常驻）

| 时间 | 画面/操作（中文指引） | cue |
| --- | --- | --- |
| 0:00–0:30 | **第一段**：静态卡「买家看到的中继账单 vs 谁来查？」→ `cre/settlement-audit/main.ts` 顶部 5 阶段注释轻扫 ≤5 秒（不算代码走读）→ 切终端 | 口播第 1 段 |
| 0:30–1:10 | **第二段**：贴入 §1 命令块执行：闸门 `GATE OK: HTTP 200` → dry run 输出逐条滚出（编译等待快剪+「等待已缩短」）；`MATCH` 与 `tx=0x (dry-run)` 行必须完整在屏 | 口播第 2 段；闸门/输出段静默，让观众读屏 |
| 1:10–1:50 | **第三段**：explorer 打开历史报告交易 `0x9125…97330`（**只看不发**）→ 回终端看 STEP 4 readback 尾部 `READBACK PASSED (read-only)` → 定格字幕卡 `mock forwarder · local simulation · not a production DON` | 口播第 3 段；结尾静止 2–3 秒 |

## 5. 英文口播（逐句照读；短句、常用词；共 126 词）

### 第 1 段（54 词；问题 + 工作流）

🎙 "In TokenShare, the buyer pays what the relay reports. Who checks that bill? We built the check with Chainlink CRE. The relay runs inside a TEE — a Trusted Execution Environment. The workflow starts when the escrow emits Settled. It reads the payment and price, pulls the receipt, checks the signature, and checks the math."

### 第 2 段（40 词；现场 dry run；闸门与输出滚屏时静默）

🎙 "Now a live dry run — no broadcast today. Payment five was a Kimi call: ninety-three prompt, zero cached, forty output. Bill: nine hundred eighteen micro USDC. The signature matches the pinned signer. Nine hundred eighteen versus nine hundred eighteen — MATCH."

### 第 3 段（32 词；历史广播 + 范围）

🎙 "One report went on-chain on October eighth. Today makes no new transaction. The readback is read-only: MATCH, verified. Scope, honestly: a local simulation on a testnet mock forwarder — not a production DON."

## 6. 词数与时长（重算）

- 合计 **126 词**（机算规则：仅 🎙 引号口播行 → 去引号 → 剔除独立 `—`/`+` 纯标点 token → 空白分词；`TEE` 记 1 词；无占位符）。分段：54 / 40 / 32。
- 100–120 wpm → 口播净时长 **1:03–1:16**；刻意停顿 + 屏上操作（闸门、编译等待、explorer）补足至 **1:40–1:50**——此为**剪辑后成片目标**：原始执行可能更长，彩排掐表；成片 ≤2:00，等待快剪并打「等待已缩短」字幕，不伪造输出。
- 超时削减顺序：① 删第 2 段 "Bill: nine hundred eighteen micro USDC."（−6 词 → 120）② 删第 3 段 "Today makes no new transaction."（−5 词 → 115）。
- 长哈希只上屏/字幕，**不口述**。

## 7. 口播必须讲清的限制（简短、常用词，散在 cue 与第 3 段）

- 结论范围 = **触发块历史价格算术 + 最后一张已签名收据的金额核对**（918 vs 918）。
- **不是**：真实 token 用量证明、累计结算审计、锁定报价有效性、生产 DON、TEE 机密执行证明。
- 定格字幕卡：`mock forwarder · local simulation · not a production DON`。**不声称 fresh 链上状态**——历史数值一律带 "October eighth"。

## 8. 屏幕隐私（录屏前+录制中持续检查）

隐藏：`.env`、`cre/secrets.yaml`、`cre.yaml` 登录段、私钥/API key、shell history、AI 对话上下文、通知中心。屏上只允许 `dummy-bearer-token` 占位与 `$VAR` 引用。**本稿与录制只在本地处理，不上传任何原始素材**；终审确认成片画面无凭据/密钥后，可自行选择**公开可访问的 HTTPS 托管**按投稿要求上传（如 YouTube / Loom）。**不承诺任何平台接受/过审**。

## 9. 失败处理（一律 STOP，不盲试）

| 情形 | 动作 |
| --- | --- |
| `~/.cre/bin/cre` 不存在 / 版本报错 | **停**；不现场安装，改期 |
| 登录/鉴权错误（whoami/login 后仍失败） | **停**；凭据问题不出镜不排查细节 |
| 闸门非 200（含 404、超时、无响应） | **停**；`exit 1` 已终止；无任何 fallback |
| simulate / readback RPC 失败、非 MATCH、readback 非 PASSED | **停**；不重发交易、不换参数盲试 |

STOP 后：粘贴**已脱敏**错误（抹掉任何 key/token/私钥片段）到工单/会话，改期再录。绝不为了录成而重试交易或伪造输出。

## 10. 范围备注

- 本稿唯一修改对象是本文件；`cre/README.md` 的过时 cwd 说法**保持原样未改**（超出本稿范围），录制以 §1 命令块为准。
- 命令与预期措辞出处：[../CRE_DEMO_EVIDENCE.md](../CRE_DEMO_EVIDENCE.md)（2026-10-08 完成记录）、[chainlink.md](../submission/chainlink.md)。
