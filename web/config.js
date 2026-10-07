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

  /* seller operator addresses listed on the market page */
  sellers: [
    "0x38c26E1782b7E6F656Aa35EbF473e2ff0D718Dd6",
    "0x1F1A8C5c1E56eD339C8021a0396095a724aca687",
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
      verifiedAt: "2026-10-08",
      digest: "9d262b9036f3fb7a9ff5b2b15dcd6c1089a9558df22bc3af85e53b2c2a191905",
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
  // verifier, 2026-10-08, full quote digest 9d262b90…905 (from the
  // deployment audit record). A TDX boot rotates the quoteDigest —
  // after any image/boot change, re-verify the quote offline and
  // refresh verifiedAt + digest from the new verification record.
};
