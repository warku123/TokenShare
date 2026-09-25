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

  async function loadMarket() {
    if (!listingsEl) return;

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
    listingsEl.innerHTML =
      `<div class="ls-loading"><span class="txl-dot is-pending"></span> reading the Registry on-chain …</div>`;

    /* M10: enumerate the seller set on-chain (sellerCount + getSellers);
       config.js sellers remain the fallback when the enumeration calls
       are unavailable (pre-v3 Registry / RPC hiccup) */
    let listings;
    try {
      listings = (await T.fetchMarketListings(T.readProvider())).listings;
    } catch (e) {
      listingsEl.innerHTML = "";
      notice(
        `<b>RPC unreachable</b> — failed to read ${T.esc(cfg.rpcUrl)} (network or RPC CORS).` +
        `<span class="dim">${T.esc(e.shortMessage || e.message || "")}</span>`
      );
      return;
    }

    const visible = listings.filter((l) => l.registered && !l.error);
    if (visible.length === 0) {
      listingsEl.innerHTML = "";
      notice(`no registered listing from Registry enumeration or config.js sellers — check that <code class="inl">registryAddr</code> points at the current Registry, that the RPC is reachable, or that a seller has registered.`);
      return;
    }

    /* preview cap — the full paginated/filterable market lives on
       market.html (entry panel sits right below the grid) */
    const preview = visible.slice(0, 2);
    const cards = board.render(preview);

    /* stats count the full configured seller set, not the 2-card preview */
    const active = visible.filter((l) => l.active).length;
    if (statListings) statListings.textContent = String(visible.length);
    if (statActive) statActive.textContent = String(active);
    if (heroStats.listings) heroStats.listings.textContent = String(visible.length);
    if (heroStats.active) heroStats.active.textContent = String(active);
    if (statOnline) statOnline.textContent = "…";
    const online = await T.probeListings(cards);
    if (statOnline) statOnline.textContent = `${online}/${cards.length}`;
    if (heroStats.online) heroStats.online.textContent = `${online}/${cards.length}`;
  }

  if (refreshBtn) refreshBtn.addEventListener("click", loadMarket);
  T.installCopyHandlers();
  loadMarket();
  /* light auto-refresh keeps the demo table alive */
  setInterval(loadMarket, 30000);
})();
