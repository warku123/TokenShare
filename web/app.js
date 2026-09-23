/* TOKENSHARE — market page.
   Live listings from the on-chain Registry (config.js sellers
   array → getListing each) + relay /health probes. Read-only:
   a JsonRpcProvider is enough; no wallet, no keys. */
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

  function priceCell(tier, native) {
    return (
      `<div class="ls-price">` +
      `<span class="tier">${tier}</span>` +
      `<span class="usd">$${T.fmtUsdc(native)}<b> /1M</b></span>` +
      `<span class="units">${T.fmtInt(native)} units · 6dp</span>` +
      `</div>`
    );
  }

  function listingCard(l) {
    const badge = l.active
      ? `<span class="badge">ACTIVE</span>`
      : `<span class="badge off">INACTIVE</span>`;
    const models = l.models.length
      ? l.models.map((m) => `<span class="mtag">${T.esc(m)}</span>`).join("")
      : `<span class="mtag">—</span>`;
    return (
      `<article class="card listing rv in${l.active ? "" : " inactive"}" data-endpoint="${T.esc(l.endpoint)}">` +
        `<div class="ls-top">` +
          `<a class="ls-addr" data-copy="${T.esc(l.operator)}" href="${T.addrLink(l.operator)}" target="_blank" rel="noopener" title="${T.esc(l.operator)} — click copies address">${T.truncAddr(l.operator)}</a>` +
          `<span class="ls-health" title="relay /health probe pending"><span class="hdot"></span><span class="ls-health-lbl">probing</span></span>` +
          badge +
        `</div>` +
        `<p class="ls-endpoint" title="${T.esc(l.endpoint)}">${T.esc(T.hostOf(l.endpoint))}</p>` +
        `<div class="ls-models">${models}</div>` +
        `<div class="ls-prices">` +
          priceCell("CACHED IN", l.priceCachedIn) +
          priceCell("INPUT", l.priceInput) +
          priceCell("OUTPUT", l.priceOutput) +
        `</div>` +
        `<div class="ls-meta">` +
          `<span>operator <a href="${T.addrLink(l.operator)}" target="_blank" rel="noopener" class="ls-link">explorer ↗</a></span>` +
          `<span class="chip chip-monad">${T.esc((cfg.chainName || "MONAD").toUpperCase())} · ${cfg.chainId}</span>` +
        `</div>` +
      `</article>`
    );
  }

  async function probeAll(cards) {
    let online = 0;
    await Promise.all(cards.map(async ({ el, endpoint }) => {
      const slot = el.querySelector(".ls-health");
      const r = await T.probeHealth(endpoint);
      const dot = slot.querySelector(".hdot");
      const lbl = slot.querySelector(".ls-health-lbl");
      if (r.ok) {
        online += 1;
        dot.classList.add("ok");
        lbl.textContent = `${r.ms}ms`;
        slot.title = `relay /health OK · ${r.ms}ms`;
        /* M7 touchpoint: TEE / upstream-policy badges via GET /info.
           Silent degrade — unreachable or non-JSON → no badge, no throw. */
        const info = await T.probeInfo(endpoint);
        if (info) {
          const top = el.querySelector(".ls-top");
          if (info.teeEnabled) {
            const a = document.createElement("a");
            a.className = "badge tee";
            a.href = T.joinUrl(endpoint, "/attestation");
            a.target = "_blank";
            a.rel = "noopener";
            a.title = "TEE attested relay — view /attestation quote (derived key, reportData, quoteDigest)";
            a.textContent = "TEE";
            top.insertBefore(a, top.querySelector(".badge"));
          }
          if (info.upstreamHost) {
            const meta = el.querySelector(".ls-meta span");
            meta.innerHTML =
              `upstream <b class="${info.official ? "ok" : "bad"}">${T.esc(info.upstreamHost)}${info.official ? " · official" : " · CUSTOM"}</b> · ` +
              meta.innerHTML;
          }
        }
      } else {
        dot.classList.add("off");
        lbl.textContent = "offline";
        slot.title = "relay unreachable — or CORS not enabled yet (等待 relay CORS 配置)";
      }
    }));
    return online;
  }

  async function loadMarket() {
    if (!listingsEl) return;

    if (!T.cfgReady()) {
      notice(
        `<b>config.js 未配置</b> — 部署后从 <code class="inl">contracts/deployed.json</code> 填入 ` +
        `<code class="inl">escrowAddr / registryAddr / sellers</code>，市场页即读真链。`
      );
      listingsEl.innerHTML = "";
      return;
    }
    if (!cfg.sellers || cfg.sellers.length === 0) {
      notice(`<b>sellers 数组为空</b> — 在 <code class="inl">web/config.js</code> 登记 seller operator 地址后展示 listings。`);
      listingsEl.innerHTML = "";
      return;
    }

    notice("");
    listingsEl.innerHTML =
      `<div class="ls-loading"><span class="txl-dot is-pending"></span> reading Registry.getListing on-chain …</div>`;

    let listings;
    try {
      listings = await T.fetchListings(T.readProvider());
    } catch (e) {
      listingsEl.innerHTML = "";
      notice(
        `<b>RPC 不可达</b> — ${T.esc(cfg.rpcUrl)} 读取失败（网络或 RPC CORS）。` +
        `<span class="dim">${T.esc(e.shortMessage || e.message || "")}</span>`
      );
      return;
    }

    const visible = listings.filter((l) => l.registered && !l.error);
    if (visible.length === 0) {
      listingsEl.innerHTML = "";
      notice(`配置的 ${listings.length} 个 seller 均未在 Registry 登记（或读取失败）。`);
      return;
    }

    listingsEl.innerHTML = visible.map(listingCard).join("");
    const cards = visible.map((l) => ({
      el: [...listingsEl.children].find((c) => c.dataset.endpoint === l.endpoint),
      endpoint: l.endpoint,
    })).filter((c) => c.el);

    /* stats */
    const active = visible.filter((l) => l.active).length;
    if (statListings) statListings.textContent = String(visible.length);
    if (statActive) statActive.textContent = String(active);
    if (heroStats.listings) heroStats.listings.textContent = String(visible.length);
    if (heroStats.active) heroStats.active.textContent = String(active);
    if (statOnline) statOnline.textContent = "…";
    const online = await probeAll(cards);
    if (statOnline) statOnline.textContent = `${online}/${cards.length}`;
    if (heroStats.online) heroStats.online.textContent = `${online}/${cards.length}`;
  }

  if (refreshBtn) refreshBtn.addEventListener("click", loadMarket);
  T.installCopyHandlers();
  loadMarket();
  /* light auto-refresh keeps the demo table alive */
  setInterval(loadMarket, 30000);
})();
