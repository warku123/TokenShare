/* ═══════════════════════════════════════════════════════════
   TOKENSHARE — deployment configuration surface.
   The ONLY file in web/ where chain facts live (same nature as
   a .env). Everything else derives from window.TS_CONFIG.

   Fill after Monad testnet deploy:
     contracts/deployed.json  →  escrowAddr / registryAddr
     (usdc below is the official Circle USDC on Monad testnet)
   ═══════════════════════════════════════════════════════════ */
window.TS_CONFIG = {
  chainId: 10143,
  chainName: "Monad Testnet",
  nativeCurrency: { name: "Monad", symbol: "MON", decimals: 18 },
  rpcUrl: "https://testnet-rpc.monad.xyz",
  explorer: "https://testnet.monadscan.com",

  /* TODO(deploy): paste from contracts/deployed.json after
     forge script script/Deploy.s.sol --sig run(string) monad_testnet */
  escrowAddr: "",      // deployed.json .escrow
  registryAddr: "",    // deployed.json .registry

  /* official Circle USDC on Monad testnet (fixed chain fact) */
  usdcAddr: "0x534b2f3A21130d7a60830c2Df862319e593943A3",

  /* seller operator addresses listed on the market page.
     TODO(deploy): add registered seller addresses, e.g.
     sellers: ["0x1234…abcd"], */
  sellers: [],
};
