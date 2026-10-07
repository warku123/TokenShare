#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════
   TokenShare — M15 shared-relay custody · web-side unit checks
   (node ≥20 · ZERO new dependencies: the repo's own
   web/vendor/ethers.umd.min.js + Node WebCrypto drive real
   ECDH/HKDF/AES-GCM, keccak256, EIP-191 and EIP-712).

   The custody core in web/common.js is environment-agnostic by
   construction (hashes/subtle injected), so this file exercises the
   EXACT code the browser runs: URL normalization, m15 config
   validation, frozen message/AAD/body formats, envelope seal
   roundtrip + tamper, relay pin matrix, nonce re-validation,
   single vs shared receipt verification, the buyer-side shared-relay
   endpoint matcher, delegate calldata, the form-gate predicates
   (edit-stale / verify-failure / async-race / publish endpoint), and
   the catalog-resume eligibility matrix + its console.js wiring
   evidence (C1 fix: resume used to gate on gen === 0 — unreachable
   after the first connect).

   Cross-language byte-level parity with the Python relay is checked in
   section L whenever relay/tests/vectors/custody_vector.json exists —
   READ-ONLY, and the section SKIPS (never fails) when the file is
   absent, so this suite stays self-contained and never blocks on the
   relay lane. The fixture's embedded body hash is never extracted and
   fed back as an input (no circular self-proof); fields the fixture
   has not shipped yet report [PEND] — listed, never counted as PASS.

   Usage:  node scripts/check-custody-web.mjs
   Exit:   0 = all pass · 1 = failures
   ═══════════════════════════════════════════════════════════ */
import { readFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const subtle = globalThis.crypto.subtle;
const rand = (b) => globalThis.crypto.getRandomValues(b);

/* ── minimal browser-global stubs, THEN import the real page library ── */
const ethersMod = await import(join(ROOT, "web/vendor/ethers.umd.min.js"));
const ethers = ethersMod.ethers || ethersMod.default || ethersMod;

const store = new Map();
globalThis.window = globalThis;
globalThis.Event = class { constructor(type) { this.type = type; } };
window.addEventListener = () => {};
window.dispatchEvent = () => true;
globalThis.document = { readyState: "complete", addEventListener() {} };
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};

/* test deployment config — synthetic, complete m15 block */
const RELAY = "https://relay.test";
const sellerWallet = ethers.Wallet.createRandom();   // listing operator
const relayWallet = ethers.Wallet.createRandom();    // shared relay signer
const UPLOAD = await subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, true, ["deriveBits"]);
const UPLOAD_PUB = new Uint8Array(await subtle.exportKey("raw", UPLOAD.publicKey)); // 65B, 0x04‖x‖y

const sha256Hex = async (bytes) => T.custody.bytesToHex(new Uint8Array(await subtle.digest("SHA-256", bytes)));
const keccak256Hex = (bytes) => ethers.keccak256(bytes).slice(2);
const keccak256TextHex = (s) => ethers.keccak256(ethers.toUtf8Bytes(s)).slice(2);
const hashers = { sha256Hex, keccak256Hex, keccak256TextHex };

const TEST_CFG = {
  chainId: 10143,
  chainName: "Monad Testnet",
  nativeCurrency: { name: "Monad", symbol: "MON", decimals: 18 },
  rpcUrl: "https://testnet-rpc.monad.xyz",
  explorer: "https://testnet.monadvision.com",
  escrowAddr: "0x157C551D145d3c4bBF8f3554c43Fb3C931D71aD5",
  registryAddr: "0xeD347cDc1761750E20C024459b38dedFb1462254",
  usdcAddr: "0x534b2f3A21130d7a60830c2Df862319e593943A3",
  sellers: [],
  m15: {
    mode: "shared",
    relayOrigin: RELAY,
    expectedSigner: relayWallet.address,
    appId: "0x" + "ab".repeat(20),
    uploadPubkeySha256: "",   // filled below (needs the custody codec)
    attestEvidence: {
      verifier: "https://cloud-api.phala.com/api/v1/attestations/verify",
      verifiedAt: "2026-10-06",
      digest: "cd".repeat(32),
    },
  },
};

/* b64url encode without the library (independent of common.js) */
const b64url = (bytes) => Buffer.from(bytes).toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
TEST_CFG.m15.uploadPubkeySha256 = Buffer.from(await subtle.digest("SHA-256", UPLOAD_PUB)).toString("hex");

window.TS_CONFIG = TEST_CFG;
window.ethers = ethers;
await import(join(ROOT, "web/common.js"));
const T = window.TS;
if (!T || !T.custody) { console.error("FATAL: window.TS.custody missing after common.js load"); process.exit(1); }
const M15 = T.custody.validateM15Config(TEST_CFG.m15).cfg;

/* ── assert harness ── */
let pass = 0, fail = 0;
const failures = [];
function ok(cond, name, detail = "") {
  if (cond) { pass++; console.log(`[PASS] ${name}`); }
  else { fail++; failures.push(name); console.log(`[FAIL] ${name}${detail ? ` — ${detail}` : ""}`); }
}
const eq = (a, b, name) => ok(a === b, name, a === b ? "" : `\n  got:      ${a}\n  expected: ${b}`);
/* PEND = interop dependency not yet confirmable (e.g. fixture fields a
   sibling lane has not shipped). Printed, listed in the summary, NEVER
   counted as PASS — and never a fake green. */
const pendings = [];
function pend(name, why) {
  pendings.push(`${name} — ${why}`);
  console.log(`[PEND] ${name} — ${why}`);
}
async function throws(fn, name) {
  try { await fn(); ok(false, name, "did not throw"); }
  catch { ok(true, name); }
}

/* ═══ A. URL normalization ═══ */
console.log("\n== A. URL normalization ==");
{
  const nu = T.custody.normalizeUpstreamUrl;
  eq(nu(" https://api.kimi.com/coding/v1 ").url, "https://api.kimi.com/coding", "A1 upstream: trim + strip one terminal /v1");
  eq(nu("https://api.kimi.com/coding/v1/").url, "https://api.kimi.com/coding", "A2 trailing slash after /v1");
  eq(nu("https://API.KIMI.COM:443/coding").url, "https://api.kimi.com/coding", "A3 host lowercased + default port omitted");
  eq(nu("https://relay.example.com:8787").url, "https://relay.example.com:8787", "A4 non-default port kept");
  eq(nu("https://x.com/v1/v1").url, "https://x.com/v1", "A5 only ONE terminal /v1 stripped");
  ok(!nu("https://user:pw@api.kimi.com/coding").ok, "A6 userinfo rejected");
  ok(!nu("https://api.kimi.com/coding?x=1").ok, "A7 query rejected");
  ok(!nu("https://api.kimi.com/coding#f").ok, "A8 fragment rejected");
  ok(!nu("https://api.kimi.com/coding|evil").ok, "A9 pipe rejected");
  ok(!nu("http://api.kimi.com/coding").ok, "A10 http rejected by default");
  eq(nu("http://localhost:8787", { allowHttp: true }).url, "http://localhost:8787", "A11 http allowed only under explicit dev policy");
  ok(!nu("https://api.kimi.com/cöding").ok, "A12 non-ASCII rejected");
  ok(!nu("not a url").ok, "A13 unparseable rejected");

  const no = T.custody.normalizeRelayOrigin;
  eq(no("https://relay.test/").origin, "https://relay.test", "A14 relay origin: trailing slash stripped");
  ok(!no("https://relay.test/sub").ok, "A15 relay origin: non-root path rejected");
  ok(!no("https://relay.test/v1").ok, "A16 relay origin: /v1 NOT stripped (origins are root-only)");
}

