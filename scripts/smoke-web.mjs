#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════
   TokenShare — web 静态冒烟（node ≥18 · 零依赖）

   用法:
     python3 -m http.server 8080 -d web   # 先起静态站
     node scripts/smoke-web.mjs           # 或: node scripts/smoke-web.mjs http://other:port

   检查项:
     1. 四页 fetch 200（/, /market.html, /console.html, /policy.html）
     2. 每页关键 DOM id 存在（grep html 源码）
     3. index/market/console 的 POLICY 导航 href="policy.html" 存在（includes 机制）
     4. web/*.js 语法全过（node --check 子进程）
     5. config.js 关键键非空（registryAddr / escrowAddr / chainId）
     6. html 资源零外链（src=/href= 不含外部 http；出站 <a> 白名单豁免）
     7. CJK 用户串扫描 → 告警（TESTING.md 除外；注释命中可接受，仅提示）
   8. policy consent gate 静态接线（#policy-consent/#policy-agree · guardPaid 六付费按钮 · POLICY_VERSION 一致 · 退出路径不拦）

   Exit code 语义:
     0 = 全部检查 PASS（WARN 允许）
     1 = 存在 FAIL（冒烟不通过）
     2 = 前置条件不满足（8080 服务不可达，未执行到实质检查）
   ═══════════════════════════════════════════════════════════ */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, dirname, relative, extname } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const WEB = join(ROOT, "web");
const BASE = (process.argv[2] || process.env.SMOKE_URL || "http://localhost:8080").replace(/\/$/, "");

/* ── 关键 DOM id（与 TESTING.md 功能面一一对应，改动页面时同步维护）── */
const REQUIRED_IDS = {
  "/index.html": [
    "topnav", "hstat-listings", "hstat-active", "hstat-online",
    "market", "listings", "mkt-refresh", "mechanism", "features",
  ],
  "/market.html": [
    "mkt-source", "f-search", "f-sort", "f-count", "f-models",
    "listings", "mkt-refresh", "pg-prev", "pg-info", "pg-next",
  ],
  "/console.html": [
    "wallet-connect", "wallet-info", "wallet-addr", "net-badge", "wallet-usdc",
    "card-account", "b-escrow-bal", "b-dep-amt", "b-dep-btn", "b-withdraw-amt", "b-withdraw-btn",
    "tab-seller", "tab-buyer", "panel-seller", "panel-buyer",
    "s-endpoint", "s-submit", "b-seller", "b-lock-bal", "b-max",
  ],
  /* policy.html 为静态说明页（无交互 DOM）；REQUIRED_IDS 仅锚定共享导航。
     注意勿把页面文案写进检查——结构 id 即可，文案改动不应导致冒烟误报。 */
  "/policy.html": [
    "topnav",
  ],
};
const PAGES = Object.keys(REQUIRED_IDS);

/* ── 结果收集 ── */
const results = [];
const rec = (name, status, detail = "") => {
  results.push({ name, status, detail });
  const tag = status === "PASS" ? "\x1b[32mPASS\x1b[0m" : status === "WARN" ? "\x1b[33mWARN\x1b[0m" : "\x1b[31mFAIL\x1b[0m";
  console.log(`[${tag}] ${name}${detail ? ` — ${detail}` : ""}`);
};

console.log(`\n== TokenShare web smoke == base=${BASE}\n`);

/* ── 1+2+3. 四页 fetch 200 + 关键 DOM id + POLICY 导航 href ── */
const pages = new Map(); // path -> html
for (const path of PAGES) {
  let html = null;
  try {
    const res = await fetch(BASE + path, { redirect: "follow" });
    if (res.ok) {
      html = await res.text();
      pages.set(path, html);
    }
    rec(`fetch ${path} → ${res.status}`, res.ok ? "PASS" : "FAIL", res.ok ? `${html.length} bytes` : res.statusText);
  } catch (e) {
    rec(`fetch ${path}`, "FAIL", `服务不可达: ${e.cause?.code || e.message}`);
  }
  if (html) {
    const missing = REQUIRED_IDS[path].filter((id) => !html.includes(`id="${id}"`));
    rec(`dom-ids ${path}`, missing.length ? "FAIL" : "PASS",
      missing.length ? `缺失: ${missing.join(", ")}` : `${REQUIRED_IDS[path].length} ids ok`);
  }
}

/* ── 3. 三个功能页的 POLICY 导航可达（复用 includes 机制，不查文案）── */
for (const path of ["/index.html", "/market.html", "/console.html"]) {
  const html = pages.get(path);
  if (!html) continue; // fetch 已 FAIL，不重复报
  rec(`policy-link ${path}`, html.includes('href="policy.html"') ? "PASS" : "FAIL",
    'href="policy.html"');
}

