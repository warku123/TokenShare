/* ═══════════════════════════════════════════════════════════
   TOKENSHARE — shared browser library (market + console).

   Contains the PIN-exact signing / receipt logic. The message
   formats below are byte-for-byte pinned by the interface
   contract (.slim/deepwork/m3-m5-e2e.md 「接口契约 PIN」) and
   must not drift from cli/tokenshare_cli/signing.py|receipt.py.

   Requires: window.TS_CONFIG (config.js) + window.ethers
   (vendor/ethers.umd.min.js, v6 UMD).
   ═══════════════════════════════════════════════════════════ */
"use strict";

window.TS = (() => {
  const cfg = window.TS_CONFIG || {};

  /* ── config state ────────────────────────────────────────── */
  const cfgReady = () =>
    Boolean(cfg.rpcUrl && cfg.escrowAddr && cfg.registryAddr && cfg.usdcAddr);

  const chainIdHex = () => "0x" + Number(cfg.chainId).toString(16);

  /* ── ABIs (verbatim from cli/tokenshare_cli/abis.py +
        contracts/src function signatures) ────────────────── */
  const ESCROW_ABI = [
    "function balances(address) view returns (uint256)",
    "function deposit(uint256 amount)",
    "function getPayment(uint256 paymentId) view returns (address buyer, address seller, uint256 maxAmount, uint64 expiresAt, uint8 state)",
    "function isValid(uint256 paymentId, address seller, uint256 minAmount) view returns (bool)",
    "function lock(address seller, uint256 maxAmount, uint64 ttl) returns (uint256 paymentId)",
    "function nextPaymentId() view returns (uint256)",
    "function refund(uint256 paymentId)",
    "function withdraw(uint256 amount)",
    "event Locked(uint256 indexed paymentId, address indexed buyer, address indexed seller, uint256 maxAmount, uint64 expiresAt)",
    "event Refunded(uint256 indexed paymentId, address indexed buyer, uint256 amount, address indexed caller)",
    /* custom errors (contracts/src/Escrow.sol) — declared so ethers
       decodes reverts into e.revert {name, args} for human-readable
       UI copy (was: "execution reverted (unknown custom error)") */
    "error ZeroAmount()",
    "error InvalidSeller()",
    "error SelfLock()",
    "error InsufficientBalance(uint256 requested, uint256 available)",
    "error NotLocked(uint256 paymentId, uint8 state)",
    "error NotSeller(address caller, address seller)",
    "error ExceedsMaxAmount(uint256 actual, uint256 maxAmount)",
    "error TtlNotElapsed(uint256 paymentId, uint64 expiresAt)",
  ];

  /* Registry v2 (M9, contracts/src/Registry.sol): per-model pricing.
     Price = tuple(uint256 cachedIn, uint256 input, uint256 output), 6dp
     native units per 1M tokens. getListing returns ONE Listing struct
     (single tuple output — the wire encoding wraps the fields in an extra
     offset, so the ABI must declare the nested tuple, not flat fields).
     register takes the FULL parallel Price[] in one tx (atomic); per-model
     price changes go through updateModelPrice(model, price) afterwards. */
  const REGISTRY_ABI = [
    "function getListing(address operator) view returns (tuple(address operator, string endpoint, string[] models, tuple(uint256 cachedIn, uint256 input, uint256 output)[] prices, bool active) listing)",
    "function getPrice(address operator, string model) view returns (tuple(uint256 cachedIn, uint256 input, uint256 output) price)",
    "function register(string endpoint, string[] models, tuple(uint256 cachedIn, uint256 input, uint256 output)[] prices)",
    "function updateModelPrice(string model, tuple(uint256 cachedIn, uint256 input, uint256 output) price)",
    "function deactivate()",
    /* M10 v3 enumeration (v2 face unchanged, additions only):
       append-only seller set — O(1) discovery for market/CLI */
    "function sellerCount() view returns (uint256)",
    "function getSellers(uint256 start, uint256 count) view returns (address[] sellers)",
    /* M12 v4 (additions only, v3 face untouched): per-model delist —
       swap-and-pop removes model+price at the same index of the parallel
       arrays; the LAST model is guarded (RemoveLastModel → use deactivate);
       NO active requirement (an inactive listing may prune models too) */
    "function removeModel(string model)",
    "event Registered(address indexed operator, string endpoint, string[] models)",
    "event PriceUpdated(address indexed operator, string model, uint256 cachedIn, uint256 input, uint256 output)",
    "event Deactivated(address indexed operator)",
    "event ModelRemoved(address indexed operator, string model)",
    /* custom errors (contracts/src/Registry.sol) — declared so ethers
       decodes reverts into e.revert.name for human-readable UI copy */
    "error AlreadyRegistered()",
    "error NotActive()",
    "error ModelNotFound()",
    "error EmptyModels()",
    "error LengthMismatch()",
    "error RemoveLastModel()",
  ];

  const ERC20_ABI = [
    "function allowance(address owner, address spender) view returns (uint256)",
    "function approve(address spender, uint256 value) returns (bool)",
    "function balanceOf(address account) view returns (uint256)",
  ];

  /* Escrow.State enum order (contracts/src/Escrow.sol) */
  const PAYMENT_STATES = ["None", "Locked", "Settled", "Refunded"];

  /* Relay/CLI per-request token caps, PIN defaults
     (PROMPT_TOKEN_CAP=200000, COMPLETION_TOKEN_CAP=32000) — used for
     the buyer-side minAmount estimate hint. */
  const PROMPT_TOKEN_CAP = 200000n;
  const COMPLETION_TOKEN_CAP = 32000n;

  /* ── providers / contracts ───────────────────────────────── */
  let _readProvider = null;
  function readProvider() {
    if (!_readProvider) {
      _readProvider = new ethers.JsonRpcProvider(cfg.rpcUrl, Number(cfg.chainId), {
        staticNetwork: true,
      });
    }
    return _readProvider;
  }

  const registry = (p) => new ethers.Contract(cfg.registryAddr, REGISTRY_ABI, p || readProvider());
  const escrow = (p) => new ethers.Contract(cfg.escrowAddr, ESCROW_ABI, p || readProvider());
  const usdc = (p) => new ethers.Contract(cfg.usdcAddr, ERC20_ABI, p || readProvider());

  /* ── units discipline ──────────────────────────────────────
     On-chain: 6-decimal native integers everywhere.
     Display layer: divide by 1e6. Prices = USDC per 1M tokens. */
  const toNative = (usdcString) => ethers.parseUnits(String(usdcString).trim() || "0", 6);
  /* fixed 6dp for tabular mono display (ethers.formatUnits trims zeros — pad back) */
  const fmtUsdc = (native) => {
    const s = ethers.formatUnits(native, 6);
    if (!s.includes(".")) return s + ".000000";
    return s + "0".repeat(Math.max(0, 7 - (s.length - s.indexOf("."))));
  };
  const fmtUsdcTrim = (native) => ethers.formatUnits(native, 6);
  /* like fmtUsdcTrim but drops the padding dust: 10.0 → 10, 2.999674 stays */
  const fmtUsdcBare = (native) => {
    const s = fmtUsdcTrim(native);
    return s.includes(".") ? s.replace(/0+$/, "").replace(/\.$/, "") : s;
  };
  const fmtInt = (native) => Number(native).toLocaleString("en-US");

  /* ── Escrow custom-error → human copy ──────────────────────
     ethers v6 populates e.revert {name, args} when the error is
     declared in the ABI (ESCROW_ABI above); a raw revert-data
     selector match is the fallback for errors that arrive detached
     (nested e.info.error.data); a message-name match comes last. */
  const ESCROW_ERR_SIGS = {
    ZeroAmount: "ZeroAmount()",
    InvalidSeller: "InvalidSeller()",
    SelfLock: "SelfLock()",
    InsufficientBalance: "InsufficientBalance(uint256,uint256)",
    NotLocked: "NotLocked(uint256,uint8)",
    NotSeller: "NotSeller(address,address)",
    ExceedsMaxAmount: "ExceedsMaxAmount(uint256,uint256)",
    TtlNotElapsed: "TtlNotElapsed(uint256,uint64)",
  };
  const ESCROW_ERR_IFACE = new ethers.Interface(Object.values(ESCROW_ERR_SIGS).map((s) => "error " + s));

  /* → {name, args} or null when the error is not an Escrow custom error */
  function decodeEscrowErr(e) {
    if (e && e.revert && e.revert.name && ESCROW_ERR_SIGS[e.revert.name]) {
      return { name: e.revert.name, args: e.revert.args || [] };
    }
    const raw = e && (typeof e.data === "string" ? e.data : (e.info && e.info.error && e.info.error.data));
    if (typeof raw === "string" && raw.startsWith("0x") && raw.length >= 10) {
      try {
        const parsed = ESCROW_ERR_IFACE.parseError(raw);
        if (parsed) return { name: parsed.name, args: parsed.args };
      } catch { /* not an Escrow error */ }
    }
    const msg = String((e && (e.shortMessage || e.reason || e.message)) || "");
    const named = Object.keys(ESCROW_ERR_SIGS).find((k) => msg.includes(k));
    return named ? { name: named, args: [] } : null;
  }

  /* → human sentence (with the decoded numbers inline) or null */
  function humanizeEscrowErr(e) {
    const d = decodeEscrowErr(e);
    if (!d) return null;
    const a = d.args || [];
    switch (d.name) {
      case "ZeroAmount": return "amount must be > 0 (ZeroAmount)";
      case "InvalidSeller": return "invalid seller address (InvalidSeller) — pick a seller from the market listing";
      case "SelfLock": return "buyer and seller are the same address (SelfLock) — a lock needs two different parties";
      case "InsufficientBalance":
        return a.length >= 2
          ? `insufficient escrow balance — escrow $${fmtUsdcBare(a[1])} < requested $${fmtUsdcBare(a[0])}; deposit $${fmtUsdcBare(a[0] - a[1])} more first (InsufficientBalance)`
          : "insufficient escrow balance (InsufficientBalance) — deposit first";
      case "NotLocked":
        return a.length >= 2
          ? `payment #${a[0].toString()} is ${PAYMENT_STATES[Number(a[1])] || "?"} — not Locked (NotLocked)`
          : "payment is not Locked (NotLocked) — wrong id, or already settled/refunded";
      case "NotSeller": return "only the lock's designated seller may settle (NotSeller)";
      case "ExceedsMaxAmount":
        return a.length >= 2
          ? `settle amount $${fmtUsdcBare(a[0])} exceeds the locked max $${fmtUsdcBare(a[1])} (ExceedsMaxAmount)`
          : "settle amount exceeds the locked max (ExceedsMaxAmount)";
      case "TtlNotElapsed":
        return a.length >= 2
          ? `lock #${a[0].toString()} has not expired — refundable after ${new Date(Number(a[1]) * 1000).toLocaleString()} (TtlNotElapsed)`
          : "lock has not expired yet (TtlNotElapsed) — refund opens once the ttl elapses";
      default: return null;
    }
  }

  /* lock pre-check: MAX AMOUNT vs the live escrow balance. null = fine;
     otherwise {more} + the inline-note copy (native 6dp BigInts in). */
  function lockShortfall(maxNative, balNative) {
    if (maxNative == null || balNative == null || maxNative <= balNative) return null;
    const more = maxNative - balNative;
    return {
      more,
      text: `insufficient escrow balance — deposit $${fmtUsdcBare(more)} more (escrow $${fmtUsdcBare(balNative)} < lock $${fmtUsdcBare(maxNative)})`,
    };
  }

  /* ── display helpers ─────────────────────────────────────── */
  const esc = (s) =>
    String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));

  const truncAddr = (a) => (a && a.length > 12 ? `${a.slice(0, 6)}…${a.slice(-4)}` : a || "");
  const addrLink = (a) => `${cfg.explorer}/address/${a}`;
  const txLink = (h) => `${cfg.explorer}/tx/${h}`;

  const joinUrl = (base, path) => String(base || "").replace(/\/+$/, "") + path;
  const hostOf = (endpoint) => {
    try { return new URL(endpoint).host; } catch { return endpoint; }
  };

  const sameAddr = (a, b) => {
    try { return ethers.getAddress(a) === ethers.getAddress(b); } catch { return false; }
  };
  const isZeroAddr = (a) => !a || /^0x0{40}$/i.test(a);

  /* ── relay health probe (CORS failure → silent gray) ─────── */
  async function probeHealth(endpoint, timeoutMs = 5000) {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), timeoutMs);
    const started = performance.now();
    try {
      const r = await fetch(joinUrl(endpoint, "/health"), { signal: ctrl.signal, cache: "no-store" });
      return { ok: r.ok, ms: Math.round(performance.now() - started) };
    } catch {
      /* network error OR CORS block — indistinguishable in browser;
         UI shows gray dot with an explanatory title */
      return { ok: false, unreachable: true };
    } finally { clearTimeout(t); }
  }

  /* fetch JSON with timeout; classifies CORS/network failures */
  async function fetchJson(url, opts = {}, timeoutMs = 15000) {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const r = await fetch(url, { ...opts, signal: ctrl.signal });
      let body = null;
      try { body = await r.json(); } catch { /* non-JSON body */ }
      return { ok: r.ok, status: r.status, body, response: r };
    } catch (e) {
      const aborted = e && e.name === "AbortError";
      return {
        ok: false, status: 0, body: null, corsOrNetwork: true,
        error: aborted ? "timeout" : "unreachable-or-cors",
      };
    } finally { clearTimeout(t); }
  }

  /* ── listings ────────────────────────────────────────────── */
  async function fetchListing(provider, operator) {
    const raw = await registry(provider).getListing(operator);
    /* ethers v6 COLLAPSES a single struct output: the awaited Contract
       call IS the tuple Result (named .models/.prices/.active). The
       outer `.listing` wrapper only exists in manual Interface decoding
       (decodeFunctionResult) — support both shapes. */
    const li = (raw && raw.models !== undefined) ? raw : ((raw && (raw.listing || raw[0])) || raw);
    const models = (li.models || []).filter((m) => m && m.trim() !== ""); // PIN: empty strings filtered
    /* v2: prices is a Price[] PARALLEL to models — prices[i] prices models[i].
       Keep BigInts; guard against short arrays (corrupt/old contracts). */
    const prices = models.map((_, i) => {
      const p = li.prices && li.prices[i];
      return p ? { cachedIn: p.cachedIn, input: p.input, output: p.output } : null;
    });
    return {
      operator,
      registered: !isZeroAddr(li.operator),
      endpoint: li.endpoint,
      models,
      prices,
      active: li.active,
    };
  }

  /* tiered price triple for one model, or null when absent */
  const priceFor = (listing, model) => {
    if (!listing || !listing.models || !listing.prices) return null;
    const i = listing.models.indexOf(model);
    return i >= 0 ? listing.prices[i] || null : null;
  };

  async function fetchListings(provider) {
    const out = [];
    for (const addr of cfg.sellers || []) {
      try { out.push(await fetchListing(provider, addr)); }
      catch { out.push({ operator: addr, registered: false, error: true, models: [] }); }
    }
    return out;
  }

  /* ── shared v2 market card board (index preview + market.html) ──
     One board per grid container: renders listing cards (model chip
     picker + per-model price cells), keeps per-operator chip selection
     across re-renders, returns the probeable card handles. */
  function marketBoard(rootEl) {
    const selByOperator = {};   // operator → last picked model
    let cardData = new Map();   // operator → listing (for click re-renders)

    function priceCell(tier, native) {
      return (
        `<div class="ls-price">` +
        `<span class="tier">${tier}</span>` +
        `<span class="usd">$${fmtUsdc(native)}</span>` +
        `<span class="units">${fmtInt(native)} units</span>` +
        `</div>`
      );
    }
    function priceCells(l, model) {
      const p = priceFor(l, model);
      if (!p) return `<div class="ls-prices-empty">no on-chain price for ${esc(model)}</div>`;
      return priceCell("CACHED IN", p.cachedIn) + priceCell("INPUT", p.input) + priceCell("OUTPUT", p.output);
    }
    function cardHTML(l) {
      const badge = l.active
        ? `<span class="badge">ACTIVE</span>`
        : `<span class="badge off">INACTIVE</span>`;
      const sel = l.models.includes(selByOperator[l.operator]) ? selByOperator[l.operator] : l.models[0];
      const models = l.models.length
        ? l.models.map((m) =>
            `<button type="button" class="mtag msel" aria-pressed="${m === sel}" data-model="${esc(m)}">${esc(m)}</button>`
          ).join("")
        : `<span class="mtag">—</span>`;
      const prices = l.models.length
        ? `<div class="ls-prices">${priceCells(l, sel)}</div>` +
          `<div class="ls-prices-cap">USDC per 1M tokens · native 6dp units</div>`
        : `<div class="ls-prices-empty">no models listed — nothing priced on-chain</div>`;
      return (
        `<article class="card listing rv in${l.active ? "" : " inactive"}" data-endpoint="${esc(l.endpoint)}" data-operator="${esc(l.operator)}">` +
          `<div class="ls-top">` +
            `<a class="ls-addr" data-copy="${esc(l.operator)}" href="${addrLink(l.operator)}" target="_blank" rel="noopener" title="${esc(l.operator)} — click copies address">${truncAddr(l.operator)}</a>` +
            `<span class="ls-health" title="relay /health probe pending"><span class="hdot"></span><span class="ls-health-lbl">probing</span></span>` +
            badge +
          `</div>` +
          `<p class="ls-endpoint" title="${esc(l.endpoint)}">${esc(hostOf(l.endpoint))}</p>` +
          `<div class="ls-models" role="group" aria-label="model selector — pick a model to see its prices">${models}</div>` +
          prices +
          `<div class="ls-meta">` +
            `<span>operator <a href="${addrLink(l.operator)}" target="_blank" rel="noopener" class="ls-link">explorer ↗</a></span>` +
            `<span class="chip chip-monad">${esc((cfg.chainName || "MONAD").toUpperCase())} · ${cfg.chainId}</span>` +
          `</div>` +
        `</article>`
      );
    }

    /* delegated chip clicks — survives full re-renders */
    rootEl.addEventListener("click", (e) => {
      const btn = e.target.closest(".msel");
      if (!btn) return;
      const card = btn.closest(".listing");
      const l = card && cardData.get(card.dataset.operator);
      if (!l) return;
      const model = btn.dataset.model;
      selByOperator[l.operator] = model;
      card.querySelectorAll(".msel").forEach((b) =>
        b.setAttribute("aria-pressed", String(b === btn)));
      const grid = card.querySelector(".ls-prices");
      if (grid) grid.innerHTML = priceCells(l, model);
    });

    return {
      render(listings) {
        rootEl.innerHTML = listings.map(cardHTML).join("");
        cardData = new Map(listings.map((l) => [l.operator, l]));
        /* match by operator (endpoints may collide across sellers) */
        return listings.map((l) => ({
          el: [...rootEl.children].find((c) => c.dataset.operator === l.operator),
          endpoint: l.endpoint,
        })).filter((c) => c.el);
      },
    };
  }

  /* probe rendered cards: /health dot + ms, then /info TEE & upstream
     badges (silent degrade). Returns the online count. */
  async function probeListings(cards) {
    let online = 0;
    await Promise.all(cards.map(async ({ el, endpoint }) => {
      const slot = el.querySelector(".ls-health");
      const r = await probeHealth(endpoint);
      const dot = slot.querySelector(".hdot");
      const lbl = slot.querySelector(".ls-health-lbl");
      if (r.ok) {
        online += 1;
        dot.classList.add("ok");
        lbl.textContent = `${r.ms}ms`;
        slot.title = `relay /health OK · ${r.ms}ms`;
        /* M7 touchpoint: TEE / upstream-policy badges via GET /info.
           Silent degrade — unreachable or non-JSON → no badge, no throw. */
        const info = await probeInfo(endpoint);
        if (info) {
          const top = el.querySelector(".ls-top");
          if (info.teeEnabled) {
            const a = document.createElement("a");
            a.className = "badge tee";
            a.href = joinUrl(endpoint, "/attestation");
            a.target = "_blank";
            a.rel = "noopener";
            a.title = "TEE attested relay — view /attestation quote (derived key, reportData, quoteDigest)";
            a.textContent = "TEE";
            top.insertBefore(a, top.querySelector(".badge"));
          }
          if (info.upstreamHost) {
            const meta = el.querySelector(".ls-meta span");
            meta.innerHTML =
              `upstream <b class="${info.official ? "ok" : "bad"}">${esc(info.upstreamHost)}${info.official ? " · official" : " · CUSTOM"}</b> · ` +
              meta.innerHTML;
          }
        }
      } else {
        dot.classList.add("off");
        lbl.textContent = "offline";
        slot.title = "relay unreachable — or CORS not enabled yet";
      }
    }));
    return online;
  }

  /* ── seller enumeration (M10, Registry v3) ──────────────────
     O(1) on-chain discovery replaces the M9 Registered-event scan
     (which degraded linearly with chain age + RPC getLogs limits).
     v3 keeps the whole v2 face and adds:
       sellerCount() → uint256
       getSellers(start, count) → address[]   (append-only, clamp:
         start≥len → [], count>500 → 500, start+count>len → tail)
     Enumeration order = first-registration order; re-registering after
     deactivate does NOT re-append. Freshness/active/prices always come
     from getListing per operator. */
  const SELLER_PAGE = 100; /* < the contract's 500 clamp — polite pages */

  async function fetchSellerSet(provider) {
    const reg = registry(provider);
    const total = Number(await reg.sellerCount());
    const out = [];
    for (let start = 0; start < total; start += SELLER_PAGE) {
      const batch = await reg.getSellers(start, SELLER_PAGE);
      if (!batch || batch.length === 0) break; /* defensive clamp */
      for (const a of batch) out.push(a);
    }
    return out;
  }

  /* enumerated sellers → enriched listings (_enumIndex = enumeration
     position, drives the NEWEST-first default sort). Falls back to the
     config.js sellers array when the enumeration calls are unavailable
     (pre-v3 Registry / RPC hiccup) — never throws. */
  async function fetchMarketListings(provider) {
    let addrs;
    try {
      addrs = await fetchSellerSet(provider);
    } catch (e) {
      const degraded = (e && (e.shortMessage || e.message)) || "unknown";
      let fallback = [];
      try {
        fallback = (await fetchListings(provider)).filter((l) => l.registered && !l.error);
      } catch { /* dead RPC — empty market */ }
      return { listings: fallback, source: "config", degraded };
    }
    const listings = [];
    for (let i = 0; i < addrs.length; i++) {
      try {
        const l = await fetchListing(provider, addrs[i]);
        if (l.registered) { l._enumIndex = i; listings.push(l); }
      } catch { /* unreadable seller — skip */ }
    }
    return { listings, source: "enum", degraded: null };
  }

  /* PIN minAmount estimate, native units, for ONE model's price triple:
     (price.input*PROMPT_TOKEN_CAP + price.output*COMPLETION_TOKEN_CAP) // 1e6 */
  const minAmountEstimate = (price) =>
    (price.input * PROMPT_TOKEN_CAP + price.output * COMPLETION_TOKEN_CAP) / 1000000n;

  /* strictest estimate across a listing's models — the safe lock bound */
  const maxMinAmountEstimate = (listing) => {
    const ps = (listing && listing.prices) || [];
    return ps.filter(Boolean).reduce((mx, p) => {
      const e = minAmountEstimate(p);
      return e > mx ? e : mx;
    }, 0n);
  };

  /* ══════════════════════════════════════════════════════════
     EIP-191 request signature (buyer → relay) — PIN verbatim:

       msg = f"{METHOD}|{path}|{sha256(raw_body_bytes).hexdigest()}|{paymentId}"

     METHOD uppercase · path = /v1/chat/completions · hash is 64
     lowercase hex with no 0x prefix · paymentId is a decimal
     string with no leading zeros.
     Signed with EIP-191 personal_sign over the msg UTF-8 bytes
     (eth_account.encode_defunct(text=msg) ≡ signer.signMessage(msg)).
     ══════════════════════════════════════════════════════════ */
  const RELAY_CHAT_PATH = "/v1/chat/completions";

  function buildEip191Message(method, path, bodyString, paymentId) {
    const bodyHash = ethers.sha256(ethers.toUtf8Bytes(bodyString)).slice(2); // 64 lowercase hex, no 0x
    const paymentDecimal = BigInt(paymentId).toString(10);                   // decimal, no leading zeros
    return `${method.toUpperCase()}|${path}|${bodyHash}|${paymentDecimal}`;
  }

  /* ══════════════════════════════════════════════════════════
     EIP-712 receipt (relay → buyer) — PIN verbatim:

       domain  = {name:"TokenShare Relay", version:"1", chainId}
       Receipt(uint256 paymentId, uint256 promptTokens,
               uint256 cachedTokens, uint256 completionTokens,
               uint256 actualAmount, address seller,
               string upstreamHost, string model)
       X-Receipt = unpadded base64url(JSON {domain,message,signature})
     ══════════════════════════════════════════════════════════ */
  const RECEIPT_DOMAIN_NAME = "TokenShare Relay";
  const RECEIPT_DOMAIN_VERSION = "1";
  const RECEIPT_TYPES = {
    Receipt: [
      { name: "paymentId", type: "uint256" },
      { name: "promptTokens", type: "uint256" },
      { name: "cachedTokens", type: "uint256" },
      { name: "completionTokens", type: "uint256" },
      { name: "actualAmount", type: "uint256" },
      { name: "seller", type: "address" },
      { name: "upstreamHost", type: "string" },
      { name: "model", type: "string" },
    ],
  };

  function b64urlDecode(raw) {
    let s = String(raw || "").trim().replace(/[\r\n]/g, "").replace(/-/g, "+").replace(/_/g, "/");
    s += "=".repeat((4 - (s.length % 4)) % 4); // unpadded input tolerated (JS % keeps sign — never negative here)
    const bin = atob(s);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new TextDecoder().decode(bytes);
  }

  function decodeReceiptHeader(headerValue) {
    const obj = JSON.parse(b64urlDecode(headerValue));
    if (!obj || typeof obj !== "object" || !obj.domain || !obj.message || !obj.signature) {
      throw new Error("X-Receipt JSON must carry {domain, message, signature}");
    }
    return obj;
  }

  /* Mirrors cli/tokenshare_cli/receipt.py verify_receipt:
     0 domain verbatim (name/version + chainId) · 1 recover ·
     2 recovered == seller · 3 message.seller == seller · 4 paymentId */
  function verifyReceipt(receipt, expectedSeller, expectedChainId, expectedPaymentId) {
    const d = receipt.domain || {};
    if (d.name !== RECEIPT_DOMAIN_NAME || d.version !== RECEIPT_DOMAIN_VERSION) {
      return { ok: false, reason: "domain-mismatch" };
    }
    try {
      if (Number(d.chainId) !== Number(expectedChainId)) return { ok: false, reason: "domain-mismatch" };
    } catch { return { ok: false, reason: "domain-mismatch" }; }

    let recovered;
    try {
      recovered = ethers.verifyTypedData(receipt.domain, RECEIPT_TYPES, receipt.message, receipt.signature);
    } catch (e) {
      return { ok: false, reason: "recover-failed: " + (e.shortMessage || e.message) };
    }
    if (!sameAddr(recovered, expectedSeller)) return { ok: false, recovered, reason: "recover-mismatch" };
    if (!sameAddr(receipt.message.seller, expectedSeller)) return { ok: false, recovered, reason: "seller-mismatch" };
    if (expectedPaymentId != null) {
      try {
        if (BigInt(receipt.message.paymentId) !== BigInt(expectedPaymentId)) {
          return { ok: false, recovered, reason: "paymentid-mismatch" };
        }
      } catch { return { ok: false, recovered, reason: "paymentid-mismatch" }; }
    }
    return { ok: true, recovered };
  }

  /* ── tx lifecycle UI line ──────────────────────────────────
     pending → hash (explorer link) → confirmed / reverted */
  function txLine(container, label) {
    const el = document.createElement("div");
    el.className = "txl";
    el.innerHTML =
      `<span class="txl-dot"></span>` +
      `<span class="txl-label">${esc(label)}</span>` +
      `<span class="txl-state"></span>`;
    container.appendChild(el);
    const dot = el.querySelector(".txl-dot");
    const state = el.querySelector(".txl-state");
    const set = (cls, html) => {
      el.className = "txl " + cls;
      state.innerHTML = html;
    };
    return {
      el,
      pending() { set("is-pending", `pending<span class="txl-dots"><i>.</i><i>.</i><i>.</i></span> — confirm in wallet / wait for block`); },
      hash(h) { set("is-pending", `broadcast · <a href="${txLink(h)}" target="_blank" rel="noopener">${esc(h.slice(0, 12))}…${esc(h.slice(-6))}</a>`); },
      confirmed(h) { set("is-ok", `confirmed${h ? ` · <a href="${txLink(h)}" target="_blank" rel="noopener">view tx</a>` : ""}`); },
      failed(err) {
        /* Escrow custom errors get the human sentence first — raw ethers
           copy ("execution reverted (unknown custom error)") is the fallback */
        const msg = humanizeEscrowErr(err) || (err && (err.shortMessage || err.reason || err.message)) || "failed";
        const rejected = (err && (err.code === "ACTION_REJECTED" || err.code === 4001 ||
          (err.info && err.info.error && err.info.error.code === 4001)));
        set("is-bad", rejected ? "rejected in wallet" : `reverted/failed — ${esc(msg)}`);
      },
    };
  }

  /* send a contract tx through the full lifecycle; resolves the
     receipt or null on failure */
  async function runTx(line, txPromise) {
    line.pending();
    try {
      const tx = await txPromise;
      line.hash(tx.hash);
      const rcpt = await tx.wait();
      if (rcpt && Number(rcpt.status) === 1) { line.confirmed(tx.hash); return rcpt; }
      line.failed({ shortMessage: "transaction reverted on-chain" });
      return null;
    } catch (e) {
      line.failed(e);
      return null;
    }
  }

  /* parse paymentId out of a lock() receipt's Locked event */
  function parseLockedPaymentId(receipt) {
    const iface = new ethers.Interface(ESCROW_ABI);
    for (const log of receipt.logs || []) {
      try {
        const parsed = iface.parseLog({ topics: log.topics, data: log.data });
        if (parsed && parsed.name === "Locked") return parsed.args.paymentId;
      } catch { /* foreign log */ }
    }
    return null;
  }

  /* ── session locks + dispute ledger (localStorage) ───────── */
  const LOCKS_KEY = "tokenshare.locks";
  const DISPUTES_KEY = "tokenshare.disputes";

  const loadJson = (k) => { try { return JSON.parse(localStorage.getItem(k)) || []; } catch { return []; } };
  const saveJson = (k, v) => localStorage.setItem(k, JSON.stringify(v));

  const locks = {
    all: () => loadJson(LOCKS_KEY),
    add(lock) { const all = locks.all(); all.unshift(lock); saveJson(LOCKS_KEY, all.slice(0, 50)); },
  };

  const disputes = {
    all: () => loadJson(DISPUTES_KEY),
    add(d) { const all = disputes.all(); all.unshift(d); saveJson(DISPUTES_KEY, all.slice(0, 100)); },
    clear() { localStorage.removeItem(DISPUTES_KEY); },
  };

  /* ── relay /info probe (M7) — TEE + upstream policy descriptor.
     Graceful by contract: any failure returns null, callers hide the badge. */
  async function probeInfo(endpoint, timeoutMs = 5000) {
    const r = await fetchJson(joinUrl(endpoint, "/info"), {}, timeoutMs);
    if (!r.ok || !r.body) return null;
    const b = r.body;
    return {
      teeEnabled: Boolean(b.tee && b.tee.enabled),
      official: Boolean(b.upstream && b.upstream.official),
      upstreamHost: (b.upstream && b.upstream.host) || null,
      seller: b.seller || null,
      chainId: b.chainId ?? null,
    };
  }

  /* click-to-copy affordance: <span data-copy="0x…">…</span> —
     one delegated listener per page handles the copy + flash */
  function installCopyHandlers(root = document) {
    root.addEventListener("click", (e) => {
      const el = e.target.closest("[data-copy]");
      if (!el) return;
      e.preventDefault(); /* a data-copy link copies instead of navigating */
      const text = el.dataset.copy;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).catch(() => {});
      }
      el.classList.add("copied");
      setTimeout(() => el.classList.remove("copied"), 900);
    });
  }

  /* ── relay error copy (PIN error codes) ──────────────────── */
  function relayErrorCopy(status) {
    switch (status) {
      case 400: return "model not in the seller's listing (or listing inactive) — pick a listed model";
      case 401: return "signature missing/invalid — reconnect the wallet and retry";
      case 402: return "payment proof rejected — maxAmount below the seller's minAmount estimate; raise it and re-lock";
      case 409: return "lock about to expire (ttl margin) — refund & re-lock with a longer ttl";
      case 502: return "upstream (official API) error — not settled; retry or refund after ttl";
      default: return null;
    }
  }

  /* ══════════════════════════════════════════════════════════
     M11 — EIP-6963 multi-wallet discovery + self-drawn selector.

     Discovery: a lifetime window listener collects
     "eip6963:announceProvider" (detail = {info:{uuid,name,icon,rdns},
     provider}); "eip6963:requestProvider" is (re)dispatched once the
     DOM is ready, so late-injected wallets still replay their
     announcement. Dedupe by uuid, rdns as the fallback key.

     PIN discipline:
     · the ONLY popup in the whole flow is eth_requestAccounts on the
       user-picked provider, fired after the selector row click;
     · silent resume NEVER goes through getSigner (ethers auto-falls
       back to eth_requestAccounts when eth_accounts is empty) — bare
       provider.request({method:"eth_accounts"}) only;
     · accountsChanged/chainChanged bind the RAW provider (ethers
       BrowserProvider forwards neither); switching wallets
       removeListener's the old binding first;
     · window.ethereum is consulted ONLY when zero 6963 announcements
       arrived (OKX hijacks it as "default wallet") — .providers
       arrays are labeled best-effort off isMetaMask/isOkxWallet;
     · selector rows are createElement("img") + textContent only —
       no innerHTML (a data-URI SVG icon can carry script).
     ══════════════════════════════════════════════════════════ */
  const WALLET_RDNS_KEY = "tokenshare.wallet.rdns";
  /* lib-6: persistent per-wallet logout marker — `logout:<rdns>`. While
     set, silent resume for that wallet is suppressed (an explicit DISCONNECT
     survives a page refresh); the ONLY clears are an explicit reconnect
     (selector pick / SWITCH ACCOUNT success). accountsChanged([]) from the
     wallet side clears state WITHOUT writing this flag. */
  const WALLET_LOGOUT_PREFIX = "tokenshare.wallet.logout:";

  const wallet = (() => {
    const announced = new Map(); /* dedupe key (uuid, else rdns) → {info, provider} */
    const subs = { accounts: [], chain: [] };
    let current = null;      /* picked {info, provider} */
    let binding = null;      /* {provider, onAccounts, onChain} — raw listeners */
    let modalList = null;    /* row container of the open selector (singleton) */
    let modalResolve = null;
    let modalTeardown = null;

    function onAnnounce(ev) {
      const d = ev && ev.detail;
      if (!d || !d.info || !d.provider) return;
      const { uuid, rdns } = d.info;
      if (uuid && announced.has(uuid)) return;                                     /* uuid dedupe */
      if (rdns && [...announced.values()].some((w) => w.info.rdns === rdns)) return; /* rdns dedupe */
      announced.set(uuid || rdns || `anon#${announced.size}`, { info: d.info, provider: d.provider });
      if (modalList) renderModalRows(); /* late announcement → live row in the open modal */
    }
    window.addEventListener("eip6963:announceProvider", onAnnounce); /* never removed */

    const rediscover = () => window.dispatchEvent(new Event("eip6963:requestProvider"));
    /* dispatch after DOMContentLoaded, then give late wallets a beat to
       replay before resume() reads the announced set */
    const announcedReady = new Promise((resolve) => {
      const kick = () => { rediscover(); setTimeout(resolve, 150); };
      if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", kick, { once: true });
      } else kick();
    });

    /* legacy fallback — consulted ONLY when zero 6963 announcements arrived */
    function legacyEntries() {
      const eth = window.ethereum;
      if (!eth) return [];
      const label = (p) =>
        p.isOkxWallet ? { name: "OKX Wallet", rdns: "com.okex.wallet" }
        : p.isMetaMask ? { name: "MetaMask", rdns: "io.metamask" }
        : p.isCoinbaseWallet ? { name: "Coinbase Wallet", rdns: "com.coinbase.wallet" }
        : { name: "Injected Wallet", rdns: null };
      const mk = (p) => {
        const { name, rdns } = label(p);
        return { info: { uuid: `legacy:${rdns || name}`, name, rdns, icon: "" }, provider: p };
      };
      return Array.isArray(eth.providers) && eth.providers.length ? eth.providers.map(mk) : [mk(eth)];
    }

    const list = () => (announced.size ? [...announced.values()] : legacyEntries());

    const remember = (entry) => {
      if (entry.info.rdns) {
        try {
          localStorage.setItem(WALLET_RDNS_KEY, entry.info.rdns);
          localStorage.removeItem(WALLET_LOGOUT_PREFIX + entry.info.rdns); /* explicit connect clears a prior logout */
        } catch { /* private mode */ }
      }
    };
    const forget = () => { try { localStorage.removeItem(WALLET_RDNS_KEY); } catch { /* ignore */ } };
    const logoutMarked = (rdns) => {
      if (!rdns) return false;
      try { return localStorage.getItem(WALLET_LOGOUT_PREFIX + rdns) === "1"; } catch { return false; }
    };
    const markLogout = (rdns) => {
      if (!rdns) return;
      try { localStorage.setItem(WALLET_LOGOUT_PREFIX + rdns, "1"); } catch { /* private mode */ }
    };
    const clearLogout = (rdns) => {
      if (!rdns) return;
      try { localStorage.removeItem(WALLET_LOGOUT_PREFIX + rdns); } catch { /* ignore */ }
    };

    function emit(kind, arg) {
      for (const cb of subs[kind]) { try { cb(arg); } catch { /* one bad callback ≠ all */ } }
    }
    function unbind() {
      if (!binding) return;
      try { binding.provider.removeListener("accountsChanged", binding.onAccounts); } catch { /* ignore */ }
      try { binding.provider.removeListener("chainChanged", binding.onChain); } catch { /* ignore */ }
      binding = null;
    }
    /* bind the RAW provider — switching wallets unbinds the old one first */
    function bind(provider) {
      unbind();
      const onAccounts = (accs) => {
        const a = accs || [];
        if (!a.length) {
          /* disconnected inside the wallet → drop the memory and the whole
             local connection state — but NO logout flag (this is not an
             explicit on-page DISCONNECT) */
          forget();
          unbind();
          current = null;
        }
        emit("accounts", a);
      };
      const onChain = (chainId) => emit("chain", chainId);
      provider.on("accountsChanged", onAccounts);
      provider.on("chainChanged", onChain);
      binding = { provider, onAccounts, onChain };
    }

    function select(entry) {
      if (!current || current.provider !== entry.provider) bind(entry.provider);
      current = entry;
      return current;
    }

    /* ── selector modal (self-drawn; createElement + textContent only) ── */
    function renderModalRows() {
      const listEl = modalList;
      if (!listEl) return;
      listEl.textContent = ""; /* safe clear */
      const entries = list();
      if (!entries.length) {
        /* 无钱包空态 — install links */
        const empty = document.createElement("div");
        empty.className = "wsel-empty";
        const p1 = document.createElement("p");
        p1.textContent = "No wallet detected — zero EIP-6963 announcements and window.ethereum is empty.";
        const p2 = document.createElement("p");
        p2.textContent = "Install one, then refresh this page:";
        const links = document.createElement("div");
        links.className = "wsel-install";
        for (const [name, url] of [["MetaMask", "https://metamask.io/download/"], ["OKX Wallet", "https://www.okx.com/web3"]]) {
          const a = document.createElement("a");
          a.href = url;
          a.target = "_blank";
          a.rel = "noopener";
          a.textContent = name + " ↗";
          links.appendChild(a);
        }
        empty.append(p1, p2, links);
        listEl.appendChild(empty);
        return;
      }
      for (const entry of entries) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "wsel-item";
        if (entry.info.icon) {
          const img = document.createElement("img"); /* wallet-supplied data URI — never innerHTML */
          img.className = "wsel-icon";
          img.src = entry.info.icon;
          img.alt = "";
          btn.appendChild(img);
        }
        const name = document.createElement("span");
        name.className = "wsel-name";
        name.textContent = entry.info.name || "Injected Wallet";
        btn.appendChild(name);
        if (entry.info.rdns) {
          const rd = document.createElement("span");
          rd.className = "wsel-rdns";
          rd.textContent = entry.info.rdns;
          btn.appendChild(rd);
        }
        btn.addEventListener("click", () => closeModal(entry));
        listEl.appendChild(btn);
      }
    }

    function closeModal(result) {
      const resolve = modalResolve;
      if (modalTeardown) modalTeardown();
      modalResolve = null;
      modalList = null;
      modalTeardown = null;
      if (resolve) resolve(result || null);
    }

    function openSelector() {
      if (modalResolve) closeModal(null); /* singleton */
      return new Promise((resolve) => {
        modalResolve = resolve;

        const overlay = document.createElement("div");
        overlay.className = "wsel-overlay";
        const box = document.createElement("div");
        box.className = "wsel";
        box.setAttribute("role", "dialog");
        box.setAttribute("aria-modal", "true");
        box.setAttribute("aria-label", "select wallet");

        const bar = document.createElement("div");
        bar.className = "wsel-bar";
        for (let i = 0; i < 3; i++) { const d = document.createElement("span"); d.className = "tdot"; bar.appendChild(d); }
        const title = document.createElement("span");
        title.className = "wsel-title";
        title.textContent = "select wallet — eip-6963 discovery";
        bar.appendChild(title);
        box.appendChild(bar);

        const listEl = document.createElement("div");
        listEl.className = "wsel-list";
        box.appendChild(listEl);

        const foot = document.createElement("div");
        foot.className = "wsel-foot";
        const hint = document.createElement("span");
        hint.className = "wsel-hint";
        hint.textContent = "esc / click outside to close · the picked wallet pops once";
        const cancel = document.createElement("button");
        cancel.type = "button";
        cancel.className = "wsel-cancel";
        cancel.textContent = "[ CANCEL ]";
        cancel.addEventListener("click", () => closeModal(null));
        foot.append(hint, cancel);
        box.appendChild(foot);

        const onKey = (e) => {
          if (e.key === "Escape") { e.stopPropagation(); closeModal(null); }
        };
        document.addEventListener("keydown", onKey, true);
        overlay.addEventListener("click", (e) => { if (e.target === overlay) closeModal(null); });
        const prevOverflow = document.body.style.overflow;
        document.body.style.overflow = "hidden";

        modalTeardown = () => {
          document.removeEventListener("keydown", onKey, true);
          document.body.style.overflow = prevOverflow;
          overlay.remove();
        };

        overlay.appendChild(box);
        document.body.appendChild(overlay);
        modalList = listEl;
        renderModalRows();
      });
    }

    /* THE interactive path: selector → eth_requestAccounts on the pick
       (the single wallet popup of the whole flow). Resolves
       {provider, address, name}, or null on cancel / empty selection. */
    async function connectInteractive() {
      const entry = await openSelector();
      if (!entry) return null;
      const accs = await entry.provider.request({ method: "eth_requestAccounts" });
      if (!accs || !accs.length) return null;
      select(entry);
      remember(entry);
      return { provider: entry.provider, address: ethers.getAddress(accs[0]), name: entry.info.name };
    }

    /* ── lib-6 DISCONNECT → true logout ──────────────────────────
       1. best-effort wallet_revokePermissions({eth_accounts:{}}), capped
          at 300ms via Promise.race, ALL errors swallowed:
            MetaMask truly revokes the site permission;
            OKX resolves as a silent no-op;
            Coinbase rejects (harmless).
       2. persist the `logout:<rdns>` marker (survives refresh);
       3. wipe local connection state (memory, listeners, current).
       Raw provider only — NEVER ethers getSigner here. Returns
       {manual} telling the UI whether the wallet ignores programmatic
       revoke (OKX/Coinbase → the user must also disconnect the site
       inside the wallet). */
    async function logout() {
      const entry = current;
      if (!entry) return { manual: false };
      const rdns = (entry.info && entry.info.rdns) || null;
      const manual = !!(rdns && /okex|coinbase/i.test(rdns)) ||
        !!(entry.provider && (entry.provider.isOkxWallet || entry.provider.isCoinbaseWallet));
      try {
        await Promise.race([
          entry.provider.request({ method: "wallet_revokePermissions", params: [{ eth_accounts: {} }] }),
          new Promise((_, reject) => setTimeout(() => reject(new Error("revoke timeout")), 300)),
        ]);
      } catch { /* unsupported / timeout / rejected — best-effort by contract */ }
      markLogout(rdns);
      forget();
      unbind();
      current = null;
      return { manual };
    }

    /* ── lib-6 SWITCH ACCOUNT ────────────────────────────────────
       wallet_requestPermissions({eth_accounts:{}}) first — MetaMask
       opens its account picker even when already connected.
         4001 → user cancelled, keep everything as-is;
         any other error → degrade to eth_requestAccounts;
       after success, calibrate via bare eth_accounts (never getSigner)
       and clear the logout marker. */
    async function switchAccount() {
      if (!current) return { ok: false, cancelled: true };
      const provider = current.provider;
      try {
        await provider.request({ method: "wallet_requestPermissions", params: [{ eth_accounts: {} }] });
      } catch (e) {
        if (e && (e.code === 4001 || e.code === "ACTION_REJECTED")) return { ok: false, cancelled: true };
        /* wallet doesn't implement the permissions method → classic popup */
        try {
          await provider.request({ method: "eth_requestAccounts" });
        } catch (e2) {
          if (e2 && (e2.code === 4001 || e2.code === "ACTION_REJECTED")) return { ok: false, cancelled: true };
          throw e2;
        }
      }
      let accs = [];
      try { accs = await provider.request({ method: "eth_accounts" }); } catch { /* keep going — stale is fine */ }
      if (!accs || !accs.length) return { ok: false, cancelled: true };
      remember(current); /* re-affirm rdns + clear any logout marker */
      return { ok: true, address: ethers.getAddress(accs[0]) };
    }

    /* silent session restore: remembered rdns → announced match → bare
       eth_accounts. NEVER getSigner here — it would auto-pop
       eth_requestAccounts when the wallet is unauthorized.
       lib-6: the `logout:<rdns>` marker wins — after an explicit
       DISCONNECT, a refresh must NOT silently reconnect. */
    async function resume() {
      let rdns = null;
      try { rdns = localStorage.getItem(WALLET_RDNS_KEY); } catch { return null; }
      if (!rdns) return null;
      if (logoutMarked(rdns)) { forget(); return null; } /* explicit logout — stay out (marker persists) */
      await announcedReady;
      const entry = list().find((w) => w.info.rdns === rdns);
      if (!entry) return null; /* wallet absent this session — keep the memory */
      let accs = [];
      try { accs = await entry.provider.request({ method: "eth_accounts" }); } catch { return null; }
      if (!accs || !accs.length) { forget(); return null; } /* 无账户清存 */
      select(entry);
      return { provider: entry.provider, address: ethers.getAddress(accs[0]), name: entry.info.name };
    }

    return {
      list,
      selected: () => current,
      connectInteractive,
      resume,
      forget,
      logout,          /* lib-6: true disconnect (revoke best-effort + logout marker) */
      switchAccount,   /* lib-6: wallet_requestPermissions account re-pick */
      logoutMarked,    /* test/introspection: is `logout:<rdns>` set? */
      /* one encapsulation: raw picked provider → ethers wrapper */
      browserProvider: () => (current ? new ethers.BrowserProvider(current.provider) : null),
      request: (method, params) =>
        current ? current.provider.request({ method, params }) : Promise.reject(new Error("no wallet selected")),
      onChange(handlers) {
        if (handlers && handlers.accounts) subs.accounts.push(handlers.accounts);
        if (handlers && handlers.chain) subs.chain.push(handlers.chain);
      },
      _bound: () => binding, /* test introspection only */
    };
  })();

  return {
    cfg, cfgReady, chainIdHex,
    ESCROW_ABI, REGISTRY_ABI, ERC20_ABI, PAYMENT_STATES,
    PROMPT_TOKEN_CAP, COMPLETION_TOKEN_CAP,
    readProvider, registry, escrow, usdc,
    toNative, fmtUsdc, fmtUsdcTrim, fmtInt,
    esc, truncAddr, addrLink, txLink, joinUrl, hostOf, sameAddr, isZeroAddr,
    probeHealth, probeInfo, installCopyHandlers, fetchJson, fetchListing, fetchListings,
    priceFor, minAmountEstimate, maxMinAmountEstimate,
    marketBoard, probeListings, fetchSellerSet, fetchMarketListings, SELLER_PAGE,
    RELAY_CHAT_PATH, buildEip191Message,
    RECEIPT_DOMAIN_NAME, RECEIPT_DOMAIN_VERSION, RECEIPT_TYPES,
    decodeReceiptHeader, verifyReceipt,
    decodeEscrowErr, humanizeEscrowErr, lockShortfall,
    txLine, runTx, parseLockedPaymentId,
    locks, disputes, relayErrorCopy,
    wallet, WALLET_RDNS_KEY,
  };
})();