/* ═══ B. m15 config validation ═══ */
console.log("\n== B. m15 config validation ==");
{
  ok(T.custody.validateM15Config(undefined).absent === true, "B1 absent m15 → single-seller mode");
  ok(T.custody.validateM15Config(TEST_CFG.m15).ok, "B2 complete block validates");
  const v = T.custody.validateM15Config(TEST_CFG.m15);
  eq(v.cfg.relayOrigin, RELAY, "B3 relayOrigin normalized");
  eq(v.cfg.expectedSigner, relayWallet.address.toLowerCase(), "B4 expectedSigner lowercased");
  const noSigner = T.custody.validateM15Config({ ...TEST_CFG.m15, expectedSigner: undefined });
  ok(!noSigner.ok && noSigner.missing.includes("expectedSigner"), "B5 missing expectedSigner → fail-closed, named");
  const badDigest = T.custody.validateM15Config({ ...TEST_CFG.m15, attestEvidence: { ...TEST_CFG.m15.attestEvidence, digest: "zz" } });
  ok(!badDigest.ok && badDigest.missing.includes("attestEvidence.digest"), "B6 malformed evidence digest → fail-closed");
  const badDate = T.custody.validateM15Config({ ...TEST_CFG.m15, attestEvidence: { ...TEST_CFG.m15.attestEvidence, verifiedAt: "Oct 6 2026" } });
  ok(!badDate.ok, "B7 non-ISO verifiedAt → fail-closed");
  const badMode = T.custody.validateM15Config({ ...TEST_CFG.m15, mode: "single" });
  ok(!badMode.ok, "B8 mode≠shared → fail-closed");
  /* default web/config.js ships the LIVE shared-custody deployment: the m15
     block must be ACTIVE and every identity pin fully pinned — non-empty,
     correctly formatted, no *_FILL_* placeholder (fail-closed otherwise).
     The real config is evaluated in isolation (TEST_CFG above stays the
     synthetic fixture for every other section). */
  const cfgSrc = readFileSync(join(ROOT, "web/config.js"), "utf8");
  const liveCfg = new Function(cfgSrc.replace(/^\s*window\.TS_CONFIG\s*=/m, "return "))();
  const HEX64 = (s) => /^[0-9a-f]{64}$/.test(String(s || "").toLowerCase());
  const m15 = liveCfg && typeof liveCfg.m15 === "object" ? liveCfg.m15 : null;
  ok(!!m15, "B9 web/config.js ships an ACTIVE m15 block (shared custody is the shipped mode)");
  if (m15) {
    ok(typeof m15.relayOrigin === "string" && m15.relayOrigin.startsWith("https://"),
      "B9a relayOrigin is an https origin", String(m15.relayOrigin));
    ok(/^0x[0-9a-fA-F]{40}$/.test(String(m15.expectedSigner || "")),
      "B9b expectedSigner is 0x + 40 hex", String(m15.expectedSigner));
    /* appId on this deployment is the Phala cloud app_id — 40 hex (20B),
       compared verbatim against /attestation appId by checkRelayPins;
       64-hex ids stay accepted for forward-compat. Placeholder-free. */
    ok(/^(0x)?[0-9a-f]{40}$|^(0x)?[0-9a-f]{64}$/i.test(String(m15.appId || "")) && !/fill/i.test(String(m15.appId)),
      "B9c appId is 40/64 hex — the live /attestation top-level app_id (no placeholder)", String(m15.appId));
    ok(HEX64(m15.uploadPubkeySha256) && !/fill/i.test(String(m15.uploadPubkeySha256)),
      "B9d uploadPubkeySha256 is 64 hex (no placeholder)", String(m15.uploadPubkeySha256));
    const ev = m15.attestEvidence && typeof m15.attestEvidence === "object" ? m15.attestEvidence : {};
    ok(typeof ev.verifier === "string" && ev.verifier.startsWith("https://"),
      "B9e attestEvidence.verifier is https", String(ev.verifier));
    ok(/^\d{4}-\d{2}-\d{2}$/.test(String(ev.verifiedAt || "")),
      "B9f attestEvidence.verifiedAt is YYYY-MM-DD", String(ev.verifiedAt));
    ok(HEX64(ev.digest) && !/fill/i.test(String(ev.digest)),
      "B9g attestEvidence.digest is the full 64-hex quote digest (no FILL placeholder)", String(ev.digest));
    ok(T.custody.validateM15Config(m15).ok,
      "B9h the active block passes the fail-closed runtime validator");
  }
}

/* ═══ C. frozen message / AAD / body formats ═══ */
console.log("\n== C. message / AAD / body formats ==");
{
  const F = {
    seller: sellerWallet.address.toLowerCase(), chain: 10143,
    escrow: TEST_CFG.escrowAddr.toLowerCase(), registry: TEST_CFG.registryAddr.toLowerCase(),
    relay: RELAY, upstream: "https://api.kimi.com/coding",
    nonce: "0x" + "11".repeat(32), issued: 1700000000, expires: 1700000600,
  };
  const aad = T.custody.buildCustodyAad(F);
  eq(aad,
    `TokenShare custody envelope|action=submit|seller=${F.seller}|chain=10143|escrow=${F.escrow}|registry=${F.registry}|relay=${RELAY}|upstream=https://api.kimi.com/coding|nonce=${F.nonce}|issued=1700000000|expires=1700000600`,
    "C1 AAD byte-exact (envelope prefix, NO body hash)");
  ok(!aad.includes("body_sha256"), "C2 AAD carries no body hash (anti-circularity)");

  const env = { alg: T.custody.ENVELOPE_ALG, epk: "EPK", iv: "IV", ct: "CT" };
  const body = T.custody.canonicalSubmitBody({
    nonce: F.nonce, issued_at: F.issued, expires_at: F.expires, upstream_base_url: F.upstream, envelope: env,
  });
  eq(body,
    `{"nonce":"${F.nonce}","issued_at":1700000000,"expires_at":1700000600,"upstream_base_url":"https://api.kimi.com/coding","envelope":{"alg":"${T.custody.ENVELOPE_ALG}","epk":"EPK","iv":"IV","ct":"CT"}}`,
    "C3 body JSON byte-exact, pinned key order");
  eq(JSON.stringify(Object.keys(JSON.parse(body))), `["nonce","issued_at","expires_at","upstream_base_url","envelope"]`, "C4 body key order pinned");
  eq(JSON.stringify(Object.keys(JSON.parse(body).envelope)), `["alg","epk","iv","ct"]`, "C5 envelope key order pinned");

  const bodySha = Buffer.from(await subtle.digest("SHA-256", new TextEncoder().encode(body))).toString("hex");
  const msg = T.custody.buildCustodySubmitMessage({ ...F, bodySha256: bodySha });
  eq(msg,
    `TokenShare key custody|action=submit|seller=${F.seller}|chain=10143|escrow=${F.escrow}|registry=${F.registry}|relay=${RELAY}|upstream=https://api.kimi.com/coding|nonce=${F.nonce}|body_sha256=${bodySha}|issued=1700000000|expires=1700000600`,
    "C6 submit message byte-exact (custody prefix, body_sha256 present)");
  ok(msg.includes(`|body_sha256=${bodySha}|`) && msg.indexOf("|body_sha256=") > msg.indexOf("|nonce="),
    "C7 message embeds the hash of the serialized body (computed after envelope + serialization), after the nonce");
  ok(msg !== aad && aad.startsWith("TokenShare custody envelope|") && msg.startsWith("TokenShare key custody|"), "C8 AAD and message are different strings with different prefixes");

  eq(T.custody.buildCustodyRevokeMessage(F),
    `TokenShare key custody|action=revoke|seller=${F.seller}|chain=10143|escrow=${F.escrow}|registry=${F.registry}|relay=${RELAY}|nonce=${F.nonce}|issued=1700000000|expires=1700000600`,
    "C9 revoke message byte-exact (no upstream, no body hash)");
  eq(T.custody.canonicalRevokeBody({ nonce: F.nonce, issued_at: F.issued, expires_at: F.expires }),
    `{"nonce":"${F.nonce}","issued_at":1700000000,"expires_at":1700000600}`, "C10 revoke body byte-exact");

  await throws(() => T.custody.canonicalSubmitBody({
    nonce: F.nonce, issued_at: F.issued, expires_at: F.expires,
    upstream_base_url: "https://x/" + "a".repeat(70000), envelope: env,
  }), "C11 body > 64 KiB throws");
  await throws(() => T.custody.canonicalSubmitBody({
    nonce: F.nonce, issued_at: F.issued, expires_at: F.expires,
    upstream_base_url: "https://x/cöding", envelope: env,
  }), "C12 non-ASCII body throws");
}

