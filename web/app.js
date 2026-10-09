/* TOKENSHARE — landing page.
   Market PREVIEW from the on-chain Registry (config.js sellers →
   getListing, ≤2 cards); the full discovered/filtered market lives on
   market.html. Read-only: a JsonRpcProvider is enough; no wallet. */
"use strict";
(() => {
  const T = window.TS;
  const cfg = T.cfg;

  /* flag: JS active — gates the hidden start state of .rv reveals in CSS.
     without JS, .rv elements never hide and the page renders fully visible. */
  document.documentElement.classList.add("js");

  /* ── scroll reveal ───────────────────────────────────────── */
  const revealEls = document.querySelectorAll(".rv");
  if ("IntersectionObserver" in window) {
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
        }
      },
      { threshold: 0.12 }
    );
    revealEls.forEach((el) => io.observe(el));
  } else {
    revealEls.forEach((el) => el.classList.add("in"));
  }

  /* ── mobile nav toggle ───────────────────────────────────── */
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

  /* ── chapter scrollspy (landing only) ──────────────────────
     marks the visible chapter on the second-row strip with
     aria-current="location". chapter anchors only — the global
     nav (FULL MARKET & co.) is never scrollspy'd. */
  const chapternav = document.querySelector(".chapternav");
  if (chapternav) {
    const strip = chapternav.querySelector(".chapternav-in") || chapternav;
    const links = [...chapternav.querySelectorAll('a[href^="#"]')];
    const secs = links
      .map((a) => document.getElementById(a.getAttribute("href").slice(1)))
      .filter(Boolean);
    const topbar = document.querySelector(".topbar");
    const setActive = (id) => {
      for (const a of links) {
        const on = a.getAttribute("href") === `#${id}`;
        if (on) a.setAttribute("aria-current", "location");
        else a.removeAttribute("aria-current");
        /* keep the active chip in view inside the horizontally
           scrollable strip (mobile) — horizontal only, the page
           scroll position is never touched */
        if (on && strip.scrollWidth > strip.clientWidth) {
          const d = a.getBoundingClientRect().left - strip.getBoundingClientRect().left;
          if (d < 24) strip.scrollLeft += d - 24;
          else if (d + a.offsetWidth > strip.clientWidth - 24)
            strip.scrollLeft += d + a.offsetWidth - strip.clientWidth + 24;
        }
      }
    };
    let ticking = false;
    const update = () => {
      ticking = false;
      const line = (topbar ? topbar.offsetHeight : 0) + 24;
      let current = null;
      for (const s of secs) {
        if (s.getBoundingClientRect().top <= line) current = s.id;
      }
      /* the tail of the page belongs to the last chapter */
      if (secs.length && window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4) {
        current = secs[secs.length - 1].id;
      }
      setActive(current);
    };
    const onScroll = () => {
      if (!ticking) { ticking = true; requestAnimationFrame(update); }
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onScroll);
    update();
  }

  /* ── state machine hover tracing ─────────────────────────── */
  const sm = document.getElementById("smachine");
  if (sm) {
    const nodes = sm.querySelectorAll(".sm-node");
    const edges = sm.querySelectorAll(".edge");
    const focus = (name) => {
      sm.classList.add("focus");
      nodes.forEach((n) => n.classList.toggle("on", n.dataset.node === name));
      edges.forEach((e) => e.classList.toggle("on", (e.dataset.touch || "").split(" ").includes(name)));
    };
    const clear = () => {
      sm.classList.remove("focus");
      nodes.forEach((n) => n.classList.remove("on"));
      edges.forEach((e) => e.classList.remove("on"));
    };
    nodes.forEach((n) => {
      n.addEventListener("mouseenter", () => focus(n.dataset.node));
      n.addEventListener("mouseleave", clear);
      n.addEventListener("focus", () => focus(n.dataset.node));
      n.addEventListener("blur", clear);
    });
  }

  /* ── live market ─────────────────────────────────────────── */
  const listingsEl = document.getElementById("listings");
  const noticeEl = document.getElementById("mkt-notice");
  const refreshBtn = document.getElementById("mkt-refresh");
  const statListings = document.getElementById("stat-listings");
  const statActive = document.getElementById("stat-active");
  const statOnline = document.getElementById("stat-online");
  const statNet = document.getElementById("stat-net");

  const netLabel = `${(cfg.chainName || "chain").toUpperCase()} · ${cfg.chainId}`;
  if (statNet) statNet.textContent = netLabel;
  const hstatNet = document.getElementById("hstat-net");
  if (hstatNet) hstatNet.textContent = netLabel;

  const heroStats = {
    listings: document.getElementById("hstat-listings"),
    active: document.getElementById("hstat-active"),
    online: document.getElementById("hstat-online"),
  };

  function notice(html) {
    if (!noticeEl) return;
    noticeEl.hidden = !html;
    noticeEl.innerHTML = html || "";
  }

  /* ── market preview (M9: full market moved to market.html) ──
     Shared v2 board from common.js: model-chip picker + per-model price
     cells + chip-click delegation. This page renders a ≤2 card preview
     of the config.js sellers; discovery/filter/sort/paging live on the
     market subpage. */
  const board = listingsEl ? T.marketBoard(listingsEl) : null;

  /* refresh discipline (#3): the "reading…" skeleton is FIRST-LOAD ONLY —
     the 30s auto-refresh keeps the old cards on screen and swaps nodes
     in place once fresh data is in. Background tabs skip the tick; an
     in-flight guard stops interval/manual overlap. */
  let marketBusy = false;
  let marketLoaded = false;

  async function loadMarket() {
    if (!listingsEl || marketBusy) return;
    if (document.hidden) return; /* background tab — don't churn the DOM */
    marketBusy = true;
    try {
      if (!T.cfgReady()) {
        notice(
          `<b>config.js not configured</b> — after deployment, copy <code class="inl">escrowAddr / registryAddr / sellers</code> ` +
          `from <code class="inl">contracts/deployed.json</code> and the market reads the real chain.`
        );
        listingsEl.innerHTML = "";
        return;
      }
      /* sellers array is FALLBACK-ONLY since M10 (enumeration is primary);
         an empty array no longer blocks the preview */
      if (!board) return;

      notice("");
      if (!marketLoaded) {
        listingsEl.innerHTML =
          `<div class="ls-loading"><span class="txl-dot is-pending"></span> reading the Registry on-chain …</div>`;
      }

      /* M10: enumerate the seller set on-chain (sellerCount + getSellers);
         config.js sellers remain the fallback when the enumeration calls
         are unavailable (pre-v3 Registry / RPC hiccup) */
      let listings;
      try {
        listings = (await T.fetchMarketListings(T.readProvider())).listings;
      } catch (e) {
        if (!marketLoaded) listingsEl.innerHTML = "";
        notice(
          `<b>RPC unreachable</b> — failed to read ${T.esc(cfg.rpcUrl)} (network or RPC CORS).` +
          `<span class="dim">${T.esc(e.shortMessage || e.message || "")}</span>`
        );
        return;
      }

      const visible = listings.filter((l) => l.registered && !l.error);
      if (visible.length === 0) {
        if (!marketLoaded) listingsEl.innerHTML = "";
        notice(`no registered listing from Registry enumeration or config.js sellers — check that <code class="inl">registryAddr</code> points at the current Registry, that the RPC is reachable, or that a seller has registered.`);
        return;
      }

      /* preview cap — the full paginated/filterable market lives on
         market.html (entry panel sits right below the grid) */
      const preview = visible.slice(0, 2);
      const cards = board.render(preview); /* data in hand → swap in place */
      marketLoaded = true;

      /* stats count the full configured seller set, not the 2-card preview */
      const active = visible.filter((l) => l.active).length;
      if (statListings) statListings.textContent = String(visible.length);
      if (statActive) statActive.textContent = String(active);
      if (heroStats.listings) heroStats.listings.textContent = String(visible.length);
      if (heroStats.active) heroStats.active.textContent = String(active);
      if (statOnline) statOnline.textContent = "…";
      T.probeListings(cards); /* paints the preview dots (count below is full-set) */
      /* RELAYS ONLINE = the whole visible seller set, not the 2 preview
         cards — probeHealth is 30s-TTL-cached, so the rendered pair are
         cache hits and only the off-screen relays cost a fetch */
      const probes = await Promise.all(visible.map((l) => T.probeHealth(l.endpoint)));
      const onlineAll = probes.filter((r) => r.ok).length;
      if (statOnline) statOnline.textContent = `${onlineAll}/${visible.length}`;
      if (heroStats.online) heroStats.online.textContent = `${onlineAll}/${visible.length}`;
    } finally { marketBusy = false; }
  }

  if (refreshBtn) refreshBtn.addEventListener("click", loadMarket);
  T.installCopyHandlers();
  loadMarket();
  /* light auto-refresh keeps the demo table alive (skipped while hidden) */
  setInterval(loadMarket, 30000);
})();
