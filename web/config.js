/* ═══════════════════════════════════════════════════════════
   TOKENSHARE — deployment configuration surface.
   The ONLY file in web/ where chain facts live (same nature as
   a .env). Everything else derives from window.TS_CONFIG.

   部署快照 2026-09-23（contracts/deployed.monad.json），重部署时同步更新。
   ═══════════════════════════════════════════════════════════ */
window.TS_CONFIG = {
  chainId: 10143,
  chainName: "Monad Testnet",
  nativeCurrency: { name: "Monad", symbol: "MON", decimals: 18 },
  rpcUrl: "https://testnet-rpc.monad.xyz",
  explorer: "https://testnet.monadscan.com",

  escrowAddr: "0x654c83F23669908C867f02EF3E20B2126c4753De",
  registryAddr: "0x27c7128F7290653f104E3080cf17576706F6b77A",

  /* official Circle USDC on Monad testnet (fixed chain fact) */
  usdcAddr: "0x534b2f3A21130d7a60830c2Df862319e593943A3",

  /* seller operator addresses listed on the market page */
  sellers: ["0x38c26E1782b7E6F656Aa35EbF473e2ff0D718Dd6"],
};
