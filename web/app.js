/* TOKENSHARE — market prototype
   static demo logic: mock listings, settle ticker, scroll reveal,
   state-machine hover tracing. no network, no wallet, no chain. */
"use strict";

/* flag: JS active — gates the hidden start state of .rv reveals in CSS.
   without JS, .rv elements never hide and the page renders fully visible. */
document.documentElement.classList.add("js");

/* ── mock Registry state ────────────────────────────────────
   prices: USDC per 1M tokens · units: 6-decimal native integer */
const LISTINGS = [
  {
    operator: "0x7A3f…9c2E",
    endpoint: "https://relay.sentient-grid.dev/v1",
    chain: "base",
    models: ["gpt-4o", "gpt-4o-mini"],
    cached: 0.30, input: 1.20, output: 4.80,
    active: true, latencyMs: 240, calls24h: 1204,
  },
  {
    operator: "0xB91c…04FA",
    endpoint: "https://gw.northstack.io/v1",
    chain: "monad",
    models: ["gpt-4.1", "gpt-4.1-mini"],
    cached: 0.40, input: 1.60, output: 6.40,
    active: true, latencyMs: 310, calls24h: 862,
  },
  {
    operator: "0x4De8…77b1",
    endpoint: "https://edge-03.loworbit.net/v1",
    chain: "monad",
    models: ["gpt-4o-mini", "o4-mini"],
    cached: 0.08, input: 0.35, output: 1.40,
    active: true, latencyMs: 195, calls24h: 2317,
  },
  {
    operator: "0xE0a2…3D99",
    endpoint: "https://monad-relay.pinnacle.sh/v1",
    chain: "monad",
    models: ["gpt-4o-mini"],
    cached: 0.02, input: 0.10, output: 0.40,
    active: true, latencyMs: 410, calls24h: 5908,
  },
  {
    operator: "0x88bB…2f07",
    endpoint: "https://relay.dormant-capital.xyz/v1",
    chain: "base",
    models: ["gpt-3.5-turbo"],
    cached: 0.05, input: 0.20, output: 0.80,
    active: false, latencyMs: null, calls24h: 0,
  },
];

/* recent fake settlements for the ticker */
const SETTLES = [
  { amt: "0.045552", model: "gpt-4o",      ago: 8  },
  { amt: "0.002180", model: "gpt-4o-mini", ago: 23 },
  { amt: "0.011904", model: "o4-mini",     ago: 41 },
  { amt: "0.128760", model: "gpt-4.1",     ago: 66 },
  { amt: "0.000312", model: "gpt-4o-mini", ago: 89 },
];

/* ── render listings ─────────────────────────────────────── */
const CHAIN = {
  base:  { label: "BASE SEPOLIA",  cls: "chip-base"  },
  monad: { label: "MONAD TESTNET", cls: "chip-monad" },
};

function priceCell(tier, usd) {
  const units = Math.round(usd * 1e6);
  return (
    `<div class="ls-price">` +
    `<span class="tier">${tier}</span>` +
    `<span class="usd">$${usd.toFixed(2)}<b> /1M</b></span>` +
    `<span class="units">${units.toLocaleString("en-US")} units</span>` +
    `</div>`
  );
}

function listingCard(l) {
  const chain = CHAIN[l.chain];
  const badge = l.active
    ? `<span class="badge">ACTIVE</span>`
    : `<span class="badge off">INACTIVE</span>`;
  const meta = l.active
    ? `<span>~${l.latencyMs}ms · ${l.calls24h.toLocaleString("en-US")} calls/24h</span>`
    : `<span>deactivated by operator</span>`;

  return (
    `<article class="card listing rv${l.active ? "" : " inactive"}">` +
      `<div class="ls-top"><span class="ls-addr">${l.operator}</span>${badge}</div>` +
      `<p class="ls-endpoint" title="${l.endpoint}">${l.endpoint}</p>` +
      `<div class="ls-models">${l.models.map((m) => `<span class="mtag">${m}</span>`).join("")}</div>` +
      `<div class="ls-prices">` +
        priceCell("CACHED IN", l.cached) +
        priceCell("INPUT", l.input) +
        priceCell("OUTPUT", l.output) +
      `</div>` +
      `<div class="ls-meta">${meta}<span class="chip ${chain.cls}">${chain.label}</span></div>` +
    `</article>`
  );
}

const listingsEl = document.getElementById("listings");
if (listingsEl) {
  listingsEl.innerHTML = LISTINGS.map(listingCard).join("");
}

/* ── settle ticker ───────────────────────────────────────── */
const tickerText = document.getElementById("ticker-text");
if (tickerText) {
  let i = 0;
  const renderTick = () => {
    const s = SETTLES[i % SETTLES.length];
    const ago = s.ago + Math.floor((Date.now() / 1000) % 3);
    tickerText.textContent = `${s.amt} USDC · ${s.model} · ${ago}s ago`;
    i += 1;
  };
  renderTick();
  setInterval(renderTick, 3000);
}

/* ── scroll reveal ───────────────────────────────────────── */
const revealEls = document.querySelectorAll(".rv");
if ("IntersectionObserver" in window) {
  const io = new IntersectionObserver(
    (entries) => {
      for (const e of entries) {
        if (e.isIntersecting) {
          e.target.classList.add("in");
          io.unobserve(e.target);
        }
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
  navToggle.addEventListener("click", () =>
    setNav(!topnav.classList.contains("open"))
  );
  topnav.addEventListener("click", (e) => {
    if (e.target.closest("a")) setNav(false);
  });
}

/* ── state machine hover tracing ─────────────────────────── */
const sm = document.getElementById("smachine");
if (sm) {
  const nodes = sm.querySelectorAll(".sm-node");
  const edges = sm.querySelectorAll(".edge");

  const focus = (name) => {
    sm.classList.add("focus");
    nodes.forEach((n) => n.classList.toggle("on", n.dataset.node === name));
    edges.forEach((e) =>
      e.classList.toggle("on", (e.dataset.touch || "").split(" ").includes(name))
    );
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
