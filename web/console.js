/* TOKENSHARE — console (Seller / Buyer).
   Every state change goes through the user-picked wallet (EIP-6963
   discovery + selector in common.js; window.ethereum is only the
   zero-6963 fallback). Private keys never touch the page. Reads fall
   back to the public RPC so the market stays visible without a wallet. */
"use strict";
(() => {
  const T = window.TS;
  const cfg = T.cfg;

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
  selectTab(location.hash === "#buyer" ? "buyer" : "seller", false);

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
    syncLockGate();
    renderWallet();
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
    try {
      const bal = await T.usdc(T.readProvider()).balanceOf(state.address);
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
    await Promise.all([refreshBalances(), loadMyListing(), loadListingsIntoSelects()]);
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
    accessible: [],   // relay-tested model face (upstream /v1/models)
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

  function renderModelNote() {
    const note = $("s-models-note");
    const endpoint = $("s-endpoint").value.trim();
    if (modelsState.status === "loading") {
      note.className = "model-verify-note";
      note.textContent = `GET ${modelsState.base}/verify-upstream …`;
    } else if (modelsState.status === "error") {
      note.className = "model-verify-note err";
      note.textContent = `✗ ${modelsState.reason} — fix and re-run the precheck`;
    } else if (modelsState.status === "ok") {
      if (endpoint !== modelsState.base) {
        note.className = "model-verify-note warn";
        note.textContent = `⚠ endpoint changed (prechecked ${T.hostOf(modelsState.base)}) — re-run before submitting`;
      } else {
        note.className = "model-verify-note ok";
        note.textContent = `✓ ${T.hostOf(modelsState.base)} can actually call ${modelsState.accessible.length} model(s) — pick from these only`;
      }
    } else {
      note.className = "model-verify-note";
      note.textContent = "not prechecked yet — load the real callable model face from the relay before submitting";
    }
  }

  function renderModelZone() {
    const zone = $("s-model-checks");
    $("s-load-models").disabled = modelsState.status === "loading";
    if (modelsState.status === "ok") {
      zone.innerHTML = modelsState.accessible.map((m) =>
        `<label class="mcheck"><input type="checkbox" name="s-model" value="${T.esc(m)}"${modelsState.checked.has(m) ? " checked" : ""}><span>${T.esc(m)}</span></label>`
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
    const endpoint = $("s-endpoint").value.trim();
    const { models, prices, bad } = collectPrices();
    const active = $("s-active").checked;
    const lines = [];
    const mine = state.myListing;
    const btn = $("s-submit");
    if (active && !modelsGateOk(endpoint)) {
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
     inline verify so chips & diagnostics reflect the new on-chain truth */
  const reverifyAfterTx = (endpoint) => { runVerify(endpoint).catch(() => {}); };

  /* multi-tx update flow: models after a failure are not attempted —
     list them as skipped so the partial state is explicit */
  function markSkipped(txbox, models) {
    for (const m of models) {
      const line = T.txLine(txbox, `updateModelPrice(${m})`);
      line.el.classList.add("is-skip");
      line.el.querySelector(".txl-state").textContent = "skipped — fix the failure above and resubmit";
    }
  }

  $("s-submit").addEventListener("click", () => guard($("s-submit"), async () => {
    if (needWallet() || needConfig()) return;
    const txbox = $("s-tx");
    txbox.innerHTML = "";
    const endpoint = $("s-endpoint").value.trim();
    const { models, prices, bad } = collectPrices();
    const active = $("s-active").checked;

    if (active) {
      if (!endpoint) return formErr(txbox, "endpoint required (https://…:8787)");
      if (!modelsGateOk(endpoint)) return formErr(txbox, gateReason());
      if (!models.length) return formErr(txbox, "check at least one relay-verified model");
      if (bad.length) return formErr(txbox, `${bad.join(", ")} — ${PRICE_ERR} · prices are USDC per 1M tokens`);
    }

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
        const pf = await preflight(() => reg.register.staticCall(endpoint, models, priceArr));
        if (pf) { formErr(txbox, pf.text); return; }
        const rcpt2 = await T.runTx(T.txLine(txbox, "register(…) — step 2/2"), reg.register(endpoint, models, priceArr));
        if (rcpt2) { await loadMyListing(); reverifyAfterTx(endpoint); }
        return;
      }
      if (active) {
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
    const sf = T.lockShortfall(readPrice("b-max"), bal);
    note.hidden = !sf;
    note.textContent = sf ? sf.text : "";
    $("b-lock-btn").disabled = Boolean(sf);
  }
  $("b-max").addEventListener("input", syncLockGate);

  /* — deposit (approve → deposit) — */
  $("b-dep-btn").addEventListener("click", () => guard($("b-dep-btn"), async () => {
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
                `<button class="btn btn-sm btn-ghost" type="button" data-mint="${pid}" data-seller="${T.esc(String(l.seller || ""))}">[ MINT API KEY ]</button>` +
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
    const close = () => {
      document.removeEventListener("keydown", onKey, true);
      document.body.style.overflow = prevOverflow;
      overlay.remove();
    };
    const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
    document.addEventListener("keydown", onKey, true);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
    overlay.querySelector(".wsel-cancel").addEventListener("click", close);
    document.body.appendChild(overlay);

    const body = overlay.querySelector(".wsel-body");
    const l = listingOf(seller);
    (async () => {
      let p;
      try { p = await T.escrow(T.readProvider()).getPayment(paymentId); }
      catch (e) {
        body.innerHTML = `<p class="empty-hint err">read failed — ${T.esc(T.humanizeEscrowErr(e) || e.shortMessage || e.message)}</p>`;
        return;
      }
      const st = T.PAYMENT_STATES[Number(p.state)] || "?";
      const expiry = Number(p.expiresAt);
      const expiryStr = new Date(expiry * 1000).toLocaleString();
      const expired = expiry * 1000 <= Date.now();
      const mintable = st === "Locked" && !expired;
      const msg = T.buildMintMessage(paymentId, expiry, p.maxAmount);
      body.innerHTML =
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
      if (go) go.addEventListener("click", () => guard(go, async () => {
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
        renderMintResult(body, { paymentId, key, endpoint: l ? l.endpoint : null, model: l && l.models[0], expiryStr, maxAmount: p.maxAmount });
      }));
    })();
  }

  function renderMintResult(body, { paymentId, key, endpoint, model, expiryStr, maxAmount }) {
    const curl = endpoint
      ? `curl ${endpoint}${T.RELAY_CHAT_PATH} \\\n  -H "Authorization: Bearer ${key}" \\\n  -H "Content-Type: application/json" \\\n  -d '{"model":"${model || "MODEL"}","messages":[{"role":"user","content":"hi"}]}'`
      : "";
    body.innerHTML =
      `<div class="kv"><span>STATUS</span><b class="ok">✓ key minted — payment #${T.esc(paymentId)}</b></div>` +
      `<div class="fld"><span class="fld-lbl">API KEY <i>click to copy — shown once, not stored anywhere</i></span>` +
        `<div class="mint-key mono wrap-anywhere" data-copy="${T.esc(key)}" title="click to copy">${T.esc(key)}</div></div>` +
      (endpoint
        ? `<div class="kv"><span>BASE URL</span><b class="mono wrap-anywhere" data-copy="${T.esc(endpoint)}" title="click to copy">${T.esc(endpoint)}</b></div>` +
          `<div class="fld"><span class="fld-lbl">CURL <i>OpenAI-compatible</i></span><pre class="preview mono wrap-anywhere">${T.esc(curl)}</pre></div>`
        : "") +
      `<p class="mint-warn">⚠ stateless &amp; <b>non-revocable</b> — a leak can spend up to <b>$${T.fmtUsdc(maxAmount)}</b> (this lock's max). ` +
      `The key dies with the lock TTL (${T.esc(expiryStr)}); relay rejects it afterwards. Mint a fresh key per lock.</p>`;
  }

  /* — M13 USAGE: relay's accrued-usage view, inline under the lock row — */
  async function fetchUsage(paymentId, seller) {
    const panel = document.querySelector(`[data-usage-panel="${paymentId}"]`);
    if (!panel) return;
    const refreshBtn = `<button class="btn btn-sm btn-ghost" type="button" data-usage-refresh="${T.esc(paymentId)}" data-seller="${T.esc(seller)}">[ REFRESH ]</button>`;
    panel.hidden = false;
    panel.innerHTML = `<span class="dim mono">GET /payment/${T.esc(paymentId)}/usage …</span>`;
    const l = listingOf(seller);
    if (!l) {
      panel.innerHTML = `<span class="dim">seller not in the current listings — endpoint unknown (RPC hiccup? refresh the page)</span>`;
      return;
    }
    const r = await T.fetchJson(T.joinUrl(l.endpoint, `/payment/${paymentId}/usage`), {}, 15000);
    if (r.corsOrNetwork) {
      panel.innerHTML = `<span class="bad mono">relay unreachable (${r.error === "timeout" ? "timeout after 15s" : "network/CORS"})</span> ` + refreshBtn;
      return;
    }
    if (!r.ok || !r.body) {
      panel.innerHTML = `<span class="bad mono">HTTP ${r.status} — usage unavailable</span> ` + refreshBtn;
      return;
    }
    /* native 6dp ints; relay accrued total may lead the on-chain counter
       while a background settlePartial flush is in flight */
    const cap = BigInt(Math.trunc(Number(r.body.captured)));
    const max = BigInt(Math.trunc(Number(r.body.maxAmount)));
    const rem = BigInt(Math.trunc(Number(r.body.remaining)));
    const pct = max > 0n ? Number((cap * 10000n) / max) / 100 : 0;
    panel.innerHTML =
      `<div class="usage-line mono">captured <b>$${T.fmtUsdc(cap)}</b> · remaining <b>$${T.fmtUsdc(rem)}</b> · max <b>$${T.fmtUsdc(max)}</b></div>` +
      `<div class="usage-bar" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"><i style="width:${pct}%"></i></div>` +
      `<div class="usage-foot"><span class="dim">${new Date().toLocaleTimeString()} · relay-accrued view (may lead on-chain captured during flush)</span>${refreshBtn}</div>`;
  }

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
    Promise.resolve(guard($("b-lock-btn"), async () => {
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

  $("c-send").addEventListener("click", () => guard($("c-send"), async () => {
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
    try {
      receipt = T.decodeReceiptHeader(receiptHeader);
      check = T.verifyReceipt(receipt, l.operator, cfg.chainId, paymentId);
    } catch (e) {
      check = { ok: false, reason: "decode-failed: " + e.message };
    }

    if (check.ok) {
      const m = receipt.message;
      rcptPanel.classList.add("ok");
      rcptPanel.innerHTML =
        `<b>✓ receipt signature verified</b> <span class="mono dim">recovered ${T.esc(T.truncAddr(check.recovered))} == listing.operator</span>` +
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
        `<p class="dim mono" style="margin-top:6px">recorded in the disputes ledger (localStorage). recovered: ${T.esc(check.recovered || "—")} · expected: ${T.esc(T.truncAddr(l.operator))}</p>`;
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
  renderDisputes();
  updatePreview();
  loadListingsIntoSelects();

  /* staggered card reveal on load */
  document.querySelectorAll(".panel .rv").forEach((el, i) => {
    el.style.setProperty("--d", `${i * 0.05}s`);
    requestAnimationFrame(() => el.classList.add("in"));
  });
})();
