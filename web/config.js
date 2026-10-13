/* ═══════════════════════════════════════════════════════════
   TOKENSHARE — deployment configuration surface.
   The ONLY file in web/ where chain facts live (same nature as
   a .env). Everything else derives from window.TS_CONFIG.

   部署快照 2026-10-07（contracts/deployed.monad.json，Escrow v3.2），重部署时同步更新。
   ═══════════════════════════════════════════════════════════ */
window.TS_CONFIG = {
  chainId: 10143,
  chainName: "Monad Testnet",
  nativeCurrency: { name: "Monad", symbol: "MON", decimals: 18 },
  rpcUrl: "https://testnet-rpc.monad.xyz",
  explorer: "https://testnet.monadvision.com",

  escrowAddr: "0xe4D5Eb0dBDB6DB8063C07ECF7EDFcCdDB9Ad514c",
  registryAddr: "0xeD347cDc1761750E20C024459b38dedFb1462254",

  /* official Circle USDC on Monad testnet (fixed chain fact) */
  usdcAddr: "0x534b2f3A21130d7a60830c2Df862319e593943A3",

  /* seller operator addresses listed on the market page — FALLBACK ONLY.
     Registry enumeration is primary; models/prices always come from the
     chain via getListing — this file carries no price data.
     Snapshot 2026-10-13 (chainId 10143, Registry sellerCount=3):
     · 0x38c2…Dd6  Kimi, active   — k3-256k 30/60/90 ·
       kimi-for-coding-highspeed 15/30/45          (USDC per 1M tokens)
     · 0xD617…Ba39 Qwen, active   — qwen3.8-max 2/4/36 ·
       qwen3.8-flash 1/2/3                         (USDC per 1M tokens)
     raw 6-decimal native values: 30000000/60000000/90000000 ·
     15000000/30000000/45000000 · 2000000/4000000/36000000 ·
     1000000/2000000/3000000.
     The old Qwen wallet 0x1F1A…c687 was deactivated by its seller on
     2026-10-09 (listing data retained, active=false) — deliberately NOT
     listed here; the third on-chain seller is that inactive record. */
  sellers: [
    "0x38c26E1782b7E6F656Aa35EbF473e2ff0D718Dd6",
    "0xD6172dC960D1533e49A367253FF9Db01AE83Ba39",
  ],

  // ── M15 shared-relay custody (ACTIVE — live TEE deployment) ──
  //
  // This deployment runs SHARED mode: the console shows the shared
  // custody form (upstream base URL + API key; NO relay endpoint
  // field — the endpoint comes from these trusted pins).
  //
  // Every identity pin is mandatory — a present but partial or
  // malformed block fails closed (VERIFY disabled, hard error shown).
  // Never fill these from a relay's own /info response (no TOFU):
  // values come from the deployment record + the Phala verifier.
  //
  // STATE: this block is ACTIVE — the live deployment pins below are
  // what the console enforces. To fall back to the classic single-
  // seller form (own relay endpoint + LOAD FROM RELAY), comment out
  // the whole m15 block: an absent or incomplete block re-arms the
  // fail-closed validator and hides the custody card.
  //
  m15: {
    mode: "shared",
    relayOrigin: "https://c30652f4833465adda9bdc7ac557e8b45e8e66c6-8787.dstack-pha-prod5.phala.network",
    expectedSigner: "0x49CA3eADB3b8F23623f735918A6F2b945F60fb2D",
    appId: "c30652f4833465adda9bdc7ac557e8b45e8e66c6",
    uploadPubkeySha256: "b2d216f2037b78b15976cfdb6ae5ea02c4babf7fd0030c2772291cd824314e5d",
    attestEvidence: {
      verifier: "https://cloud-api.phala.com/api/v1/attestations/verify",
      verifiedAt: "2026-10-12",
      digest: "ce36139c54f2cf3a75ea748c973e9646986aed8c45c63ccb73077646c6607c56",
    },
    //
    // codeMeasure — enclave code-fingerprint pins (PHASE 1, soft).
    // Shape: TDX measurement registers; every value is 0x + 96 hex
    // (48 bytes): rtmr0..rtmr3 + mrseam. Source: quote.body.rtmr0-3 /
    // mrseam of a verified quote — the same fields the relay now
    // returns under /attestation.quoteBody, so the console compares
    // pins against what the relay reports live.
    // Stage semantics (validated in web/common.js validateM15Config):
    //   · UNCONFIGURED (all null, as shipped today) → skipped
    //     entirely; the identity pins above remain the complete
    //     phase-1 gate (relayOrigin · expectedSigner · appId ·
    //     uploadPubkeySha256 · attestEvidence).
    //   · CONFIGURED → the console attestation panel compares each
    //     value against the relay-reported registers and renders a
    //     YELLOW "code fingerprint mismatch" row on a miss. Soft by
    //     design: it never blocks VERIFY, never enters the pinsOk
    //     gate, and a malformed value is a soft signal, not a
    //     fail-closed one.
    // TODO phase-2: promote to hard pins once the measurements are
    // proven stable across relay restarts/boots (a redeploy changes
    // every rtmr — until that churn is pinned down a hard gate would
    // fail closed on legitimate image swaps).
    codeMeasure: {
      rtmr0: null,
      rtmr1: null,
      rtmr2: null,
      rtmr3: null,
      mrseam: null,
    },
  },
  //
  // appId provenance (live deployment record 2026-10-08): the appId
  // above is the TOP-LEVEL appId of the live /attestation response —
  // the Phala cloud app_id of the running image. The OLD value
  // 5460de744533436d4f14b700e058feac33a19025 was the dstack
  // guest/compose identity; after the image swap it is no longer part
  // of the response, and pinning it now yields DEPLOYMENT MISMATCH
  // (fail-closed). appId comes from the /attestation quote top level,
  // NEVER from /info. expectedSigner + uploadPubkeySha256 (sha256 of
  // the 65-byte uncompressed point) are unchanged — the signer/upload
  // identity survived the image swap (re-verified via /info).
  //
  // attestEvidence records the OFFLINE verification: Phala cloud
  // verifier, 2026-10-12 — full quote re-verified online (success:true,
  // quote.body.reportdata === /attestation reportData; quoteDigest =
  // keccak256 of the raw quote bytes, checked locally). 2026-10-12
  // re-verify record: dstack rotates the quoteDigest on EVERY call/boot
  // (per-call timestamp in the quote), so this digest is the snapshot of
  // the pinned verification run — the live /attestation will almost
  // always differ (the console attestation panel flags that as a soft
  // warning, never a failure). The digest stays BARE 64-hex (no 0x) —
  // scripts/check-custody-web.mjs B9g enforces that format. Previous
  // record: 2026-10-08 · 9d262b90…905. After any image/boot change,
  // re-verify the quote offline and refresh verifiedAt + digest.
};