/* ── 3b. 两级导航回归：四页全局导航一致（顺序 OVERVIEW→FULL MARKET→CONSOLE→POLICY，
        每页恰好一个 aria-current="page" 且指向本页）；章节条仅 index 持有，
        app.js scrollspy 只标章节链接（aria-current="location"）── */
try {
  const GLOBAL_ORDER = ["index.html", "market.html", "console.html", "policy.html"];
  for (const path of PAGES) {
    const html = pages.get(path);
    if (!html) continue;
    const m = html.match(/<nav class="topnav" id="topnav"[^>]*>([\s\S]*?)<\/nav>/);
    if (!m) { rec(`nav-global ${path}`, "FAIL", "#topnav nav block missing"); continue; }
    const tags = [...m[1].matchAll(/<a ([^>]*)>/g)].map((x) => x[1]);
    const hrefs = tags.map((t) => (t.match(/href="([^"]*)"/) || [])[1]);
    const orderOk = GLOBAL_ORDER.every((h, i) => hrefs[i] === h);
    const curTags = tags.filter((t) => /aria-current="page"/.test(t));
    const curHref = curTags.length === 1 ? (curTags[0].match(/href="([^"]*)"/) || [])[1] : null;
    rec(`nav-global ${path}`, orderOk && curHref === path.slice(1) ? "PASS" : "FAIL",
      `order=${orderOk ? "ok" : hrefs.join("→")} · current=${curHref ?? `${curTags.length} markers`}`);
  }
  const idx = pages.get("/index.html");
  if (idx) {
    const ch = idx.match(/<nav class="chapternav"[^>]*>([\s\S]*?)<\/nav>/);
    const anchors = ch ? [...ch[1].matchAll(/href="#([a-z]+)"/g)].map((x) => x[1]) : [];
    rec("nav-chapters /index.html",
      !!ch && ["market", "mechanism", "state", "features"].every((a, i) => anchors[i] === a) ? "PASS" : "FAIL",
      ch ? `anchors: ${anchors.join(", ")}` : "chapter strip missing");
  }
  for (const path of ["/market.html", "/console.html", "/policy.html"]) {
    const html = pages.get(path);
    if (!html) continue;
    rec(`nav-chapters ${path} absent`, !html.includes('class="chapternav"') ? "PASS" : "FAIL",
      "chapter strip is landing-only");
  }
  const appJs = readFileSync(join(WEB, "app.js"), "utf8");
  rec("nav-scrollspy hook",
    appJs.includes('querySelector(".chapternav")') && appJs.includes('"location"') ? "PASS" : "FAIL",
    "app.js scrollspy sets aria-current=location on chapter links only");
} catch (e) {
  rec("nav-structure", "FAIL", `读取失败: ${e.message}`);
}

/* ── 3c. 浅色模式接线：prefers-color-scheme 系统跟随 + 暗色硬编码已 token 化 ── */
try {
  const css = readFileSync(join(WEB, "styles.css"), "utf8");
  rec("light-scheme block",
    /@media \(prefers-color-scheme: light\)/.test(css) && css.includes("color-scheme: light") && css.includes("color-scheme: dark") ? "PASS" : "FAIL",
    "prefers-color-scheme: light 媒体查询 + color-scheme 双值（原生控件/滚动条跟随）");
  /* 暗色字面值只允许出现在 token 定义行（--xxx: …），不得散落于规则体 */
  const litNames = ["#05070a", "#8fb8ff", "rgba(7, 9, 13", "rgba(4, 6, 9", "rgba(120, 150, 190"];
  const stray = [];
  css.split("\n").forEach((line, i) => {
    if (/^\s*--[\w-]+\s*:/.test(line)) return; // token 定义行豁免
    for (const s of litNames) if (line.includes(s)) stray.push(`L${i + 1}: ${s}`);
  });
  rec("dark hardcodes token-ized", stray.length === 0 ? "PASS" : "FAIL",
    stray.length ? `散落: ${stray.slice(0, 4).join("; ")}` : "term/topbar/overlay/grid/scanline/t-s 仅存活于 token 定义");
  rec("svg re-ink rules",
    css.includes("#smachine .edge path") && css.includes("#smachine marker path") ? "PASS" : "FAIL",
    "状态机边/箭头 CSS 覆盖（压过 index.html 内联表现属性）");
} catch (e) {
  rec("light-scheme static", "FAIL", `读取失败: ${e.message}`);
}
if (pages.size === 0) {
  console.log(`\n8080 服务不可达（先跑: python3 -m http.server 8080 -d web）→ exit 2`);
  process.exit(2);
}

/* ── 3. web/*.js 语法（node --check 子进程；vendor/ 第三方 minified 跳过）── */
const walkJs = (dir, acc = []) => {
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    if (e.name === "vendor") continue; // 第三方产物不进语法门
    const p = join(dir, e.name);
    if (e.isDirectory()) walkJs(p, acc);
    else if (extname(e.name) === ".js") acc.push(p);
  }
  return acc;
};
const jsFiles = walkJs(WEB);
let syntaxBad = 0;
for (const f of jsFiles) {
  const rel = relative(ROOT, f);
  const r = spawnSync(process.execPath, ["--check", f], { encoding: "utf8" });
  const ok = r.status === 0;
  if (!ok) syntaxBad++;
  rec(`syntax ${rel}`, ok ? "PASS" : "FAIL", ok ? "" : (r.stderr || "").trim().split("\n")[0]);
}
if (jsFiles.length === 0) rec("syntax web/*.js", "FAIL", "未找到任何 .js 文件");

