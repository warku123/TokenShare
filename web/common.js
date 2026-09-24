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
    "event Registered(address indexed operator, string endpoint, string[] models)",
    "event PriceUpdated(address indexed operator, string model, uint256 cachedIn, uint256 input, uint256 output)",
    "event Deactivated(address indexed operator)",
    /* custom errors (contracts/src/Registry.sol) — declared so ethers
       decodes reverts into e.revert.name for human-readable UI copy */
    "error AlreadyRegistered()",
    "error NotActive()",
    "error ModelNotFound()",
    "error EmptyModels()",
    "error LengthMismatch()",
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
  const fmtInt = (native) => Number(native).toLocaleString("en-US");

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
        slot.title = "relay unreachable — or CORS not enabled yet (等待 relay CORS 配置)";
      }
    }));
    return online;
  }

  /* ── multi-seller discovery for market.html (M9) ────────────
     Sellers are FOUND by scanning Registry.Registered events, then
     enriched with getListing (freshness + active flag + prices).
     Event (contracts/src/Registry.sol, v2 keeps the v1 shape):
       event Registered(address indexed operator, string endpoint, string[] models)
     → topic0 = keccak256("Registered(address,string,string[])"); the
     operator sits in topics[1]. Monad caps eth_getLogs at 100 blocks per
     call → scan in 90-block windows and halve the window on failure.
     deactivate() deletes nothing: same-operator re-registers resolve by
     taking the LATEST event; live state always comes from getListing. */
  const REGISTERED_TOPIC = ethers.id("Registered(address,string,string[])");
  const SCAN_WINDOW = 90;   /* < Monad's 100-block eth_getLogs cap */
  const SCAN_FLOOR = 5;     /* give up halving below this → real outage */

  async function scanRegisteredLogs(fromBlock, toBlock, onProgress) {
    const prov = readProvider();
    /* precompute windows, then a small worker pool — sequential getLogs
       measures ~0.7s/window on the public RPC; 4 in flight keeps the
       first paint honest without hammering the endpoint */
    const windows = [];
    for (let s = fromBlock; s <= toBlock; s += SCAN_WINDOW) {
      windows.push([s, Math.min(toBlock, s + SCAN_WINDOW - 1)]);
    }
    const total = windows.length;
    let done = 0;
    let next = 0;
    const latest = new Map(); // lowercase operator → {operator, blockNumber}

    const fetchWindow = async ([s, e]) => {
      let w = e - s + 1;
      for (let attempt = 0; attempt < 8; attempt++) {
        try {
          return await prov.getLogs({
            address: cfg.registryAddr,
            topics: [REGISTERED_TOPIC],
            fromBlock: s,
            toBlock: Math.min(e, s + w - 1),
          });
        } catch (err) {
          const nw = Math.floor(w / 2);
          if (nw < SCAN_FLOOR) throw err; /* tiny window still failing → abort */
          w = nw;
        }
      }
      throw new Error("scan window failed after retries");
    };

    const worker = async () => {
      while (next < windows.length) {
        const win = windows[next++];
        const logs = await fetchWindow(win);
        for (const log of logs) {
          if (!log.topics || log.topics.length < 2) continue;
          try {
            const op = ethers.getAddress("0x" + log.topics[1].slice(26));
            const blk = Number(log.blockNumber);
            const prev = latest.get(op.toLowerCase());
            if (!prev || blk >= prev.blockNumber) latest.set(op.toLowerCase(), { operator: op, blockNumber: blk });
          } catch { /* malformed topic — skip */ }
        }
        done += 1;
        if (onProgress) onProgress(done, total);
      }
    };

    await Promise.all(Array.from({ length: Math.min(4, windows.length) }, worker));
    return [...latest.values()];
  }

  /* one bounded scan pass. Default: [head-depth+1 … head] with the floor
   * at cfg.registryFromBlock. `fromBlock` given → older extension chunk
   * ending at that block (load-earlier). Never scans below the floor. */
  async function discoverRange({ depth, fromBlock: earlierTo, onProgress } = {}) {
    const prov = readProvider();
    const head = await prov.getBlockNumber();
    const depthBlocks = Math.max(1, Number(depth ?? cfg.scanDepthBlocks) || 50000);
    const floorBlock = Math.max(0, Number(cfg.registryFromBlock) || 0);
    const to = earlierTo != null ? earlierTo : head;
    const from = Math.max(floorBlock, to - depthBlocks + 1);
    if (from > to) return { operators: [], from, to, head, floorBlock };
    const operators = await scanRegisteredLogs(from, to,
    onProgress ? (done, total) => onProgress(done, total, { from, to }) : undefined);
  return { operators, from, to, head, floorBlock };
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
        const msg = (err && (err.shortMessage || err.reason || err.message)) || "failed";
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
      case 402: return "payment proof rejected — maxAmount 低于卖家 minAmount 估计，调高金额后重新 lock";
      case 409: return "lock about to expire (ttl margin) — refund & re-lock with a longer ttl";
      case 502: return "upstream (official API) error — not settled; retry or refund after ttl";
      default: return null;
    }
  }

  return {
    cfg, cfgReady, chainIdHex,
    ESCROW_ABI, REGISTRY_ABI, ERC20_ABI, PAYMENT_STATES,
    PROMPT_TOKEN_CAP, COMPLETION_TOKEN_CAP,
    readProvider, registry, escrow, usdc,
    toNative, fmtUsdc, fmtUsdcTrim, fmtInt,
    esc, truncAddr, addrLink, txLink, joinUrl, hostOf, sameAddr, isZeroAddr,
    probeHealth, probeInfo, installCopyHandlers, fetchJson, fetchListing, fetchListings,
    priceFor, minAmountEstimate, maxMinAmountEstimate,
    marketBoard, probeListings, discoverRange, REGISTERED_TOPIC, SCAN_WINDOW,
    RELAY_CHAT_PATH, buildEip191Message,
    RECEIPT_DOMAIN_NAME, RECEIPT_DOMAIN_VERSION, RECEIPT_TYPES,
    decodeReceiptHeader, verifyReceipt,
    txLine, runTx, parseLockedPaymentId,
    locks, disputes, relayErrorCopy,
  };
})();