/* ═══ D. envelope seal — roundtrip + tamper (real WebCrypto) ═══ */
console.log("\n== D. envelope crypto ==");
{
  const aad = "TokenShare custody envelope|action=submit|test";
  const env = await T.custody.encryptEnvelope({
    uploadPubBytes: UPLOAD_PUB, apiKey: "sk-test-123", aad, subtle, getRandomValues: rand,
  });
  eq(env.alg, "ECDH-P256-HKDF-SHA256-A256GCM", "D1 alg string pinned");
  const epk = T.custody.b64urlToBytes(env.epk);
  ok(epk.length === 65 && epk[0] === 4, "D2 epk = 65-byte uncompressed point");
  eq(T.custody.b64urlToBytes(env.iv).length, 12, "D3 iv = 12 bytes");
  const ctBytes = T.custody.b64urlToBytes(env.ct);
  eq(ctBytes.length, JSON.stringify({ api_key: "sk-test-123" }).length + 16, "D4 ct = plaintext + 16B GCM tag");

  /* independent decrypt path: upload private key × epk public */
  const epkKey = await subtle.importKey("raw", epk, { name: "ECDH", namedCurve: "P-256" }, false, []);
  const z = await subtle.deriveBits({ name: "ECDH", public: epkKey }, UPLOAD.privateKey, 256);
  const salt = await subtle.digest("SHA-256", new TextEncoder().encode(T.custody.HKDF_SALT_STRING));
  const hkdf = await subtle.importKey("raw", z, "HKDF", false, ["deriveBits"]);
  const aesBits = await subtle.deriveBits(
    { name: "HKDF", hash: "SHA-256", salt, info: new TextEncoder().encode(T.custody.HKDF_INFO_STRING) }, hkdf, 256);
  const aes = await subtle.importKey("raw", aesBits, { name: "AES-GCM" }, false, ["decrypt"]);
  const pt = await subtle.decrypt(
    { name: "AES-GCM", iv: T.custody.b64urlToBytes(env.iv), additionalData: new TextEncoder().encode(aad), tagLength: 128 },
    aes, ctBytes);
  eq(new TextDecoder().decode(pt), `{"api_key":"sk-test-123"}`, "D5 roundtrip — receiver side recovers the exact plaintext JSON");

  const tampered = ctBytes.slice(); tampered[0] ^= 1;
  await throws(() => subtle.decrypt(
    { name: "AES-GCM", iv: T.custody.b64urlToBytes(env.iv), additionalData: new TextEncoder().encode(aad), tagLength: 128 },
    aes, tampered), "D6 tampered ct → GCM auth failure");
  await throws(() => subtle.decrypt(
    { name: "AES-GCM", iv: T.custody.b64urlToBytes(env.iv), additionalData: new TextEncoder().encode(aad + "x"), tagLength: 128 },
    aes, ctBytes), "D7 wrong AAD → GCM auth failure (context binding holds)");
  await throws(() => T.custody.encryptEnvelope({
    uploadPubBytes: UPLOAD_PUB, apiKey: "sk-nön-ascii", aad, subtle, getRandomValues: rand,
  }), "D8 non-ASCII api key rejected before sealing");
  await throws(() => T.custody.encryptEnvelope({
    uploadPubBytes: UPLOAD_PUB.slice(0, 33), apiKey: "sk", aad, subtle, getRandomValues: rand,
  }), "D9 malformed upload pubkey rejected");
}

/* ═══ E. relay pin matrix (live binding, fail-closed) ═══ */
console.log("\n== E. pin matrix ==");
{
  const reportData = new Uint8Array(64);
  reportData.set(T.custody.hexToBytes(M15.expectedSigner), 0);         /* signer 20B */
  reportData.set(T.custody.hexToBytes(keccak256Hex(UPLOAD_PUB)), 32);  /* keccak(pub65) at 32, 12 zero bytes between */
  const goodInfo = {
    shared: {
      origin: RELAY, signer: relayWallet.address,
      uploadPubkey: b64url(UPLOAD_PUB), uploadPubkeySha256: TEST_CFG.m15.uploadPubkeySha256,
    },
  };
  const goodAtt = {
    appId: TEST_CFG.m15.appId, derivedAddress: relayWallet.address,
    reportData: "0x" + Buffer.from(reportData).toString("hex"),        /* API shape: 0x + hex */
  };
  const good = await T.custody.checkRelayPins({ info: goodInfo, attest: goodAtt, cfg: M15, ...hashers });
  ok(good.ok && good.pubBytes && good.pubBytes.length === 65, "E1 consistent deployment passes, pubkey extracted");
  const rowOk = (res, key) => { const r = res.rows.find((x) => x.key === key); return r && !r.ok; };

  ok(rowOk(await T.custody.checkRelayPins({ info: { shared: { ...goodInfo.shared, origin: "https://evil.test" } }, attest: goodAtt, cfg: M15, ...hashers }), "origin"),
    "E2 origin mismatch fails (origin row)");
  ok(rowOk(await T.custody.checkRelayPins({ info: { shared: { ...goodInfo.shared, signer: sellerWallet.address } }, attest: goodAtt, cfg: M15, ...hashers }), "signer"),
    "E3 signer mismatch fails");
  const badPub = UPLOAD_PUB.slice(); badPub[64] ^= 1;
  ok(rowOk(await T.custody.checkRelayPins({ info: { shared: { ...goodInfo.shared, uploadPubkey: b64url(badPub) } }, attest: goodAtt, cfg: M15, ...hashers }), "upload-key"),
    "E4 upload pubkey ≠ pinned sha256 fails");
  ok(rowOk(await T.custody.checkRelayPins({ info: { shared: { ...goodInfo.shared, uploadPubkeySha256: "00".repeat(32) } }, attest: goodAtt, cfg: M15, ...hashers }), "upload-key"),
    "E5 /info-declared hash ≠ computed hash fails (info-hash consistency)");
  ok(rowOk(await T.custody.checkRelayPins({ info: goodInfo, attest: { ...goodAtt, appId: "0x" + "ff".repeat(20) }, cfg: M15, ...hashers }), "app-id"),
    "E6 quote appId ≠ pin fails");
  ok(rowOk(await T.custody.checkRelayPins({ info: goodInfo, attest: { ...goodAtt, appId: null }, cfg: M15, ...hashers }), "app-id"),
    "E7 absent appId fails closed (never inferred)");
  ok(rowOk(await T.custody.checkRelayPins({ info: goodInfo, attest: { ...goodAtt, derivedAddress: sellerWallet.address }, cfg: M15, ...hashers }), "derived-address"),
    "E8 derivedAddress ≠ signer fails");
  const badRd = reportData.slice(); badRd[5] ^= 1;
  ok(rowOk(await T.custody.checkRelayPins({ info: goodInfo, attest: { ...goodAtt, reportData: "0x" + Buffer.from(badRd).toString("hex") }, cfg: M15, ...hashers }), "report-data"),
    "E9 reportData binding mismatch fails");
  ok(rowOk(await T.custody.checkRelayPins({ info: goodInfo, attest: { ...goodAtt, reportData: "0x" + "00".repeat(63) }, cfg: M15, ...hashers }), "report-data"),
    "E10 reportData ≠ 64 bytes fails");
  ok(rowOk(await T.custody.checkRelayPins({ info: {}, attest: goodAtt, cfg: M15, ...hashers }), "shared-mode"),
    "E11 /info without a shared section fails");
  ok(!(await T.custody.checkRelayPins({ info: goodInfo, attest: null, cfg: M15, ...hashers })).ok,
    "E12 missing /attestation fails");
}

