/**
 * TokenShare — Settlement Audit Anchor (Chainlink CRE workflow, TypeScript)
 *
 * Bounty track: "Best Workflow with CRE" (Chainlink, $3k). Flow = M7 素材节
 * 构想 #1 + #3 合一 (settlement audit anchor + receipt notary):
 *
 *   1. TRIGGER (index 0): Escrow.Settled log on Monad testnet (chain 10143).
 *   2. EVM READ (EXACT TRIGGER BLOCK): every read references the Settled
 *      log's own blockNumber — the block-END state the settlement priced
 *      against. Missing blockNumber ⇒ fail closed. This file is the
 *      composition root (network + real capability clients); the handler
 *      and all pure verification/adjudication logic live under src/:
 *      src/abi.ts (pinned ABI subsets), src/config.ts (zod config schema,
 *      incl. the trusted receipt signer), src/receipt.ts (EIP-712 receipt
 *      schema + authenticated recovery), src/pricing.ts (PIN formula + ±1
 *      verdict), src/payload.ts (seven-field report wire format),
 *      src/handler.ts (fail-closed handler with a testable capability seam).
 *   3. CONFIDENTIAL HTTP: GET {relayBaseUrl}/receipt/{paymentId} — the relay's
 *      EIP-712 signed receipt. The endpoint is anonymous (HTTP 200 without any
 *      Authorization check); the {{.receipts_bearer}} header stays optional
 *      plumbing — template-resolved inside the enclave (simulation: value
 *      injected from .env; deployed: Vault DON) — and is NOT authentication.
 *      Receipt authenticity is proven solely by its EIP-712 signature.
 *   4. VERIFY  : signature recovers EXACTLY to config.receiptSignerAddress
 *      (the R2 shared signer — preconfigured in config, never taken from
 *      the receipt). Receipt JSON must be an exact safe-integer/6dp schema
 *      against the pinned receipt.py contract; paymentId must equal the
 *      trigger's; receipt seller == event seller == getPayment seller;
 *      event buyer == getPayment buyer; payment state == Settled. Price for
 *      the RECEIPT's model is read at the exact trigger block via
 *      Registry.getPrice — STRICT, no fallback (no Listing read, no fallback
 *      height; any transport/revert/decode/model failure ⇒ NO report,
 *      strict fail-closed, ora39 2026-10-08); expected =
 *      (cached*cachedIn + (prompt-cached)*input + completion*output) // 1e6,
 *      ±1 native-unit tolerance. A good signed receipt with an amount
 *      mismatch still anchors, with verdict=MISMATCH; auth/schema failures
 *      anchor NOTHING. The verdict is receipt-vs-estimate; the anchored
 *      record still carries the on-chain Escrow settled amount in
 *      settledAmount (see cre/README.md "Verdict semantics").
 *   5. WRITE   : runtime.report(prepareReportRequest(<seven abi-encoded
 *      business fields>)) — NOT encodeFunctionData(onReport(...)) calldata —
 *      so the official KeystoneForwarder routes exactly those bytes to
 *      ReceiptAnchor.onReport's second argument, where the consumer abi-
 *      decodes the same seven fields it has since M9. ReceiptAnchor stores
 *      keccak(signature) + amounts + verdict, emitting ReceiptAnchored (and
 *      DiscrepancyFlagged on mismatch) so the audit trail is permanent.
 *
 * Trigger surface: exactly ONE trigger (Settled) → `--trigger-index 0`.
 * Single payment per invocation; any failure raises NO report. There is no
 * cross-payment aggregation and no cumulative audit variant.
 *
 * Monad testnet facts (verified 2026-10-07):
 *   chain id 10143, rpc https://testnet-rpc.monad.xyz (project.yaml)
 *   escrow   0xe4D5Eb0dBDB6DB8063C07ECF7EDFcCdDB9Ad514c (Escrow v3.2)
 *   registry 0xeD347cDc1761750E20C024459b38dedFb1462254 (Registry v4)
 * The authoritative source for these addresses is always
 * contracts/deployed.monad.json — refresh this header + both config.*.json
 * files after any redeployment.
 */
import {
  ConfidentialHTTPClient,
  EVMClient,
  getNetwork,
  handler,
  logTriggerConfig,
  Runner,
} from "@chainlink/cre-sdk"
import { SETTLED_TOPIC0 } from "./src/abi"
import { configSchema, type Config } from "./src/config"
import { makeOnSettled, type OnSettledDeps } from "./src/handler"

// ─── Workflow definition ────────────────────────────────────────────────────
function initWorkflow(config: Config) {
  const network = getNetwork({
    chainFamily: "evm",
    chainSelectorName: config.chainSelectorName,
    isTestnet: true,
  })
  if (!network) throw new Error(`Network not found: ${config.chainSelectorName}`)

  const evm = new EVMClient(network.chainSelector.selector)
  const deps: OnSettledDeps = {
    evm,
    http: new ConfidentialHTTPClient(),
  }
  return [
    handler(
      evm.logTrigger(
        logTriggerConfig({
          addresses: [config.escrowAddress as `0x${string}`],
          topics: [[SETTLED_TOPIC0]], // topic0 only: any Escrow.Settled
        }),
      ),
      makeOnSettled(deps),
    ),
  ]
}

export async function main() {
  const runner = await Runner.newRunner<Config>({ configSchema })
  await runner.run(initWorkflow)
}

main()
