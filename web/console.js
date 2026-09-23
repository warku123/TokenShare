/* TOKENSHARE — console (Seller / Buyer).
   Every state change goes through window.ethereum (MetaMask);
   private keys never touch the page. Reads fall back to the
   public RPC so the market stays visible without a wallet. */
"use strict";
(() => {
  const T = window.TS;
  const cfg = T.cfg;

  document.documentElement.classList.add("js");

  const $ = (id) => document.getElementById(id);
  const state = {
    provider: null,   // BrowserProvider
    signer: null,
    address: null,
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
    walletAddr.title = `${state.address} — click copies, ⧉ explorer via market page`;
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
      await connect(false);
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
      await window.ethereum.request({ method: "wallet_switchEthereumChain", params: [{ chainId: hex }] });
      return true;
    } catch (e) {
      if (e && e.code === 4902) {
        await window.ethereum.request({
          method: "wallet_addEthereumChain",
          params: [{
            chainId: hex,
            chainName: cfg.chainName,
            nativeCurrency: cfg.nativeCurrency,
            rpcUrls: [cfg.rpcUrl],
            blockExplorerUrls: [cfg.explorer],
          }],
        });
        return true;
      }
      throw e;
    }
  }

  async function connect(autoSwitch = true) {
    if (!window.ethereum) {
      connectBtn.textContent = "[ NO WALLET — INSTALL METAMASK ]";
      connectBtn.disabled = true;
      return;
    }
    try {
      state.provider = new ethers.BrowserProvider(window.ethereum);
      await state.provider.send("eth_requestAccounts", []);
      const net = await state.provider.getNetwork();
      if (Number(net.chainId) !== Number(cfg.chainId)) {
        renderNet(false);
        if (!autoSwitch) { renderWallet(); return; }
        await ensureChain();
      }
      /* re-create after a possible switch */
      state.provider = new ethers.BrowserProvider(window.ethereum);
      state.signer = await state.provider.getSigner();
      state.address = await state.signer.getAddress();
      renderNet(true);
      renderWallet();
      await Promise.all([refreshBalances(), loadMyListing(), loadListingsIntoSelects()]);
    } catch (e) {
      renderNet(false);
      connectBtn.textContent = "[ CONNECT WALLET ]";
      const rejected = e && (e.code === 4001 || e.code === "ACTION_REJECTED");
      if (!rejected) console.error("wallet connect failed:", e);
    }
  }

  connectBtn.addEventListener("click", () => connect(true));
  if (window.ethereum) {
    window.ethereum.on("accountsChanged", () => { state.signer = null; state.address = null; renderWallet(); connect(false); });
    window.ethereum.on("chainChanged", () => window.location.reload());
    /* silent resume: previously-authorized wallet reconnects without a popup */
    window.ethereum.request({ method: "eth_accounts" })
      .then((accs) => { if (accs && accs.length) connect(false); })
      .catch(() => {});
  }
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
    if (!T.cfgReady() || !(cfg.sellers || []).length) return;
    try {
      state.listings = (await T.fetchListings(T.readProvider())).filter((l) => l.registered);
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
  async function loadMyListing() {
    const box = $("s-my-listing");
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
        `<p class="empty-hint">尚未登记。填写下方表单完成 <code class="inl">Registry.register</code>，` +
        `listing 即刻出现在市场页。</p>`;
    } else {
      box.innerHTML =
        `<div class="kv"><span>STATUS</span><b class="${l.active ? "ok" : "dim"}">${l.active ? "ACTIVE" : "INACTIVE"}</b></div>` +
        `<div class="kv"><span>ENDPOINT</span><b class="mono wrap-anywhere">${T.esc(l.endpoint)}</b></div>` +
        `<div class="kv"><span>MODELS</span><b>${l.models.map((m) => `<span class="mtag">${T.esc(m)}</span>`).join(" ") || "—"}</b></div>` +
        `<div class="kv"><span>PRICES /1M</span><b class="mono">cached $${T.fmtUsdc(l.priceCachedIn)} · in $${T.fmtUsdc(l.priceInput)} · out $${T.fmtUsdc(l.priceOutput)}</b></div>`;
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
      /* prefill form */
      $("s-endpoint").value = l.endpoint;
      setTimeout(() => {
        for (const m of l.models) addModelChip(m);
        syncModelChecks();
      }, 0);
      $("s-price-cached").value = T.fmtUsdc(l.priceCachedIn);
      $("s-price-input").value = T.fmtUsdc(l.priceInput);
      $("s-price-output").value = T.fmtUsdc(l.priceOutput);
      $("s-active").checked = l.active;
    }
    updatePreview();
  }

  /* — models multi-select (presets + custom) — */
  const PRESET_MODELS = ["kimi-for-coding", "kimi-for-coding-highspeed", "k3", "k3-256k"];
  const chosenCustom = new Set();

  function selectedModels() {
    const checked = [...document.querySelectorAll("input[name=s-model]:checked")].map((i) => i.value);
    return [...checked, ...chosenCustom];
  }

  function renderCustomChips() {
    $("s-custom-list").innerHTML = [...chosenCustom].map((m) =>
      `<span class="mtag chip-x" data-model="${T.esc(m)}">${T.esc(m)} <button type="button" aria-label="remove">×</button></span>`
    ).join("") || `<span class="dim mono" style="font-size:11px">no custom models</span>`;
    updatePreview();
  }
  $("s-custom-list").addEventListener("click", (e) => {
    const chip = e.target.closest(".chip-x");
    if (chip) { chosenCustom.delete(chip.dataset.model); renderCustomChips(); }
  });
  function addModelChip(m) {
    m = (m || "").trim();
    if (!m) return;
    if (PRESET_MODELS.includes(m)) {
      const box = document.querySelector(`input[name=s-model][value="${m}"]`);
      if (box) box.checked = true;
    } else if (!chosenCustom.has(m)) {
      chosenCustom.add(m); renderCustomChips();
    }
  }
  $("s-add-model").addEventListener("click", () => {
    addModelChip($("s-custom-model").value);
    $("s-custom-model").value = "";
  });
  function syncModelChecks() {
    /* checks already applied by addModelChip */ updatePreview();
  }
  document.querySelectorAll("input[name=s-model]").forEach((i) => i.addEventListener("change", updatePreview));

  /* — register / update form — */
  function readPrice(id) {
    const v = $(id).value.trim();
    if (!v || isNaN(Number(v)) || Number(v) < 0) return null;
    try { return T.toNative(v); } catch { return null; } /* >6dp decimals etc. */
  }
  const PRICE_ERR = "amounts must be non-negative numbers, ≤6 decimals (USDC)";

  function updatePreview() {
    const endpoint = $("s-endpoint").value.trim();
    const models = selectedModels();
    const pc = readPrice("s-price-cached"), pi = readPrice("s-price-input"), po = readPrice("s-price-output");
    const active = $("s-active").checked;
    const lines = [];
    const mine = state.myListing;
    if (mine && mine.active && !active) {
      lines.push(`<span class="t-a">deactivate()</span> <span class="t-d">// listing stays auditable, stops serving</span>`);
    } else if (mine && mine.active) {
      const sameShape = mine.endpoint === endpoint && JSON.stringify(mine.models) === JSON.stringify(models);
      if (sameShape) {
        lines.push(`<span class="t-a">updatePrice(${pc ?? "?"}, ${pi ?? "?"}, ${po ?? "?"})</span> <span class="t-d">// prices only</span>`);
      } else {
        lines.push(`<span class="t-a">deactivate()</span> <span class="t-d">// step 1 — endpoint/models changed</span>`);
        lines.push(`<span class="t-g">register(endpoint, models, prices)</span> <span class="t-d">// step 2 — re-register</span>`);
      }
    } else if (active) {
      lines.push(`<span class="t-g">register(</span>`);
      lines.push(`  endpoint: <span class="t-s">"${T.esc(endpoint) || "…"}"</span>,`);
      lines.push(`  models: <span class="t-s">[${models.map((m) => `"${T.esc(m)}"`).join(", ") || "…"}]</span>,`);
      lines.push(`  prices: <span class="t-n">${pc ?? "?"}, ${pi ?? "?"}, ${po ?? "?"}</span> <span class="t-d">// native 6dp, per 1M tokens</span>`);
      lines.push(`<span class="t-g">)</span>`);
    } else {
      lines.push(`<span class="t-d">// nothing to do — listing inactive and toggle off</span>`);
    }
    $("s-preview").innerHTML = lines.join("\n");
  }
  ["s-endpoint", "s-price-cached", "s-price-input", "s-price-output"].forEach((id) =>
    $(id).addEventListener("input", updatePreview));
  $("s-active").addEventListener("change", updatePreview);

  $("s-submit").addEventListener("click", () => guard($("s-submit"), async () => {
    if (needWallet() || needConfig()) return;
    const txbox = $("s-tx");
    txbox.innerHTML = "";
    const endpoint = $("s-endpoint").value.trim();
    const models = selectedModels();
    const pc = readPrice("s-price-cached"), pi = readPrice("s-price-input"), po = readPrice("s-price-output");
    const active = $("s-active").checked;

    if (active) {
      if (!endpoint) return formErr(txbox, "endpoint required (https://…:8787)");
      if (!models.length) return formErr(txbox, "select at least one model");
      if (pc === null || pi === null || po === null) return formErr(txbox, PRICE_ERR + " — prices are USDC per 1M tokens");
    }

    const reg = T.registry(state.signer);
    const mine = state.myListing;
    try {
      if (mine && mine.active && !active) {
        const rcpt = await T.runTx(T.txLine(txbox, "deactivate()"), reg.deactivate());
        if (rcpt) { state.myListing.active = false; await loadMyListing(); }
        return;
      }
      if (mine && mine.active) {
        const sameShape = mine.endpoint === endpoint && JSON.stringify(mine.models) === JSON.stringify(models);
        if (sameShape) {
          const rcpt = await T.runTx(T.txLine(txbox, "updatePrice(…)"), reg.updatePrice(pc, pi, po));
          if (rcpt) await loadMyListing();
          return;
        }
        /* endpoint/models changed → deactivate then re-register */
        const rcpt1 = await T.runTx(T.txLine(txbox, "deactivate() — step 1/2"), reg.deactivate());
        if (!rcpt1) return;
        const rcpt2 = await T.runTx(T.txLine(txbox, "register(…) — step 2/2"), reg.register(endpoint, models, pc, pi, po));
        if (rcpt2) await loadMyListing();
        return;
      }
      if (active) {
        const rcpt = await T.runTx(T.txLine(txbox, "register(…)"), reg.register(endpoint, models, pc, pi, po));
        if (rcpt) await loadMyListing();
      }
    } catch (e) {
      formErr(txbox, e.shortMessage || e.message);
    }
  }));
  const formErr = (box, msg) => {
    const el = document.createElement("div");
    el.className = "txl is-bad";
    el.innerHTML = `<span class="txl-dot"></span><span class="txl-label">form</span><span class="txl-state">${T.esc(msg)}</span>`;
    box.appendChild(el);
  };

  /* — upstream precheck — */
  $("p-run").addEventListener("click", () => guard($("p-run"), async () => {
    const base = $("p-base").value.trim();
    const out = $("p-result");
    if (!base) { out.innerHTML = `<p class="empty-hint err">enter the relay base URL first</p>`; return; }
    out.innerHTML = `<p class="empty-hint">GET ${T.esc(T.joinUrl(base, "/verify-upstream"))} …</p>`;
    const r = await T.fetchJson(T.joinUrl(base, "/verify-upstream"), {}, 20000);
    if (r.corsOrNetwork) {
      out.innerHTML =
        `<p class="empty-hint err">relay 不可达（或 CORS 未开启 — 等待 relay CORS 配置）。` +
        `<button class="btn btn-sm" id="p-retry" type="button">[ RETRY ]</button></p>`;
      $("p-retry").addEventListener("click", () => $("p-run").click());
      return;
    }
    if (!r.ok || !r.body) {
      out.innerHTML = `<p class="empty-hint err">HTTP ${r.status} — verify-upstream unavailable</p>`;
      return;
    }
    const b = r.body;
    const want = new Set(selectedModels());
    const accessible = b.accessible_models || [];
    /* relay shape: mismatches = [{model, reason}] (objects, not strings) */
    const mismatches = (b.mismatches || []).map((m) =>
      (m && typeof m === "object") ? `${m.model} — ${m.reason}` : String(m));
    const diffExtra = [...want].filter((m) => !accessible.includes(m));
    out.innerHTML =
      `<div class="kv"><span>KEY</span><b class="${b.key_valid ? "ok" : "bad"}">${b.key_valid ? "✓ valid" : "✗ INVALID"}</b></div>` +
      `<div class="kv"><span>UPSTREAM</span><b class="mono">${T.esc(b.upstream_host || "—")}</b></div>` +
      (b.error ? `<div class="kv"><span>ERROR</span><b class="bad mono wrap-anywhere">${T.esc(b.error)}</b></div>` : "") +
      `<div class="kv"><span>ACCESSIBLE</span><b>${accessible.length ? accessible.map((m) => `<span class="mtag">${T.esc(m)}</span>`).join(" ") : "—"}</b></div>` +
      `<div class="kv"><span>LISTING ON-CHAIN</span><b class="${b.listing_ok ? "ok" : "dim"}">${b.listing_ok ? "✓ consistent" : "✗ mismatch / not registered"}</b></div>` +
      (b.listed_models && b.listed_models.length ? `<div class="kv"><span>LISTED</span><b>${b.listed_models.map((m) => `<span class="mtag">${T.esc(m)}</span>`).join(" ")}</b></div>` : "") +
      (mismatches.length ? `<div class="kv"><span>MISMATCHES</span><b class="bad">${mismatches.map((m) => `<span class="mtag">${T.esc(m)}</span>`).join(" ")}</b></div>` : "") +
      (diffExtra.length ? `<div class="kv"><span>FORM vs KEY</span><b class="bad">selected but not accessible: ${diffExtra.map((m) => `<span class="mtag">${T.esc(m)}</span>`).join(" ")}</b></div>` : "");
  }));

  /* ═══ BUYER tab ═══════════════════════════════════════════ */

  /* — seller pickers info — */
  function syncSellerInfo() {
    const l = listingOf($("b-seller").value);
    const info = $("b-lock-info");
    if (!l) { info.innerHTML = `<p class="empty-hint">选择卖家后显示三档价与 minAmount 估计。</p>`; return; }
    const min = T.minAmountEstimate(l);
    info.innerHTML =
      `<div class="kv"><span>PRICES /1M</span><b class="mono">cached $${T.fmtUsdc(l.priceCachedIn)} · in $${T.fmtUsdc(l.priceInput)} · out $${T.fmtUsdc(l.priceOutput)}</b></div>` +
      `<div class="kv"><span>MIN ESTIMATE</span><b class="mono">≥ $${T.fmtUsdc(min)} <span class="dim">(in×200k + out×32k caps)</span></b></div>`;
    $("b-lock-hint").textContent = `≥ 卖家三档价×caps 估计（$${T.fmtUsdc(min)}），参考市场页三档价`;
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
  function renderSessionLocks() {
    const mine = T.locks.all().filter((l) => !state.address || T.sameAddr(l.buyer || "", state.address) || !l.buyer);
    const list = $("c-payment-list");
    const rlist = $("r-payment-list");
    const opts = mine.map((l) => `<option value="${l.paymentId}">#${l.paymentId} · ${T.truncAddr(l.seller)} · max $${T.fmtUsdc(l.maxAmount)}</option>`).join("");
    list.innerHTML = opts;
    rlist.innerHTML = opts;
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
    const min = T.minAmountEstimate(l);
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

    let res;
    try {
      res = await fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Payment-Id": BigInt(paymentId).toString(10),
          "X-Signature": sig,
        },
        body,
      });
    } catch {
      return termLine(term, `<span class="t-a">✗</span> relay 不可达（网络或 CORS — 等待 relay CORS 配置）`);
    }

    if (!res.ok) {
      const copy = T.relayErrorCopy(res.status);
      let detail = "";
      try { const j = await res.json(); detail = j.detail || JSON.stringify(j); } catch { /* ignore */ }
      termLine(term, `<span class="t-a">✗ HTTP ${res.status}</span> ${T.esc(copy || "")} ${T.esc(detail)}`);
      return;
    }

    let data;
    try { data = await res.json(); }
    catch { return termLine(term, `<span class="t-a">✗</span> relay returned non-JSON`); }

    const reply = data && data.choices && data.choices[0] && data.choices[0].message
      ? data.choices[0].message.content : "(no content)";
    const u = data.usage || {};
    const cached = (u.prompt_tokens_details && u.prompt_tokens_details.cached_tokens) ?? u.cached_tokens ?? 0;
    const settle = res.headers.get("X-Settle-Status");

    termLine(term, `<span class="t-g">✓ 200 OK</span>`);
    const replyEl = document.createElement("div");
    replyEl.className = "call-reply";
    replyEl.textContent = reply;
    term.appendChild(replyEl);
    termLine(
      term,
      `<span class="t-d">usage</span> cached <span class="t-n">${cached}</span> · in <span class="t-n">${u.prompt_tokens ?? "?"}</span> · out <span class="t-n">${u.completion_tokens ?? "?"}</span>` +
      (settle ? ` · <span class="t-d">settle</span> <span class="${settle === "settled" ? "t-g" : "t-a"}">${T.esc(settle)}</span>` : "")
    );

    /* EIP-712 receipt verification */
    const receiptHeader = res.headers.get("X-Receipt");
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
  renderCustomChips();
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
