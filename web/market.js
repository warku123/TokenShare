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
  /* setScan(pct, label, detail, title): label is the human line ("discovering
     sellers on-chain …"); detail is the faint technical suffix (windows);
     title carries the full engineering context on the strip's tooltip. */
  function setScan(pct, label, detail, title) {
    scanBar.hidden = false;
    scanBar.className = "scan-bar";
    scanBar.title = title || "";
    if (scanFill) scanFill.style.width = `${Math.min(100, Math.max(0, pct))}%`;
    if (scanText) {
      scanText.innerHTML =
        `<span class="scan-lbl">${label}</span>` +
        (detail ? `<span class="scan-detail">${detail}</span>` : "");
    }
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
    setScan(0, "connecting to the chain …", "", `${T.esc(cfg.rpcUrl)} · chainId ${cfg.chainId}`);

    let listings = [];
    try {
      const head = await T.readProvider().getBlockNumber();
      const depth = Number(cfg.scanDepthBlocks) || 50000;
      const cache = readDiscoveryCache();
      let operators, from, to;

      if (cache && cache.to === head) {
        /* nothing new on-chain — reuse the session's operator set */
        operators = cache.operators; from = cache.from; to = cache.to;
        setScan(100, "✓ sellers already discovered — refreshing live prices …",
          "this visit scans nothing new", `session cache hit at head ${T.fmtInt(head)}`);
      } else if (cache && cache.to < head && head - cache.to <= depth) {
        /* incremental: scan only the blocks since the cached pass */
        const res = await T.discoverRange({
          fromBlock: cache.to + 1,
          onProgress: (done, total, r) =>
            setScan((done / total) * 100,
              "checking for newly registered sellers …",
              `step ${done}/${total}`,
              `incremental Registered-event scan · ${T.fmtInt(r.from)} → ${T.fmtInt(r.to)} · 90-block windows`),
        });
        operators = mergeOps(cache.operators, res.operators);
        from = Math.min(cache.from, res.from); to = res.to;
        writeDiscoveryCache({ from, to, operators });
        setScan(100, "✓ up to date",
          res.operators.length ? `+${res.operators.length} new since block ${T.fmtInt(cache.to)}` : "no new sellers since your last visit",
          `incremental scan covered ${T.fmtInt(cache.to + 1)} → ${T.fmtInt(to)}`);
      } else {
        /* cold start (or cache too stale): bounded depth scan */
        const res = await T.discoverRange({
          onProgress: (done, total, r) =>
            setScan((done / total) * 100,
              "discovering sellers on-chain …",
              `step ${done}/${total}`,
              `Registry.Registered event scan · blocks ${T.fmtInt(r.from)} → ${T.fmtInt(r.to)} · 90-block windows (Monad caps eth_getLogs at 100)`),
        });
        operators = res.operators; from = res.from; to = res.to;
        writeDiscoveryCache({ from, to, operators });
      }

      state.scannedFrom = from;
      state.scannedTo = to;
      state.source = "events";
      state.degraded = null;
      listings = await enrich(operators);
      const n = operators.length;
      setScan(100, `✓ ${n} seller${n === 1 ? "" : "s"} found on-chain`,
        n ? "click a model chip to see its prices" : "none registered in the scanned range",
        `scanned blocks ${T.fmtInt(from)} → ${T.fmtInt(to)} · enriched via getListing`);
      scanBar.className = "scan-bar is-ok";
      if (scanFill) scanFill.style.width = "100%";
    } catch (e) {
      /* event scan failed (RPC down / limit churn) → config.js sellers */
      state.source = "config";
      state.degraded = (e && (e.shortMessage || e.message)) || "unknown";
      state.scannedFrom = state.scannedTo = null;
      setScan(0, "couldn't read the chain — showing configured sellers instead",
        "", T.esc(state.degraded));
      scanBar.className = "scan-bar is-bad";
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
    setScan(0, "looking further back in history …", "",
      `extending the Registered-event scan downwards from block ${state.scannedFrom}`);
    try {
      const res = await T.discoverRange({
        fromBlock: state.scannedFrom - 1,
        onProgress: (done, total, r) =>
          setScan((done / total) * 100,
            "looking further back in history …",
            `step ${done}/${total}`,
            `blocks ${T.fmtInt(r.from)} → ${T.fmtInt(r.to)} · 90-block windows`),
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
      setScan(100, fresh.length
        ? `✓ ${fresh.length} more seller${fresh.length === 1 ? "" : "s"} found`
        : "✓ no additional sellers in that range",
        "earlier history merged",
        `scanned ${T.fmtInt(res.from)} → ${T.fmtInt(res.to)}`);
      scanBar.className = "scan-bar is-ok";
    } catch (e) {
      setScan(0, "couldn't read the chain",
        "", T.esc((e && (e.shortMessage || e.message)) || "unknown"));
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
        `<span class="bad">on-chain discovery unavailable</span> — ${T.esc(state.degraded || "")}. ` +
        `显示 <code class="inl">config.js</code> 里手工登记的 ${state.listings.length} 个 seller。`;
      scanMore.hidden = true;
      return;
    }
    const atFloor = state.scannedFrom != null && Number(cfg.registryFromBlock) >= state.scannedFrom;
    const n = state.listings.length;
    scanMeta.innerHTML =
      `<b>${n}</b> seller${n === 1 ? "" : "s"} · discovered from the Registry's on-chain registration events` +
      ` <span class="dim mono">blocks ${state.scannedFrom != null ? T.fmtInt(state.scannedFrom) : "—"} → ${state.scannedTo != null ? T.fmtInt(state.scannedTo) : "—"}</span>` +
      (atFloor
        ? ` <span class="ok">· complete history</span>`
        : ` <span class="dim">· showing the most recent ${T.fmtInt(Number(cfg.scanDepthBlocks) || 50000)} blocks</span>`);
    scanMore.hidden = atFloor;
    scanMore.textContent = atFloor ? "" : `[ LOOK FURTHER BACK · −${T.fmtInt(Number(cfg.scanDepthBlocks) || 50000)} BLOCKS ]`;
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
          ? `链上读取失败，且 config.js 手工登记的 sellers 也无 listing — 检查 RPC 后重试。`
          : `这段链上历史里没有已登记的 seller — 试试 <b>LOOK FURTHER BACK</b> 再往前找，或核对 <code class="inl">config.js registryFromBlock</code>。`),
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
