/* TOKENSHARE — full market subpage (M10).
   Seller DISCOVERY via Registry v3 on-chain enumeration
   (sellerCount + getSellers, O(1) — no event scanning), enriched per
   operator with getListing. Fallback: config.js sellers array.
   Read-only — JsonRpcProvider only, no wallet, no keys. */
"use strict";
(() => {
  const T = window.TS;
  const cfg = T.cfg;
  const $ = (id) => document.getElementById(id);

  document.documentElement.classList.add("js");

  /* ── mobile nav toggle (same pattern as app.js) ── */
  const navToggle = document.querySelector(".nav-toggle");
  const topnav = document.getElementById("topnav");
  if (navToggle && topnav) {
    const setNav = (open) => {
      topnav.classList.toggle("open", open);
      navToggle.setAttribute("aria-expanded", String(open));
      navToggle.textContent = open ? "[ CLOSE ]" : "[ MENU ]";
    };
    navToggle.addEventListener("click", () => setNav(!topnav.classList.contains("open")));
    topnav.addEventListener("click", (e) => { if (e.target.closest("a")) setNav(false); });
  }

  /* ── dom ─────────────────────────────────────────────────── */
  const listingsEl = $("listings");
  const noticeEl = $("mkt-notice");
  const refreshBtn = $("mkt-refresh");
  const sourceEl = $("mkt-source");
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
    listings: [],        // enriched listings (registered only), _enumIndex attached
    source: null,        // "enum" | "config"
    degraded: null,      // enumeration failure reason when falling back
    page: 1,
    modelFilter: null,   // null = all
    q: "",
    sort: "reg-desc",
    busy: false,
  };

  /* ── skeleton: enumeration is a couple of eth_calls — near instant,
     but the grid should never sit empty while they resolve ── */
  function paintSkeleton() {
    if (!listingsEl) return;
    listingsEl.innerHTML = Array.from({ length: 6 }, (_, i) =>
      `<article class="card listing skel" aria-hidden="true">` +
      `<div class="skel-line" style="width:${34 + (i % 3) * 8}%"></div>` +
      `<div class="skel-line" style="width:${58 + (i % 2) * 10}%"></div>` +
      `<div class="skel-line" style="width:100%"></div>` +
      `<div class="skel-line" style="width:72%"></div>` +
      `</article>`).join("");
  }

  /* ── enumerate → enrich (REFRESH = same path, always fresh) ── */
  async function discover() {
    if (state.busy || !board) return;
    state.busy = true;
    if (refreshBtn) refreshBtn.disabled = true;
    paintSkeleton();
    if (countEl) countEl.textContent = "…";

    /* fetchMarketListings never throws: enumeration → getListing per
       seller; on enumeration failure it falls back to config.js sellers */
    const res = await T.fetchMarketListings(T.readProvider());
    state.listings = res.listings;
    state.source = res.source;
    state.degraded = res.degraded;
    state.page = 1;
    state.busy = false;
    if (refreshBtn) refreshBtn.disabled = false;
    renderSource();
    rebuild();
  }

  function renderSource() {
    if (!sourceEl) return;
    const n = state.listings.length;
    if (state.source === "config") {
      sourceEl.className = "mkt-source bad";
      sourceEl.title = T.esc(state.degraded || "");
      sourceEl.innerHTML =
        `couldn't read the Registry — showing ${n} seller${n === 1 ? "" : "s"} from config.js instead`;
      return;
    }
    sourceEl.className = "mkt-source";
    sourceEl.title = "Registry.sellerCount() + getSellers() enumeration · details via getListing";
    sourceEl.innerHTML =
      `<b>${n}</b> seller${n === 1 ? "" : "s"} · read directly from the Registry on-chain · prices live via getListing`;
  }

  /* ── view pipeline: filter → search → sort → paginate ────── */
  const firstPriceIn = (l) => {
    const p = T.priceFor(l, l.models[0]);
    return p ? Number(p.input) : null; /* display doubles are exact for 6dp prices */
  };

  /* _enumIndex = enumeration position (first-registration order,
     append-only) → desc = newest seller first */
  const SORTERS = {
    "reg-desc": (a, b) => (b._enumIndex ?? -1) - (a._enumIndex ?? -1),
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
        ? `no match for the current filter/search — <code class="inl">${T.esc(state.q || state.modelFilter || "")}</code> hit 0 listings; loosen the criteria or <button class="btn btn-sm" id="f-reset" type="button">[ RESET ]</button>.`
        : (state.source === "config"
          ? `on-chain read failed and the manually listed config.js sellers have no listing either — check the RPC / Registry address and retry.`
          : `Registry enumeration returned 0 sellers — confirm a seller has <code class="inl">register</code>ed, or check that <code class="inl">config.js registryAddr</code> points at the current Registry.`),
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
      `<b>config.js not configured</b> — after deployment, copy <code class="inl">escrowAddr / registryAddr</code> ` +
      `from <code class="inl">contracts/deployed.json</code> and the market page enumerates sellers from the Registry.`
    );
    if (sourceEl) sourceEl.hidden = true;
  } else {
    discover();
  }
})();
