/* TOKENSHARE — full market subpage (M9).
   Seller DISCOVERY via Registry.Registered event scan (bounded depth,
   90-block windows under Monad's 100-block eth_getLogs cap), enriched
   per operator with getListing. Fallback: config.js sellers array.
   Read-only — JsonRpcProvider only, no wallet, no keys. */
"use strict";
(() => {
  const T = window.TS;
  const cfg = T.cfg;
  const $ = (id) => document.getElementById(id);

  document.documentElement.classList.add("js");

  /* ── dom ─────────────────────────────────────────────────── */
  const listingsEl = $("listings");
  const noticeEl = $("mkt-notice");
  const refreshBtn = $("mkt-refresh");
  const scanBar = $("scan-bar");
  const scanFill = $("scan-fill");
  const scanText = $("scan-text");
  const scanMeta = $("scan-meta");
  const scanMore = $("scan-more");
  const searchIn = $("f-search");
  const sortSel = $("f-sort");
  const countEl = $("f-count");
  const modelsEl = $("f-models");
  const pgPrev = $("pg-prev");
  const pgNext = $("pg-next");
  const pgInfo = $("pg-info");

  const board = listingsEl ? T.marketBoard(listingsEl) : null;
  const PAGE_SIZE = 12;

  const netLabel = `${(cfg.chainName || "chain").toUpperCase()} · ${cfg.chainId}`;
  const statNet = $("stat-net");
  if (statNet) statNet.textContent = netLabel;

  function notice(html, cls = "") {
    if (!noticeEl) return;
    noticeEl.hidden = !html;
    noticeEl.className = "mkt-notice" + (cls ? " " + cls : "");
    noticeEl.innerHTML = html || "";
  }

  /* ── state ───────────────────────────────────────────────── */
  const state = {
    listings: [],        // enriched listings (registered only), _regBlock attached
    scannedFrom: null,   // lowest block covered by the event scan
    scannedTo: null,
    source: null,        // "events" | "config"
    degraded: null,      // scan failure reason when falling back
    page: 1,
    modelFilter: null,   // null = all
    q: "",
    sort: "reg-desc",
    busy: false,
  };

  /* ── scan → enrich ───────────────────────────────────────── */
  function setScan(pct, text) {
    scanBar.hidden = false;
    scanBar.className = "scan-bar";
    if (scanFill) scanFill.style.width = `${Math.min(100, Math.max(0, pct))}%`;
    if (scanText) scanText.innerHTML = text;
  }

  async function enrich(operators) {
    /* sequential getListing per operator — polite to the RPC, one listing
       each (register re-creates, never duplicates) */
    const out = [];
    const prov = T.readProvider();
    for (const { operator, blockNumber } of operators) {
      try {
        const l = await T.fetchListing(prov, operator);
        if (l.registered) { l._regBlock = blockNumber; out.push(l); }
      } catch { /* unreadable operator — skip, event still counted */ }
    }
    return out;
  }

  /* ── session discovery cache ───────────────────────────────
     Full-history scan is expensive (~0.35s effective per 90-block
     window). Cache the OPERATOR SET per session (keyed by registry +
     deploy block); repeat visits only scan the delta since cache.to
     and always re-enrich via getListing (fresh active/prices). */
  const DISCOVERY_KEY =
    `tokenshare.market.discovery:${(cfg.registryAddr || "").toLowerCase()}:${cfg.registryFromBlock || 0}`;

  function readDiscoveryCache() {
    try {
      const c = JSON.parse(sessionStorage.getItem(DISCOVERY_KEY));
      if (c && Number.isFinite(Number(c.to)) && Array.isArray(c.operators) &&
          Number.isFinite(Number(c.from))) return c;
    } catch { /* corrupted / unavailable — ignore */ }
    return null;
  }
  function writeDiscoveryCache(c) {
    try { sessionStorage.setItem(DISCOVERY_KEY, JSON.stringify(c)); } catch { /* quota — ignore */ }
  }
  const mergeOps = (older, newer) => {
    const by = new Map(older.map((o) => [o.operator.toLowerCase(), o]));
    for (const o of newer) by.set(o.operator.toLowerCase(), o); /* newer blockNumber wins */
    return [...by.values()];
  };

  async function discover() {
    if (state.busy || !board) return;
    state.busy = true;
    refreshBtn.disabled = true;
    scanMore.hidden = true;
    setScan(0, `connecting ${T.esc(cfg.rpcUrl)} …`);

    let listings = [];
    try {
      const head = await T.readProvider().getBlockNumber();
      const depth = Number(cfg.scanDepthBlocks) || 50000;
      const cache = readDiscoveryCache();
      let operators, from, to;

      if (cache && cache.to === head) {
        /* nothing new on-chain — reuse the session's operator set */
        operators = cache.operators; from = cache.from; to = cache.to;
        setScan(100, `discovery cached for head ${T.fmtInt(head)} — refreshing listings via getListing …`);
      } else if (cache && cache.to < head && head - cache.to <= depth) {
        /* incremental: scan only the blocks since the cached pass */
        const res = await T.discoverRange({
          fromBlock: cache.to + 1,
          onProgress: (done, total, r) =>
            setScan((done / total) * 100,
              `incremental scan · window ${done}/${total} · blocks ${T.fmtInt(r.from)} → ${T.fmtInt(r.to)}`),
        });
        operators = mergeOps(cache.operators, res.operators);
        from = Math.min(cache.from, res.from); to = res.to;
        writeDiscoveryCache({ from, to, operators });
        setScan(100, `+${res.operators.length} new event(s) since ${T.fmtInt(cache.to)} — merged`);
      } else {
        /* cold start (or cache too stale): bounded depth scan */
        const res = await T.discoverRange({
          onProgress: (done, total, r) =>
            setScan((done / total) * 100,
              `scanning Registered events · window ${done}/${total} · blocks ${T.fmtInt(r.from)} → ${T.fmtInt(r.to)}`),
        });
        operators = res.operators; from = res.from; to = res.to;
        writeDiscoveryCache({ from, to, operators });
      }

      state.scannedFrom = from;
      state.scannedTo = to;
      state.source = "events";
      state.degraded = null;
      listings = await enrich(operators);
      scanBar.className = "scan-bar is-ok";
      if (scanFill) scanFill.style.width = "100%";
      if (scanText) scanText.innerHTML = `scan complete — ${operators.length} operator(s) · enriched via getListing`;
    } catch (e) {
      /* event scan failed (RPC down / limit churn) → config.js sellers */
      state.source = "config";
      state.degraded = (e && (e.shortMessage || e.message)) || "unknown";
      state.scannedFrom = state.scannedTo = null;
      scanBar.hidden = true;
      try {
        listings = (await T.fetchListings(T.readProvider()))
          .filter((l) => l.registered && !l.error);
      } catch { listings = []; }
    }

    state.listings = listings;
    state.page = 1;
    state.busy = false;
    refreshBtn.disabled = false;
    renderScanMeta();
    rebuild();
  }

  async function loadEarlier() {
    if (state.busy || state.source !== "events" || state.scannedFrom == null) return;
    if (Number(cfg.registryFromBlock) >= state.scannedFrom) return; // already at floor
    state.busy = true;
    scanMore.disabled = true;
    setScan(0, "extending scan backwards …");
    try {
      const res = await T.discoverRange({
        fromBlock: state.scannedFrom - 1,
        onProgress: (done, total, r) =>
          setScan((done / total) * 100,
            `scanning earlier blocks · window ${done}/${total} · blocks ${T.fmtInt(r.from)} → ${T.fmtInt(r.to)}`),
      });
      const known = new Set(state.listings.map((l) => l.operator.toLowerCase()));
      const fresh = res.operators.filter((o) => !known.has(o.operator.toLowerCase()));
      state.listings.push(...(await enrich(fresh)));
      state.scannedFrom = res.from;
      /* earlier chunks only ADD unseen operators (newer events already won) */
      const cache = readDiscoveryCache() || { from: res.from, to: state.scannedTo, operators: [] };
      cache.operators = mergeOps(res.operators, cache.operators);
      cache.from = res.from;
      writeDiscoveryCache(cache);
      setScan(100, `+${fresh.length} earlier operator(s) merged`);
      scanBar.className = "scan-bar is-ok";
    } catch (e) {
      setScan(0, `earlier scan failed — ${T.esc((e && (e.shortMessage || e.message)) || "unknown")}`);
      scanBar.className = "scan-bar is-bad";
    }
    scanMore.disabled = false;
    state.busy = false;
    renderScanMeta();
    rebuild();
  }

  function renderScanMeta() {
    if (!scanMeta) return;
    if (state.source === "config") {
      scanMeta.innerHTML =
        `<span class="bad">EVENT SCAN FAILED</span> ${T.esc(state.degraded || "")} — ` +
        `降级为 <code class="inl">config.js sellers</code>（${state.listings.length} 个）。`;
      scanMore.hidden = true;
      return;
    }
    const atFloor = state.scannedFrom != null && Number(cfg.registryFromBlock) >= state.scannedFrom;
    scanMeta.innerHTML =
      `<b>${state.listings.length}</b> operator(s) · Registered events scanned ` +
      `<span class="mono">${state.scannedFrom != null ? T.fmtInt(state.scannedFrom) : "—"} → ${state.scannedTo != null ? T.fmtInt(state.scannedTo) : "—"}</span>` +
      (atFloor
        ? ` <span class="ok">· full history (from deploy block)</span>`
        : ` · last ${T.fmtInt(Number(cfg.scanDepthBlocks) || 50000)} blocks — `) +
      (atFloor ? "" : `enriched via getListing`);
    scanMore.hidden = atFloor;
    scanMore.textContent = `[ LOAD EARLIER · −${T.fmtInt(Number(cfg.scanDepthBlocks) || 50000)} BLOCKS ]`;
  }

  /* ── view pipeline: filter → search → sort → paginate ────── */
  const firstPriceIn = (l) => {
    const p = T.priceFor(l, l.models[0]);
    return p ? Number(p.input) : null; /* display doubles are exact for 6dp prices */
  };

  const SORTERS = {
    "reg-desc": (a, b) => (b._regBlock || 0) - (a._regBlock || 0),
    "price-asc": (a, b) => (firstPriceIn(a) ?? Infinity) - (firstPriceIn(b) ?? Infinity),
    "price-desc": (a, b) => (firstPriceIn(b) ?? -Infinity) - (firstPriceIn(a) ?? -Infinity),
    "operator-asc": (a, b) => a.operator.localeCompare(b.operator),
    "operator-desc": (a, b) => b.operator.localeCompare(a.operator),
    "models-desc": (a, b) => b.models.length - a.models.length,
    "models-asc": (a, b) => a.models.length - b.models.length,
  };

  function viewListings() {
    let v = state.listings;
    if (state.modelFilter) v = v.filter((l) => l.models.includes(state.modelFilter));
    if (state.q) {
      const q = state.q.toLowerCase();
      v = v.filter((l) =>
        l.operator.toLowerCase().includes(q) ||
        l.models.some((m) => m.toLowerCase().includes(q)));
    }
    v = [...v].sort(SORTERS[state.sort] || SORTERS["reg-desc"]);
    return v;
  }

  function renderFilterChips() {
    if (!modelsEl) return;
    const union = new Map(); // model → count
    for (const l of state.listings) {
      for (const m of l.models) union.set(m, (union.get(m) || 0) + 1);
    }
    const models = [...union.keys()].sort((a, b) => a.localeCompare(b));
    if (!models.length) { modelsEl.innerHTML = ""; modelsEl.hidden = true; return; }
    modelsEl.hidden = false;
    const chip = (label, value, count, on) =>
      `<button type="button" class="fchip${on ? " on" : ""}" aria-pressed="${on}" data-model="${T.esc(value ?? "")}">` +
      `${T.esc(label)}${count != null ? ` <i>${count}</i>` : ""}</button>`;
    modelsEl.innerHTML =
      chip("ALL MODELS", "", state.listings.length, state.modelFilter === null) +
      models.map((m) => chip(m, m, union.get(m), state.modelFilter === m)).join("");
  }

  function rebuild() {
    renderFilterChips();
    const view = viewListings();
    const pages = Math.max(1, Math.ceil(view.length / PAGE_SIZE));
    if (state.page > pages) state.page = pages;
    const start = (state.page - 1) * PAGE_SIZE;
    const pageItems = view.slice(start, start + PAGE_SIZE);

    if (countEl) countEl.textContent = `${view.length} listing${view.length === 1 ? "" : "s"}`;

    if (!view.length) {
      listingsEl.innerHTML = "";
      notice(state.listings.length
        ? `筛选/搜索无匹配 — 当前条件 <code class="inl">${T.esc(state.q || state.modelFilter || "")}</code> 命中 0 条，放宽条件或 <button class="btn btn-sm" id="f-reset" type="button">[ RESET ]</button>。`
        : (state.source === "config"
          ? `事件扫描失败且 config sellers 无登记 listing — 检查 RPC / registryFromBlock。`
          : `扫描范围内无 Registered 事件 — 试试 <b>LOAD EARLIER</b>，或核对 <code class="inl">config.js registryFromBlock</code>。`),
        "net-warn");
      const rst = $("f-reset");
      if (rst) rst.addEventListener("click", () => {
        state.q = ""; state.modelFilter = null; searchIn.value = ""; rebuild();
      });
    } else {
      notice("");
      const cards = board.render(pageItems);
      T.probeListings(cards).catch(() => {});
    }

    /* pager */
    const from = view.length ? start + 1 : 0;
    const to = Math.min(start + PAGE_SIZE, view.length);
    if (pgInfo) pgInfo.textContent = `SHOWING ${from}–${to} OF ${view.length} · PAGE ${state.page}/${pages}`;
    if (pgPrev) pgPrev.disabled = state.page <= 1;
    if (pgNext) pgNext.disabled = state.page >= pages;
  }

  /* ── wiring ──────────────────────────────────────────────── */
  let qTimer = null;
  if (searchIn) searchIn.addEventListener("input", () => {
    clearTimeout(qTimer);
    qTimer = setTimeout(() => { state.q = searchIn.value.trim(); state.page = 1; rebuild(); }, 200);
  });
  if (sortSel) sortSel.addEventListener("change", () => { state.sort = sortSel.value; rebuild(); });
  if (modelsEl) modelsEl.addEventListener("click", (e) => {
    const chip = e.target.closest(".fchip");
    if (!chip) return;
    state.modelFilter = chip.dataset.model || null;
    state.page = 1;
    rebuild();
  });
  if (pgPrev) pgPrev.addEventListener("click", () => { state.page = Math.max(1, state.page - 1); rebuild(); });
  if (pgNext) pgNext.addEventListener("click", () => { state.page += 1; rebuild(); });
  if (scanMore) scanMore.addEventListener("click", () => loadEarlier());
  if (refreshBtn) refreshBtn.addEventListener("click", () => discover());

  /* ── boot ────────────────────────────────────────────────── */
  T.installCopyHandlers();

  /* staggered card reveal on load */
  document.querySelectorAll(".rv").forEach((el, i) => {
    el.style.setProperty("--d", `${i * 0.05}s`);
    requestAnimationFrame(() => el.classList.add("in"));
  });

  if (!T.cfgReady()) {
    notice(
      `<b>config.js 未配置</b> — 部署后从 <code class="inl">contracts/deployed.json</code> 填入 ` +
      `<code class="inl">escrowAddr / registryAddr / registryFromBlock</code>，市场子页即扫链发现 sellers。`
    );
    scanBar.hidden = true;
  } else {
    discover();
  }
})();