/* ═══ F. nonce re-validation (never sign a tampered nonce) ═══ */
console.log("\n== F. nonce validation ==");
{
  const now = 1700000100;
  const good = {
    nonce: "0x" + "ab".repeat(32), issued_at: 1700000000, expires_at: 1700000600,
    origin: RELAY, chain_id: 10143,
    escrow_addr: TEST_CFG.escrowAddr, registry_addr: TEST_CFG.registryAddr, mode: "shared",
  };
  const ctx = { chainId: 10143, escrowAddr: TEST_CFG.escrowAddr, registryAddr: TEST_CFG.registryAddr, relayOrigin: RELAY };
  ok(T.custody.checkNonce(good, ctx, now).ok, "F1 well-formed nonce passes");
  ok(T.custody.checkNonce({ ...good, origin: RELAY + "/" }, ctx, now).ok, "F2 origin trailing slash normalizes");
  ok(!T.custody.checkNonce({ ...good, nonce: "0x" + "AB".repeat(32) }, ctx, now).ok, "F3 uppercase nonce rejected (format pinned lowercase)");
  ok(!T.custody.checkNonce({ ...good, nonce: "ab".repeat(32) }, ctx, now).ok, "F4 nonce without 0x rejected");
  ok(!T.custody.checkNonce({ ...good, expires_at: 1700000601 }, ctx, now).ok, "F5 window > 600s rejected");
  ok(!T.custody.checkNonce({ ...good, expires_at: 1700000100 }, ctx, now).ok, "F6 expired nonce rejected");
  ok(!T.custody.checkNonce({ ...good, chain_id: 1 }, ctx, now).ok, "F7 chain_id mismatch rejected");
  ok(!T.custody.checkNonce({ ...good, escrow_addr: sellerWallet.address }, ctx, now).ok, "F8 escrow_addr mismatch rejected");
  ok(!T.custody.checkNonce({ ...good, registry_addr: sellerWallet.address }, ctx, now).ok, "F9 registry_addr mismatch rejected");
  ok(!T.custody.checkNonce({ ...good, origin: "https://evil.test" }, ctx, now).ok, "F10 origin mismatch rejected");
  ok(!T.custody.checkNonce({ ...good, mode: "single" }, ctx, now).ok, "F11 mode ≠ shared rejected");
  ok(!T.custody.checkNonce({ ...good, issued_at: "1700000000" }, ctx, now).ok, "F12 string times rejected (integers only)");
}

/* ═══ G. receipts — single rule preserved, shared pin opt-in ═══ */
console.log("\n== G. receipt verification (real EIP-712) ==");
{
  const domain = { name: T.RECEIPT_DOMAIN_NAME, version: T.RECEIPT_DOMAIN_VERSION, chainId: 10143 };
  const mkMsg = (seller) => ({
    paymentId: 7n, promptTokens: 100n, cachedTokens: 10n, completionTokens: 50n,
    actualAmount: 12345n, seller, upstreamHost: "api.kimi.com", model: "k3-256k",
  });
  const receiptOf = async (wallet, msg) => ({
    domain, message: msg, signature: await wallet.signTypedData(domain, T.RECEIPT_TYPES, msg),
  });

  /* single-seller rule (the old 4-arg call must behave exactly as before) */
  const rSingle = await receiptOf(sellerWallet, mkMsg(sellerWallet.address));
  ok(T.verifyReceipt(rSingle, sellerWallet.address, 10143, 7).ok, "G1 single: operator-signed receipt verifies (4-arg legacy call)");
  eq(T.verifyReceipt(rSingle, relayWallet.address, 10143, 7).reason, "recover-mismatch", "G2 single: wrong seller → recover-mismatch");
  ok(!T.verifyReceipt(rSingle, sellerWallet.address, 1, 7).ok, "G3 wrong chainId → domain-mismatch");
  eq(T.verifyReceipt(rSingle, sellerWallet.address, 10143, 8).reason, "paymentid-mismatch", "G4 wrong paymentId → paymentid-mismatch");

  /* shared rule: relay signer pinned, seller field stays the operator */
  const rShared = await receiptOf(relayWallet, mkMsg(sellerWallet.address));
  const okShared = T.verifyReceipt(rShared, sellerWallet.address, 10143, 7, { expectedSigner: relayWallet.address });
  ok(okShared.ok && ethers.getAddress(okShared.recovered) === ethers.getAddress(relayWallet.address),
    "G5 shared: relay-signed receipt verifies under the pinned signer");
  eq(T.verifyReceipt(rShared, sellerWallet.address, 10143, 7, { expectedSigner: sellerWallet.address }).reason,
    "signer-mismatch", "G6 shared: wrong pin → signer-mismatch");
  eq(T.verifyReceipt(rShared, relayWallet.address, 10143, 7, { expectedSigner: relayWallet.address }).reason,
    "seller-mismatch", "G7 shared: seller field must stay the listing operator");
  eq(T.verifyReceipt(rShared, sellerWallet.address, 10143, 7).reason,
    "recover-mismatch", "G8 shared receipt under the OLD rule fails (no silent downgrade)");
  const rForged = await receiptOf(relayWallet, mkMsg(relayWallet.address));
  eq(T.verifyReceipt(rForged, sellerWallet.address, 10143, 7, { expectedSigner: relayWallet.address }).reason,
    "seller-mismatch", "G9 relay signing its own address as seller is rejected for the operator's listing");
}