/* ── 4. config.js 关键键非空 ── */
try {
  const cfg = readFileSync(join(WEB, "config.js"), "utf8");
  const get = (k) => {
    const m = cfg.match(new RegExp(`${k}\\s*:\\s*("([^"]*)"|'([^']*)'|([\\d.]+))`));
    return m ? (m[2] ?? m[3] ?? m[4] ?? "") : undefined;
  };
  const need = ["registryAddr", "escrowAddr", "chainId"];
  const bad = need.filter((k) => { const v = get(k); return v === undefined || v === ""; });
  rec("config.js 关键键", bad.length ? "FAIL" : "PASS",
    bad.length ? `缺失/为空: ${bad.join(", ")}` : `${need.map((k) => `${k}=${get(k)}`).join(" · ")}`);
} catch (e) {
  rec("config.js 关键键", "FAIL", `读取失败: ${e.message}`);
}

/* ── 5. policy consent gate 静态接线（checkbox id / guard 接线 / POLICY_VERSION 一致）── */
try {
  const commonJs = readFileSync(join(WEB, "common.js"), "utf8");
  const consoleJs = readFileSync(join(WEB, "console.js"), "utf8");
  const consoleHtml = pages.get("/console.html") || "";
  const policyHtml = pages.get("/policy.html") || "";
  const VER = "2026-10-08";
  const c = (name, ok, detail = "") => rec(`consent ${name}`, ok ? "PASS" : "FAIL", detail);
  c("consent-bar-id", consoleHtml.includes('id="policy-consent"') && consoleHtml.includes('id="policy-agree"'),
    "console.html #policy-consent + #policy-agree");
  c("consent-links-policy", consoleHtml.includes('href="policy.html" target="_blank"'),
    "consent label → policy.html（新窗口）");
  c("policy-version-pinned", commonJs.includes(`POLICY_VERSION = "${VER}"`), `web/common.js POLICY_VERSION = ${VER}`);
  c("policy-page-version", policyHtml.includes(VER), `policy.html 页首/页脚标注 ${VER}`);
  c("session-storage-record", commonJs.includes('"tokenshare.policyConsent.v1"'),
    'sessionStorage 键 tokenshare.policyConsent.v1 → {"version","wallets":{0x…:ts}}（per-wallet）');
  c("consent-disconnect-revokes",
    commonJs.includes("revoke(wallet)") && consoleJs.includes("clearConnState({ revokeConsent: true })") &&
    consoleJs.includes("policyConsent.revoke(gone)"),
    "DISCONNECT/钱包内断开 → revoke 出站钱包 consent（重连需重 tick）");
  c("consent-switch-preserves",
    consoleJs.includes("clearConnState({ revokeConsent: !(accs && accs.length) })") &&
    commonJs.includes("rec.wallets[") && !consoleJs.includes("T.policyConsent.clear()"),
    "账号切换不清 consent —— per-wallet 记录保留（A→B→A 仍有效）");
  c("guard-throws", consoleJs.includes("class PolicyConsentRequired") && consoleJs.includes("requirePolicyConsent()"),
    "paid handler 入口抛 PolicyConsentRequired");
  const gated = ["s-submit", "s-verify-btn", "sc-authorize", "b-dep-btn", "b-lock-btn", "c-send"];
  const missing = gated.filter((id) => !consoleJs.includes(`guardPaid($("${id}")`));
  c("paid-guard-wiring", missing.length === 0,
    missing.length ? `缺 guardPaid 接线: ${missing.join(", ")}` : gated.map((id) => `#${id}`).join(" "));
  c("exit-paths-open",
    consoleJs.includes(`guard($("r-btn")`) && consoleJs.includes(`guard($("b-withdraw-btn")`) &&
    consoleJs.includes(`guard($("sc-revoke-delegate")`) && consoleJs.includes(`guard($("sc-revoke-key")`) &&
    !/"sc-revoke-delegate", "sc-revoke-key"/.test(consoleJs),
    "refund/withdraw/custody revokes 仍为普通 guard（退出路径不拦）");
} catch (e) {
  rec("consent static-wiring", "FAIL", `读取失败: ${e.message}`);
}

