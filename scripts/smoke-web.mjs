#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════
   TokenShare — web 静态冒烟（node ≥18 · 零依赖）

   用法:
     python3 -m http.server 8080 -d web   # 先起静态站
     node scripts/smoke-web.mjs           # 或: node scripts/smoke-web.mjs http://other:port

   检查项:
     1. 三页 fetch 200（/, /market.html, /console.html）
     2. 每页关键 DOM id 存在（grep html 源码）
     3. web/*.js 语法全过（node --check 子进程）
     4. config.js 关键键非空（registryAddr / escrowAddr / chainId）
     5. html 资源零外链（src=/href= 不含外部 http；出站 <a> 白名单豁免）
     6. CJK 用户串扫描 → 告警（TESTING.md 除外；注释命中可接受，仅提示）

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
    "tab-seller", "tab-buyer", "panel-seller", "panel-buyer",
    "s-endpoint", "s-submit", "b-seller", "b-lock-bal", "b-max",
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

/* ── 1+2. 三页 fetch 200 + 关键 DOM id ── */
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

/* ── 5. 零外链资源扫描（出站 <a> 白名单豁免）── */
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