/* ═══ H. buyer-side shared-relay endpoint matcher ═══ */
console.log("\n== H. matchesSharedRelay ==");
{
  const m = T.custody.matchesSharedRelay;
  ok(m(RELAY, RELAY), "H1 exact origin matches");
  ok(m(RELAY + "/", RELAY), "H2 trailing slash still matches");
  ok(m("https://relay.test:443", RELAY), "H3 default port equivalent");
  ok(!m("https://relay.test:8443", RELAY), "H4 different port no match");
  ok(!m("https://relay.test/sub", RELAY), "H5 non-root path no match");
  ok(!m("https://relay.test/?x=1", RELAY), "H6 query no match");
  ok(!m("https://relay.test/#f", RELAY), "H7 fragment no match");
  ok(!m("https://user:pw@relay.test", RELAY), "H8 userinfo no match");
  ok(!m("http://relay.test", RELAY), "H9 http no match");
  ok(!m("https://evil.test", RELAY), "H10 foreign host no match — the shared pin never leaks to arbitrary endpoints");
}

/* ═══ I. delegate calldata (AUTHORIZE / REVOKE) ═══ */
console.log("\n== I. delegate calldata ==");
{
  const sel = ethers.id("approveSettleDelegate(address)").slice(2, 10);
  const data = T.custody.delegateCalldata(keccak256TextHex, M15.expectedSigner);
  eq(data, "0x" + sel + "0".repeat(24) + M15.expectedSigner.slice(2), "I1 AUTHORIZE calldata = selector ∥ zero-padded pinned signer");
  const rev = T.custody.delegateCalldata(keccak256TextHex, T.custody.ZERO_ADDRESS);
  eq(rev, "0x" + sel + "0".repeat(64), "I2 REVOKE calldata = selector ∥ zero address");
  await throws(() => T.custody.delegateCalldata(keccak256TextHex, "0x123"), "I3 malformed delegate rejected");
  /* the ABI addition mirrors the pinned selector (guards drift between
     the raw-calldata write path and the read path's Contract) */
  ok(T.ESCROW_ABI.some((s) => s.includes("approveSettleDelegate")) && T.ESCROW_ABI.some((s) => s.includes("settleDelegateOf")),
    "I4 ESCROW_ABI carries approveSettleDelegate + settleDelegateOf");
}

/* ═══ J. form-gate predicates (edit-stale / failure / race / publish) ═══ */
console.log("\n== J. form-gate predicates ==");
{
  const me = sellerWallet.address.toLowerCase();
  const snap = { gen: 3, address: me };
  const goodRes = { ok: true, upstream: "https://api.kimi.com/coding", fingerprint: "ff".repeat(32) };
  const applied = T.custody.applyVerifyResult({ gen: 3, address: me }, snap, goodRes);
  ok(applied.applied && applied.anchor.gen === 3 && applied.anchor.upstream === goodRes.upstream, "J1 verify result applies on the same generation+account");
  ok(!T.custody.applyVerifyResult({ gen: 4, address: me }, snap, goodRes).applied, "J2 URL/key edit during flight (gen bump) → result discarded");
  ok(!T.custody.applyVerifyResult({ gen: 3, address: relayWallet.address.toLowerCase() }, snap, goodRes).applied, "J3 account switch during flight → result discarded");
  ok(!T.custody.applyVerifyResult({ gen: 3, address: me }, snap, { ok: false, reason: "x" }).applied, "J4 failed verify → never applied");

  const base = { pinsOk: true, verifyStatus: "ok", anchor: applied.anchor, gen: 3, address: me, keyStored: true, selectedCount: 1, servableCount: 2 };
  ok(T.custody.publishReady(base).ok, "J5 publish gate passes when everything is fresh");
  ok(!T.custody.publishReady({ ...base, pinsOk: false }).ok, "J6 pins not verified → blocked");
  ok(!T.custody.publishReady({ ...base, verifyStatus: "error" }).ok, "J7 failed verify → blocked");
  ok(!T.custody.publishReady({ ...base, verifyStatus: "loading" }).ok, "J8 in-flight verify → blocked");
  ok(!T.custody.publishReady({ ...base, gen: 4 }).ok, "J9 edit-stale anchor → blocked");
  ok(!T.custody.publishReady({ ...base, address: relayWallet.address.toLowerCase() }).ok, "J10 account switch → blocked");
  ok(!T.custody.publishReady({ ...base, keyStored: false }).ok, "J11 no stored key → blocked");
  ok(!T.custody.publishReady({ ...base, servableCount: 0 }).ok, "J12 zero servable models → blocked");
  ok(!T.custody.publishReady({ ...base, selectedCount: 0 }).ok, "J13 nothing checked → blocked");
  ok(!T.custody.publishReady({ ...base, anchor: null }).ok, "J14 no anchor → blocked");

  eq(T.custody.registerEndpoint(M15), RELAY, "J15 PUBLISH endpoint = pinned relay origin, automatically");
  ok(T.custody.registerEndpoint(M15) !== "https://api.kimi.com/coding", "J16 PUBLISH endpoint is never the upstream URL");
}

/* ═══ K. static wiring contract (console.html ↔ console.js) ═══ */
console.log("\n== K. static wiring ==");
{
  const html = readFileSync(join(ROOT, "web/console.html"), "utf8");
  const js = readFileSync(join(ROOT, "web/console.js"), "utf8");
  const ids = ["custody-card", "sc-relay", "s-upstream-url", "s-upstream-key", "s-verify-btn", "s-verify-note",
    "sc-pins", "sc-status", "sc-authorize", "sc-revoke-delegate", "sc-revoke-key", "sc-tx", "s-endpoint-fld", "s-models-hint"];
  const missing = ids.filter((id) => !html.includes(`id="${id}"`));
  ok(missing.length === 0, "K1 console.html carries every custody element id", missing.join(", "));
  ok(/id="custody-card"[^>]*hidden/.test(html.replace(/\n/g, " ")) || html.includes('id="custody-card" hidden'),
    "K2 custody card is hidden by default (classic form preserved without m15)");
  const unused = ids.filter((id) => !js.includes(`"${id}"`));
  ok(unused.length === 0, "K3 console.js references every custody id", unused.join(", "));
  ok(js.includes("T.custody.delegateCalldata"), "K4 AUTHORIZE goes through the pinned calldata builder");
  ok(js.includes("matchesSharedRelay"), "K5 buyer receipt path applies the shared pin via the strict matcher");
  ok(!/s-upstream-key[^\n]*localStorage/.test(js), "K6 the upstream key never touches localStorage");
  ok(js.includes('"X-Tokenshare-Signature"') && !/"Signature":/.test(js),
    "K7 custody POST/DELETE authenticate via X-Tokenshare-Signature (never a bare Signature header)");
  ok(js.includes('"X-Tokenshare-Seller"'), "K8 custody requests carry X-Tokenshare-Seller");
}

/* ═══ L. relay-lane fixture parity (READ-ONLY · skip ≠ fail) ═══
   When relay/tests/vectors/custody_vector.json exists, prove the exact
   browser code reproduces it: AAD byte-exact, canonical body pinned
   against an independently constructed literal, sha256 computed HERE
   over the real body bytes, message structure byte-exact, deterministic
   re-encryption + receiver-side decrypt.
   DISCIPLINE (ora19): the fixture message's embedded body_sha256 is
   NEVER extracted and fed back as an input — that would be circular
   self-proof. The body hash used to build OUR message is computed from
   our own canonical serialization; the fixture's hash segment is only
   ever COMPARED against (masked out for the structural compare, checked
   for real once the fixture ships raw_body/body_sha256 — relay fix46).
   Fixture fields that have not landed yet are reported [PEND], never
   counted as PASS. */