/* ── 5b. 共享 ACCOUNT 面板结构：在 .tabs 之前（两角色可见），buyer 面板不再携带资金入口 ── */
{
  const con = pages.get("/console.html");
  if (con) {
    const iAcct = con.indexOf('id="card-account"');
    const iTabs = con.indexOf('class="tabs"');
    const iBuyer = con.indexOf('id="panel-buyer"');
    rec("console shared account panel",
      iAcct > -1 && iTabs > -1 && iAcct < iTabs && iTabs < iBuyer ? "PASS" : "FAIL",
      "card-account 位于 .tabs 之前 · panel-buyer 之外");
    const buyer = iBuyer > -1 ? con.slice(iBuyer) : "";
    const dup = ['id="b-dep-btn"', 'id="b-withdraw-btn"', 'id="b-escrow-bal"'].filter((s) => buyer.includes(s));
    rec("console buyer funds removed", dup.length === 0 ? "PASS" : "FAIL",
      dup.length ? `panel-buyer 仍含: ${dup.join(", ")}` : "buyer 面板无重复 deposit/withdraw/balance 入口");
  }
}

/* ── 6. 零外链资源扫描（出站 <a> 白名单豁免）── */
const TAG_RE = /<([a-zA-Z][a-zA-Z0-9]*)((?:[^>"']|"[^"]*"|'[^']*')*)>/g;
const ATTR_RE = /\b(src|href)\s*=\s*(?:"([^"]*)"|'([^']*)')/gi;
const isLocal = (u) =>
  !/^https?:\/\//i.test(u) || /^https?:\/\/(localhost|127\.0\.0\.1|\[::1\])(:\d+)?/i.test(u);
for (const [path, html] of pages) {
  const bad = [];
  let anchors = 0;
  for (const m of html.matchAll(TAG_RE)) {
    const tag = m[1].toLowerCase();
    for (const a of m[2].matchAll(ATTR_RE)) {
      const val = a[2] ?? a[3] ?? "";
      if (!val || val.startsWith("data:") || val.startsWith("#") || val.startsWith("mailto:")) continue;
      if (isLocal(val)) continue;
      if (tag === "a") { anchors++; continue; } // 出站导航链接豁免
      bad.push(`${tag} ${a[1]}="${val}"`);
    }
  }
  rec(`zero-external ${path}`, bad.length ? "FAIL" : "PASS",
    bad.length ? `外部资源: ${bad.slice(0, 3).join("; ")}${bad.length > 3 ? " …" : ""}`
               : `0 外部资源（${anchors} 个出站 <a> 已豁免）`);
}

/* ── 6. CJK 用户串扫描（告警级；TESTING.md 不在扫描面，注释命中可接受）── */
const CJK = /[一-鿿]/;
for (const f of jsFiles.concat(PAGES.map((p) => join(WEB, p.slice(1))))) {
  const rel = relative(ROOT, f);
  const lines = readFileSync(f, "utf8").split("\n");
  const hits = lines.map((l, i) => (CJK.test(l) ? i + 1 : 0)).filter(Boolean);
  rec(`cjk-scan ${rel}`, hits.length ? "WARN" : "PASS",
    hits.length ? `${hits.length} 行含中文（行 ${hits.slice(0, 8).join(",")}${hits.length > 8 ? "…" : ""}）— 须人工确认均为代码注释` : "0 命中");
}

/* ── 汇总 + exit 语义 ── */
const fails = results.filter((r) => r.status === "FAIL").length;
const warns = results.filter((r) => r.status === "WARN").length;
console.log(`\n== 汇总: ${results.length} 项 · PASS ${results.length - fails - warns} · WARN ${warns} · FAIL ${fails} ==`);
console.log(fails ? "exit 1 — 冒烟不通过" : warns ? "exit 0 — 冒烟通过（含告警）" : "exit 0 — 全绿");
process.exit(fails ? 1 : 0);
