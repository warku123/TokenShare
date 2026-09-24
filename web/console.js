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
  };

  /* ═══ config gate ═════════════════════════════════════════ */
  const configBanner = $("config-banner");
  if (!T.cfgReady()) {
    configBanner.hidden = false;
    configBanner.innerHTML =
      `<b>config.js 待填</b> — 部署后从 <code class="inl">contracts/deployed.json</code> 填入 ` +
      `<code class="inl">escrowAddr / registryAddr</code>（及 <code class="inl">sellers</code>）。` +
      `当前仅展示页面骨架，链读写不可用。`;
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

  function renderWallet() {
    if (!state.address) {
      connectBtn.hidden = false;
      walletInfo.hidden = true;
      return;
    }
    connectBtn.hidden = true;
    walletInfo.hidden = false;
    walletAddr.textContent = T.truncAddr(state.address);
    walletAddr.href = T.addrLink(state.address);
    walletAddr.dataset.copy = state.address;
    walletAddr.title = `${state.address}${state.walletName ? ` · via ${state.walletName}` : ""} — click copies, ⧉ explorer via market page`;
    const nameEl = $("wallet-name");
    if (nameEl) nameEl.textContent = (state.walletName || "").toUpperCase() || "WALLET";
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
      $("b-escrow-bal").textContent = `$${T.fmtUsdc(eb)}`;
      $("b-escrow-bal").title = `${T.fmtInt(eb)} native units`;
    } catch { $("b-escrow-bal").textContent = "—"; }
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
      state.signer = null;
      state.address = null;
      state.walletName = "";
      renderWallet();
      /* non-empty = account switch inside the same wallet → silent re-sync,
         no picker, no popup; empty = disconnected (layer already forgot rdns) */
      if (accs && accs.length) finishConnect(false).catch((e) => console.error("account switch failed:", e));
    },
    chain: () => window.location.reload(),
  });
  /* silent resume: remembered rdns → bare eth_accounts — never pops */
  T.wallet.resume()
    .then((r) => { if (r) return finishConnect(false); })
    .catch(() => {});
  renderWallet();

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
    const opts = state.listings.map((l) =>
      `<option value="${T.esc(l.operator)}" ${l.active ? "" : "disabled"}>` +
      `${T.truncAddr(l.operator)} · ${T.esc(T.hostOf(l.endpoint))}${l.active ? "" : " (inactive)"}</option>`
    ).join("");
    for (const id of ["b-seller", "c-seller"]) {
      const sel = $(id);
      const prev = sel.value;
      sel.innerHTML = `<option value="">— choose seller —</option>` + opts;
      sel.value = prev;
    }
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
      box.innerHTML = `<p class="empty-hint">连接钱包后展示你的 Registry listing。</p>`;
      return;
    }
    box.innerHTML = `<p class="empty-hint">reading getListing(${T.esc(T.truncAddr(state.address))}) …</p>`;
    let l;
    try { l = await T.fetchListing(T.readProvider(), state.address); }
    catch (e) { box.innerHTML = `<p class="empty-hint err">read failed — ${T.esc(e.shortMessage || e.message)}</p>`; return; }
    state.myListing = l.registered ? l : null;

    if (!l.registered) {
      box.innerHTML =
        `<p class="empty-hint">尚未登记。先预检加载 relay 真实模型面，再完成 <code class="inl">Registry.register</code>，` +
        `listing 即刻出现在市场页。</p>`;
    } else {
      /* v2: one price triple per model — group rows by model */
      const priceRows = l.models.map((m) => {
        const p = T.priceFor(l, m);
        return `<div class="mpr"><span class="mtag">${T.esc(m)}</span>` +
          (p
            ? `<span class="mono">cached $${T.fmtUsdc(p.cachedIn)} · in $${T.fmtUsdc(p.input)} · out $${T.fmtUsdc(p.output)}</span>`
            : `<span class="dim">no price on-chain</span>`) +
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
          `<p class="empty-hint deact-note">已停用 — 市场页转 INACTIVE 灰态、买家不可再锁单；在途（Locked）payment 仍可正常 settle。` +
          `右侧表单勾选 LISTING ACTIVE 并提交 <code class="inl">register</code> 即可恢复。</p>`);
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
    if (modelsState.status === "loading") return "模型预检进行中 — 等结果出来再提交";
    if (modelsState.status === "error") return `模型预检失败（${modelsState.reason}）— 修复后点 LOAD FROM RELAY 重试`;
    if (modelsState.status === "ok") return `endpoint 与预检来源（${modelsState.base}）不一致 — 对当前 endpoint 重新预检`;
    return "先点 LOAD FROM RELAY 预检 — 只有 relay 实测可调的模型才能登记，杜绝虚空模型";
  }

  function renderModelNote() {
    const note = $("s-models-note");
    const endpoint = $("s-endpoint").value.trim();
    if (modelsState.status === "loading") {
      note.className = "model-verify-note";
      note.textContent = `GET ${modelsState.base}/verify-upstream …`;
    } else if (modelsState.status === "error") {
      note.className = "model-verify-note err";
      note.textContent = `✗ ${modelsState.reason} — 修复后重新预检`;
    } else if (modelsState.status === "ok") {
      if (endpoint !== modelsState.base) {
        note.className = "model-verify-note warn";
        note.textContent = `⚠ endpoint 已改（预检来源 ${T.hostOf(modelsState.base)}）— 提交前需重新预检`;
      } else {
        note.className = "model-verify-note ok";
        note.textContent = `✓ ${T.hostOf(modelsState.base)} 实测可调 ${modelsState.accessible.length} 个模型 — 只能从中勾选`;
      }
    } else {
      note.className = "model-verify-note";
      note.textContent = "未预检 — 提交前必须先从 relay 拉取真实可调模型面";
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
       otherwise: 默认全选
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

  /* shared precheck: one fetch drives BOTH the precheck card and the
     register-form chips (联动) */
  async function runVerify(base, origin) {
    const out = $("p-result");
    base = (base || "").trim().replace(/\/+$/, "");
    if (!base) {
      out.innerHTML = `<p class="empty-hint err">enter the relay base URL first</p>`;
      if (origin === "form") failVerify("endpoint 为空 — 先填 RELAY ENDPOINT");
      return;
    }
    if (origin === "form") { $("s-endpoint").value = base; $("p-base").value = base; }
    const wasOk = modelsState.status === "ok"; /* before the loading flip */
    modelsState.status = "loading";
    modelsState.base = base;
    renderModelZone();
    out.innerHTML = `<p class="empty-hint">GET ${T.esc(T.joinUrl(base, "/verify-upstream"))} …</p>`;
    const r = await T.fetchJson(T.joinUrl(base, "/verify-upstream"), {}, 20000);
    if (r.corsOrNetwork) {
      out.innerHTML =
        `<p class="empty-hint err">relay 不可达（${r.error === "timeout" ? "超时" : "网络或 CORS 未开启"}）。` +
        `<button class="btn btn-sm" id="p-retry" type="button">[ RETRY ]</button></p>`;
      $("p-retry").addEventListener("click", () => runVerify($("p-base").value, "card"));
      failVerify("relay 不可达（网络/CORS）");
      return;
    }
    if (!r.ok || !r.body) {
      out.innerHTML = `<p class="empty-hint err">HTTP ${r.status} — verify-upstream unavailable</p>`;
      failVerify(`verify-upstream HTTP ${r.status}`);
      return;
    }
    const b = r.body;
    const accessible = Array.isArray(b.accessible_models) ? b.accessible_models : [];
    /* relay shape: mismatches = [{model, reason}] (objects, not strings) */
    const mismatches = (b.mismatches || []).map((m) =>
      (m && typeof m === "object") ? `${m.model} — ${m.reason}` : String(m));

    if (!b.key_valid) {
      failVerify("上游 key 无效（key_valid=false）— relay 拿不到真实模型面");
      renderPrecheckCard(b, accessible, mismatches);
      return;
    }
    if (!accessible.length) {
      failVerify("relay 返回空模型面（accessible_models 为空）");
      renderPrecheckCard(b, accessible, mismatches);
      return;
    }
    applyVerified(base, accessible, wasOk);
    renderPrecheckCard(b, accessible, mismatches);
  }

  function renderPrecheckCard(b, accessible, mismatches) {
    const want = new Set(selectedModels());
    const diffExtra = [...want].filter((m) => !accessible.includes(m));
    $("p-result").innerHTML =
      `<div class="kv"><span>KEY</span><b class="${b.key_valid ? "ok" : "bad"}">${b.key_valid ? "✓ valid" : "✗ INVALID"}</b></div>` +
      `<div class="kv"><span>UPSTREAM</span><b class="mono">${T.esc(b.upstream_host || "—")}</b></div>` +
      (b.error ? `<div class="kv"><span>ERROR</span><b class="bad mono wrap-anywhere">${T.esc(b.error)}</b></div>` : "") +
      `<div class="kv"><span>ACCESSIBLE</span><b>${accessible.length ? accessible.map((m) => `<span class="mtag">${T.esc(m)}</span>`).join(" ") : "—"}</b></div>` +
      `<div class="kv"><span>LISTING ON-CHAIN</span><b class="${b.listing_ok ? "ok" : "dim"}">${b.listing_ok ? "✓ consistent" : "✗ mismatch / not registered"}</b></div>` +
      (b.listed_models && b.listed_models.length ? `<div class="kv"><span>LISTED</span><b>${b.listed_models.map((m) => `<span class="mtag">${T.esc(m)}</span>`).join(" ")}</b></div>` : "") +
      (mismatches.length ? `<div class="kv"><span>MISMATCHES</span><b class="bad">${mismatches.map((m) => `<span class="mtag">${T.esc(m)}</span>`).join(" ")}</b></div>` : "") +
      (diffExtra.length ? `<div class="kv"><span>FORM vs KEY</span><b class="bad">selected but not accessible: ${diffExtra.map((m) => `<span class="mtag">${T.esc(m)}</span>`).join(" ")}</b></div>` : "");
  }

  $("s-load-models").addEventListener("click", () => guard($("s-load-models"), () => runVerify($("s-endpoint").value, "form")));
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

  /* human-readable copy for the Registry v2 custom errors (frozen contract) */
  const REGISTRY_ERR_COPY = {
    AlreadyRegistered: "链上已存在 active listing，register 必 revert。唯一改形路径：deactivate → 重新 register（两笔交易）",
    NotActive: "listing 未激活（NotActive）— 改价需 active listing；改模型集/端点走 deactivate → register",
    ModelNotFound: "模型不在链上 listing 中（ModelNotFound）— 增删模型需 deactivate → 重新 register",
    EmptyModels: "models 为空（EmptyModels）— 至少勾选一个",
    LengthMismatch: "models 与 prices 长度不一致（LengthMismatch）— 页面组装错误，请反馈",
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
        { kind: "revert", text: `链上预演 revert — ${T.esc(e.shortMessage || e.reason || e.message || "unknown")}` };
    }
  }
  function offerTwoStepFix(txbox) {
    const btn = document.createElement("button");
    btn.className = "btn";
    btn.type = "button";
    btn.textContent = "[ 一键修复：DEACTIVATE → REGISTER · 依次签两笔 ]";
    btn.addEventListener("click", () => $("s-submit").click());
    txbox.appendChild(btn);
  }
  /* after a successful register/updateModelPrice: refresh listing + re-run the
     verify so chips & precheck card reflect the new on-chain truth */
  const reverifyAfterTx = (endpoint) => { runVerify(endpoint, "form").catch(() => {}); };

  /* multi-tx update flow: models after a failure are not attempted —
     list them as skipped so the partial state is explicit */
  function markSkipped(txbox, models) {
    for (const m of models) {
      const line = T.txLine(txbox, `updateModelPrice(${m})`);
      line.el.classList.add("is-skip");
      line.el.querySelector(".txl-state").textContent = "skipped — 修复上方失败后重新提交";
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
      if (!models.length) return formErr(txbox, "至少勾选一个实测可调的模型");
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
          if (!changed.length) return formErr(txbox, "所有模型价格与链上一致 — 无需发送");
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

  /* — deactivate: confirm dialog (self-drawn on the wsel chrome —
       createElement + textContent only, zero innerHTML, zero external
       links; same injection discipline as the M11 wallet selector) — */
  function confirmDeactivate() {
    return new Promise((resolve) => {
      const overlay = document.createElement("div");
      overlay.className = "wsel-overlay";
      const box = document.createElement("div");
      box.className = "wsel cnf";
      box.setAttribute("role", "alertdialog");
      box.setAttribute("aria-modal", "true");
      box.setAttribute("aria-label", "confirm deactivate");

      const bar = document.createElement("div");
      bar.className = "wsel-bar";
      for (let i = 0; i < 3; i++) { const d = document.createElement("span"); d.className = "tdot"; bar.appendChild(d); }
      const title = document.createElement("span");
      title.className = "wsel-title";
      title.textContent = "confirm — registry.deactivate()";
      bar.appendChild(title);
      box.appendChild(bar);

      const body = document.createElement("div");
      body.className = "cnf-body";
      const copy = document.createElement("p");
      copy.className = "cnf-copy";
      copy.textContent =
        "下线后：市场页 ACTIVE 展示即刻撤下（卡转 INACTIVE 灰态，买家不可再锁单）；" +
        "已在途（Locked）的 payment 仍可正常 settle；随时可在右侧表单重新 register 恢复 ACTIVE。";
      const det = document.createElement("p");
      det.className = "cnf-det";
      det.textContent = "deactivate() → active=false · 枚举条目保留（append-only，链上可审计）· 1 tx · 钱包签名";
      body.append(copy, det);
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
      go.textContent = "[ SIGN DEACTIVATE ]";
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

  /* [ DEACTIVATE ] — one-tap offboarding from the MY LISTING card.
     Signer comes from finishConnect (the M11 wallet layer) — never
     window.ethereum directly. */
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

  /* — upstream precheck card — shares runVerify with the register form:
       a successful run feeds accessible_models into the form chips (联动) */
  $("p-run").addEventListener("click", () => guard($("p-run"), () => runVerify($("p-base").value, "card")));

  /* ═══ BUYER tab ═══════════════════════════════════════════ */

  /* — seller pickers info — */
  function syncSellerInfo() {
    const l = listingOf($("b-seller").value);
    const info = $("b-lock-info");
    if (!l) { info.innerHTML = `<p class="empty-hint">选择卖家后按模型显示三档价与 minAmount 估计。</p>`; return; }
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
      `≥ 最贵模型估计 $${T.fmtUsdc(maxMin)}（in×200k + out×32k caps，按调用模型取对应档）`;
    /* mirror into call tab */
    const csel = $("c-seller");
    if (!csel.value) { csel.value = l.operator; syncCallModels(); }
  }
  $("b-seller").addEventListener("change", syncSellerInfo);

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
    } catch (e) { formErr(txbox, e.shortMessage || e.message); }
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
    for (const l of mine) {
      try {
        const pid = T.esc(String(l.paymentId ?? "?"));
        const seller = T.esc(T.truncAddr(String(l.seller || "?")));
        let max = null;
        try { max = T.fmtUsdc(l.maxAmount); } catch { max = null; } /* formatUnits throws on garbage */
        opts.push(`<option value="${pid}">#${pid} · ${seller}${max != null ? ` · max $${max}` : " · max ?"}</option>`);
      } catch { /* unexpected shape — skip the row entirely */ }
    }
    const html = opts.join("");
    list.innerHTML = html;
    rlist.innerHTML = html;
  }

  $("b-lock-btn").addEventListener("click", () => guard($("b-lock-btn"), async () => {
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
      T.locks.add({
        paymentId: pid.toString(), buyer: state.address, seller,
        maxAmount: max.toString(), ttl, txHash: rcpt.hash, ts: Date.now(),
      });
      renderSessionLocks();
      $("c-payment").value = pid.toString();
      if (!$("c-seller").value) { $("c-seller").value = seller; syncCallModels(); }
    } else {
      $("b-payment-id").innerHTML = `<span class="dim mono">Locked — paymentId in the Locked event (see tx)</span>`;
    }
    await refreshBalances();
  }));

  /* — call demo — */
  function syncCallModels() {
    const l = listingOf($("c-seller").value);
    const sel = $("c-model");
    if (!l) { sel.innerHTML = `<option value="">— seller first —</option>`; return; }
    sel.innerHTML = l.models.map((m) => `<option value="${T.esc(m)}">${T.esc(m)}</option>`).join("");
  }
  $("c-seller").addEventListener("change", syncCallModels);

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
        `<span class="t-a">✗</span> relay ${r.error === "timeout" ? "请求超时（15s）— relay 无响应" : "不可达（网络或 CORS — 等待 relay CORS 配置）"}`);
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
      rcptPanel.innerHTML = `<b>NO RECEIPT</b> — ${settle === "settle-failed" ? "settle failed on-chain; response served, you may refund after ttl (只警告，不记争议)" : "relay did not attach X-Receipt"}`;
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
        `<b>✓ 收据验签通过</b> <span class="mono dim">recovered ${T.esc(T.truncAddr(check.recovered))} == listing.operator</span>` +
        `<div class="rcpt-grid">` +
        `<div><span>ACTUAL</span><b>$${T.fmtUsdc(m.actualAmount)}</b></div>` +
        `<div><span>UPSTREAM</span><b class="mono">${T.esc(m.upstreamHost)}</b></div>` +
        `<div><span>MODEL</span><b class="mono">${T.esc(m.model)}</b></div>` +
        `<div><span>TOKENS</span><b class="mono">${m.promptTokens}·${m.cachedTokens}·${m.completionTokens}</b></div>` +
        `</div>`;
    } else {
      rcptPanel.classList.add("bad");
      rcptPanel.innerHTML =
        `<b>✗ 收据验签失败 — ${T.esc(check.reason)}</b>` +
        `<p class="dim mono" style="margin-top:6px">已记入争议列表（localStorage）。recovered: ${T.esc(check.recovered || "—")} · expected: ${T.esc(T.truncAddr(l.operator))}</p>`;
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
    } catch (e) { return formErr(txbox, e.shortMessage || e.message); }

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
    if (!all.length) { list.innerHTML = `<p class="empty-hint">无争议记录。收据验签失败会自动记入此处。</p>`; return; }
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