console.log("\n== L. relay fixture parity ==");
const FIXTURE_PATH = join(ROOT, "relay/tests/vectors/custody_vector.json");
let fx = null;
try { fx = JSON.parse(readFileSync(FIXTURE_PATH, "utf8")); } catch { /* absent → skip */ }
if (!fx) {
  console.log("   (fixture absent — section L skipped; suite is self-contained)");
} else {
  const unb64u = (s) => new Uint8Array(Buffer.from(String(s).replace(/-/g, "+").replace(/_/g, "/"), "base64"));
  const unhex = (h) => new Uint8Array(Buffer.from(String(h).replace(/^0x/i, ""), "hex"));
  const f = fx.aad_fields;
  const fields = {
    seller: f.seller, chain: f.chain_id, escrow: f.escrow_addr, registry: f.registry_addr,
    relay: f.origin, upstream: f.upstream_base_url, nonce: f.nonce, issued: f.issued, expires: f.expires,
  };
  eq(T.custody.buildCustodyAad(fields), fx.aad_ascii, "L1 AAD byte-exact vs relay fixture");
  ok(!fx.aad_ascii.includes("body_sha256"), "L2 AAD carries no body hash (independent string)");

  /* canonical body, serialized HERE — then hashed HERE. The literal
     below is the independent construction (Python json.dumps compact
     twin); the fixture message's embedded hash is never an input. */
  const bodyExpected =
    `{"nonce":"${f.nonce}","issued_at":${f.issued},"expires_at":${f.expires},` +
    `"upstream_base_url":"${f.upstream_base_url}","envelope":{"alg":"${fx.alg}",` +
    `"epk":"${fx.expected.epk_b64url}","iv":"${fx.expected.iv_b64url}","ct":"${fx.expected.ct_b64url}"}}`;
  const bodyActual = T.custody.canonicalSubmitBody({
    nonce: f.nonce, issued_at: f.issued, expires_at: f.expires, upstream_base_url: f.upstream_base_url,
    envelope: { alg: fx.alg, epk: fx.expected.epk_b64url, iv: fx.expected.iv_b64url, ct: fx.expected.ct_b64url },
  });
  eq(bodyActual, bodyExpected, "L3 canonical submit body ≡ Python json.dumps compact byte layout");
  const bodySha = await sha256Hex(new TextEncoder().encode(bodyActual));
  ok(/^[0-9a-f]{64}$/.test(bodySha), "L4 body sha256 computed locally (64 lower hex)");

  /* message: built with OUR computed hash. The fixture's hash segment is
     masked in BOTH strings for the structural compare (every other
     segment byte-exact), then checked for real in L9 when terminal. */
  const msgMine = T.custody.buildCustodySubmitMessage({ ...fields, bodySha256: bodySha });
  const maskHash = (m) => String(m).replace(/\|body_sha256=[0-9a-fA-F]{64}(?=\|)/, "|body_sha256=#");
  const fxHash = (fx.message.split("|body_sha256=")[1] || "").split("|")[0] || "";
  ok(maskHash(fx.message) !== fx.message || !fx.message.includes("|body_sha256="),
    "L5 fixture message carries a syntactically hash-shaped body_sha256 segment (maskable)");
  eq(maskHash(msgMine), maskHash(fx.message),
    "L6 submit message byte-exact vs relay fixture (all non-hash segments)");
  ok(fx.message.startsWith("TokenShare key custody|action=submit|") && !fx.message.includes("\n") && !fx.message.includes("\r"),
    "L7 fixture message is the single-line pipe format");

  /* real body-hash interop — needs the terminal fixture (relay fix46:
     raw_body + body_sha256 fields). Absent → PEND, never greenwash. */
  if (typeof fx.raw_body === "string" && fx.raw_body) {
    eq(bodyActual, fx.raw_body, "L8 canonical body == fixture raw_body (byte-exact)");
    if (typeof fx.body_sha256 === "string" && fx.body_sha256) {
      eq(bodySha, fx.body_sha256.toLowerCase().replace(/^0x/, ""), "L9 sha256(raw body) == fixture body_sha256");
    } else {
      pend("L9 body_sha256 field compare", "fixture has raw_body but no body_sha256 field");
    }
    eq(fxHash, bodySha, "L10 fixture message embeds the REAL body hash (no synthetic constant)");
  } else {
    pend("L8–L10 real body-hash interop", "fixture lacks raw_body/body_sha256 — pending relay fix46");
  }

  /* synthetic-EOA signature interop (relay fix46): recover the signer
     from the fixture's EIP-191 signature over its own message and pin it
     against the declared seller/signer field. */
  const fxSig = fx.seller_signature || fx.signature || (fx.expected && fx.expected.signature) || null;
  const fxSigner = fx.signer || fx.expected_signer || fx.seller_eoa || f.seller || null;
  if (fxSig && fxSigner) {
    let recovered = "";
    try { recovered = ethers.verifyMessage(fx.message, fxSig); } catch (e) { recovered = `recover-failed: ${e.message}`; }
    eq(String(recovered).toLowerCase(), String(fxSigner).toLowerCase(),
      "L11 EIP-191 recover(fixture message, fixture signature) == fixture signer EOA");
  } else {
    pend("L11 EOA signature recovery", "fixture carries no signature/signer pair — pending relay fix46");
  }

  /* deterministic re-encryption through Node WebCrypto (same primitive
     chain T.custody.encryptEnvelope drives in the browser) */
  const ephPriv = unhex(fx.eph_priv_hex), epkRaw = unb64u(fx.expected.epk_b64url);
  const fxUploadPub = unb64u(fx.upload_pub_b64url);
  const ephKey = await subtle.importKey("jwk",
    { kty: "EC", crv: "P-256", d: b64url(ephPriv), x: b64url(epkRaw.slice(1, 33)), y: b64url(epkRaw.slice(33, 65)), ext: true },
    { name: "ECDH", namedCurve: "P-256" }, false, ["deriveBits"]);
  const pubKey = await subtle.importKey("raw", fxUploadPub, { name: "ECDH", namedCurve: "P-256" }, false, []);
  const z = await subtle.deriveBits({ name: "ECDH", public: pubKey }, ephKey, 256);
  const saltFx = await subtle.digest("SHA-256", new TextEncoder().encode(T.custody.HKDF_SALT_STRING));
  const hkdfFx = await subtle.importKey("raw", z, "HKDF", false, ["deriveBits"]);
  const aesBitsFx = await subtle.deriveBits(
    { name: "HKDF", hash: "SHA-256", salt: saltFx, info: new TextEncoder().encode(T.custody.HKDF_INFO_STRING) }, hkdfFx, 256);
  const aesFx = await subtle.importKey("raw", aesBitsFx, { name: "AES-GCM" }, false, ["encrypt"]);
  const ctFx = await subtle.encrypt(
    { name: "AES-GCM", iv: unb64u(fx.iv_b64url), additionalData: new TextEncoder().encode(fx.aad_ascii), tagLength: 128 },
    aesFx, new TextEncoder().encode(fx.plaintext_json));
  eq(b64url(new Uint8Array(ctFx)), fx.expected.ct_b64url, "L12 ct byte-exact (ECDH·HKDF·AES-GCM parity)");
  eq(b64url(epkRaw), fx.expected.epk_b64url, "L13 epk encoding parity");

  /* receiver side: decrypt the fixture ct with the upload private key */
  const upPriv = unhex(fx.upload_priv_hex);
  const upKey = await subtle.importKey("jwk",
    { kty: "EC", crv: "P-256", d: b64url(upPriv), x: b64url(fxUploadPub.slice(1, 33)), y: b64url(fxUploadPub.slice(33, 65)), ext: true },
    { name: "ECDH", namedCurve: "P-256" }, false, ["deriveBits"]);
  const epkPubKey = await subtle.importKey("raw", epkRaw, { name: "ECDH", namedCurve: "P-256" }, false, []);
  const z2 = await subtle.deriveBits({ name: "ECDH", public: epkPubKey }, upKey, 256);
  const hkdf2 = await subtle.importKey("raw", z2, "HKDF", false, ["deriveBits"]);
  const aesBits2 = await subtle.deriveBits(
    { name: "HKDF", hash: "SHA-256", salt: saltFx, info: new TextEncoder().encode(T.custody.HKDF_INFO_STRING) }, hkdf2, 256);
  const aes2 = await subtle.importKey("raw", aesBits2, { name: "AES-GCM" }, false, ["decrypt"]);
  const ptFx = await subtle.decrypt(
    { name: "AES-GCM", iv: unb64u(fx.iv_b64url), additionalData: new TextEncoder().encode(fx.aad_ascii), tagLength: 128 },
    aes2, unb64u(fx.expected.ct_b64url));
  eq(new TextDecoder().decode(ptFx), fx.plaintext_json, "L14 receiver decrypt == fixture plaintext");

  eq(await sha256Hex(fxUploadPub), Buffer.from(await subtle.digest("SHA-256", fxUploadPub)).toString("hex"),
    "L15 uploadPubkeySha256 pin derivation matches an independent digest");
  ok(!fx.expected.epk_b64url.includes("=") && !fx.expected.ct_b64url.includes("="),
    "L16 fixture encodings are unpadded base64url (the wire dialect)");
}
/* ═══ M. catalog resume eligibility (canResumeCatalog) + console wiring ═══
   C1 regression: refreshCustodyStatus used to gate the relay-stored
   catalog resume on `custodyState.gen === 0` — but resetCustodySession
   bumps gen on EVERY connect (finishConnect → refreshCustodySession), so
   after the very first connect the gate was unreachable: a refresh or
   reconnect with a stored key showed pins green yet never re-anchored
   the catalog / unlocked PUBLISH until the key was re-pasted.
   The eligibility matrix now lives in T.custody.canResumeCatalog and
   console.js wires it with the session baseline (sessionStartGen) + a
   failure marker (sessionFailed) + the request generation/address.
   The predicate is driven here through the positive matrix (first
   connect / page refresh / reconnect) and the negative matrix (URL/key
   edit, failed VERIFY, pins, in-flight race, old-account late return,
   same-address disconnect-reconnect late return, no key, empty
   catalog, anchor present). The page module itself needs a live DOM +
   injected wallet provider and cannot run under Node — its wiring is
   pinned by static evidence below, the same discipline as section K. */
