/* TOKENSHARE — console (Seller / Buyer).
   Every state change goes through the user-picked wallet (EIP-6963
   discovery + selector in common.js; window.ethereum is only the
   zero-6963 fallback). Private keys never touch the page. Reads fall
   back to the public RPC so the market stays visible without a wallet. */
"use strict";
(() => {
  const T = window.TS;
  const cfg = T.cfg;

  /* ═══ M15 shared-relay custody — config gate ═══════════════
     cfg.m15 ABSENT → classic single-seller form, untouched.
     Present + complete → shared mode: the relay endpoint is pinned
     (no manual input), VERIFY seals the upstream key to the TEE.
     Present + malformed → fail-closed (custody disabled, hard error). */
  const m15v = T.custody.validateM15Config(cfg.m15);
  const shared = m15v.ok ? m15v.cfg : null;
  const m15Broken = !shared && cfg.m15 != null;

  const custodyState = {
    gen: 0,             // bumped by every upstream-URL/key edit + account change — anchors a verified/resumed catalog
    sessionStartGen: 0, // gen baseline at the last session reset — "session untouched" = gen still equals it (resume gate)
    sessionFailed: false, // a VERIFY failed this connection session — blocks catalog resume until a fresh VERIFY/reset
    pinsOk: false,      // live /info + /attestation pin matrix passed against config
    anchor: null,       // {kind:"verified"|"resumed", upstream|null, fingerprint, gen, address}
    keyStored: false,   // this-session POST success, or relay /status has_key
    fingerprint: null,  // relay-reported sha256 of the stored key (display only)
    delegate: undefined, // chain settleDelegateOf(me): undefined = unread, else lower-hex
    statusInfo: null,   // last public /sellers/{me}/status body (display only — never identity authority)
  };
  /* injected hashers — the custody core stays environment-agnostic */
  const hashers = {
    sha256Hex: async (bytes) => T.custody.bytesToHex(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))),
    keccak256Hex: (bytes) => ethers.keccak256(bytes).slice(2),
    keccak256TextHex: (s) => ethers.keccak256(ethers.toUtf8Bytes(s)).slice(2),
  };
  /* the register endpoint: pinned relay origin in shared mode, else the
     manual input. EVERY register/preview/gate read goes through here. */
  const formEndpoint = () => (shared ? shared.relayOrigin : $("s-endpoint").value.trim());

  document.documentElement.classList.add("js");

  const $ = (id) => document.getElementById(id);
  const state = {
    provider: null,   // BrowserProvider (wraps the picked raw provider)
    signer: null,
    address: null,
    walletName: "",   // picked wallet's display name (EIP-6963 info.name)
    listings: [],     // market listings (shared with buyer selects)
    myListing: null,
    lastPaymentId: null,
    escrowBal: null,  // live Escrow balances(me), native 6dp BigInt — drives the lock gate
  };

  /* ═══ config gate ═════════════════════════════════════════ */
  const configBanner = $("config-banner");
  if (!T.cfgReady()) {
    configBanner.hidden = false;
    configBanner.innerHTML =
      `<b>config.js not filled in</b> — after deployment, copy <code class="inl">escrowAddr / registryAddr</code> ` +
      `(and <code class="inl">sellers</code>) from <code class="inl">contracts/deployed.json</code>. ` +
      `Only the page skeleton renders; chain reads/writes are unavailable.`;
  }

  /* ═══ tabs ════════════════════════════════════════════════ */
  const tabBtns = { seller: $("tab-seller"), buyer: $("tab-buyer") };
  const panels = { seller: $("panel-seller"), buyer: $("panel-buyer") };
  function selectTab(name, push = true) {
    for (const k of Object.keys(tabBtns)) {
      tabBtns[k].classList.toggle("active", k === name);
      tabBtns[k].setAttribute("aria-selected", String(k === name));
      panels[k].hidden = k !== name;
    }
    if (push) history.replaceState(null, "", "#" + name);
  }
  tabBtns.seller.addEventListener("click", () => selectTab("seller"));
  tabBtns.buyer.addEventListener("click", () => selectTab("buyer"));
  /* default landing = BUYER (the judge's happy path; the seller flow needs
     a running relay). #seller stays explicit; card anchors (#card-deposit …)
     land on buyer and re-scroll once the panel is unhidden */
  const bootHash = location.hash;
  selectTab(bootHash === "#seller" ? "seller" : "buyer", false);
  if (/^#card-/.test(bootHash)) {
    const t = document.getElementById(bootHash.slice(1));
    if (t) requestAnimationFrame(() => t.scrollIntoView({ block: "start" }));
  }

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

  /* ═══ wallet bar ══════════════════════════════════════════ */
  const connectBtn = $("wallet-connect");
  const walletInfo = $("wallet-info");
  const walletAddr = $("wallet-addr");
  const netBadge = $("net-badge");
  const usdcBal = $("wallet-usdc");
  const switchBtn = $("wallet-switch");
  const disconnBtn = $("wallet-disconnect");
  const walletNote = $("wallet-note");
  let noteTimer = null;

  function renderWallet() {
    const ops = $("wallet-ops");
    if (!state.address) {
      connectBtn.hidden = false;
      walletInfo.hidden = true;
      if (ops) ops.hidden = true;
      return;
    }
    connectBtn.hidden = true;
    walletInfo.hidden = false;
    if (ops) ops.hidden = false;
    walletAddr.textContent = T.truncAddr(state.address);
    walletAddr.href = T.addrLink(state.address);
    walletAddr.dataset.copy = state.address;
    walletAddr.title = `${state.address}${state.walletName ? ` · via ${state.walletName}` : ""} — click copies, ⧉ explorer via market page`;
    const nameEl = $("wallet-name");
    if (nameEl) nameEl.textContent = (state.walletName || "").toUpperCase() || "WALLET";
  }

  /* transient wallet-bar note (logout hints, switch failures) */
  function showWalletNote(html, ms = 12000) {
    walletNote.innerHTML = html;
    walletNote.hidden = false;
    clearTimeout(noteTimer);
    noteTimer = setTimeout(() => { walletNote.hidden = true; }, ms);
  }

  /* wipe every local trace of the connection — shared by DISCONNECT and
     accountsChanged([]). Balance displays reset to placeholders; MY LISTING
     returns to its connect-wallet empty state. */
  function clearConnState() {
    state.signer = null;
    state.address = null;
    state.walletName = "";
    state.escrowBal = null;
    usdcBal.textContent = "—";
    const mirror = $("b-wallet-usdc-mirror");
    if (mirror) mirror.textContent = "see wallet bar ↑";
    $("b-escrow-bal").textContent = "—";
    $("wallet-faucets").hidden = true;
    /* policy consent is wallet-bound — disconnect / account-switch
       invalidates it (record dropped, re-tick required) */
    T.policyConsent.clear();
    syncPolicyGate(); /* consent checkbox back to its disconnected (disabled) face */
    renderWallet();
    resetCustodySession(); /* M15: account context wiped with the connection */
    loadMyListing();
  }

  function renderNet(ok) {
    netBadge.className = "net-badge " + (ok ? "ok" : "bad");
    netBadge.textContent = ok
      ? `${(cfg.chainName || "chain").toUpperCase()} · ${cfg.chainId}`
      : `WRONG NETWORK — switch to ${cfg.chainName} (${cfg.chainId})`;
    netBadge.title = ok ? "" : "click to switch";
    netBadge.style.cursor = ok ? "" : "pointer";
    const banner = $("net-banner");
    banner.hidden = ok;
    $("net-banner-name").textContent = `${cfg.chainName} (chainId ${cfg.chainId})`;
  }

  async function switchChain() {
    try {
      await ensureChain();
      await finishConnect(false);
    } catch (e) {
      if (!(e && (e.code === 4001 || e.code === "ACTION_REJECTED"))) console.error("chain switch failed:", e);
    }
  }
  $("net-switch").addEventListener("click", switchChain);
  netBadge.addEventListener("click", () => { if (netBadge.classList.contains("bad")) switchChain(); });

  async function refreshBalances() {
    if (!state.address || !T.cfgReady()) return;
    let walletUsdc = null;
    try {
      const bal = await T.usdc(T.readProvider()).balanceOf(state.address);
      walletUsdc = bal;
      usdcBal.textContent = `$${T.fmtUsdc(bal)}`;
      usdcBal.title = `${T.fmtInt(bal)} native units`;
      const mirror = $("b-wallet-usdc-mirror");
      if (mirror) mirror.textContent = `$${T.fmtUsdc(bal)}`;
    } catch { usdcBal.textContent = "—"; }
    try {
      const eb = await T.escrow(T.readProvider()).balances(state.address);
      state.escrowBal = eb;
      $("b-escrow-bal").textContent = `$${T.fmtUsdc(eb)}`;
      $("b-escrow-bal").title = `${T.fmtInt(eb)} native units`;
    } catch { state.escrowBal = null; $("b-escrow-bal").textContent = "—"; }
    /* faucet hint: connected but dry — gas MON nearly out (18dp native),
       or zero USDC in both wallet and escrow */
    try {
      const mon = await T.readProvider().getBalance(state.address);
      const dry = mon < 5000000000000000n /* 0.005 MON */ ||
        (walletUsdc === 0n && state.escrowBal === 0n);
      $("wallet-faucets").hidden = !dry;
    } catch { /* leave the hint as-is on a flaky read */ }
    syncLockGate(); /* lock form mirrors the balance + re-gates the button */
  }

  async function ensureChain() {
    const hex = T.chainIdHex();
    try {
      await T.wallet.request("wallet_switchEthereumChain", [{ chainId: hex }]);
      return true;
    } catch (e) {
      if (e && e.code === 4902) {
        await T.wallet.request("wallet_addEthereumChain", [{
          chainId: hex,
          chainName: cfg.chainName,
          nativeCurrency: cfg.nativeCurrency,
          rpcUrls: [cfg.rpcUrl],
          blockExplorerUrls: [cfg.explorer],
        }]);
        return true;
      }
      throw e;
    }
  }

  /* shared post-connect tail — the wallet layer already holds the picked
     raw provider; the BrowserProvider wrap lives HERE (single place).
     getSigner stays popup-free: accounts were authorized by the selector's
     eth_requestAccounts (interactive) or verified by bare eth_accounts
     (silent resume) before we get here. */
  async function finishConnect(autoSwitch) {
    state.provider = T.wallet.browserProvider();
    const net = await state.provider.getNetwork();
    if (Number(net.chainId) !== Number(cfg.chainId)) {
      renderNet(false);
      if (!autoSwitch) { renderWallet(); return; }
      await ensureChain();
      state.provider = T.wallet.browserProvider(); /* re-create after a possible switch */
    }
    state.signer = await state.provider.getSigner();
    state.address = await state.signer.getAddress();
    const sel = T.wallet.selected();
    state.walletName = (sel && sel.info && sel.info.name) || "";
    renderNet(true);
    renderWallet();
    syncPolicyGate(); /* fresh address → consent re-resolves (new wallet = fresh tick) */
    await Promise.all([refreshBalances(), loadMyListing(), loadListingsIntoSelects()]);
    /* M15: fresh account context → re-run the live pin check, read the
       delegate from chain, and offer the relay-stored catalog (resume) */
    if (shared) refreshCustodySession().catch(() => {});
  }

  /* CONNECT → the EIP-6963 selector modal; the single
     eth_requestAccounts popup fires only after the user picks a row */
  async function connect(autoSwitch = true) {
    try {
      const picked = await T.wallet.connectInteractive();
      if (!picked) { renderWallet(); return; } /* cancelled, or the no-wallet empty state */
      await finishConnect(autoSwitch);
    } catch (e) {
      renderNet(false);
      connectBtn.textContent = "[ CONNECT WALLET ]";
      const rejected = e && (e.code === 4001 || e.code === "ACTION_REJECTED");
      if (!rejected) console.error("wallet connect failed:", e);
    }
  }

  connectBtn.addEventListener("click", () => connect(true));

  /* events ride the RAW picked provider inside the wallet layer (ethers
     BrowserProvider forwards neither); the layer removeListener's the old
     provider whenever the pick changes */
  T.wallet.onChange({
    accounts: (accs) => {
      clearConnState();
      /* non-empty = account switch inside the same wallet → silent re-sync,
         no picker, no popup; empty = disconnected (the layer already forgot
         rdns and cleared its own state — no logout marker is written) */
      if (accs && accs.length) finishConnect(false).catch((e) => console.error("account switch failed:", e));
    },
    chain: () => window.location.reload(),
  });
  /* silent resume: remembered rdns → bare eth_accounts — never pops; the
     layer refuses to resume while `logout:<rdns>` is set (lib-6) */
  T.wallet.resume()
    .then((r) => { if (r) return finishConnect(false); })
    .catch(() => {});
  renderWallet();

  /* [ SWITCH ACCOUNT ] — wallet_requestPermissions re-opens the account
     picker (MetaMask pops it even when already connected). 4001 = user
     cancelled → state untouched. Calibration runs inside the wallet layer
     (raw eth_accounts, never getSigner); finishConnect re-syncs the page. */
  switchBtn.addEventListener("click", () => guard(switchBtn, async () => {
    try {
      const r = await T.wallet.switchAccount();
      if (!r || !r.ok) return; /* cancelled — keep everything as-is */
      await finishConnect(false); /* event may fire too — both paths are idempotent */
    } catch (e) {
      console.error("account switch failed:", e);
      showWalletNote(`account switch failed — ${T.esc(e.shortMessage || e.message || "unknown error")}`);
    }
  }));

  /* [ DISCONNECT ] — true logout (lib-6): best-effort wallet_revokePermissions
     (MetaMask truly revokes; OKX/Coinbase ignore it → manual hint) + the
     persistent `logout:<rdns>` marker (refresh will not auto-reconnect).
     Logging out does NOT touch on-chain USDC approvals — those live in the
     token contract and need their own revoke. */
  disconnBtn.addEventListener("click", () => guard(disconnBtn, async () => {
    const r = await T.wallet.logout();
    clearConnState();
    const head = r && r.manual
      ? `logged out locally — <b>also disconnect this site inside your wallet</b> (it ignores programmatic revoke).`
      : `logged out — site permission revoked in the wallet.`;
    showWalletNote(
      `${head} refresh will not auto-reconnect.<br>` +
      `<span class="dim">on-chain USDC approvals (approve) are separate and unaffected — revoke them on-chain if needed.</span>`
    );
  }));

  const needWallet = () => {
    if (!state.signer) { connectBtn.focus(); connectBtn.classList.add("flash"); setTimeout(() => connectBtn.classList.remove("flash"), 900); return true; }
    return false;
  };

  /* button anti-double-click: disable for the duration of the async op */
  const guard = async (btn, fn) => {
    if (btn.disabled) return;
    btn.disabled = true;
    try { await fn(); } finally { btn.disabled = false; }
  };

  /* ═══ policy consent gate (web policy reading consent) ═════
     Paid seller/buyer actions require the reading consent bound to the
     policy version + connected wallet. Storage + validity live in
     common.js T.policyConsent (sessionStorage "tokenshare.policyConsent.v1"
     = {"version","wallet","ts"}; a version bump / account switch /
     disconnect invalidates it → re-tick required). Two layers: gated
     buttons render disabled with a hint, AND every paid handler
     re-checks at entry — a devtools re-enable throws
     PolicyConsentRequired and never reaches a tx / signature popup.
     Exit paths stay open: refund / withdraw / deactivate / removeModel /
     buyer key revoke / seller custody key+delegate revoke / disconnect /
     switch. The consent itself never asks for a wallet signature. */
  class PolicyConsentRequired extends Error {
    constructor() {
      super("policy consent required — tick the Policy & Risks checkbox above first");
      this.name = "PolicyConsentRequired";
      this.code = "POLICY_CONSENT_REQUIRED";
    }
  }
  const policyBox = () => $("policy-agree");
  const POLICY_GATE_TITLE =
    "disabled — tick the Policy & Risks consent checkbox (top of page) to enable this paid action";
  const consentOk = () => T.policyConsent.isValid(state.address);
  const requirePolicyConsent = () => { if (!consentOk()) throw new PolicyConsentRequired(); };

  /* flash the consent bar + focus the checkbox — the guard's UX tail */
  const consentNudge = () => {
    const bar = $("policy-consent");
    if (bar) {
      bar.classList.remove("flash-bd");
      void bar.offsetWidth;
      bar.classList.add("flash-bd");
      bar.scrollIntoView({ block: "nearest" });
    }
    const cb = policyBox();
    if (cb && !cb.disabled) cb.focus({ preventScroll: true });
  };

  /* guard wrapper for PAID actions only: consent gate runs first; any
     other error keeps the original guard() semantics (propagates) */
  const guardPaid = (btn, fn) => {
    const p = guard(btn, async () => {
      try { requirePolicyConsent(); await fn(); }
      catch (e) {
        if (e instanceof PolicyConsentRequired) { consentNudge(); return; }
        throw e;
      }
    });
    /* guard() re-enabled the button in its finally — re-apply the consent
       disabled-state (the checkbox may have flipped while the tx ran) */
    return p.finally(syncPolicyGate);
  };

  /* Statically-wired paid-action buttons. b-lock-btn is deliberately
     absent — syncLockGate co-owns its disabled state (balance gate ∨
     consent gate). Dynamic [ MINT API KEY ] rows gate inside
     renderSessionLocks. Exit paths (revokes / deactivations) are never
     listed here. Only buttons THIS gate disabled get re-enabled —
     pre-existing disables (e.g. m15Broken s-verify-btn) stay untouched. */
  const POLICY_GATED_BTNS = ["s-submit", "s-verify-btn", "sc-authorize", "b-dep-btn", "c-send"];
  function syncPolicyGate() {
    const ok = consentOk();
    const cb = policyBox();
    if (cb) {
      cb.disabled = !state.address; /* consent binds to a wallet */
      cb.checked = ok;              /* mirror the stored record */
      const hint = $("policy-consent-hint");
      if (hint) {
        hint.textContent = state.address
          ? "required before paid actions (register · custody verify/authorize · deposit · lock · mint key · calls) — refunds, withdrawals, deactivation and revokes stay open."
          : "connect a wallet to confirm — required before paid actions (register · custody verify/authorize · deposit · lock · mint key · calls). refunds, withdrawals, deactivation and revokes stay open.";
      }
    }
    for (const id of POLICY_GATED_BTNS) {
      const b = $(id);
      if (!b) continue;
      if (!ok) {
        if (!b.disabled) { b.disabled = true; b.dataset.policyDisabled = "1"; }
        if (!b.title) { b.title = POLICY_GATE_TITLE; b.dataset.policyTitle = "1"; }
      } else {
        if (b.dataset.policyDisabled) { b.disabled = false; delete b.dataset.policyDisabled; }
        if (b.dataset.policyTitle) { b.removeAttribute("title"); delete b.dataset.policyTitle; }
      }
    }
    renderSessionLocks(); /* MINT rows mirror the consent state */
    syncLockGate();       /* lock button: balance gate ∨ consent gate */
  }

  if (policyBox()) policyBox().addEventListener("change", () => {
    const cb = policyBox();
    if (!state.address) { cb.checked = false; return; } /* cb is disabled then — belt */
    if (cb.checked) T.policyConsent.set(state.address);
    else T.policyConsent.clear();
    syncPolicyGate();
  });

  const needConfig = () => {
    if (T.cfgReady()) return false;
    configBanner.hidden = false;
    configBanner.classList.remove("flash-bd");
    void configBanner.offsetWidth;
    configBanner.classList.add("flash-bd");
    return true;
  };

  /* ═══ shared: listings → selects ══════════════════════════ */
  async function loadListingsIntoSelects() {
    if (!T.cfgReady()) return;
    try {
      /* L1/M10: on-chain enumeration is the primary source (multi-seller
         ready); fetchMarketListings falls back to config.js sellers when
         the enumeration calls are unavailable — never throws */
      const res = await T.fetchMarketListings(T.readProvider());
      state.listings = res.listings.filter((l) => l.registered);
    } catch { return; }
    /* feed the searchable pickers (they keep the picked operator in the
       hidden inputs b-seller / c-seller across refreshes) */
    bSellerPick.refresh();
    cSellerPick.refresh();
    syncSellerInfo();
    syncCallModels();
    /* api-key rows resolve base_url/usage off listingOf(seller) — re-render
       now that fresh listings are in (rows degrade gracefully before this) */
    renderApiKeys();
  }

  const listingOf = (op) => state.listings.find((l) => T.sameAddr(l.operator, op));

  /* ═══ SELLER tab ══════════════════════════════════════════ */

  /* — my listing card — */
  const deactBtn = $("s-deactivate");
  const myTx = $("s-my-tx");

  async function loadMyListing({ prefill = true } = {}) {
    const box = $("s-my-listing");
    deactBtn.hidden = true; /* until fresh chain truth says active */
    if (!state.address || !T.cfgReady()) {
      box.innerHTML = `<p class="empty-hint">connect a wallet to view your Registry listing.</p>`;
      return;
    }
    box.innerHTML = `<p class="empty-hint">reading getListing(${T.esc(T.truncAddr(state.address))}) …</p>`;
    let l;
    try { l = await T.fetchListing(T.readProvider(), state.address); }
    catch (e) { box.innerHTML = `<p class="empty-hint err">read failed — ${T.esc(e.shortMessage || e.message)}</p>`; return; }
    state.myListing = l.registered ? l : null;

    if (!l.registered) {
      box.innerHTML =
        `<p class="empty-hint">not registered yet — load the relay's verified model face in the register form, ` +
        `then submit <code class="inl">Registry.register</code>; the listing appears on the market immediately.</p>`;
    } else {
      /* v2: one price triple per model — group rows by model.
         M12 (Registry v4): each row carries a small delist control
         (removeModel, single tx) — deliberately lower-key than the
         card-level [ DEACTIVATE ALL ]. The LAST model's control is
         disabled (contract guards RemoveLastModel): listing-level
         offboarding stays on the big button. removeModel has no
         active requirement → controls render for INACTIVE listings too. */
      const multiModel = l.models.length > 1;
      const priceRows = l.models.map((m) => {
        const p = T.priceFor(l, m);
        const del = multiModel
          ? `<button type="button" class="mdel" data-model="${T.esc(m)}" title="delist this model — removeModel(&quot;${T.esc(m)}&quot;) · single tx · stops serving immediately">[ REMOVE ]</button>`
          : `<button type="button" class="mdel" disabled title="last model — use [ DEACTIVATE ALL ] below · contract guards RemoveLastModel">[ REMOVE ]</button>`;
        return `<div class="mpr"><span class="mtag">${T.esc(m)}</span>` +
          (p
            ? `<span class="mono">cached $${T.fmtUsdc(p.cachedIn)} · in $${T.fmtUsdc(p.input)} · out $${T.fmtUsdc(p.output)}</span>`
            : `<span class="dim">no price on-chain</span>`) +
          del +
          `</div>`;
      }).join("");
      box.innerHTML =
        `<div class="kv"><span>STATUS</span><b>${l.active
          ? `<span class="badge">ACTIVE</span>`
          : `<span class="badge off">INACTIVE</span>`}</b></div>` +
        `<div class="kv"><span>ENDPOINT</span><b class="mono wrap-anywhere">${T.esc(l.endpoint)}</b></div>` +
        `<div class="kv"><span>MODELS</span><b>${l.models.map((m) => `<span class="mtag">${T.esc(m)}</span>`).join(" ") || "—"}</b></div>` +
        `<div class="kv"><span>PRICES /1M</span><b class="${l.active ? "" : "weak"}">${priceRows || "—"}</b></div>` +
        (l.active ? "" :
          `<p class="empty-hint deact-note">inactive — the market turns this listing INACTIVE gray and buyers can no longer lock; in-flight (Locked) payments still settle normally. ` +
          `Re-check LISTING ACTIVE in the form and submit <code class="inl">register</code> to restore it.</p>`);
      deactBtn.hidden = !l.active;
      /* M7 touchpoint: TEE / upstream-policy rows via GET /info — silent degrade */
      T.probeInfo(l.endpoint).then((info) => {
        if (!info) return;
        const rows =
          `<div class="kv"><span>TEE</span><b>${info.teeEnabled
            ? `<a class="ok" href="${T.esc(T.joinUrl(l.endpoint, "/attestation"))}" target="_blank" rel="noopener">✓ TEE attested — view quote ↗</a>`
            : `<span class="dim">off (env key)</span>`}</b></div>` +
          (info.upstreamHost
            ? `<div class="kv"><span>UPSTREAM</span><b class="mono">${T.esc(info.upstreamHost)} <span class="${info.official ? "ok" : "bad"}">${info.official ? "· official" : "· CUSTOM (dev)"}</span></b></div>`
            : "");
        box.insertAdjacentHTML("beforeend", rows);
      }).catch(() => {});
      /* prefill form (skipped when refreshing for the AlreadyRegistered
         two-step retry — user edits must survive). v2: base inputs take the
         FIRST model's price; every model's own triple seeds modelPrices. */
      if (prefill) {
        $("s-endpoint").value = l.endpoint;
        const first = l.prices[0];
        if (first) {
          $("s-price-cached").value = T.fmtUsdc(first.cachedIn);
          $("s-price-input").value = T.fmtUsdc(first.input);
          $("s-price-output").value = T.fmtUsdc(first.output);
        }
        l.models.forEach((m, i) => {
          const p = l.prices[i];
          if (p) modelPrices.set(m, { c: T.fmtUsdc(p.cachedIn), i: T.fmtUsdc(p.input), o: T.fmtUsdc(p.output) });
        });
        lastBase = readBase();
        $("s-active").checked = l.active;
        renderModelNote(); /* endpoint changed → re-evaluate precheck-source match */
      }
    }
    updatePreview();
  }

  /* — models: verified via relay /verify-upstream, chips-only selection —
       the seller can only register models the relay's upstream key can
       actually call; no free-text, no phantom model kinds. */
  const modelsState = {
    status: "idle",   // idle | loading | ok | error
    base: null,       // endpoint the accessible set was verified against
    accessible: [],   // relay-tested model face (upstream /v1/models) — shared mode: SERVABLE names only
    entries: null,    // shared mode: full catalog [{model, servable, reason}] — non-servable render greyed, unselectable
    checked: new Set(), // authoritative user picks — survives loading re-renders
    reason: "",       // human reason when status === "error"
  };

  function selectedModels() {
    return [...document.querySelectorAll("#s-model-checks input[name=s-model]:checked")].map((i) => i.value);
  }

  /* ═══ per-model prices (Registry v2) ═══════════════════════
     The three base inputs price the FIRST model; every checked model gets
     its own triple in #s-model-prices, seeded from the base values and
     editable. Fields still equal to the previous base value keep following
     base edits (live inheritance); user-edited cells stick. */
  const modelPrices = new Map();   // model → {c, i, o} decimal strings
  let lastBase = { c: "", i: "", o: "" };

  const readBase = () => ({
    c: $("s-price-cached").value.trim(),
    i: $("s-price-input").value.trim(),
    o: $("s-price-output").value.trim(),
  });

  function ensurePriceSeed(model) {
    if (!modelPrices.has(model)) {
      const b = readBase();
      modelPrices.set(model, { c: b.c, i: b.i, o: b.o });
    }
    return modelPrices.get(model);
  }

  function renderPriceRows() {
    const zone = $("s-model-prices");
    const models = selectedModels();
    if (!models.length) { zone.innerHTML = ""; return; }
    const rows = models.map((m) => {
      const p = ensurePriceSeed(m);
      return (
        `<div class="mprice" data-model="${T.esc(m)}">` +
        `<span class="mprice-name" title="${T.esc(m)}">${T.esc(m)}</span>` +
        `<input type="text" inputmode="decimal" data-f="c" value="${T.esc(p.c)}" placeholder="cached" aria-label="${T.esc(m)} cached-in price, USDC per 1M tokens">` +
        `<input type="text" inputmode="decimal" data-f="i" value="${T.esc(p.i)}" placeholder="input" aria-label="${T.esc(m)} input price, USDC per 1M tokens">` +
        `<input type="text" inputmode="decimal" data-f="o" value="${T.esc(p.o)}" placeholder="output" aria-label="${T.esc(m)} output price, USDC per 1M tokens">` +
        `</div>`
      );
    }).join("");
    zone.innerHTML =
      `<div class="mprice mprice-head" aria-hidden="true"><span>PER-MODEL PRICE</span><span>CACHED</span><span>INPUT</span><span>OUTPUT</span></div>` +
      rows;
  }

  /* write state → rendered inputs without clobbering the focused cell */
  function syncPriceRows() {
    document.querySelectorAll("#s-model-prices .mprice[data-model]").forEach((row) => {
      const p = modelPrices.get(row.dataset.model);
      if (!p) return;
      for (const f of ["c", "i", "o"]) {
        const inp = row.querySelector(`input[data-f="${f}"]`);
        if (inp && inp !== document.activeElement && inp.value !== p[f]) inp.value = p[f];
      }
    });
  }

  /* base edit → fields still inheriting (== previous base) follow along */
  function onBaseInput() {
    const nb = readBase();
    for (const p of modelPrices.values()) {
      if (p.c === lastBase.c) p.c = nb.c;
      if (p.i === lastBase.i) p.i = nb.i;
      if (p.o === lastBase.o) p.o = nb.o;
    }
    lastBase = nb;
    syncPriceRows();
    updatePreview();
  }

  /* parse a "USDC per 1M" decimal string to native 6dp, null when invalid */
  function parsePriceStr(v) {
    v = (v || "").trim();
    if (!v || isNaN(Number(v)) || Number(v) < 0) return null;
    try { return T.toNative(v); } catch { return null; } /* >6dp decimals etc. */
  }

  /* form prices as native BigInts, keyed by model, in selected order */
  function collectPrices() {
    const models = selectedModels();
    const prices = new Map();
    const bad = [];
    for (const m of models) {
      const s = modelPrices.get(m) || { c: "", i: "", o: "" };
      const c = parsePriceStr(s.c), i = parsePriceStr(s.i), o = parsePriceStr(s.o);
      if (c === null || i === null || o === null) bad.push(m);
      prices.set(m, { c, i, o });
    }
    return { models, prices, bad };
  }

  /* which models' form prices differ from the on-chain triples */
  const changedModels = (mine, models, prices) =>
    models.filter((m) => {
      const onchain = T.priceFor(mine, m);
      const form = prices.get(m);
      if (!onchain || !form || form.c === null || form.i === null || form.o === null) return true;
      return onchain.cachedIn !== form.c || onchain.input !== form.i || onchain.output !== form.o;
    });

  /* submit gate: models must come from a successful precheck of the
     endpoint currently in the form */
  const modelsGateOk = (endpoint) =>
    modelsState.status === "ok" && modelsState.base === endpoint && modelsState.accessible.length > 0;
  function gateReason() {
    if (modelsState.status === "loading") return "model precheck in flight — wait for the result before submitting";
    if (modelsState.status === "error") return `model precheck failed (${modelsState.reason}) — fix it, then LOAD FROM RELAY again`;
    if (modelsState.status === "ok") return `endpoint differs from the prechecked source (${modelsState.base}) — re-run LOAD FROM RELAY on the current endpoint`;
    return "run LOAD FROM RELAY first — only models the relay can actually call may be listed (no phantom models)";
  }

  /* M15 shared-mode PUBLISH gate: the pure publishReady predicate
     (unit-tested in scripts/check-custody-web.mjs) + the catalog-source
     pin. Failed verify / edited URL-or-key / account switch / zero
     servable models all land here as hard blocks. */
  function sharedGate() {
    const g = T.custody.publishReady({
      pinsOk: custodyState.pinsOk,
      verifyStatus: modelsState.status,
      anchor: custodyState.anchor,
      gen: custodyState.gen,
      address: state.address,
      keyStored: custodyState.keyStored,
      selectedCount: selectedModels().length,
      servableCount: modelsState.accessible.length,
    });
    if (g.ok && modelsState.base !== shared.relayOrigin) {
      return { ok: false, reason: "catalog source drifted from the pinned relay origin — VERIFY again" };
    }
    return g;
  }

  function renderModelNote() {
    const note = $("s-models-note");
    const endpoint = formEndpoint();
    if (modelsState.status === "loading") {
      note.className = "model-verify-note";
      note.textContent = shared ? "VERIFY in flight — the relay is probing the live catalog …" : `GET ${modelsState.base}/verify-upstream …`;
    } else if (modelsState.status === "error") {
      note.className = "model-verify-note err";
      note.textContent = `✗ ${modelsState.reason} — fix and re-run the ${shared ? "VERIFY" : "precheck"}`;
    } else if (modelsState.status === "ok") {
      if (endpoint !== modelsState.base) {
        note.className = "model-verify-note warn";
        note.textContent = `⚠ endpoint changed (prechecked ${T.hostOf(modelsState.base)}) — re-run before submitting`;
      } else if (shared) {
        const greyed = (modelsState.entries || []).filter((e) => !e.servable).length;
        note.className = "model-verify-note ok";
        note.textContent = `✓ TEE-verified catalog — ${modelsState.accessible.length} servable model(s)${greyed ? ` · ${greyed} greyed (this key cannot serve them)` : ""} — pick from servable only`;
      } else {
        note.className = "model-verify-note ok";
        note.textContent = `✓ ${T.hostOf(modelsState.base)} can actually call ${modelsState.accessible.length} model(s) — pick from these only`;
      }
    } else {
      note.className = "model-verify-note";
      note.textContent = shared
        ? "not verified yet — run [ VERIFY + SEAL KEY ] in KEY CUSTODY above; only catalog models the relay can actually call become checkable"
        : "not prechecked yet — load the real callable model face from the relay before submitting";
    }
  }

  function renderModelZone() {
    const zone = $("s-model-checks");
    $("s-load-models").disabled = modelsState.status === "loading";
    if (modelsState.status === "ok") {
      /* shared mode renders the FULL catalog: non-servable models are
         greyed, disabled, and carry the relay's reason — visible but
         never selectable, so a key's real capability boundary shows */
      zone.innerHTML = (modelsState.entries || modelsState.accessible.map((m) => ({ model: m, servable: true }))).map((e) =>
        e.servable
          ? `<label class="mcheck"><input type="checkbox" name="s-model" value="${T.esc(e.model)}"${modelsState.checked.has(e.model) ? " checked" : ""}><span>${T.esc(e.model)}</span></label>`
          : `<label class="mcheck off" title="${T.esc(e.reason || "the stored key cannot serve this model")}"><input type="checkbox" disabled><span>${T.esc(e.model)}</span></label>`
      ).join("");
    } else {
      zone.innerHTML = "";
    }
    renderPriceRows(); /* rows follow the checked set; reseeds new checks */
    renderModelNote();
    updatePreview();
  }

  /* successful precheck → chips = accessible_models; check-state rules:
       re-verify (wasOk): keep user picks, newly-discovered models default on
       first verify with listing: pre-check listed ∩ accessible
       otherwise: default = all checked
       (wasOk must be captured by the caller BEFORE flipping to "loading") */
  function applyVerified(base, accessible, wasOk) {
    const prevChecked = modelsState.checked;
    const prevAccessible = modelsState.accessible;
    modelsState.status = "ok";
    modelsState.base = base;
    modelsState.accessible = accessible.slice();
    modelsState.reason = "";
    let checked;
    if (wasOk) {
      checked = new Set();
      for (const m of accessible) {
        if (prevChecked.has(m) || !prevAccessible.includes(m)) checked.add(m);
      }
    } else if (state.myListing && state.myListing.models.length) {
      checked = new Set(accessible.filter((m) => state.myListing.models.includes(m)));
    } else {
      checked = new Set(accessible);
    }
    modelsState.checked = checked;
    renderModelZone();
  }

  function failVerify(reason) {
    modelsState.status = "error";
    modelsState.reason = reason;
    modelsState.accessible = [];
    renderModelZone();
  }

  /* register-form inline precheck (merged into the form — no separate
     card): LOAD FROM RELAY → GET {endpoint}/verify-upstream → success
     renders accessible_models as checkable chips into the price table;
     every failure state renders inline in the form area too */
  async function runVerify(base) {
    const detail = $("s-verify-detail");
    base = (base || "").trim().replace(/\/+$/, "");
    if (!base) {
      failVerify("endpoint is empty — fill RELAY ENDPOINT first");
      detail.innerHTML = `<p class="empty-hint err">fill RELAY ENDPOINT first — step 1 of this form</p>`;
      return;
    }
    $("s-endpoint").value = base;
    const wasOk = modelsState.status === "ok"; /* before the loading flip */
    modelsState.status = "loading";
    modelsState.base = base;
    renderModelZone();
    const r = await T.fetchJson(T.joinUrl(base, "/verify-upstream"), {}, 20000);
    if (r.corsOrNetwork) {
      failVerify(`relay unreachable (${r.error === "timeout" ? "timeout" : "network/CORS"})`);
      detail.innerHTML =
        `<p class="empty-hint err">relay unreachable — ${r.error === "timeout" ? "timed out after 20s" : "network error or CORS not enabled"}. ` +
        `<button class="btn btn-sm" id="s-verify-retry" type="button">[ RETRY ]</button></p>`;
      $("s-verify-retry").addEventListener("click", () => runVerify($("s-endpoint").value));
      return;
    }
    if (!r.ok || !r.body) {
      failVerify(`verify-upstream HTTP ${r.status}`);
      detail.innerHTML = `<p class="empty-hint err">HTTP ${r.status} — verify-upstream unavailable. ` +
        `<button class="btn btn-sm" id="s-verify-retry" type="button">[ RETRY ]</button></p>`;
      $("s-verify-retry").addEventListener("click", () => runVerify($("s-endpoint").value));
      return;
    }
    const b = r.body;
    const accessible = Array.isArray(b.accessible_models) ? b.accessible_models : [];
    /* relay shape: mismatches = [{model, reason}] (objects, not strings) */
    const mismatches = (b.mismatches || []).map((m) =>
      (m && typeof m === "object") ? `${m.model} — ${m.reason}` : String(m));

    if (!b.key_valid) {
      failVerify("upstream key invalid (key_valid=false) — the relay has no real model face");
      renderVerifyDetail(b, accessible, mismatches);
      return;
    }
    if (!accessible.length) {
      failVerify("relay returned an empty model face (accessible_models is empty)");
      renderVerifyDetail(b, accessible, mismatches);
      return;
    }
    applyVerified(base, accessible, wasOk);
    renderVerifyDetail(b, accessible, mismatches);
  }

  /* verify diagnostics rendered INLINE inside the register form (the old
     standalone precheck card is gone): key validity, upstream host,
     accessible face, listing consistency, mismatches */
  function renderVerifyDetail(b, accessible, mismatches) {
    const want = new Set(selectedModels());
    const diffExtra = [...want].filter((m) => !accessible.includes(m));
    $("s-verify-detail").innerHTML =
      `<div class="kv"><span>KEY</span><b class="${b.key_valid ? "ok" : "bad"}">${b.key_valid ? "✓ valid" : "✗ INVALID"}</b></div>` +
      `<div class="kv"><span>UPSTREAM</span><b class="mono">${T.esc(b.upstream_host || "—")}</b></div>` +
      (b.error ? `<div class="kv"><span>ERROR</span><b class="bad mono wrap-anywhere">${T.esc(b.error)}</b></div>` : "") +
      `<div class="kv"><span>ACCESSIBLE</span><b>${accessible.length ? accessible.map((m) => `<span class="mtag">${T.esc(m)}</span>`).join(" ") : "—"}</b></div>` +
      `<div class="kv"><span>LISTING ON-CHAIN</span><b class="${b.listing_ok ? "ok" : "dim"}">${b.listing_ok ? "✓ consistent" : "✗ mismatch / not registered"}</b></div>` +
      (b.listed_models && b.listed_models.length ? `<div class="kv"><span>LISTED</span><b>${b.listed_models.map((m) => `<span class="mtag">${T.esc(m)}</span>`).join(" ")}</b></div>` : "") +
      (mismatches.length ? `<div class="kv"><span>MISMATCHES</span><b class="bad">${mismatches.map((m) => `<span class="mtag">${T.esc(m)}</span>`).join(" ")}</b></div>` : "") +
      (diffExtra.length ? `<div class="kv"><span>FORM vs KEY</span><b class="bad">selected but not accessible: ${diffExtra.map((m) => `<span class="mtag">${T.esc(m)}</span>`).join(" ")}</b></div>` : "");
  }

  $("s-load-models").addEventListener("click", () => guard($("s-load-models"), () => runVerify($("s-endpoint").value)));
  /* chips are rendered dynamically → delegated change listener keeps the
     authoritative checked set in sync */
  $("s-model-checks").addEventListener("change", (e) => {
    if (!e.target.matches("input[name=s-model]")) return;
    if (e.target.checked) {
      modelsState.checked.add(e.target.value);
      ensurePriceSeed(e.target.value); /* new check inherits the base price */
    } else {
      modelsState.checked.delete(e.target.value);
      modelPrices.delete(e.target.value); /* re-check → fresh inherit */
    }
    renderPriceRows();
    updatePreview();
  });
  /* per-model price edits → state (rows render dynamically → delegated) */
  $("s-model-prices").addEventListener("input", (e) => {
    const row = e.target.closest(".mprice[data-model]");
    if (!row || !e.target.dataset.f) return;
    const p = modelPrices.get(row.dataset.model);
    if (p) p[e.target.dataset.f] = e.target.value.trim();
    updatePreview();
  });

  /* — register / update form — */
  function readPrice(id) { return parsePriceStr($(id).value); }
  const PRICE_ERR = "amounts must be non-negative numbers, ≤6 decimals (USDC)";

  /* shape comparison must be order-insensitive — chips render in relay
     order, chain keeps register order; same set ≠ "changed" */
  const sameShape = (l, endpoint, models) =>
    l.endpoint === endpoint &&
    JSON.stringify([...l.models].sort()) === JSON.stringify([...models].sort());

  /* inline "(cached, input, output)" for preview lines — BigInt native or ? */
  const fmtTriple = (p) => (p ? `(${p.c ?? "?"}, ${p.i ?? "?"}, ${p.o ?? "?"})` : "(?)");

  function updatePreview() {
    const endpoint = formEndpoint();
    const { models, prices, bad } = collectPrices();
    const active = $("s-active").checked;
    const lines = [];
    const mine = state.myListing;
    const btn = $("s-submit");
    if (active && shared) {
      /* shared mode: PUBLISH is blocked until the full custody gate
         passes — pins verified, catalog fresh-anchored, key stored,
         ≥1 servable model checked */
      const g = sharedGate();
      if (!g.ok) { lines.push(`<span class="t-a">! ${T.esc(g.reason)}</span>`); lines.push(""); }
    } else if (active && !modelsGateOk(endpoint)) {
      lines.push(`<span class="t-a">! ${T.esc(gateReason())}</span>`);
      lines.push("");
    }
    if (active && bad.length) {
      lines.push(`<span class="t-a">! ${T.esc(bad.join(", "))} — ${PRICE_ERR}</span>`);
      lines.push("");
    }
    const pricesInline = models.map((m) => fmtTriple(prices.get(m))).join(", ");
    if (mine && mine.active && !active) {
      lines.push(`<span class="t-a">deactivate()</span> <span class="t-d">// listing stays auditable, stops serving</span>`);
      btn.textContent = "[ DEACTIVATE ]";
    } else if (mine && mine.active) {
      if (sameShape(mine, endpoint, models)) {
        /* v2: prices are per-model — one updateModelPrice per changed model */
        const changed = changedModels(mine, models, prices);
        if (!changed.length) {
          lines.push(`<span class="t-d">// every model price matches on-chain — nothing to send</span>`);
          btn.textContent = "[ PRICES UNCHANGED ]"; /* click → formErr, no tx */
        } else {
          changed.forEach((m, idx) => {
            lines.push(`<span class="t-a">updateModelPrice("${T.esc(m)}", ${fmtTriple(prices.get(m))})</span> <span class="t-d">// ${idx + 1}/${changed.length} — one signature per model</span>`);
          });
          btn.textContent = changed.length === 1 ? "[ UPDATE PRICE · 1 TX ]" : `[ UPDATE PRICES · ${changed.length} TX ]`;
        }
      } else {
        lines.push(`<span class="t-a">deactivate()</span> <span class="t-d">// step 1/2 — active listing can't be re-registered (AlreadyRegistered)</span>`);
        lines.push(`<span class="t-g">register(endpoint, models, prices)</span> <span class="t-d">// step 2/2 — new shape + per-model prices, one atomic tx</span>`);
        btn.textContent = "[ DEACTIVATE → RE-REGISTER · 2 TX ]";
      }
    } else if (active) {
      lines.push(`<span class="t-g">register(</span>`);
      lines.push(`  endpoint: <span class="t-s">"${T.esc(endpoint) || "…"}"</span>,`);
      lines.push(`  models: <span class="t-s">[${models.map((m) => `"${T.esc(m)}"`).join(", ") || "…"}]</span>,`);
      lines.push(`  prices: <span class="t-n">[${pricesInline || "…"}]</span> <span class="t-d">// parallel to models · native 6dp per 1M tok</span>`);
      lines.push(`<span class="t-g">)</span>`);
      btn.textContent = "[ SUBMIT TO REGISTRY ]";
    } else {
      lines.push(`<span class="t-d">// nothing to do — listing inactive and toggle off</span>`);
      btn.textContent = "[ SUBMIT TO REGISTRY ]";
    }
    $("s-preview").innerHTML = lines.join("\n");
  }
  $("s-endpoint").addEventListener("input", () => { renderModelNote(); updatePreview(); });
  ["s-price-cached", "s-price-input", "s-price-output"].forEach((id) =>
    $(id).addEventListener("input", onBaseInput));
  $("s-active").addEventListener("change", updatePreview);

  /* human-readable copy for the Registry custom errors (v2 frozen face +
     v4 M12 additions — RemoveLastModel / ModelNotFound power the row-level
     delist flow's preflight humanization) */
  const REGISTRY_ERR_COPY = {
    AlreadyRegistered: "an active listing already exists on-chain — register always reverts. The only reshape path: deactivate → re-register (two txs)",
    NotActive: "listing is not active (NotActive) — price updates need an active listing; changing the model set / endpoint goes through deactivate → register",
    ModelNotFound: "model is not in the on-chain listing (ModelNotFound) — the row was likely just removed (it disappears after refresh); re-add via deactivate → register, remove via the row-level [ REMOVE ] in MY LISTING",
    EmptyModels: "models is empty (EmptyModels) — check at least one",
    LengthMismatch: "models and prices lengths differ (LengthMismatch) — page assembly bug, please report",
    RemoveLastModel: "the last model cannot be removed (RemoveLastModel) — a listing keeps at least one model; to go fully offline use [ DEACTIVATE ALL ] at the card bottom",
  };
  function humanizeRegistryErr(e) {
    let key = (e && e.revert && e.revert.name && REGISTRY_ERR_COPY[e.revert.name]) ? e.revert.name : null;
    const raw = (e && (e.data || (e.info && e.info.error && e.info.error.data))) || "";
    if (!key && typeof raw === "string" && raw.startsWith("0x")) {
      for (const k of Object.keys(REGISTRY_ERR_COPY)) {
        try { if (raw.slice(0, 10) === ethers.id(`${k}()`).slice(0, 10)) { key = k; break; } } catch { /* ignore */ }
      }
    }
    const msg = String((e && (e.shortMessage || e.reason || e.message)) || "");
    if (!key) key = Object.keys(REGISTRY_ERR_COPY).find((k) => msg.toLowerCase().includes(k.toLowerCase())) || null;
    if (!key) return null;
    return { kind: key, text: REGISTRY_ERR_COPY[key] };
  }
  /* simulate before wallet popup → revert reason in human words, no wasted signature */
  async function preflight(call) {
    try { await call(); return null; }
    catch (e) {
      return humanizeRegistryErr(e) ||
        { kind: "revert", text: `on-chain simulation reverted — ${T.esc(e.shortMessage || e.reason || e.message || "unknown")}` };
    }
  }
  function offerTwoStepFix(txbox) {
    const btn = document.createElement("button");
    btn.className = "btn";
    btn.type = "button";
    btn.textContent = "[ ONE-CLICK FIX: DEACTIVATE → REGISTER · SIGN TWO TXS ]";
    btn.addEventListener("click", () => $("s-submit").click());
    txbox.appendChild(btn);
  }
  /* after a successful register/updateModelPrice: refresh listing + re-run the
     inline verify so chips & diagnostics reflect the new on-chain truth.
     Shared mode has no /verify-upstream precheck — the custody status
     (listing_bound / catalog) refreshes instead. */
  const reverifyAfterTx = (endpoint) => {
    if (shared) { refreshCustodyStatus().catch(() => {}); return; }
    runVerify(endpoint).catch(() => {});
  };

  /* multi-tx update flow: models after a failure are not attempted —
     list them as skipped so the partial state is explicit */
  function markSkipped(txbox, models) {
    for (const m of models) {
      const line = T.txLine(txbox, `updateModelPrice(${m})`);
      line.el.classList.add("is-skip");
      line.el.querySelector(".txl-state").textContent = "skipped — fix the failure above and resubmit";
    }
  }

  $("s-submit").addEventListener("click", () => guardPaid($("s-submit"), async () => {
    if (needWallet() || needConfig()) return;
    const txbox = $("s-tx");
    txbox.innerHTML = "";
    const endpoint = formEndpoint(); /* shared mode: the pinned relay origin, never the upstream URL */
    const { models, prices, bad } = collectPrices();
    const active = $("s-active").checked;

    if (active) {
      if (!endpoint) return formErr(txbox, "endpoint required (https://…:8787)");
      if (shared) {
        const g = sharedGate();
        if (!g.ok) return formErr(txbox, g.reason);
      } else if (!modelsGateOk(endpoint)) return formErr(txbox, gateReason());
      if (!models.length) return formErr(txbox, shared ? "check at least one servable catalog model" : "check at least one relay-verified model");
      if (bad.length) return formErr(txbox, `${bad.join(", ")} — ${PRICE_ERR} · prices are USDC per 1M tokens`);
    }
    /* shared mid-flight re-check: the user can edit URL/key (or switch
       account) while a wallet popup is open — every signing leg of the
       register paths re-validates the custody gate right before it sends */
    const sharedRecheck = () => {
      if (!shared) return null;
      const g = sharedGate();
      return g.ok ? null : `form changed mid-flight (${g.reason}) — nothing more was sent; re-review and submit again`;
    };

    const reg = T.registry(state.signer);
    const mine = state.myListing;
    /* parallel Price[] for register: prices[i] prices models[i] */
    const priceArr = models.map((m) => { const p = prices.get(m); return [p.c, p.i, p.o]; });
    try {
      if (mine && mine.active && !active) {
        const rcpt = await T.runTx(T.txLine(txbox, "deactivate()"), reg.deactivate());
        if (rcpt) { state.myListing.active = false; await loadMyListing(); }
        return;
      }
      if (mine && mine.active) {
        if (sameShape(mine, endpoint, models)) {
          /* shape unchanged → per-model updateModelPrice, one tx per changed
             model; each gets its own wallet signature, failures stop the
             queue and mark the rest skipped */
          const changed = changedModels(mine, models, prices);
          if (!changed.length) return formErr(txbox, "every model price matches on-chain — nothing to send");
          const staleU = sharedRecheck();
          if (staleU) return formErr(txbox, staleU);
          for (let idx = 0; idx < changed.length; idx++) {
            const m = changed[idx];
            const p = prices.get(m);
            const label = `updateModelPrice(${m})` + (changed.length > 1 ? ` — ${idx + 1}/${changed.length}` : "");
            const pf = await preflight(() => reg.updateModelPrice.staticCall(m, [p.c, p.i, p.o]));
            if (pf) { formErr(txbox, `${m}: ${pf.text}`); markSkipped(txbox, changed.slice(idx + 1)); return; }
            const rcpt = await T.runTx(T.txLine(txbox, label), reg.updateModelPrice(m, [p.c, p.i, p.o]));
            if (!rcpt) { markSkipped(txbox, changed.slice(idx + 1)); return; }
          }
          await loadMyListing(); reverifyAfterTx(endpoint);
          return;
        }
        /* shape changed → the ONLY contract path: deactivate, then re-register
           (register carries the full parallel Price[] in one atomic tx) */
        const rcpt1 = await T.runTx(T.txLine(txbox, "deactivate() — step 1/2"), reg.deactivate());
        if (!rcpt1) return;
        const stale2 = sharedRecheck();
        if (stale2) return formErr(txbox, `deactivate already sent · ${stale2}`);
        const pf = await preflight(() => reg.register.staticCall(endpoint, models, priceArr));
        if (pf) { formErr(txbox, pf.text); return; }
        const rcpt2 = await T.runTx(T.txLine(txbox, "register(…) — step 2/2"), reg.register(endpoint, models, priceArr));
        if (rcpt2) { await loadMyListing(); reverifyAfterTx(endpoint); }
        return;
      }
      if (active) {
        const staleR = sharedRecheck();
        if (staleR) return formErr(txbox, staleR);
        const pf = await preflight(() => reg.register.staticCall(endpoint, models, priceArr));
        if (pf) {
          formErr(txbox, pf.text);
          if (pf.kind === "AlreadyRegistered") {
            /* local state was stale: refresh WITHOUT clobbering the form,
               then the same submit click takes the two-step path */
            await loadMyListing({ prefill: false });
            offerTwoStepFix(txbox);
          }
          return;
        }
        const rcpt = await T.runTx(T.txLine(txbox, "register(…)"), reg.register(endpoint, models, priceArr));
        if (rcpt) { await loadMyListing(); reverifyAfterTx(endpoint); }
      }
    } catch (e) {
      const hz = humanizeRegistryErr(e);
      formErr(txbox, hz ? hz.text : (e.shortMessage || e.message));
    }
  }));
  const formErr = (box, msg) => {
    const el = document.createElement("div");
    el.className = "txl is-bad";
    el.innerHTML = `<span class="txl-dot"></span><span class="txl-label">form</span><span class="txl-state">${T.esc(msg)}</span>`;
    box.appendChild(el);
  };

  /* — danger confirm dialog (self-drawn on the wsel chrome —
       createElement + textContent only, zero innerHTML, zero external
       links; same injection discipline as the M11 wallet selector).
       Shared by the card-level [ DEACTIVATE ] and the M12 row-level
       model 下线 control. — */
  function dangerConfirm({ ariaLabel, title, copy, det, goLabel }) {
    return new Promise((resolve) => {
      const overlay = document.createElement("div");
      overlay.className = "wsel-overlay";
      const box = document.createElement("div");
      box.className = "wsel cnf";
      box.setAttribute("role", "alertdialog");
      box.setAttribute("aria-modal", "true");
      box.setAttribute("aria-label", ariaLabel);

      const bar = document.createElement("div");
      bar.className = "wsel-bar";
      for (let i = 0; i < 3; i++) { const d = document.createElement("span"); d.className = "tdot"; bar.appendChild(d); }
      const titleEl = document.createElement("span");
      titleEl.className = "wsel-title";
      titleEl.textContent = title;
      bar.appendChild(titleEl);
      box.appendChild(bar);

      const body = document.createElement("div");
      body.className = "cnf-body";
      const copyEl = document.createElement("p");
      copyEl.className = "cnf-copy";
      copyEl.textContent = copy;
      const detEl = document.createElement("p");
      detEl.className = "cnf-det";
      detEl.textContent = det;
      body.append(copyEl, detEl);
      box.appendChild(body);

      const foot = document.createElement("div");
      foot.className = "wsel-foot";
      const hint = document.createElement("span");
      hint.className = "wsel-hint";
      hint.textContent = "esc / click outside to cancel";
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "wsel-cancel";
      cancel.textContent = "[ CANCEL ]";
      const go = document.createElement("button");
      go.type = "button";
      go.className = "btn btn-danger btn-sm";
      go.textContent = goLabel;
      foot.append(hint, cancel, go);
      box.appendChild(foot);

      const prevOverflow = document.body.style.overflow;
      const done = (v) => {
        document.removeEventListener("keydown", onKey, true);
        document.body.style.overflow = prevOverflow;
        overlay.remove();
        resolve(v);
      };
      const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); done(false); } };
      cancel.addEventListener("click", () => done(false));
      go.addEventListener("click", () => done(true));
      document.addEventListener("keydown", onKey, true);
      overlay.addEventListener("click", (e) => { if (e.target === overlay) done(false); });

      document.body.style.overflow = "hidden";
      overlay.appendChild(box);
      document.body.appendChild(overlay);
      cancel.focus(); /* destructive action never takes the default focus */
    });
  }

  const confirmDeactivate = () => dangerConfirm({
    ariaLabel: "confirm deactivate",
    title: "confirm — registry.deactivate()",
    copy:
      "Delisting turns the whole listing off: the ACTIVE market presence is pulled immediately (card turns INACTIVE gray, " +
      "buyers can no longer lock); in-flight (Locked) payments still settle normally; " +
      "re-register from the form at any time to restore ACTIVE.",
    det: "deactivate() → active=false · enumeration entry kept (append-only, auditable on-chain) · 1 tx · wallet signature",
    goLabel: "[ SIGN DEACTIVATE ]",
  });

  /* M12 row-level delist — model name rides textContent only (chain data,
     still treated as untrusted) */
  const confirmRemoveModel = (model) => dangerConfirm({
    ariaLabel: "confirm remove model",
    title: "confirm — registry.removeModel()",
    copy:
      `Delist model ${model}: it stops serving immediately (getPrice reverts at once, the relay answers 400 to new calls, ` +
      "buyers can no longer lock against it); in-flight (Locked) payments will go settle-failed and buyers refund in " +
      "full after ttl; you can re-add it later via the form's deactivate → register.",
    det: `removeModel("${model}") → models[]/prices[] parallel arrays removed at the same index (swap-and-pop) · 1 tx · wallet signature · emits ModelRemoved(operator, model)`,
    goLabel: "[ SIGN REMOVE ]",
  });

  /* [ DEACTIVATE ALL ] — one-tap listing-level offboarding from the MY
     LISTING card. Signer comes from finishConnect (the M11 wallet layer) —
     never window.ethereum directly. */
  deactBtn.addEventListener("click", () => guard(deactBtn, async () => {
    if (needWallet() || needConfig()) return;
    const mine = state.myListing;
    if (!mine || !mine.active) return; /* stale card — nothing to deactivate */
    if (!(await confirmDeactivate())) return;
    myTx.innerHTML = "";
    try {
      const rcpt = await T.runTx(T.txLine(myTx, "deactivate()"), T.registry(state.signer).deactivate());
      if (rcpt) {
        /* optimistic flip, then chain-truth refresh: card → INACTIVE 徽章 +
           价格区弱化, buyer selects → this seller disabled; market pages read
           getListing live and annotate INACTIVE on their next render */
        state.myListing.active = false;
        await Promise.all([loadMyListing(), loadListingsIntoSelects()]);
      } else {
        /* revert/reject (e.g. NotActive from a stale-active card) — resync
           the card without clobbering form edits */
        await loadMyListing({ prefill: false });
      }
    } catch (e) { formErr(myTx, e.shortMessage || e.message); }
  }));

  /* — removeModel (M12, Registry v4): row-level 下线 inside MY LISTING.
       Single tx per model — NOT a deactivate→register combo. Signer comes
       from finishConnect (the M11 wallet layer) — never window.ethereum. — */
  let rmBusy = false; /* one delist at a time — covers every row button */
  const myListingBox = $("s-my-listing");
  const syncMdelDisabled = () => {
    const multi = !!(state.myListing && state.myListing.models && state.myListing.models.length > 1);
    myListingBox.querySelectorAll(".mdel").forEach((b) => { b.disabled = rmBusy || !multi; });
  };
  myListingBox.addEventListener("click", async (e) => {
    const btn = e.target.closest(".mdel");
    if (!btn || btn.disabled || rmBusy) return;
    rmBusy = true;
    syncMdelDisabled(); /* freeze all rows for the duration of the op */
    try { await removeModelFlow(btn.dataset.model); }
    finally {
      rmBusy = false;
      /* success/revert paths re-render the rows (fresh disabled states);
         only the dialog-cancel path touches the old DOM */
      syncMdelDisabled();
    }
  });

  async function removeModelFlow(model) {
    if (needWallet() || needConfig()) return;
    const mine = state.myListing;
    /* stale-card guards — chain truth is re-read after every outcome */
    if (!mine || !mine.registered) return;
    if (!mine.models.includes(model) || mine.models.length <= 1) {
      await loadMyListing({ prefill: false });
      return;
    }
    if (!(await confirmRemoveModel(model))) return;
    myTx.innerHTML = "";
    const reg = T.registry(state.signer);
    try {
      /* staticCall preflight → humanized revert (ModelNotFound on a stale
         row / RemoveLastModel on a chain-state race) without spending a
         wallet signature */
      const pf = await preflight(() => reg.removeModel.staticCall(model));
      if (pf) {
        formErr(myTx, `${model}: ${pf.text}`);
        await loadMyListing({ prefill: false });
        return;
      }
      const rcpt = await T.runTx(T.txLine(myTx, `removeModel(${model})`), reg.removeModel(model));
      if (rcpt) {
        /* chain-truth refresh: the row disappears from MY LISTING; buyer
           selects / lock info / call-model dropdown resync off getListing
           (one model fewer, naturally). Market pages read getListing live
           on their own renders. Form edits survive (prefill: false). */
        await Promise.all([loadMyListing({ prefill: false }), loadListingsIntoSelects()]);
      } else {
        await loadMyListing({ prefill: false });
      }
    } catch (e) {
      const hz = humanizeRegistryErr(e);
      formErr(myTx, hz ? hz.text : (e.shortMessage || e.message));
      await loadMyListing({ prefill: false });
    }
  }

  /* — upstream precheck card — removed (merged into the register form as
     the inline LOAD FROM RELAY step; see runVerify / renderVerifyDetail) — */

  /* ═══ M15 shared-relay custody — seller side ════════════════
     Active only when config.js carries a complete m15 block. The flow:
       upstreamBaseURL + API key → [ VERIFY + SEAL KEY ]
         → live pin check (/info + /attestation vs config pins)
         → nonce (re-validated against config before any signature)
         → in-browser envelope seal to the pinned TEE upload key
         → EIP-191 custody message → wallet signature → POST /sellers/keys
         → relay probes the live catalog → checkbox models + prices
       → PUBLISH (register endpoint = pinned relay origin, automatic)
       → AUTHORIZE (separate explicit tx: approveSettleDelegate).
     Editing URL/key or switching account stales the verified catalog;
     a failed verify blocks PUBLISH. */

  const setVerifyNote = (text, cls) => {
    const n = $("s-verify-note");
    if (!n) return;
    n.className = "model-verify-note" + (cls ? " " + cls : "");
    n.textContent = text;
  };

  /* every keystroke in the upstream URL / key fields bumps the input
     generation → a verified/resumed catalog anchored to an older
     generation is stale and PUBLISH blocks until a fresh VERIFY */
  function markCustodyEdited() {
    custodyState.gen++;
    if (custodyState.anchor && custodyState.anchor.gen !== custodyState.gen) {
      setVerifyNote("⚠ upstream URL / key edited — the catalog below is stale; VERIFY again to publish", "warn");
    }
    updatePreview();
  }

  /* account context wipe (connect/switch/disconnect): new generation,
     anchor + stored-key flags cleared, key field emptied programmatically
     (no input event — a programmatic clear must not itself mark stale) */
  function resetCustodySession() {
    if (!shared && !m15Broken) return;
    custodyState.gen++;
    custodyState.sessionStartGen = custodyState.gen; /* session baseline — resume requires gen to still equal it */
    custodyState.sessionFailed = false;
    custodyState.anchor = null;
    custodyState.keyStored = false;
    custodyState.fingerprint = null;
    custodyState.statusInfo = null;
    custodyState.delegate = undefined;
    custodyState.pinsOk = false;
    const keyEl = $("s-upstream-key");
    if (keyEl) keyEl.value = "";
    if (shared) {
      modelsState.status = "idle";
      modelsState.base = null;
      modelsState.accessible = [];
      modelsState.entries = null;
      modelsState.checked = new Set();
      modelsState.reason = "";
      renderModelZone();
      $("s-verify-detail").innerHTML = "";
      setVerifyNote("");
    }
  }

  async function refreshCustodySession() {
    resetCustodySession();
    await refreshCustodyStatus();
  }

  /* pin matrix → the custody card. Honest trust boundary: the quote is
     self-reported by the relay; the operator's offline verification
     (verifier/date/digest) is pinned in config and displayed as such —
     this page re-checks the LIVE binding, never claims a fresh DCAP proof */
  function renderPins(rows) {
    const allOk = rows.length > 0 && rows.every((r) => r.ok);
    $("sc-pins").innerHTML =
      `<div class="kv"><span>DEPLOYMENT PINS</span><b>${allOk
        ? `<span class="ok">✓ live relay matches the pinned config</span>`
        : `<span class="bad">✗ DEPLOYMENT MISMATCH — custody disabled, do not submit keys</span>`}</b></div>` +
      rows.map((r) =>
        `<div class="kv"><span>${T.esc(r.key.toUpperCase())}</span><b class="${r.ok ? "ok" : "bad"}">${r.ok ? "✓ " : "✗ "}${T.esc(r.detail)}</b></div>`
      ).join("") +
      `<p class="fld-hint">attestation evidence (pinned by the operator): quote verified via ${T.esc(shared.evidence.verifier)} ` +
      `on ${T.esc(shared.evidence.verifiedAt)} · digest <span class="mono">${T.esc(shared.evidence.digest.slice(0, 16))}…</span> — ` +
      `the quote is self-reported by the relay; this page re-checks the live binding above and does not re-run DCAP proof verification.</p>`;
  }

  /* custody status cluster — chain truth for the delegate row, public
     relay /status for the rest (display only; pins above are the
     identity authority) */
  function renderCustodyStatus() {
    const box = $("sc-status");
    if (!shared) { box.innerHTML = ""; return; }
    const st = custodyState.statusInfo;
    const fp = custodyState.fingerprint;
    const rows = [];
    rows.push(`<div class="kv"><span>STORED KEY</span><b>${custodyState.keyStored
      ? `<span class="ok">✓ on file at the relay</span>${fp ? ` · <span class="mono" title="key fingerprint (sha256)">${T.esc(fp.slice(0, 12))}…</span>` : ""}`
      : `<span class="dim">none — VERIFY seals + submits it</span>`}</b></div>`);
    if (st && st.upstream_host) {
      rows.push(`<div class="kv"><span>UPSTREAM (STORED)</span><b class="mono">${T.esc(st.upstream_host)} ` +
        `<span class="${st.upstream_official === false ? "bad" : "ok"}">${st.upstream_official === false ? "· NOT official" : "· official"}</span></b></div>`);
    }
    if (st) {
      rows.push(`<div class="kv"><span>LISTING BIND</span><b>${st.listing_bound
        ? `<span class="ok">✓ bound${st.listing_active === false ? " · inactive" : " · active"}</span>`
        : `<span class="dim">not bound — PUBLISH registers the listing below</span>`}</b></div>`);
      if (st.updated_at) rows.push(`<div class="kv"><span>RELAY STATUS AT</span><b class="mono dim">${T.esc(String(st.updated_at))}</b></div>`);
    }
    const d = custodyState.delegate;
    if (!state.address) {
      rows.push(`<div class="kv"><span>DELEGATE</span><b class="dim">connect a wallet — the chain truth reads with it</b></div>`);
    } else if (d === undefined) {
      rows.push(`<div class="kv"><span>DELEGATE</span><b class="dim">unreadable — the on-chain escrow may predate M15</b></div>`);
    } else if (!d || d === T.custody.ZERO_ADDRESS) {
      rows.push(`<div class="kv"><span>DELEGATE</span><b class="dim">none — the relay cannot settle for you yet · AUTHORIZE below</b></div>`);
    } else if (d === shared.expectedSigner) {
      rows.push(`<div class="kv"><span>DELEGATE</span><b class="ok">✓ authorized — pinned relay signer <span class="mono">${T.esc(T.truncAddr(d))}</span></b></div>`);
    } else {
      rows.push(`<div class="kv"><span>DELEGATE</span><b class="bad">⚠ authorized to <span class="mono">${T.esc(T.truncAddr(d))}</span> — NOT the pinned signer; revoke recommended</b></div>`);
    }
    box.innerHTML = rows.join("");
    const hasDelegate = d !== undefined && d && d !== T.custody.ZERO_ADDRESS;
    $("sc-authorize").hidden = !(custodyState.pinsOk && d !== shared.expectedSigner);
    $("sc-revoke-delegate").hidden = !hasDelegate;
    $("sc-revoke-key").hidden = !custodyState.keyStored;
  }

  async function refreshDelegateRow() {
    if (!shared || !state.address) return;
    try {
      const d = await T.escrow(T.readProvider()).settleDelegateOf(state.address);
      if (!state.address) return;
      custodyState.delegate = String(d).toLowerCase();
    } catch { custodyState.delegate = undefined; } /* pre-M15 escrow → reverts */
  }

  /* nonce: fetched per operation (single-use), then RE-VALIDATED against
     the trusted deployment config — a tampered nonce is never signed */
  async function fetchValidatedNonce(meLower) {
    const r = await T.fetchJson(T.joinUrl(shared.relayOrigin, `/sellers/nonce/${meLower}`), {}, 10000);
    if (r.corsOrNetwork) return { err: `relay unreachable (${r.error === "timeout" ? "timeout" : "network/CORS"})` };
    if (!r.ok || !r.body) return { err: `nonce endpoint HTTP ${r.status}` };
    const chk = T.custody.checkNonce(r.body, {
      chainId: cfg.chainId, escrowAddr: cfg.escrowAddr, registryAddr: cfg.registryAddr,
      relayOrigin: shared.relayOrigin,
    }, Math.floor(Date.now() / 1000));
    if (!chk.ok) return { err: `nonce rejected — ${chk.reason}` };
    return { nonce: r.body };
  }

  /* relay custody error codes → grounded copy; relay detail rides along
     length-capped and escaped — never echo the key ourselves */
  function custodyHttpError(r) {
    /* FastAPI carries the protocol code in `detail` (never key material);
       some proxies wrap as {error|code}. `detail` shown separately only
       when it is NOT the code itself. */
    const rawDetail = r.body && typeof r.body === "object" && r.body.detail ? String(r.body.detail).slice(0, 200) : "";
    const code = (r.body && typeof r.body === "object" &&
      (r.body.error || r.body.code)) ? String(r.body.error || r.body.code) : rawDetail;
    const byCode = {
      bad_request: "the relay rejected the request shape (bad_request)",
      envelope_invalid: "the sealed envelope failed decryption or binding checks inside the enclave (envelope_invalid)",
      upstream_not_official: "not an official platform upstream (upstream_not_official) — the relay enforces the allowlist",
      upstream_key_rejected: "the upstream platform rejected this key (upstream_key_rejected) — check it, then VERIFY again",
      unauthorized: "signature check failed at the relay (unauthorized) — sign with the connected seller wallet",
      nonce_invalid: "nonce rejected (nonce_invalid) — retry VERIFY for a fresh one",
      origin_mismatch: "the relay rejected this page's origin (origin_mismatch) — the relay deployment must allow this frontend origin (its origin allowlist / CORS configuration); nothing was sent",
      upstream_unreachable: "the relay could not reach the upstream platform (upstream_unreachable) — check the base URL",
      keystore_unavailable: "the relay could not persist the key (keystore_unavailable) — the previous key is preserved; retry",
      not_shared_mode: "this relay is not running in shared-custody mode (not_shared_mode)",
    };
    const base = byCode[code] || `HTTP ${r.status}${code ? ` (${code})` : ""}`;
    return rawDetail && rawDetail !== code ? `${base} — ${rawDetail}` : base;
  }

  /* a failed verify blocks PUBLISH (modelsState error propagates through
     sharedGate) and leaves no anchor behind — it also marks the session
     failed so the catalog-resume gate refuses to auto-restore the stored
     face until a fresh VERIFY (or a new connect session) lands */
  function custodyFail(reason) {
    modelsState.status = "error";
    modelsState.reason = reason;
    modelsState.accessible = [];
    modelsState.entries = null;
    custodyState.sessionFailed = true;
    renderModelZone();
    setVerifyNote(`✗ ${reason}`, "err");
  }

  /* async-race abort: edits / account switch landed mid-flight — discard
     the in-flight result without destroying a previously stored anchor's
     chips beyond what the generation bump already stales */
  function staleAbort(reason) {
    modelsState.status = custodyState.anchor ? "ok" : "idle";
    if (!custodyState.anchor) { modelsState.accessible = []; modelsState.entries = null; }
    renderModelZone();
    setVerifyNote(`⚠ ${reason}`, "warn");
  }

  function sanitizeCatalog(raw) {
    if (!Array.isArray(raw)) return [];
    const out = [];
    for (const e of raw) {
      if (!e || typeof e !== "object") continue;
      const m = typeof e.model === "string" ? e.model.trim() : "";
      if (!m) continue;
      /* fail-closed: only an explicit servable:true is selectable */
      out.push({ model: m, servable: e.servable === true, reason: typeof e.reason === "string" ? e.reason.slice(0, 160) : "" });
    }
    return out;
  }

  function renderSharedVerifyDetail(b, entries, normUrl) {
    const bad = entries.filter((e) => !e.servable);
    $("s-verify-detail").innerHTML =
      `<div class="kv"><span>UPSTREAM</span><b class="mono wrap-anywhere">${T.esc(normUrl)} ` +
        `<span class="${b.official === false ? "bad" : "ok"}">${b.official === false ? "· NOT official" : "· official"}</span></b></div>` +
      `<div class="kv"><span>KEY FINGERPRINT</span><b class="mono" title="sha256 of the stored key — click-free display only">${T.esc(b.key_fingerprint.slice(0, 16))}…</b></div>` +
      `<div class="kv"><span>CATALOG</span><b>${entries.length
        ? entries.map((e) => `<span class="mtag${e.servable ? "" : " off"}"${e.servable ? "" : ` title="${T.esc(e.reason || "not servable")}"`}>${T.esc(e.model)}</span>`).join(" ")
        : "—"}</b></div>` +
      (bad.length
        ? `<div class="kv"><span>NOT SERVABLE</span><b class="bad">${bad.map((e) => `<span class="mtag" title="${T.esc(e.reason || "not servable")}">${T.esc(e.model)}</span>`).join(" ")}</b></div>`
        : "") +
      `<div class="kv"><span>LISTING BIND</span><b>${b.listing_bound ? "✓ bound at the relay" : "not bound yet — PUBLISH registers below"}</b></div>` +
      `<div class="kv"><span>DELEGATE</span><b>${b.delegate_authorized ? "✓ authorized" : "not authorized — AUTHORIZE in KEY CUSTODY after PUBLISH"}</b></div>`;
  }

  /* VERIFY = pin re-check → nonce → in-browser seal → sign → POST →
     live catalog. The single source of a publishable shared catalog. */
  async function runSharedVerify() {
    if (needWallet() || needConfig() || !shared) return;
    if (!custodyState.pinsOk) {
      custodyFail("deployment pins not verified — refusing to seal a key to an unverified relay");
      return;
    }
    const nz = T.custody.normalizeUpstreamUrl($("s-upstream-url").value);
    if (!nz.ok) { custodyFail(`upstream base URL rejected — ${nz.reason}`); return; }
    const apiKey = $("s-upstream-key").value.trim();
    if (!apiKey) {
      custodyFail("paste the upstream API key — it is sealed to the pinned TEE key inside this browser and never stored");
      return;
    }

    const g0 = custodyState.gen;
    const me = (state.address || "").toLowerCase();
    const stillCurrent = () => custodyState.gen === g0 && (state.address || "").toLowerCase() === me;
    const wasOk = modelsState.status === "ok"; /* before the loading flip */
    modelsState.status = "loading";
    modelsState.base = shared.relayOrigin;
    renderModelZone();

    /* 1 — live pin re-check (pins can drift between page load and now) */
    setVerifyNote("step 1/4 · re-checking the live relay against the pinned deployment …", "");
    const pr = await T.custody.probeSharedRelay(shared, 12000, hashers);
    if (!stillCurrent()) { staleAbort("inputs or account changed while verifying — result discarded; VERIFY again"); return; }
    renderPins(pr.rows);
    custodyState.pinsOk = pr.ok;
    if (!pr.ok) {
      custodyFail("DEPLOYMENT MISMATCH — the live relay does not match the pinned config; the key was NOT sent");
      return;
    }

    /* 2 — nonce, validated against config before any signature */
    setVerifyNote("step 2/4 · fetching a one-time nonce …", "");
    const nr = await fetchValidatedNonce(me);
    if (!stillCurrent()) { staleAbort("inputs or account changed while verifying — result discarded; VERIFY again"); return; }
    if (nr.err) { custodyFail(nr.err); return; }
    const n = nr.nonce;

    /* 3 — seal the key in-browser (ECDH P-256 → HKDF-SHA256 → AES-256-GCM) */
    setVerifyNote("step 3/4 · sealing the key in-browser (ECDH P-256 → HKDF-SHA256 → AES-256-GCM) …", "");
    const fields = {
      seller: me, chain: cfg.chainId,
      escrow: cfg.escrowAddr.toLowerCase(), registry: cfg.registryAddr.toLowerCase(),
      relay: shared.relayOrigin, upstream: nz.url,
      nonce: n.nonce, issued: n.issued_at, expires: n.expires_at,
    };
    const aad = T.custody.buildCustodyAad(fields);
    let envelope, body, bodySha;
    try {
      envelope = await T.custody.encryptEnvelope({
        uploadPubBytes: pr.pubBytes, apiKey, aad,
        subtle: crypto.subtle, getRandomValues: (b) => crypto.getRandomValues(b),
      });
      body = T.custody.canonicalSubmitBody({
        nonce: n.nonce, issued_at: n.issued_at, expires_at: n.expires_at,
        upstream_base_url: nz.url, envelope,
      });
      bodySha = await hashers.sha256Hex(new TextEncoder().encode(body));
    } catch (e) { custodyFail(`envelope assembly failed — ${e.message || e}`); return; }
    const msg = T.custody.buildCustodySubmitMessage({ ...fields, bodySha256: bodySha });

    /* 4 — wallet signature over the exact custody message, then POST the
       exact body bytes that were hashed into it */
    setVerifyNote("step 4/4 · sign the custody message in your wallet …", "");
    let sig;
    try { sig = await state.signer.signMessage(msg); }
    catch (e) {
      custodyFail(`signing ${(e && e.code === "ACTION_REJECTED") ? "rejected in wallet" : "failed"} — ${T.esc((e && (e.shortMessage || e.message)) || "")} · the key was NOT sent`);
      return;
    }
    if (!stillCurrent()) { staleAbort("inputs or account changed while signing — nothing was sent; VERIFY again"); return; }

    setVerifyNote("POST /sellers/keys — the enclave verifies, stores, then probes the live catalog …", "");
    const r = await T.fetchJson(T.joinUrl(shared.relayOrigin, "/sellers/keys"), {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Tokenshare-Seller": me, "X-Tokenshare-Signature": sig },
      body,
    }, 30000);
    if (r.corsOrNetwork) {
      custodyFail(`relay unreachable (${r.error === "timeout" ? "timeout after 30s" : "network/CORS"}) — the key was sealed in-browser; the relay may not have received it`);
      return;
    }
    if (!r.ok || !r.body) { custodyFail(custodyHttpError(r)); return; }
    const b = r.body;
    if (b.stored !== true || typeof b.key_fingerprint !== "string" || !Array.isArray(b.catalog)) {
      custodyFail("malformed success response from the relay (stored / key_fingerprint / catalog) — refusing to trust it");
      return;
    }
    const entries = sanitizeCatalog(b.catalog);

    /* async-race guard — a VERIFY result only lands on the exact input
       generation + account it was started for */
    const applied = T.custody.applyVerifyResult(
      { gen: custodyState.gen, address: me },
      { gen: g0, address: me },
      { ok: true, upstream: nz.url, fingerprint: b.key_fingerprint });
    if (!applied.applied) { staleAbort(applied.reason); return; }

    /* clear the key field programmatically (no input event — the fresh
     * catalog stays valid); a MANUAL retype marks it stale again */
    $("s-upstream-key").value = "";
    custodyState.anchor = { ...applied.anchor, kind: "verified" };
    custodyState.sessionFailed = false; /* a successful VERIFY re-establishes this session's trust */
    custodyState.keyStored = true;
    custodyState.fingerprint = b.key_fingerprint;
    custodyState.statusInfo = {
      mode: "shared", has_key: true, key_fingerprint: b.key_fingerprint,
      upstream_host: b.upstream_host || null, upstream_official: b.official !== false,
      listing_bound: !!b.listing_bound, listing_active: b.listing_active,
      delegate_authorized: !!b.delegate_authorized, catalog: b.catalog,
    };
    modelsState.entries = entries;
    const servable = entries.filter((e) => e.servable).map((e) => e.model);
    applyVerified(shared.relayOrigin, servable, wasOk);
    renderSharedVerifyDetail(b, entries, nz.url);
    if (!servable.length) {
      setVerifyNote("✓ key stored — but the catalog has ZERO servable models for it; PUBLISH stays disabled", "warn");
    } else {
      setVerifyNote(`✓ key sealed + stored (fingerprint ${b.key_fingerprint.slice(0, 12)}…) · ${servable.length} servable / ${entries.length} catalog model(s) — check models below, then PUBLISH`, "ok");
    }
    renderCustodyStatus();
    refreshDelegateRow().then(renderCustodyStatus).catch(() => {});
  }

  /* status + pins + delegate refresh (connect, post-verify, post-tx).
     The optional catalog RESUME is deliberately narrow — the connection
     session must be completely untouched (no anchor, no URL/key edit
     since the session baseline, no failed VERIFY, pins verified) and the
     /status answer must land on the exact generation + wallet it was
     requested for, so a late response from a previous account or a
     disconnect→reconnect cycle can never re-anchor the stored face. Any
     touch forces a fresh VERIFY instead of trusting the resumed face.
     Eligibility itself is the pinned T.custody.canResumeCatalog
     predicate (unit-tested in scripts/check-custody-web.mjs). */
  async function refreshCustodyStatus() {
    if (!shared || !state.address) return;
    const me = state.address.toLowerCase();
    const g0 = custodyState.gen; /* request generation — re-checked by the resume gate before anything anchors */
    setVerifyNote("checking the live relay against the pinned deployment …", "");
    const pr = await T.custody.probeSharedRelay(shared, 12000, hashers);
    if ((state.address || "").toLowerCase() !== me) return; /* account switched mid-flight — drop */
    custodyState.pinsOk = pr.ok;
    renderPins(pr.rows);
    if (!pr.ok) {
      setVerifyNote("DEPLOYMENT MISMATCH — custody disabled; do not submit keys", "err");
      renderCustodyStatus();
      return;
    }
    setVerifyNote("✓ deployment pins verified — VERIFY a key to load the live catalog", "ok");
    await refreshDelegateRow();
    const r = await T.fetchJson(T.joinUrl(shared.relayOrigin, `/sellers/${me}/status`), {}, 10000);
    if ((state.address || "").toLowerCase() !== me) return;
    if (r.ok && r.body && r.body.mode === "shared") {
      custodyState.statusInfo = r.body;
      custodyState.keyStored = !!r.body.has_key; /* relay is the storage authority — sync both ways */
      const entries = sanitizeCatalog(r.body.catalog);
      const elig = T.custody.canResumeCatalog({
        anchor: custodyState.anchor,
        sessionFailed: custodyState.sessionFailed,
        gen: custodyState.gen,
        sessionStartGen: custodyState.sessionStartGen,
        pinsOk: custodyState.pinsOk,
        requestGen: g0,
        requestAddress: me,
        currentAddress: state.address,
        hasKey: !!r.body.has_key,
        entryCount: entries.length,
      });
      if (elig.ok) {
        custodyState.anchor = {
          kind: "resumed", upstream: null,
          fingerprint: r.body.key_fingerprint || null, gen: custodyState.gen, address: me,
        };
        custodyState.fingerprint = r.body.key_fingerprint || null;
        modelsState.entries = entries;
        applyVerified(shared.relayOrigin, entries.filter((e) => e.servable).map((e) => e.model), false);
        setVerifyNote(
          `✓ resumed the relay-stored catalog (key on file${custodyState.fingerprint ? ` · fingerprint ${custodyState.fingerprint.slice(0, 12)}…` : ""}) — ` +
          `pins re-verified live just now; editing UPSTREAM URL / KEY marks it stale and forces a fresh VERIFY`, "ok");
      }
    } else if (!r.corsOrNetwork) {
      custodyState.statusInfo = null; /* e.g. 404 — nothing stored yet */
    }
    renderCustodyStatus();
    updatePreview();
  }

  /* AUTHORIZE — separate, explicit user transaction. The warning is the
     product surface, not fine print. */
  const confirmAuthorizeDelegate = () => dangerConfirm({
    ariaLabel: "confirm authorize settle delegate",
    title: "confirm — escrow.approveSettleDelegate()",
    copy:
      "Authorize the shared relay as your settle delegate: it may then settle ANY of your Locked payments — " +
      "including older locks and locks past their TTL — up to each lock's full maxAmount, WITHOUT proving service. " +
      "Buyers can still refund unconsumed amounts after the TTL. The delegate can never withdraw or redirect your escrow balance.",
    det: `Escrow.approveSettleDelegate(${shared ? shared.expectedSigner : "…"}) · 1 tx · revoke any time via [ REVOKE DELEGATE ]`,
    goLabel: "[ SIGN AUTHORIZE ]",
  });

  async function sendDelegateTx(label, delegateAddr) {
    const txbox = $("sc-tx");
    const data = T.custody.delegateCalldata(hashers.keccak256TextHex, delegateAddr);
    try {
      /* static preflight: an escrow predating M15 reverts here with no
         wallet popup spent */
      await state.provider.call({ to: cfg.escrowAddr, data, from: state.address });
    } catch (e) {
      formErr(txbox, `simulation failed — this chain's escrow may predate M15 (approveSettleDelegate missing): ${T.esc(e.shortMessage || e.message || "unknown")}`);
      return null;
    }
    return T.runTx(T.txLine(txbox, label), state.signer.sendTransaction({ to: cfg.escrowAddr, data }));
  }

  async function authorizeDelegateFlow() {
    if (needWallet() || needConfig() || !shared) return;
    const txbox = $("sc-tx");
    txbox.innerHTML = "";
    if (!custodyState.pinsOk) return formErr(txbox, "relay deployment pins not verified — AUTHORIZE stays disabled");
    if (!(await confirmAuthorizeDelegate())) return;
    const rcpt = await sendDelegateTx(`approveSettleDelegate(${T.truncAddr(shared.expectedSigner)}) — pinned relay signer`, shared.expectedSigner);
    if (rcpt) { await refreshDelegateRow(); renderCustodyStatus(); }
  }

  /* REVOKE DELEGATE — approve(zero): clear UX, its own explicit tx */
  const confirmRevokeDelegate = () => dangerConfirm({
    ariaLabel: "confirm revoke settle delegate",
    title: "confirm — escrow.approveSettleDelegate(0x0)",
    copy:
      "Revoking removes the relay's settle authority immediately — it can no longer settle ANY of your Locked payments " +
      "(new settles by the relay revert NotSeller) until you AUTHORIZE again. Amounts already settled are unaffected; " +
      "buyers still refund unconsumed locks after their TTL.",
    det: `Escrow.approveSettleDelegate(0x0000000000000000000000000000000000000000) · 1 tx`,
    goLabel: "[ SIGN REVOKE ]",
  });

  async function revokeDelegateFlow() {
    if (needWallet() || needConfig() || !shared) return;
    const txbox = $("sc-tx");
    txbox.innerHTML = "";
    if (!(await confirmRevokeDelegate())) return;
    const rcpt = await sendDelegateTx("approveSettleDelegate(0x0) — revoke delegate", T.custody.ZERO_ADDRESS);
    if (rcpt) { await refreshDelegateRow(); renderCustodyStatus(); }
  }

  /* REVOKE STORED KEY — relay-side DELETE, no chain transaction. Fresh
     nonce + action=revoke signature, same header discipline as submit. */
  const confirmRevokeKey = () => dangerConfirm({
    ariaLabel: "confirm revoke stored upstream key",
    title: "confirm — relay DELETE /sellers/keys",
    copy:
      "The relay deletes your sealed upstream key immediately. Your Registry listing stays on-chain, but buyer calls " +
      "fail at upstream auth until you VERIFY a new key. The on-chain delegate authorization is separate and unaffected.",
    det: "DELETE /sellers/keys · fresh one-time nonce + EIP-191 signature (action=revoke) · no chain transaction",
    goLabel: "[ SIGN + REVOKE KEY ]",
  });

  async function revokeKeyFlow() {
    if (needWallet() || needConfig() || !shared) return;
    const txbox = $("sc-tx");
    txbox.innerHTML = "";
    if (!custodyState.pinsOk) return formErr(txbox, "relay deployment pins not verified — refusing to talk to custody endpoints");
    if (!(await confirmRevokeKey())) return;
    const me = (state.address || "").toLowerCase();
    const nr = await fetchValidatedNonce(me);
    if (nr.err) return formErr(txbox, nr.err);
    const n = nr.nonce;
    const msg = T.custody.buildCustodyRevokeMessage({
      seller: me, chain: cfg.chainId,
      escrow: cfg.escrowAddr.toLowerCase(), registry: cfg.registryAddr.toLowerCase(),
      relay: shared.relayOrigin, nonce: n.nonce, issued: n.issued_at, expires: n.expires_at,
    });
    let sig;
    try { sig = await state.signer.signMessage(msg); }
    catch (e) {
      return formErr(txbox, `signing ${(e && e.code === "ACTION_REJECTED") ? "rejected in wallet" : "failed"} — ${T.esc((e && (e.shortMessage || e.message)) || "")}`);
    }
    const body = T.custody.canonicalRevokeBody({ nonce: n.nonce, issued_at: n.issued_at, expires_at: n.expires_at });
    const r = await T.fetchJson(T.joinUrl(shared.relayOrigin, "/sellers/keys"), {
      method: "DELETE",
      headers: { "Content-Type": "application/json", "X-Tokenshare-Seller": me, "X-Tokenshare-Signature": sig },
      body,
    }, 15000);
    if (r.corsOrNetwork) return formErr(txbox, `relay unreachable (${r.error === "timeout" ? "timeout" : "network/CORS"}) — the key is NOT revoked; retry`);
    if (!r.ok) return formErr(txbox, custodyHttpError(r));
    const line = T.txLine(txbox, "DELETE /sellers/keys — sealed key revoked at the relay");
    line.confirmed(null);
    custodyState.keyStored = false;
    custodyState.anchor = null;
    custodyState.fingerprint = null;
    setVerifyNote("stored key revoked — VERIFY a fresh key to re-enable PUBLISH", "warn");
    refreshCustodyStatus().catch(() => {});
  }

  /* card init + shared-mode form transform (classic form untouched when
     m15 is absent). A malformed m15 block fails closed with a hard error. */
  function initCustodyCard() {
    $("custody-card").hidden = false;
    $("s-endpoint-fld").hidden = true;
    $("s-load-models").hidden = true;
    const hint = $("s-models-hint");
    if (hint) hint.textContent = "step 2 — VERIFY in KEY CUSTODY loads the live catalog · check the models to list";
    if (m15Broken) {
      $("sc-relay").textContent = "—";
      $("sc-pins").innerHTML =
        `<div class="kv"><span>CONFIG</span><b class="bad">✗ the m15 block in config.js is incomplete or malformed — custody is disabled (fail-closed). ` +
        `Missing / invalid: ${T.esc((m15v.missing || []).join(", ") || "unknown")}</b></div>` +
        `<p class="fld-hint">every identity pin is mandatory (relayOrigin · expectedSigner · appId · uploadPubkeySha256 · attestEvidence). ` +
        `The register form below stays in classic single-seller mode.</p>`;
      $("s-verify-btn").disabled = true;
      setVerifyNote("fix the m15 config block — custody is disabled", "err");
      return;
    }
    $("sc-relay").textContent = shared.relayOrigin;
    $("s-endpoint").value = shared.relayOrigin; /* hidden; keeps legacy readers consistent */
    setVerifyNote("idle — connect a wallet; the live pin check runs automatically", "");
    $("s-upstream-url").addEventListener("input", markCustodyEdited);
    $("s-upstream-key").addEventListener("input", markCustodyEdited);
    $("s-verify-btn").addEventListener("click", () => guardPaid($("s-verify-btn"), runSharedVerify));
    $("sc-authorize").addEventListener("click", () => guardPaid($("sc-authorize"), authorizeDelegateFlow));
    $("sc-revoke-delegate").addEventListener("click", () => guard($("sc-revoke-delegate"), revokeDelegateFlow));
    $("sc-revoke-key").addEventListener("click", () => guard($("sc-revoke-key"), revokeKeyFlow));
    renderCustodyStatus();
  }
  if (shared || m15Broken) initCustodyCard();

  /* ═══ BUYER tab ═══════════════════════════════════════════ */

  /* — seller pickers info — */
  /* searchable seller picker (one component, LOCK + CALL instances).
     The picked operator lives in the hidden input (#b-seller / #c-seller),
     so every downstream reader ($("…").value, listingOf) is unchanged.
     Filter: substring over operator address / host / model names.
     Keyboard: ↑/↓ highlight, Enter picks, Esc restores + closes.
     Health dots ride a shared per-operator cache (lazy /health probe of
     the rendered rows only, silent degrade like the market cards). */
  const sellerHealth = new Map(); /* operator → {ok, ms} | {ok:false} */

  function sellerPicker(ids, onPick) {
    const hidden = $(ids.hidden), input = $(ids.input), list = $(ids.list);
    let rows = [];   /* currently rendered (filtered) listings */
    let hi = -1;     /* highlighted row index */

    const label = (l) => `${T.truncAddr(l.operator)} · ${T.hostOf(l.endpoint)}`;
    const close = () => { list.hidden = true; input.setAttribute("aria-expanded", "false"); hi = -1; };
    const restoreLabel = () => {
      const l = listingOf(hidden.value);
      input.value = l ? label(l) : "";
    };

    function paintDots() {
      for (const l of rows) {
        const h = sellerHealth.get(l.operator);
        if (!h) continue;
        const dot = list.querySelector(`[data-dot="${l.operator}"]`);
        if (!dot) continue;
        dot.classList.add(h.ok ? "ok" : "off");
        dot.title = h.ok ? `relay /health OK · ${h.ms}ms` : "relay unreachable — or CORS not enabled";
      }
    }
    function probeDots() {
      for (const l of rows.slice(0, 12)) { /* cap: only what's rendered, only unknown */
        if (sellerHealth.has(l.operator)) continue;
        sellerHealth.set(l.operator, undefined); /* in-flight marker */
        T.probeHealth(l.endpoint).then((r) => {
          sellerHealth.set(l.operator, r.ok ? { ok: true, ms: r.ms } : { ok: false });
          paintDots();
        }).catch(() => sellerHealth.delete(l.operator));
      }
    }

    function rowHTML(l, i) {
      const p = (l.prices || []).find(Boolean);
      const chips = l.models.slice(0, 3).map((m) => `<span class="mtag">${T.esc(m)}</span>`).join("") +
        (l.models.length > 3 ? `<span class="mtag">+${l.models.length - 3}</span>` : "");
      return (
        `<div class="spick-row${l.active ? "" : " off"}" role="option" data-i="${i}" data-op="${T.esc(l.operator)}">` +
          `<div class="spick-top">` +
            `<span class="hdot" data-dot="${T.esc(l.operator)}"></span>` +
            `<span class="spick-addr">${T.truncAddr(l.operator)}</span>` +
            `<span class="spick-host">${T.esc(T.hostOf(l.endpoint))}</span>` +
            (l.active ? `<span class="badge">ACTIVE</span>` : `<span class="badge off">INACTIVE</span>`) +
          `</div>` +
          (chips ? `<div class="spick-models">${chips}</div>` : "") +
          (p ? `<div class="spick-price">c $${T.fmtUsdc(p.cachedIn)} · i $${T.fmtUsdc(p.input)} · o $${T.fmtUsdc(p.output)} · min ≥ $${T.fmtUsdc(T.minAmountEstimate(p))}</div>` : "") +
        `</div>`
      );
    }

    function paint() {
      list.querySelectorAll(".spick-row").forEach((el) => {
        const on = Number(el.dataset.i) === hi;
        el.classList.toggle("sel", on);
        el.setAttribute("aria-selected", String(on));
      });
      const el = hi >= 0 && list.querySelector(`[data-i="${hi}"]`);
      if (el) el.scrollIntoView({ block: "nearest" });
    }

    function render() {
      const q = input.value.trim().toLowerCase();
      rows = state.listings.filter((l) =>
        !q || l.operator.toLowerCase().includes(q) ||
        T.hostOf(l.endpoint).toLowerCase().includes(q) ||
        l.models.some((m) => m.toLowerCase().includes(q)));
      hi = rows.findIndex((l) => l.active);
      list.innerHTML = rows.length
        ? rows.map(rowHTML).join("")
        : `<div class="spick-empty">${state.listings.length
            ? "no sellers match — try an address, host, or model substring"
            : "no sellers on-chain yet (or RPC unreachable) — the market page shows the live set"}</div>`;
      paint();
      paintDots();
      probeDots();
    }

    function open() {
      render();
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
    }

    function pick(i) {
      const l = rows[i];
      if (!l || !l.active) return; /* INACTIVE rows are visible but not pickable (old <select disabled>) */
      hidden.value = l.operator;
      input.value = label(l);
      close();
      onPick(l.operator);
    }

    input.addEventListener("focus", () => {
      /* a picked label would filter the list to itself — clear it so the
         full set shows; blur/esc restores it when nothing new is picked */
      const l = listingOf(hidden.value);
      if (l && input.value === label(l)) input.value = "";
      open();
    });
    input.addEventListener("input", open);
    input.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        if (list.hidden) return open();
        const pickable = rows.map((l, i) => (l.active ? i : -1)).filter((i) => i >= 0);
        if (!pickable.length) return;
        const pos = pickable.indexOf(hi);
        hi = pickable[(pos + (e.key === "ArrowDown" ? 1 : pickable.length - 1)) % pickable.length];
        paint();
      } else if (e.key === "Enter") {
        e.preventDefault();
        const i = hi >= 0 ? hi : rows.findIndex((l) => l.active);
        if (i >= 0) pick(i);
      } else if (e.key === "Escape") {
        e.stopPropagation();
        restoreLabel();
        close();
        input.blur();
      }
    });
    input.addEventListener("blur", () => {
      /* delay so a row mousedown (preventDefault) wins the race */
      setTimeout(() => { restoreLabel(); close(); }, 120);
    });
    list.addEventListener("mousedown", (e) => {
      const row = e.target.closest(".spick-row");
      if (!row) return;
      e.preventDefault(); /* keep focus — no blur race with pick() */
      pick(Number(row.dataset.i));
    });

    return {
      /* listings reloaded — keep the pick if the operator still exists */
      refresh() {
        if (hidden.value && !listingOf(hidden.value)) { hidden.value = ""; }
        restoreLabel();
        if (!list.hidden) render();
      },
      /* programmatic pick (LOCK→CALL mirror, lock success) — silent, no onPick */
      set(op) {
        const l = listingOf(op);
        hidden.value = l ? l.operator : "";
        restoreLabel();
      },
    };
  }

  const bSellerPick = sellerPicker(
    { hidden: "b-seller", input: "b-seller-q", list: "b-seller-list" },
    () => syncSellerInfo());
  const cSellerPick = sellerPicker(
    { hidden: "c-seller", input: "c-seller-q", list: "c-seller-list" },
    () => syncCallModels());

  function syncSellerInfo() {
    const l = listingOf($("b-seller").value);
    const info = $("b-lock-info");
    if (!l) { info.innerHTML = `<p class="empty-hint">pick a seller to see per-model tier prices and the minAmount estimate.</p>`; return; }
    /* v2: one price triple + min estimate per model */
    const rows = l.models.map((m) => {
      const p = T.priceFor(l, m);
      if (!p) return "";
      return `<div class="mpr"><span class="mtag">${T.esc(m)}</span>` +
        `<span class="mono">c $${T.fmtUsdc(p.cachedIn)} · i $${T.fmtUsdc(p.input)} · o $${T.fmtUsdc(p.output)} · min ≥ $${T.fmtUsdc(T.minAmountEstimate(p))}</span></div>`;
    }).join("");
    const maxMin = T.maxMinAmountEstimate(l);
    info.innerHTML =
      `<div class="kv"><span>PRICES /1M · MIN EST</span><b>${rows || "—"}</b></div>`;
    $("b-lock-hint").textContent =
      `≥ priciest-model estimate $${T.fmtUsdc(maxMin)} (in×200k + out×32k caps; the tier of the called model applies)`;
    /* mirror into call tab (silent — no onPick loop) */
    if (!$("c-seller").value) { cSellerPick.set(l.operator); syncCallModels(); }
  }

  /* — lock pre-check: the LOCK card shows the live escrow balance
       (state.escrowBal, refreshed by refreshBalances() after connect and
       after every deposit/withdraw/lock/refund). MAX AMOUNT above it
       disables [ LOCK ] with an inline note — the chain would revert
       InsufficientBalance, so the click never reaches the wallet. — */
  function syncLockGate() {
    const balEl = $("b-lock-bal");
    const note = $("b-lock-bal-note");
    const bal = state.escrowBal;
    balEl.textContent = bal == null ? "—" : `$${T.fmtUsdc(bal)}`;
    balEl.title = bal == null ? "" : `${T.fmtInt(bal)} native units`;
    /* zero escrow balance (connected, read OK) → steer to DEPOSIT first;
       the amount gate below still covers the partial-shortfall case */
    const depFirst = $("b-dep-first");
    if (depFirst) depFirst.hidden = !(bal === 0n);
    const sf = T.lockShortfall(readPrice("b-max"), bal);
    note.hidden = !sf;
    note.textContent = sf ? sf.text : "";
    /* balance gate ∨ policy consent gate */
    $("b-lock-btn").disabled = Boolean(sf) || !T.policyConsent.isValid(state.address);
  }
  $("b-max").addEventListener("input", syncLockGate);

  /* — deposit (approve → deposit) — */
  $("b-dep-btn").addEventListener("click", () => guardPaid($("b-dep-btn"), async () => {
    if (needWallet() || needConfig()) return;
    const txbox = $("b-dep-tx");
    txbox.innerHTML = "";
    const amt = readPrice("b-dep-amt");
    if (amt === null || amt <= 0n) return formErr(txbox, "enter a positive USDC amount (≤6 decimals)");

    const token = T.usdc(state.signer);
    const esc = T.escrow(state.signer);
    try {
      const allowance = await token.allowance(state.address, cfg.escrowAddr);
      if (allowance < amt) {
        const rcpt = await T.runTx(T.txLine(txbox, "approve(USDC → Escrow) — step 1/2"), token.approve(cfg.escrowAddr, amt));
        if (!rcpt) return;
      } else {
        const skip = T.txLine(txbox, "approve(USDC → Escrow)");
        skip.confirmed(null);
        skip.el.querySelector(".txl-state").textContent = "skipped — allowance sufficient";
      }
      const rcpt = await T.runTx(T.txLine(txbox, `deposit(${T.fmtInt(amt)}) — step 2/2`), esc.deposit(amt));
      if (rcpt) await refreshBalances();
    } catch (e) { formErr(txbox, T.humanizeEscrowErr(e) || e.shortMessage || e.message); }
  }));

  /* — withdraw — */
  $("b-withdraw-btn").addEventListener("click", () => guard($("b-withdraw-btn"), async () => {
    if (needWallet() || needConfig()) return;
    const txbox = $("b-bal-tx");
    txbox.innerHTML = "";
    const amt = readPrice("b-withdraw-amt");
    if (amt === null || amt <= 0n) return formErr(txbox, "enter a positive USDC amount (≤6 decimals)");
    const rcpt = await T.runTx(T.txLine(txbox, `withdraw(${T.fmtInt(amt)})`), T.escrow(state.signer).withdraw(amt));
    if (rcpt) await refreshBalances();
  }));

  /* — lock — */
  /* C2 hardening: locks come from localStorage — a tampered/corrupt entry
     (missing maxAmount, non-numeric fields) must NEVER throw here: this
     runs inside the boot IIFE, and one throw would kill everything
     rendered after it (disputes, selects, cards). Per-entry guards:
     skip hard-failures, degrade unparsable amounts, esc all raw text. */
  function renderSessionLocks() {
    const mine = T.locks.all().filter((l) => !state.address || T.sameAddr(l.buyer || "", state.address) || !l.buyer);
    const list = $("c-payment-list");
    const rlist = $("r-payment-list");
    const opts = [];
    const rows = [];
    for (const l of mine) {
      try {
        const pid = T.esc(String(l.paymentId ?? "?"));
        const seller = T.esc(T.truncAddr(String(l.seller || "?")));
        let max = null;
        try { max = T.fmtUsdc(l.maxAmount); } catch { max = null; } /* formatUnits throws on garbage */
        opts.push(`<option value="${pid}">#${pid} · ${seller}${max != null ? ` · max $${max}` : " · max ?"}</option>`);
        /* visible SESSION LOCKS row (M13: mint + usage per lock) — only
           entries with a decimal paymentId become actionable rows */
        if (/^\d+$/.test(String(l.paymentId ?? ""))) {
          const when = l.ts ? new Date(l.ts).toLocaleString() : "?";
          rows.push(
            `<div class="lock-row">` +
              `<div class="lock-row-top">` +
                `<span class="lock-pid mono">#${pid}</span>` +
                `<span class="mono dim">${seller}</span>` +
                (max != null ? `<span class="mono dim">max $${max}</span>` : "") +
                `<span class="lock-when dim">${T.esc(when)}</span>` +
                `<button class="btn btn-sm btn-ghost" type="button" data-mint="${pid}" data-seller="${T.esc(String(l.seller || ""))}"${consentOk() ? "" : ` disabled title="${POLICY_GATE_TITLE}"`}>[ MINT API KEY ]</button>` +
                `<button class="btn btn-sm btn-ghost" type="button" data-usage="${pid}" data-seller="${T.esc(String(l.seller || ""))}">[ USAGE ]</button>` +
              `</div>` +
              `<div class="lock-usage" data-usage-panel="${pid}" hidden></div>` +
            `</div>`);
        }
      } catch { /* unexpected shape — skip the row entirely */ }
    }
    const html = opts.join("");
    list.innerHTML = html;
    rlist.innerHTML = html;
    $("locks-list").innerHTML = rows.join("") ||
      `<p class="empty-hint">no locks in this browser yet — a successful LOCK lands here; mint a stateless API key for agents or watch accrued usage.</p>`;
  }

  /* — M13 MINT API KEY modal (wsel chrome; singleton; innerHTML + esc like
       the listing renders, key rides data-copy → global click-to-copy) — */
  function openMintModal(paymentId, seller) {
    if (needWallet() || needConfig()) return;
    if (!consentOk()) { consentNudge(); return; } /* paid action — gate before the modal opens */
    const overlay = document.createElement("div");
    overlay.className = "wsel-overlay";
    overlay.innerHTML =
      `<div class="wsel mint" role="dialog" aria-modal="true" aria-label="mint api key">` +
        `<div class="wsel-bar"><span class="tdot"></span><span class="tdot"></span><span class="tdot"></span>` +
          `<span class="wsel-title">mint api key — payment #${T.esc(paymentId)}</span></div>` +
        `<div class="wsel-body"><p class="empty-hint">reading getPayment(${T.esc(paymentId)}) …</p></div>` +
        `<div class="wsel-foot"><span class="wsel-hint">esc / click outside to close</span>` +
          `<button class="wsel-cancel" type="button">[ CLOSE ]</button></div>` +
      `</div>`;
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    let minted = null; /* set by a successful SIGN + MINT; written to the
                          apikeys metadata registry when the modal closes */
    const close = () => {
      document.removeEventListener("keydown", onKey, true);
      document.body.style.overflow = prevOverflow;
      overlay.remove();
      if (minted) { T.apikeys.add(minted); renderApiKeys(); }
    };
    const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
    document.addEventListener("keydown", onKey, true);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
    overlay.querySelector(".wsel-cancel").addEventListener("click", close);
    document.body.appendChild(overlay);

    const body = overlay.querySelector(".wsel-body");
    (async () => {
      let p;
      try { p = await T.escrow(T.readProvider()).getPayment(paymentId); }
      catch (e) {
        body.innerHTML = `<p class="empty-hint err">read failed — ${T.esc(T.humanizeEscrowErr(e) || e.shortMessage || e.message)}</p>`;
        return;
      }
      /* C2 fix: endpoint/base_url resolve off the ON-CHAIN lock seller —
         a forged SESSION LOCKS row (localStorage) can no longer steer the
         key/curl example at an attacker's server */
      const l = listingOf(p.seller);
      const tampered = seller && !T.sameAddr(seller, p.seller);
      const st = T.PAYMENT_STATES[Number(p.state)] || "?";
      const expiry = Number(p.expiresAt);
      const expiryStr = new Date(expiry * 1000).toLocaleString();
      const expired = expiry * 1000 <= Date.now();
      const mintable = st === "Locked" && !expired;
      const msg = T.buildMintMessage(paymentId, expiry, p.maxAmount);
      body.innerHTML =
        (tampered
          ? `<p class="empty-hint err">⚠ this row's seller (${T.esc(T.truncAddr(seller))}) ≠ the on-chain lock seller (${T.esc(T.truncAddr(p.seller))}) — the localStorage entry looks tampered; using the on-chain seller</p>`
          : "") +
        `<div class="kv"><span>PAYMENT</span><b class="mono">#${T.esc(paymentId)} · ${st}${expired ? " · EXPIRED" : ""}</b></div>` +
        `<div class="kv"><span>SELLER</span><b class="mono">${T.esc(T.truncAddr(p.seller))}</b></div>` +
        `<div class="kv"><span>MAX AMOUNT</span><b class="mono">$${T.fmtUsdc(p.maxAmount)}</b></div>` +
        `<div class="kv"><span>EXPIRES</span><b class="mono">${T.esc(expiryStr)}</b></div>` +
        (l ? `<div class="kv"><span>BASE URL</span><b class="mono wrap-anywhere">${T.esc(l.endpoint)}</b></div>` : "") +
        `<p class="fld-hint">one EIP-191 signature mints a stateless key bound to this lock — agents use it as the OpenAI api_key against the seller's relay:</p>` +
        `<pre class="preview mono wrap-anywhere">${T.esc(msg)}</pre>` +
        (mintable
          ? `<button class="btn btn-wide" type="button" data-mint-go>[ SIGN + MINT ]</button>`
          : `<p class="empty-hint err">${st !== "Locked" ? `payment is ${st} — a key only works while the lock is Locked` : "lock expired — refund and re-lock for a fresh key"}</p>`);
      const go = body.querySelector("[data-mint-go]");
      if (go) go.addEventListener("click", () => guardPaid(go, async () => {
        let sig;
        try { sig = await state.signer.signMessage(msg); }
        catch (e) {
          body.insertAdjacentHTML("beforeend",
            `<p class="empty-hint err">signing ${(e && e.code === "ACTION_REJECTED") ? "rejected in wallet" : "failed"} — ${T.esc((e && (e.shortMessage || e.message)) || "")}</p>`);
          return;
        }
        const key = T.assembleApiKey({
          paymentId, expiry, maxAmount: p.maxAmount, buyer: state.address, signature: sig,
        });
        /* registry metadata — e/m from the CHAIN read above (getPayment),
           never from the localStorage lock row; the key body is NOT stored */
        minted = {
          p: BigInt(paymentId).toString(10), e: expiry, m: p.maxAmount.toString(),
          model: (l && l.models[0]) || "", createdAt: Date.now(),
        };
        renderMintResult(body, { paymentId, key, endpoint: l ? l.endpoint : null, model: l && l.models[0], expiryStr, maxAmount: p.maxAmount });
      }));
    })();
  }

  function renderMintResult(body, { paymentId, key, endpoint, model, expiryStr, maxAmount }) {
    /* C1 fix: the snippet speaks in $BASE_URL/$API_KEY placeholders, with
       every value assigned through POSIX single-quote escaping (shQuote) —
       chain-controlled strings (endpoint, model) stay inert even with
       quotes/$()/backticks/newlines in them */
    const curl = endpoint
      ? `BASE_URL=${T.shQuote(endpoint)}\n` +
        `API_KEY=${T.shQuote(key)}\n\n` +
        `curl "$BASE_URL${T.RELAY_CHAT_PATH}" \\\n` +
        `  -H "Authorization: Bearer $API_KEY" \\\n` +
        `  -H "Content-Type: application/json" \\\n` +
        `  -d ${T.shQuote(JSON.stringify({ model: model || "MODEL", messages: [{ role: "user", content: "hi" }] }))}`
      : "";
    body.innerHTML =
      `<div class="kv"><span>STATUS</span><b class="ok">✓ key minted — payment #${T.esc(paymentId)}</b></div>` +
      `<div class="fld"><span class="fld-lbl">API KEY <i>click to copy — shown once, not stored anywhere</i></span>` +
        `<div class="mint-key mono wrap-anywhere" data-copy="${T.esc(key)}" title="click to copy">${T.esc(key)}</div></div>` +
      (endpoint
        ? `<div class="kv"><span>BASE URL</span><b class="mono wrap-anywhere" data-copy="${T.esc(endpoint)}" title="click to copy">${T.esc(endpoint)}</b></div>` +
          `<div class="fld"><span class="fld-lbl">CURL <i>OpenAI-compatible · values assigned above, click the block to copy</i></span>` +
          `<pre class="preview mono wrap-anywhere mint-curl" data-copy="${T.esc(curl)}" title="click to copy the full snippet">${T.esc(curl)}</pre></div>`
        : "") +
      `<p class="mint-warn">⚠ stateless bearer — a leak can spend up to <b>$${T.fmtUsdc(maxAmount)}</b> (this lock's max) until revoked. ` +
      `Kill the key any time via <b>[ REVOKE ]</b> in the API KEYS panel (relay-side, immediate); it dies with the lock TTL (${T.esc(expiryStr)}) either way. ` +
      `Shown once — only metadata lands in the API KEYS panel, never the key itself.</p>`;
  }

  /* — M13 USAGE: relay's accrued-usage view, inline under the lock row —
     panel/chainP are optional (API KEYS panel reuses this): panel defaults
     to the session-lock row's usage slot; chainP is a pre-read getPayment
     result that saves the row a second chain call. */
  async function fetchUsage(paymentId, sellerRow, panel, chainP) {
    panel = panel || document.querySelector(`[data-usage-panel="${paymentId}"]`);
    if (!panel) return;
    const refreshBtn = `<button class="btn btn-sm btn-ghost" type="button" data-usage-refresh="${T.esc(paymentId)}" data-seller="${T.esc(sellerRow)}">[ REFRESH ]</button>`;
    panel.hidden = false;
    panel.innerHTML = `<span class="dim mono">reading lock + GET /payment/${T.esc(paymentId)}/usage …</span>`;
    /* C2: the endpoint resolves off the ON-CHAIN lock seller — a forged
       localStorage row must not redirect the usage call. Falls back to the
       row value (with a note) only when the chain read itself fails. */
    let seller = sellerRow, warn = "";
    try {
      const p = chainP || await T.escrow(T.readProvider()).getPayment(paymentId);
      if (sellerRow && !T.sameAddr(sellerRow, p.seller)) {
        warn = `<div class="usage-warn bad mono">⚠ row seller ${T.esc(T.truncAddr(sellerRow))} ≠ on-chain ${T.esc(T.truncAddr(p.seller))} — localStorage entry tampered? using the on-chain seller</div>`;
      }
      seller = p.seller;
    } catch {
      warn = `<div class="usage-warn dim mono">chain read failed — endpoint falls back to the local row seller</div>`;
    }
    const l = listingOf(seller);
    if (!l) {
      panel.innerHTML = warn + `<span class="dim">seller not in the current listings — endpoint unknown (RPC hiccup? refresh the page)</span>`;
      return;
    }
    const r = await T.fetchJson(T.joinUrl(l.endpoint, `/payment/${paymentId}/usage`), {}, 15000);
    if (r.corsOrNetwork) {
      panel.innerHTML = warn + `<span class="bad mono">relay unreachable (${r.error === "timeout" ? "timeout after 15s" : "network/CORS"})</span> ` + refreshBtn;
      return;
    }
    if (!r.ok || !r.body) {
      panel.innerHTML = warn + `<span class="bad mono">HTTP ${r.status} — usage unavailable</span> ` + refreshBtn;
      return;
    }
    /* native 6dp ints; relay accrued total may lead the on-chain counter
       while a background settlePartial flush is in flight.
       L2 guard: hostile/malformed relay values (non-numeric, Infinity) must
       degrade to an unavailable row, never throw and wedge the panel. */
    const num = (v) => { const n = Number(v); return Number.isFinite(n) ? BigInt(Math.trunc(n)) : null; };
    const cap = num(r.body.captured), max = num(r.body.maxAmount), rem = num(r.body.remaining);
    if (cap === null || max === null || rem === null) {
      panel.innerHTML = warn + `<span class="bad mono">usage data malformed — unavailable</span> ` + refreshBtn;
      return;
    }
    const pct = max > 0n ? Number((cap * 10000n) / max) / 100 : 0;
    panel.innerHTML = warn +
      `<div class="usage-line mono">captured <b>$${T.fmtUsdc(cap)}</b> · remaining <b>$${T.fmtUsdc(rem)}</b> · max <b>$${T.fmtUsdc(max)}</b></div>` +
      `<div class="usage-bar" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"><i style="width:${pct}%"></i></div>` +
      `<div class="usage-foot"><span class="dim">${new Date().toLocaleTimeString()} · relay-accrued view (may lead on-chain captured during flush)</span>${refreshBtn}</div>`;
  }

  /* ═══ M13 补件: API KEYS panel — minted-key metadata + relay revoke ═══
     The registry holds METADATA ONLY ({p,e,m,model,createdAt,revokedAt?})
     — the key body is shown once at mint and never stored. base_url and
     the revoke target re-resolve off the ON-CHAIN payment seller
     (getPayment → listingOf, C2 discipline): localStorage is untrusted. */
  const apiKeysList = $("apikeys-list");

  const fmtKeyLeft = (sec) => {
    if (sec <= 0) return "expired";
    const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600),
          m = Math.floor((sec % 3600) / 60), s = sec % 60;
    if (d > 0) return `${d}d ${h}h ${m}m left`;
    if (h > 0) return `${h}h ${m}m ${s}s left`;
    return `${m}m ${String(s).padStart(2, "0")}s left`;
  };

  /* ACTIVE / EXPIRED / REVOKED — EXPIRED is a pure local computation
     (now ≥ e), the same hard check the relay applies to the bearer grant */
  const keyStatusOf = (rec, nowSec) =>
    rec.revokedAt ? "REVOKED" : (nowSec >= Number(rec.e) ? "EXPIRED" : "ACTIVE");

  function renderApiKeys() {
    const nowSec = Math.floor(Date.now() / 1000);
    const rows = [];
    const actives = [];
    const allPids = [];
    for (const rec of T.apikeys.all()) {
      try {
        const pid = String(rec.p ?? "");
        const e = Number(rec.e);
        if (!/^\d+$/.test(pid) || !Number.isFinite(e) || e <= 0) continue; /* tampered/garbage — skip the row */
        const st = keyStatusOf(rec, nowSec);
        let max = null;
        try { max = T.fmtUsdc(BigInt(String(rec.m))); } catch { max = null; } /* formatUnits throws on garbage */
        const badge =
          st === "ACTIVE" ? `<span class="kstat kstat-active">ACTIVE</span>` :
          st === "EXPIRED" ? `<span class="kstat kstat-expired">EXPIRED</span>` :
          `<span class="kstat kstat-revoked"><i class="rdot"></i>REVOKED</span>`;
        const when = st === "ACTIVE"
          ? `<span class="mono dim" data-key-cd="${e}" title="expiry countdown — the relay hard-rejects the key at zero">${fmtKeyLeft(e - nowSec)}</span>`
          : st === "EXPIRED"
            ? `<span class="mono dim">expired ${T.esc(new Date(e * 1000).toLocaleString())}</span>`
            : `<span class="mono dim">revoked ${T.esc(new Date(Number(rec.revokedAt)).toLocaleString())}</span>`;
        rows.push(
          `<div class="lock-row key-row" data-key-row="${T.esc(pid)}">` +
            `<div class="lock-row-top">` +
              `<span class="lock-pid mono">#${T.esc(pid)}</span>` +
              `<span class="mono dim">${T.esc(rec.model ? String(rec.model) : "?")}</span>` +
              badge +
              when +
              `<button class="btn btn-sm btn-ghost" type="button" data-key-base="${T.esc(pid)}" disabled title="resolving base_url from the on-chain seller …">[ COPY BASE URL ]</button>` +
              (st === "ACTIVE"
                ? `<button class="btn btn-sm btn-danger" type="button" data-key-revoke="${T.esc(pid)}">[ REVOKE ]</button>`
                : "") +
            `</div>` +
            `<div class="key-meta dim mono">` +
              (max != null ? `max $${T.esc(max)} · ` : "") +
              `minted ${T.esc(rec.createdAt ? new Date(Number(rec.createdAt)).toLocaleString() : "?")}` +
            `</div>` +
            `<div class="key-note mono" data-key-note="${T.esc(pid)}" hidden></div>` +
            (st === "ACTIVE" ? `<div class="lock-usage" data-key-usage="${T.esc(pid)}"></div>` : "") +
          `</div>`);
        allPids.push(pid);
        if (st === "ACTIVE") actives.push(pid);
      } catch { /* unexpected shape — skip the row entirely */ }
    }
    apiKeysList.innerHTML = rows.join("") ||
      `<p class="empty-hint">no api keys minted in this browser yet — MINT API KEY from a session lock. Only metadata (paymentId/expiry/max/model) is kept here; the key body is shown once at mint and never stored.</p>`;
    /* every row resolves base_url off the chain seller (one getPayment
       each); the relay usage pull is ACTIVE-only */
    for (const pid of allPids) loadKeyRow(pid, actives.includes(pid));
  }

  /* pid → seller cache (#12): a paymentId's seller is immutable on-chain
     (set once by lock), so cache it indefinitely — kills the repeated
     getPayment every row render / REFRESH used to cost */
  const keySellerCache = new Map();
  async function keySellerOf(pid) {
    const hit = keySellerCache.get(String(pid));
    if (hit) return hit;
    const p = await T.escrow(T.readProvider()).getPayment(pid);
    const seller = String(p.seller);
    keySellerCache.set(String(pid), seller);
    if (keySellerCache.size > 200) keySellerCache.delete(keySellerCache.keys().next().value); /* FIFO cap */
    return seller;
  }

  /* fills [ COPY BASE URL ] off the on-chain seller's listing; active rows
     also pull the relay usage view (fetchUsage reuse — the cached seller
     rides chainP as {seller}, the only field it consumes) */
  async function loadKeyRow(pid, withUsage) {
    const row = apiKeysList.querySelector(`[data-key-row="${pid}"]`);
    if (!row) return;
    let seller = null;
    try { seller = await keySellerOf(pid); }
    catch { /* fall through — fetchUsage re-tries with its own fallback note */ }
    if (!row.isConnected) return; /* a re-render replaced this row meanwhile */
    const baseBtn = row.querySelector("[data-key-base]");
    if (baseBtn) {
      const l = seller ? listingOf(seller) : null;
      if (l) {
        baseBtn.disabled = false;
        baseBtn.dataset.copy = l.endpoint; /* property assignment — no HTML parsing */
        baseBtn.title = `${l.endpoint} — click to copy (resolved from the on-chain seller)`;
      } else {
        baseBtn.title = seller
          ? "seller not in the current listings (delisted? RPC hiccup?) — base_url unknown"
          : "chain read failed — base_url unresolved";
      }
    }
    if (withUsage) fetchUsage(pid, seller || "", row.querySelector(`[data-key-usage="${pid}"]`), seller ? { seller } : null);
  }

  /* 1s countdown ticker — crossing zero flips ACTIVE → EXPIRED via a
     re-render (the rebuilt row shows a static expired stamp instead) */
  setInterval(() => {
    const spans = apiKeysList.querySelectorAll("[data-key-cd]");
    if (!spans.length) return;
    const nowSec = Math.floor(Date.now() / 1000);
    let flip = false;
    spans.forEach((el) => {
      const left = Number(el.dataset.keyCd) - nowSec;
      if (left <= 0) { flip = true; return; }
      el.textContent = fmtKeyLeft(left);
    });
    if (flip) renderApiKeys();
  }, 1000);

  /* REVOKE confirm — existing dangerConfirm chrome */
  const confirmRevoke = (pid, e) => dangerConfirm({
    ariaLabel: "confirm revoke api key",
    title: `confirm — revoke api key · payment #${pid}`,
    copy:
      "Revoking kills this key immediately and cannot be undone — it stays dead until the payment TTL ends. " +
      "Amounts already captured on-chain are unaffected; the unused balance refunds as usual after the TTL.",
    det: `POST {relay}/payment/${pid}/revoke · EIP-191 sign "TokenShare API key revoke|paymentId=${pid}|expiry=${e}" · ` +
      `200 {"revoked":true} (repeat revoke idempotent) · bearer calls with this key → 401 revoked`,
    goLabel: "[ SIGN + REVOKE ]",
  });

  async function revokeKeyFlow(pid, btn) {
    if (!/^\d+$/.test(String(pid))) return;
    const note = apiKeysList.querySelector(`[data-key-note="${pid}"]`);
    const say = (html, bad = true) => {
      if (!note || !note.isConnected) return;
      note.hidden = false;
      note.classList.toggle("bad", bad);
      note.innerHTML = html;
    };
    if (needWallet() || needConfig()) return;
    const rec = T.apikeys.all().find((r) => r && String(r.p) === String(pid));
    if (!rec) return; /* row came from the registry — record must exist */
    if (!(await confirmRevoke(pid, String(rec.e)))) return;
    try {
      await guard(btn, async () => {
        say(`<span class="dim">sign the revoke message in your wallet …</span>`, false);
        const msg = T.buildRevokeMessage(rec.p, rec.e); /* throws on tampered e */
        let sig;
        try { sig = await state.signer.signMessage(msg); }
        catch (e) {
          say(`signing ${(e && e.code === "ACTION_REJECTED") ? "rejected in wallet" : "failed"} — ${T.esc((e && (e.shortMessage || e.message)) || "")}`);
          return;
        }
        /* endpoint off the ON-CHAIN seller (C2) — the registry record
           carries no seller, and localStorage is untrusted anyway; the
           pid→seller cache makes repeat revokes read-free */
        let endpoint = null;
        try {
          const seller = await keySellerOf(pid);
          const l = listingOf(seller);
          if (l) endpoint = l.endpoint;
        } catch { /* fall through */ }
        if (!endpoint) { say(`relay endpoint unresolved (chain read failed or seller delisted) — refresh and retry; the key is still live`); return; }
        say(`<span class="dim">POST /payment/${T.esc(pid)}/revoke …</span>`, false);
        const r = await T.fetchJson(T.joinUrl(endpoint, `/payment/${pid}/revoke`), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message: msg, signature: sig }),
        }, 15000);
        if (r.corsOrNetwork) { say(`relay unreachable (${r.error === "timeout" ? "timeout after 15s" : "network/CORS"}) — the key is NOT revoked; retry`); return; }
        if (r.ok && r.body && r.body.revoked === true) {
          T.apikeys.markRevoked(pid);
          renderApiKeys();
          return;
        }
        const detail = r.body && r.body.detail ? String(r.body.detail) : "";
        if (r.status === 401) { say(`relay 401 — ${T.esc(detail || "the connected wallet is not the payment buyer")} — key NOT revoked`); return; }
        say(`HTTP ${r.status}${detail ? ` — ${T.esc(detail)}` : ""} — revoke failed; the key is still live`);
      });
    } catch (e) { say(`revoke failed — ${T.esc((e && (e.shortMessage || e.message)) || String(e))}`); }
  }

  /* api-key row actions (delegated — rows re-render with renderApiKeys);
     data-copy buttons are handled by the global copy handler */
  apiKeysList.addEventListener("click", (e) => {
    const rvBtn = e.target.closest("[data-key-revoke]");
    if (rvBtn) { revokeKeyFlow(rvBtn.dataset.keyRevoke, rvBtn); return; }
    const refBtn = e.target.closest("[data-usage-refresh]");
    if (refBtn) fetchUsage(refBtn.dataset.usageRefresh, refBtn.dataset.seller, refBtn.closest("[data-key-usage]"));
  });

  /* lock-row actions (delegated — rows re-render with renderSessionLocks) */
  $("locks-list").addEventListener("click", (e) => {
    const mintBtn = e.target.closest("[data-mint]");
    if (mintBtn) { openMintModal(mintBtn.dataset.mint, mintBtn.dataset.seller); return; }
    const refBtn = e.target.closest("[data-usage-refresh]");
    if (refBtn) { fetchUsage(refBtn.dataset.usageRefresh, refBtn.dataset.seller); return; }
    const usBtn = e.target.closest("[data-usage]");
    if (usBtn) {
      const panel = document.querySelector(`[data-usage-panel="${usBtn.dataset.usage}"]`);
      if (panel && !panel.hidden) { panel.hidden = true; return; } /* toggle off */
      fetchUsage(usBtn.dataset.usage, usBtn.dataset.seller);
    }
  });

  /* guard() unconditionally re-enables the button in its finally — the
     trailing .finally re-applies the balance gate after every click run */
  $("b-lock-btn").addEventListener("click", () =>
    Promise.resolve(guardPaid($("b-lock-btn"), async () => {
    if (needWallet() || needConfig()) return;
    const txbox = $("b-lock-tx");
    txbox.innerHTML = "";
    const seller = $("b-seller").value;
    const l = listingOf(seller);
    const max = readPrice("b-max");
    const ttl = parseInt($("b-ttl").value, 10);
    if (!l) return formErr(txbox, "choose a seller");
    if (max === null || max <= 0n) return formErr(txbox, "enter a positive maxAmount (≤6 decimals)");
    if (!ttl || ttl < 60) return formErr(txbox, "ttl ≥ 60s");
    /* belt under the disabled-button gate (stale balance, devtools re-enable) */
    const sf = T.lockShortfall(max, state.escrowBal);
    if (sf) return formErr(txbox, sf.text);
    const min = T.maxMinAmountEstimate(l);
    if (max < min) formErr(txbox, `note: maxAmount below relay min estimate $${T.fmtUsdc(min)} — calls will 402`);

    const rcpt = await T.runTx(
      T.txLine(txbox, `lock(${T.truncAddr(seller)}, ${T.fmtInt(max)}, ${ttl})`),
      T.escrow(state.signer).lock(seller, max, ttl)
    );
    if (!rcpt) return;
    const pid = T.parseLockedPaymentId(rcpt);
    if (pid !== null) {
      state.lastPaymentId = pid;
      $("b-payment-id").innerHTML = `<span class="pid-label">PAYMENT ID — click to copy</span><span class="pid" data-copy="${pid.toString()}" title="click to copy">${pid.toString()}</span>`;
      /* M13: one-tap mint straight off the fresh lock */
      const mintBtn = document.createElement("button");
      mintBtn.className = "btn btn-sm";
      mintBtn.type = "button";
      mintBtn.textContent = "[ MINT API KEY ]";
      mintBtn.disabled = !consentOk(); /* belt — the lock just passed the same gate */
      mintBtn.addEventListener("click", () => openMintModal(pid.toString(), seller));
      $("b-payment-id").appendChild(mintBtn);
      T.locks.add({
        paymentId: pid.toString(), buyer: state.address, seller,
        maxAmount: max.toString(), ttl, txHash: rcpt.hash, ts: Date.now(),
      });
      renderSessionLocks();
      $("c-payment").value = pid.toString();
      if (!$("c-seller").value) { cSellerPick.set(seller); syncCallModels(); }
    } else {
      $("b-payment-id").innerHTML = `<span class="dim mono">Locked — paymentId in the Locked event (see tx)</span>`;
    }
    await refreshBalances();
    })).finally(syncLockGate));

  /* — call demo — */
  function syncCallModels() {
    const l = listingOf($("c-seller").value);
    const sel = $("c-model");
    if (!l) { sel.innerHTML = `<option value="">— seller first —</option>`; return; }
    sel.innerHTML = l.models.map((m) => `<option value="${T.esc(m)}">${T.esc(m)}</option>`).join("");
  }

  function termLine(term, html) {
    const div = document.createElement("div");
    div.innerHTML = html;
    term.appendChild(div);
    term.scrollTop = term.scrollHeight;
  }

  function recordDispute(paymentId, reason, seller) {
    T.disputes.add({ paymentId: String(paymentId), reason, seller, buyer: state.address || null, ts: Date.now() });
    renderDisputes();
  }

  $("c-send").addEventListener("click", () => guardPaid($("c-send"), async () => {
    if (needWallet()) return;
    const l = listingOf($("c-seller").value);
    const model = $("c-model").value;
    const paymentId = $("c-payment").value.trim();
    const prompt = $("c-prompt").value.trim();
    const term = $("c-out");
    const rcptPanel = $("c-receipt");
    term.innerHTML = "";
    rcptPanel.innerHTML = "";
    rcptPanel.className = "rcpt";
    rcptPanel.hidden = true;

    if (!l) return termLine(term, `<span class="t-a">!</span> choose a seller`);
    if (!model) return termLine(term, `<span class="t-a">!</span> choose a model`);
    if (!/^\d+$/.test(paymentId)) return termLine(term, `<span class="t-a">!</span> paymentId must be a decimal integer`);
    if (!prompt) return termLine(term, `<span class="t-a">!</span> prompt is empty`);

    /* body — zero extra fields (Kimi rejects temperature/top_p with 400) */
    const body = JSON.stringify({ model, messages: [{ role: "user", content: prompt }] });

    /* EIP-191 per PIN — verbatim */
    const msg = T.buildEip191Message("POST", T.RELAY_CHAT_PATH, body, paymentId);
    termLine(term, `<span class="t-d">eip-191 msg</span> <span class="t-s">${T.esc(msg)}</span>`);

    let sig;
    try {
      termLine(term, `<span class="t-d">personal_sign — confirm in wallet…</span>`);
      /* signMessage signs the UTF-8 bytes of msg (EIP-191), ≡ eth_account.encode_defunct(text=msg) */
      sig = await state.signer.signMessage(msg);
    } catch (e) {
      return termLine(term, `<span class="t-a">✗</span> signing ${e.code === "ACTION_REJECTED" ? "rejected" : "failed"} — ${T.esc(e.shortMessage || e.message)}`);
    }
    termLine(term, `<span class="t-g">✓</span> <span class="t-d">sig</span> ${T.esc(sig.slice(0, 18))}…${T.esc(sig.slice(-8))}`);

    const url = T.joinUrl(l.endpoint, T.RELAY_CHAT_PATH);
    termLine(term, `<span class="t-d">POST</span> ${T.esc(url)}`);

    /* L3: fetchJson wraps the call with a 15s abort — a hung relay can no
       longer stall the demo indefinitely. Never throws; network/CORS/timeout
       come back classified. Response headers still read off r.response. */
    const r = await T.fetchJson(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Payment-Id": BigInt(paymentId).toString(10),
        "X-Signature": sig,
      },
      body,
    });

    if (r.corsOrNetwork) {
      return termLine(term,
        `<span class="t-a">✗</span> relay ${r.error === "timeout" ? "request timed out (15s) — relay not responding" : "unreachable (network or CORS — waiting on the relay's CORS config)"}`);
    }

    if (!r.ok) {
      const copy = T.relayErrorCopy(r.status);
      const detail = r.body ? (r.body.detail || JSON.stringify(r.body)) : "";
      termLine(term, `<span class="t-a">✗ HTTP ${r.status}</span> ${T.esc(copy || "")} ${T.esc(detail)}`);
      return;
    }

    const data = r.body;
    if (!data) return termLine(term, `<span class="t-a">✗</span> relay returned non-JSON`);

    const reply = data && data.choices && data.choices[0] && data.choices[0].message
      ? data.choices[0].message.content : "(no content)";
    const u = data.usage || {};
    const cached = (u.prompt_tokens_details && u.prompt_tokens_details.cached_tokens) ?? u.cached_tokens ?? 0;
    const settle = r.response.headers.get("X-Settle-Status");

    termLine(term, `<span class="t-g">✓ 200 OK</span>`);
    const replyEl = document.createElement("div");
    replyEl.className = "call-reply";
    replyEl.textContent = reply;
    term.appendChild(replyEl);
    /* C1 hardening: usage fields are ATTACKER-CONTROLLED (relay response) —
       a malicious relay may return strings; esc keeps numbers visually
       identical and defuses markup */
    termLine(
      term,
      `<span class="t-d">usage</span> cached <span class="t-n">${T.esc(cached)}</span> · in <span class="t-n">${T.esc(u.prompt_tokens ?? "?")}</span> · out <span class="t-n">${T.esc(u.completion_tokens ?? "?")}</span>` +
      (settle ? ` · <span class="t-d">settle</span> <span class="${settle === "settled" ? "t-g" : "t-a"}">${T.esc(settle)}</span>` : "")
    );

    /* EIP-712 receipt verification */
    const receiptHeader = r.response.headers.get("X-Receipt");
    rcptPanel.hidden = false;
    if (!receiptHeader) {
      rcptPanel.classList.add("warn");
      rcptPanel.innerHTML = `<b>NO RECEIPT</b> — ${settle === "settle-failed" ? "settle failed on-chain; response served, you may refund after ttl (warning only — no dispute recorded)" : "relay did not attach X-Receipt"}`;
      return;
    }

    let receipt, check;
    /* M15 shared mode: when the listing endpoint IS the configured shared
       relay (strict origin match — root path, https, no userinfo/query/
       fragment), receipts are signed by the pinned relay signer while
       message.seller stays the chain listing's operator. Every other
       seller keeps the single-seller rule; the pin never comes from
       /info (no TOFU) and never applies to arbitrary endpoints. */
    const sharedPin = shared && T.custody.matchesSharedRelay(l.endpoint, shared.relayOrigin)
      ? shared.expectedSigner : null;
    const expectDesc = sharedPin
      ? `shared relay signer ${T.esc(T.truncAddr(sharedPin))} (pinned in config)`
      : "listing.operator";
    try {
      receipt = T.decodeReceiptHeader(receiptHeader);
      check = T.verifyReceipt(receipt, l.operator, cfg.chainId, paymentId, { expectedSigner: sharedPin });
    } catch (e) {
      check = { ok: false, reason: "decode-failed: " + e.message };
    }

    if (check.ok) {
      const m = receipt.message;
      rcptPanel.classList.add("ok");
      rcptPanel.innerHTML =
        `<b>✓ receipt signature verified</b> <span class="mono dim">recovered ${T.esc(T.truncAddr(check.recovered))} == ${expectDesc} · seller field == listing.operator</span>` +
        `<div class="rcpt-grid">` +
        `<div><span>ACTUAL</span><b>$${T.fmtUsdc(m.actualAmount)}</b></div>` +
        `<div><span>UPSTREAM</span><b class="mono">${T.esc(m.upstreamHost)}</b></div>` +
        `<div><span>MODEL</span><b class="mono">${T.esc(m.model)}</b></div>` +
        `<div><span>TOKENS</span><b class="mono">${m.promptTokens}·${m.cachedTokens}·${m.completionTokens}</b></div>` +
        `</div>`;
    } else {
      rcptPanel.classList.add("bad");
      rcptPanel.innerHTML =
        `<b>✗ receipt verification failed — ${T.esc(check.reason)}</b>` +
        `<p class="dim mono" style="margin-top:6px">recorded in the disputes ledger (localStorage). recovered: ${T.esc(check.recovered || "—")} · expected signer: ${T.esc(sharedPin ? T.truncAddr(sharedPin) + " (shared relay, pinned)" : T.truncAddr(l.operator))} · seller field must equal: ${T.esc(T.truncAddr(l.operator))}</p>`;
      recordDispute(paymentId, check.reason, l.operator);
    }
  }));

  /* — refund — */
  $("r-btn").addEventListener("click", () => guard($("r-btn"), async () => {
    if (needWallet() || needConfig()) return;
    const txbox = $("r-tx");
    txbox.innerHTML = "";
    const pid = $("r-payment").value.trim();
    if (!/^\d+$/.test(pid)) return formErr(txbox, "paymentId must be a decimal integer");

    try {
      const p = await T.escrow(T.readProvider()).getPayment(pid);
      const st = T.PAYMENT_STATES[Number(p.state)] || "?";
      const info = T.txLine(txbox, `getPayment(${pid})`);
      info.confirmed(null);
      info.el.querySelector(".txl-state").innerHTML =
        `state <b>${st}</b> · max $${T.fmtUsdc(p.maxAmount)} · expires ${new Date(Number(p.expiresAt) * 1000).toLocaleTimeString()}`;
      if (st !== "Locked") return formErr(txbox, `payment is ${st} — only Locked can be refunded`);
    } catch (e) { return formErr(txbox, T.humanizeEscrowErr(e) || e.shortMessage || e.message); }

    const rcpt = await T.runTx(T.txLine(txbox, `refund(${pid})`), T.escrow(state.signer).refund(pid));
    if (rcpt) await refreshBalances();
  }));

  /* — disputes — */
  function renderDisputes() {
    const list = $("d-list");
    /* same rule as session locks: connected → only my records; not connected
       (or legacy records without a buyer) → show all as fallback */
    const all = T.disputes.all().filter((d) =>
      !state.address || T.sameAddr(d.buyer || "", state.address) || !d.buyer);
    $("d-clear").hidden = all.length === 0;
    if (!all.length) { list.innerHTML = `<p class="empty-hint">no disputes. Receipt-verification failures land here automatically.</p>`; return; }
    list.innerHTML = all.map((d) =>
      `<div class="disp"><span class="mono">#${T.esc(d.paymentId)}</span>` +
      `<span class="disp-reason">${T.esc(d.reason)}</span>` +
      `<span class="dim mono">${new Date(d.ts).toLocaleString()}</span></div>`
    ).join("");
  }
  $("d-clear").addEventListener("click", () => { T.disputes.clear(); renderDisputes(); });

  /* ── boot ────────────────────────────────────────────────── */
  T.installCopyHandlers();
  renderModelZone();
  renderSessionLocks();
  syncPolicyGate(); /* boot: consent bar + gated buttons mirror the stored record */
  renderApiKeys();
  renderDisputes();
  updatePreview();
  loadListingsIntoSelects();

  /* staggered card reveal on load */
  document.querySelectorAll(".panel .rv").forEach((el, i) => {
    el.style.setProperty("--d", `${i * 0.05}s`);
    requestAnimationFrame(() => el.classList.add("in"));
  });
})();