console.log("\n== M. resume eligibility (canResumeCatalog) + wiring ==");
{
  const me = sellerWallet.address.toLowerCase();
  const other = relayWallet.address.toLowerCase();
  /* a fresh connection session — exactly the state resetCustodySession
     leaves behind, with the /status answer landing on the live wallet */
  const fresh = () => ({
    anchor: null, sessionFailed: false,
    gen: 5, sessionStartGen: 5, pinsOk: true,
    requestGen: 5, requestAddress: me, currentAddress: me,
    hasKey: true, entryCount: 3,
  });
  ok(T.custody.canResumeCatalog(fresh()).ok, "M1 first connect — untouched session + stored key resumes");
  ok(T.custody.canResumeCatalog({ ...fresh(), gen: 9, sessionStartGen: 9, requestGen: 9 }).ok,
    "M2 page refresh (F5) — new page, new session baseline resumes");
  ok(T.custody.canResumeCatalog({ ...fresh(), gen: 6, sessionStartGen: 6, requestGen: 6 }).ok,
    "M3 reconnect same wallet — re-baselined session resumes");

  const neg = (mut, name) => ok(!T.custody.canResumeCatalog({ ...fresh(), ...mut }).ok, name);
  neg({ gen: 6 }, "M4 URL/key edit this session (gen > sessionStartGen) → no silent resume");
  neg({ sessionFailed: true }, "M5 a VERIFY failed this session → no resume");
  neg({ pinsOk: false }, "M6 deployment pins not verified → no resume");
  neg({ requestGen: 4 }, "M7 status request raced a gen bump (edit or reset) → no resume");
  neg({ requestAddress: other }, "M8 old account's late /status return → no resume");
  neg({ gen: 6, sessionStartGen: 6, requestGen: 4 },
    "M9 same-address disconnect-reconnect late return (request gen from the previous session, address equal) → no resume");
  neg({ hasKey: false }, "M10 relay stores no key → nothing to resume");
  neg({ entryCount: 0 }, "M11 relay-stored catalog empty → no resume");
  neg({ anchor: { kind: "verified", gen: 5, address: me } }, "M12 anchor already present → no resume path taken");
  neg({ hasKey: "yes" }, "M13 non-boolean has_key fails closed (no truthy coercion)");

  const reasons = [{ sessionFailed: true }, { gen: 6 }, { pinsOk: false }, { requestGen: 4 },
    { requestAddress: other }, { hasKey: false }, { entryCount: 0 }, { anchor: {} }]
    .map((mut) => T.custody.canResumeCatalog({ ...fresh(), ...mut }).reason);
  ok(reasons.every((r) => typeof r === "string" && r.length > 0), "M14 every negative path returns a human-readable reason");
  ok(!T.custody.canResumeCatalog(null).ok && typeof T.custody.canResumeCatalog(null).reason === "string",
    "M15 null/absent input fails closed");
  ok(!T.custody.canResumeCatalog({ ...fresh(), currentAddress: "" }).ok, "M16 empty current address fails closed");
  ok(T.custody.canResumeCatalog({ ...fresh(), currentAddress: sellerWallet.address }).ok,
    "M17 checksummed (EIP-55) current address matches lowercase request — case-insensitive compare");

  /* ── console wiring evidence (static; same discipline as section K):
     the broken gen === 0 gate is gone and the wired call passes every
     input the predicate needs — session baseline, failure marker,
     request generation/address, live wallet, relay truth ── */
  const js = readFileSync(join(ROOT, "web/console.js"), "utf8");
  const fnSlice = (name) => {
    const start = js.indexOf(`function ${name}`);
    if (start < 0) return "";
    let depth = 0, opened = false, out = "";
    for (let p = js.indexOf("{", start); p < js.length; p++) {
      const c = js[p];
      out += c;
      if (c === "{") { depth++; opened = true; }
      else if (c === "}") { depth--; if (opened && depth === 0) break; }
    }
    return out;
  };
  const rcs = fnSlice("resetCustodySession");
  const rstat = fnSlice("refreshCustodyStatus");
  const cfail = fnSlice("custodyFail");
  ok(!js.includes("custodyState.gen === 0"), "M18 the broken gen === 0 resume gate is gone from console.js");
  ok(/gen\+\+\s*;\s*custodyState\.sessionStartGen = custodyState\.gen;/.test(rcs),
    "M19 resetCustodySession re-baselines sessionStartGen immediately after the gen bump");
  ok(rcs.includes("custodyState.sessionFailed = false"), "M20 resetCustodySession clears the failure marker (fresh session)");
  ok(cfail.includes("custodyState.sessionFailed = true"), "M21 custodyFail marks the session failed (blocks resume)");
  ok(rstat.includes("T.custody.canResumeCatalog({") && rstat.includes("const g0 = custodyState.gen;"),
    "M22 refreshCustodyStatus wires the predicate and captures the request generation");
  const passKeys = ["anchor: custodyState.anchor", "sessionFailed: custodyState.sessionFailed",
    "gen: custodyState.gen", "sessionStartGen: custodyState.sessionStartGen", "pinsOk: custodyState.pinsOk",
    "requestGen: g0", "requestAddress: me", "currentAddress: state.address",
    "hasKey: !!r.body.has_key", "entryCount: entries.length"];
  const missKeys = passKeys.filter((k) => !rstat.includes(k));
  ok(missKeys.length === 0, "M23 the wired call passes every predicate input", missKeys.join(", "));
  ok(rstat.includes("if (elig.ok) {") && rstat.includes('kind: "resumed"'),
    "M24 the anchor is only applied when the predicate accepts (elig.ok)");
  ok(fnSlice("markCustodyEdited").includes("custodyState.gen++"),
    "M25 markCustodyEdited still bumps gen on every keystroke (edits invalidate resume + anchors)");
  ok(fnSlice("refreshCustodySession").includes("resetCustodySession()"),
    "M26 every connect still runs reset → refresh (the session baseline is re-established per connection)");
}

/* ═══ N. live shared-relay wiring (config pins + probe/attestation/reportData) ═══
   Deployment record 2026-10-08: web/config.js now carries the two live
   identity facts (expectedSigner + uploadPubkeySha256) inside the m15
   active m15 block (activated 2026-10-08; B9 enforces the full pin set —
   every field present, correctly formatted, placeholder-free). The
   runtime contract those pins feed: console.js probes the live relay on
   EVERY custody path via T.custody.probeSharedRelay, which fetches
   /info and /attestation in parallel and hard-fails (DEPLOYMENT
   MISMATCH, fail-closed) when either is missing; checkRelayPins
   compares appId against the /attestation QUOTE, origin / signer /
   uploadPubkeySha256 against /info.shared, and re-derives
   reportData = signer(20B) ‖ zeros(12B) ‖ keccak256(uploadPub65)(32B)
   with the injected (vendored ethers) keccak. Static evidence only —
   the same discipline as sections K/M. */
console.log("\n== N. live shared-relay wiring ==");
{
  const cfgSrc = readFileSync(join(ROOT, "web/config.js"), "utf8");
  const js = readFileSync(join(ROOT, "web/console.js"), "utf8");
  const commonSrc = readFileSync(join(ROOT, "web/common.js"), "utf8");
  const PIN_SIGNER = "0x49CA3eADB3b8F23623f735918A6F2b945F60fb2D";
  const PIN_UPLOAD_SHA = "b2d216f2037b78b15976cfdb6ae5ea02c4babf7fd0030c2772291cd824314e5d";

  /* deployment record: the two filled pins are present and placeholder-free */
  ok(cfgSrc.includes(`expectedSigner: "${PIN_SIGNER}"`),
    "N1 config.js carries the live expectedSigner pin", PIN_SIGNER);
  ok(cfgSrc.includes(`uploadPubkeySha256: "${PIN_UPLOAD_SHA}"`) && /^[0-9a-f]{64}$/.test(PIN_UPLOAD_SHA),
    "N2 config.js carries the live uploadPubkeySha256 pin (64 lowercase hex)");
  ok(!cfgSrc.includes("0xEXPECTED_SIGNER_FILL_AFTER_DEPLOYMENT") &&
      !cfgSrc.includes("UPLOAD_PUBKEY_SHA256_FILL_AFTER_DEPLOYMENT"),
    "N3 the two filled fields no longer carry *_FILL_AFTER_DEPLOYMENT placeholders");

  /* console.js: BOTH custody paths (auto status refresh + runVerify)
     re-probe the live relay through the one shared entry point, and a
     failed probe flips pinsOk off before anything can anchor */
  const probeCalls = js.split("T.custody.probeSharedRelay(").length - 1;
  ok(probeCalls >= 2 && js.split("renderPins(pr.rows)").length - 1 >= 2,
    "N4 console.js probes the live relay + renders pin rows on every custody path", `${probeCalls} probe call sites`);
  ok(/custodyState\.pinsOk = pr\.ok;[\s\S]{0,160}?if \(!pr\.ok\)/.test(js),
    "N5 a failed probe hard-fails (pinsOk=false → DEPLOYMENT MISMATCH, no catalog anchor)");

  /* common.js implementation points the probe delegates to */
  const fnSlice = (name, src) => {
    const start = src.indexOf(`function ${name}`);
    if (start < 0) return "";
    let depth = 0, opened = false, out = "", parens = 0, seenParen = false;
    const parenStart = src.indexOf("(", start);
    if (parenStart < 0) return "";
    for (let p = parenStart; p < src.length; p++) {
      const c = src[p];
      out += c;
      if (c === "(") { parens++; seenParen = true; }
      else if (c === ")") { parens--; if (seenParen && parens === 0) break; } /* param list closed */
    }
    for (let p = src.indexOf("{", parenStart + out.length); p < src.length; p++) {
      const c = src[p];
      out += c;
      if (c === "{") { depth++; opened = true; }
      else if (c === "}") { depth--; if (opened && depth === 0) break; }
    }
    return out;
  };
  const probe = fnSlice("probeSharedRelay", commonSrc);
  const pins = fnSlice("checkRelayPins", commonSrc);
  ok(probe.includes('joinUrl(cfg.relayOrigin, "/info")') && probe.includes('joinUrl(cfg.relayOrigin, "/attestation")'),
    "N6 probeSharedRelay fetches /info + /attestation from the pinned origin");
  ok(probe.includes("Promise.all") && /!attR\.ok \|\| !attR\.body/.test(probe) && probe.includes("fail-closed"),
    "N7 the two fetches run in parallel and a missing /attestation fails closed");
  ok(pins.includes("att.appId") && pins.includes('row("app-id"'),
    "N8 appId is compared from the /attestation quote (never from /info.shared)");
  ok(pins.includes('row("origin"') && pins.includes('row("signer"') && pins.includes('row("upload-key"'),
    "N9 origin / signer / uploadPubkeySha256 are still compared from /info.shared");
  ok(pins.includes("expect.set(hexToBytes(cfg.expectedSigner), 0)") && pins.includes("keccak256Hex(pub)") &&
    pins.includes('row("report-data"'),
    "N10 reportData binding re-derived as signer ‖ zeros ‖ keccak256(uploadPub) with the injected keccak");
  ok(!pins.includes("attestedAt"),
    "N11 attestedAt is never a pin input (display metadata only — its absence skips nothing)");
}

console.log(`\n== custody-web: ${pass + fail} checks · PASS ${pass} · FAIL ${fail}` +
  (pendings.length ? ` · PEND ${pendings.length} (interop dependency — NOT counted as pass)` : "") + " ==");
if (pendings.length) { console.log("pending: " + pendings.join(" | ")); }
if (failures.length) { console.log("failed: " + failures.join(" | ")); process.exit(1); }
process.exit(0);
