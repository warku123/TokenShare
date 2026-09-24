/**
 * TokenShare — Settlement Audit Anchor (Chainlink CRE workflow, TypeScript)
 *
 * Bounty track: "Best Workflow with CRE" (Chainlink, $3k). Flow = M7 素材节
 * 构想 #1 + #3 合一 (settlement audit anchor + receipt notary):
 *
 *   1. TRIGGER (index 0): Escrow.Settled log on Monad testnet (chain 10143).
 *   2. EVM READ: Escrow.getPayment(paymentId) → buyer/max/expiresAt/state,
 *                Registry.getListing(seller) → Listing v2 {operator, endpoint,
 *                models[], prices[] (parallel per-model 3-tier, 6-dp USDC),
 *                active}.
 *   3. CONFIDENTIAL HTTP: GET {relayBaseUrl}/receipt/{paymentId} — the relay's
 *                EIP-712 signed receipt. The bearer credential is template-
 *                resolved inside the enclave via {{.receipts_bearer}}
 *                (simulation: value injected from .env; deployed: Vault DON).
 *   4. VERIFY  : receipt.message.actualAmount vs the on-chain PER-MODEL
 *                estimate. Price is resolved for the RECEIPT's model
 *                (Registry v2): primary getPrice(operator, model) — on-chain
 *                authoritative, reverts NotActive/ModelNotFound — with the
 *                Listing models/prices parallel arrays as fallback; expected =
 *                (cached*cachedIn + (prompt-cached)*input + completion*output)
 *                // 1e6, ±1 native-unit tolerance (0.000001 USDC rounding
 *                slop). Price is read at the TRIGGER block (log.blockNumber)
 *                to avoid the last-finalized updateModelPrice race. A receipt
 *                model missing on-chain ⇒ MODEL MISSING conclusion (logged,
 *                nothing anchored) instead of a crash. The verdict is
 *                receipt-vs-estimate; the anchored record still carries the
 *                on-chain Escrow settled amount in settledAmount (see
 *                README "Verdict semantics" for the clamping divergence).
 *                This closes the trust gap a relay can never self-close —
 *                the DON, not the seller, adjudicates the settlement.
 *   5. WRITE   : evm.writeReport → ReceiptAnchor.onReport via the Keystone
 *                forwarder. ReceiptAnchor stores keccak(signature) + amounts +
 *                verdict, emitting ReceiptAnchored (and DiscrepancyFlagged on
 *                mismatch) so the audit trail is on-chain and permanent.
 *
 * Trigger surface: exactly ONE trigger (Settled) → `--trigger-index 0`.
 * Indices are 0-based per the CLI reference; official multi-trigger examples
 * use 1 only when the log handler is the second entry.
 *
 * Monad testnet facts (verified 2026-09-23):
 *   chain id 10143, rpc https://testnet-rpc.monad.xyz (project.yaml)
 *   escrow   0x654c83F23669908C867f02EF3E20B2126c4753De
 *   registry 0x27c7128F7290653f104E3080cf17576706F6b77A (Registry v2)
 * The authoritative source for these addresses is always
 * contracts/deployed.monad.json — refresh this header + both config.*.json
 * files after any redeployment (the M10 v3 deployment chain will).
 */
import {
  bigintToProtoBigInt,
  bytesToHex,
  ConfidentialHTTPClient,
  EVMClient,
  encodeCallMsg,
  getNetwork,
  handler,
  json,
  LAST_FINALIZED_BLOCK_NUMBER,
  logTriggerConfig,
  ok,
  prepareReportRequest,
  protoBigIntToBigint,
  Runner,
  TxStatus,
  type EVMLog,
  type Runtime,
} from "@chainlink/cre-sdk"
import {
  decodeAbiParameters,
  decodeEventLog,
  encodeAbiParameters,
  encodeFunctionData,
  keccak256,
  parseAbi,
  parseAbiParameters,
  type Address,
  type Hex,
} from "viem"
import { z } from "zod"

// ─── Config ─────────────────────────────────────────────────────────────────
const configSchema = z.object({
  chainSelectorName: z.string(),
  escrowAddress: z.string(),
  registryAddress: z.string(),
  /** ReceiptAnchor consumer (cre/contracts/src/ReceiptAnchor.sol). */
  anchorAddress: z.string(),
  /** TokenShare relay base URL, e.g. http://127.0.0.1:8787 (M5b listing). */
  relayBaseUrl: z.string(),
  /** Sanity ceiling on receipt amounts, 6-dp USDC (1000 USDC default). */
  maxPriceUsd6: z.string(),
  /** Workflow owner EOA — required to scope Vault DON secret requests. */
  workflowOwner: z.string(),
})
type Config = z.infer<typeof configSchema>

// ─── ABIs (subsets of contracts/src/Escrow.sol, contracts/src/Registry.sol) ─
const escrowAbi = parseAbi([
  "event Settled(uint256 indexed paymentId, address indexed buyer, address indexed seller, uint256 actualAmount, uint256 refundedAmount)",
  "function getPayment(uint256 paymentId) view returns (address buyer, address seller, uint256 maxAmount, uint64 expiresAt, uint8 state)",
])
// Registry v2 (M9 ABI PIN): Listing{operator, endpoint, models[], prices[],
// active} — prices is parallel to models; getPrice(operator, model) returns
// the per-model 3-tier price and reverts NotActive/ModelNotFound.
const registryAbi = parseAbi([
  "function getListing(address operator) view returns (address listingOperator, string endpoint, string[] models, (uint256 priceCachedIn, uint256 priceInput, uint256 priceOutput)[] prices, bool active)",
  "function getPrice(address operator, string model) view returns (uint256 priceCachedIn, uint256 priceInput, uint256 priceOutput)",
])
const onReportAbi = parseAbi(["function onReport(bytes metadata, bytes report)"])

/**
 * Relay receipt JSON — pin-exact contract from relay/app/receipt.py:
 *   domain  = {name: "TokenShare Relay", version: "1", chainId}
 *   Receipt = (paymentId, promptTokens, cachedTokens, completionTokens,
 *              actualAmount, seller, upstreamHost, model)
 *   X-Receipt JSON = {domain, message, signature}
 */
type ReceiptJSON = {
  domain: { name: string; version: string; chainId: number }
  message: {
    paymentId: number
    promptTokens: number
    cachedTokens: number
    completionTokens: number
    actualAmount: number
    seller: string
    upstreamHost: string
    model: string
  }
  signature: string
}

/** Per-model 3-tier price row (Registry v2 Price struct, 6-dp USDC native). */
type Price = {
  readonly priceCachedIn: bigint
  readonly priceInput: bigint
  readonly priceOutput: bigint
}

type SettledArgs = {
  paymentId: bigint
  buyer: `0x${string}`
  seller: `0x${string}`
  actualAmount: bigint
  refundedAmount: bigint
}

// keccak256("Settled(uint256,address,address,uint256,uint256)")
const SETTLED_TOPIC0: Hex =
  "0xfee3b370ed9b189860c3135373a45fcc2660969d0c7892c7e952f1ab4b82c7cc"

const ZERO = "0x0000000000000000000000000000000000000000" as Address

// ─── Handler (runs once per Settled log) ────────────────────────────────────
const onSettled = (runtime: Runtime<Config>, log: EVMLog): string => {
  const config = runtime.config

  // 1. Decode the Settled event payload.
  const decoded = decodeEventLog({
    abi: escrowAbi,
    eventName: "Settled" as const,
    data: bytesToHex(log.data),
    topics: log.topics.map((t) => bytesToHex(t)) as [Hex, ...Hex[]],
  })
  const { paymentId, seller, actualAmount } = decoded.args as unknown as SettledArgs
  runtime.log(`Settled: paymentId=${paymentId} seller=${seller} actualAmount=${actualAmount}`)

  // 2. EVM READ — payment context from Escrow and pricing rows from Registry.
  const network = getNetwork({
    chainFamily: "evm",
    chainSelectorName: config.chainSelectorName,
    isTestnet: true,
  })
  if (!network) throw new Error(`Network not found: ${config.chainSelectorName}`)
  const evm = new EVMClient(network.chainSelector.selector)

  const payment = decodeAbiParameters(
    parseAbiParameters("address buyer, address seller, uint256 maxAmount, uint64 expiresAt, uint8 state"),
    evm
      .callContract(runtime, {
        call: encodeCallMsg({
          from: ZERO,
          to: config.escrowAddress as Address,
          data: encodeFunctionData({ abi: escrowAbi, functionName: "getPayment", args: [paymentId] }),
        }),
        blockNumber: LAST_FINALIZED_BLOCK_NUMBER,
      })
      .result().data,
  )
  // Registry v2 Listing — 5 top-level outputs, decoded with the same
  // decodeAbiParameters/parseAbiParameters method as the payment read:
  // [0]=operator, [1]=endpoint, [2]=models, [3]=prices (parallel to models,
  // each {priceCachedIn, priceInput, priceOutput}), [4]=active
  const listing = decodeAbiParameters(
    parseAbiParameters(
      "address listingOperator, string endpoint, string[] models, (uint256 priceCachedIn, uint256 priceInput, uint256 priceOutput)[] prices, bool active",
    ),
    evm
      .callContract(runtime, {
        call: encodeCallMsg({
          from: ZERO,
          to: config.registryAddress as Address,
          data: encodeFunctionData({ abi: registryAbi, functionName: "getListing", args: [seller] }),
        }),
        blockNumber: LAST_FINALIZED_BLOCK_NUMBER,
      })
      .result().data,
  )
  runtime.log(
    `getPayment: buyer=${payment[0]} maxAmount=${payment[2]} expiresAt=${payment[3]} state=${payment[4]}`,
  )
  runtime.log(
    `getListing: models=${listing[2].length} active=${listing[4]}`,
  )
  // 3. CONFIDENTIAL HTTP — relay receipt fetch runs inside the enclave;
  //    {{.receipts_bearer}} is template-resolved there (never in workflow
  //    memory). Simulation resolves it from .env (RECEIPTS_BEARER_ALL).
  const receiptUrl = `${config.relayBaseUrl}/receipt/${paymentId}`
  const confHTTP = new ConfidentialHTTPClient()
  const response = confHTTP
    .sendRequest(runtime, {
      request: {
        url: receiptUrl,
        method: "GET" as const,
        multiHeaders: {
          Authorization: { values: ["Bearer {{.receipts_bearer}}"] },
        },
        // Protocol max is 10s; 5s is ample for a local relay round-trip.
        timeout: { seconds: "5", nanos: 0 },
        // encryptOutput keeps the response confidential across the DON by
        // AES-GCM encrypting it for an EXTERNAL decryptor (the buyer's own
        // backend — never inside the workflow). This workflow must read the
        // receipt itself to adjudicate, so output stays plaintext here; the
        // receipt is public data anyway (EIP-712 signed, verifiable offline).
        // To hand the receipt to a backend instead: set `encryptOutput: true`
        // and add { key: "san_marino_aes_gcm_encryption_key" } to
        // vaultDonSecrets, then decrypt `nonce||ciphertext||tag` off-workflow.
        encryptOutput: false,
      },
      vaultDonSecrets: [
        { key: "receipts_bearer", owner: config.workflowOwner },
      ],
    })
    .result()

  if (!ok(response)) {
    throw new Error(`Receipt fetch failed: HTTP ${response.statusCode} for ${receiptUrl}`)
  }
  const receipt = json(response) as unknown as ReceiptJSON
  runtime.log(
    `Receipt: actualAmount=${receipt.message.actualAmount} upstreamHost=${receipt.message.upstreamHost} model=${receipt.message.model}`,
  )

  // 4. COMPARE — arbitrate receipt vs chain (DON is the referee, not the relay).
  //    Registry v2: the price is taken PER THE RECEIPT'S MODEL. Primary =
  //    getPrice(operator, model) — on-chain authoritative, reverts
  //    NotActive/ModelNotFound; fallback = locate the model in the Listing
  //    models/prices parallel arrays (covers NotActive listings too).
  //    Model missing on-chain ⇒ MODEL MISSING conclusion (no crash, nothing
  //    anchored — the DON cannot adjudicate a price that does not exist).
  const receiptAmount = BigInt(receipt.message.actualAmount)
  const ceiling = BigInt(config.maxPriceUsd6)
  if (receiptAmount > ceiling) {
    throw new Error(`Receipt amount ${receiptAmount} exceeds sanity ceiling ${ceiling} — refusing to anchor`)
  }
  const model = receipt.message.model
  // Price is read AT THE TRIGGER BLOCK (log.blockNumber), not at
  // LAST_FINALIZED_BLOCK_NUMBER: the Escrow.Settled log block is the exact
  // state the settlement priced against. Reading at last-finalized instead
  // opens a finality race — a Registry.updateModelPrice landing inside the
  // ~600ms finality window after settle would silently change the estimate
  // and fabricate a MISMATCH that gets anchored permanently on-chain.
  const triggerBlockNumber = log.blockNumber
    ? bigintToProtoBigInt(protoBigIntToBigint(log.blockNumber))
    : LAST_FINALIZED_BLOCK_NUMBER
  let price: Price | undefined
  try {
    price = decodeAbiParameters(
      parseAbiParameters("(uint256 priceCachedIn, uint256 priceInput, uint256 priceOutput)"),
      evm
        .callContract(runtime, {
          call: encodeCallMsg({
            from: ZERO,
            to: config.registryAddress as Address,
            data: encodeFunctionData({ abi: registryAbi, functionName: "getPrice", args: [seller, model] }),
          }),
          blockNumber: triggerBlockNumber,
        })
        .result().data,
    )[0]
    runtime.log(
      `getPrice(${model}): cachedIn=${price.priceCachedIn} input=${price.priceInput} output=${price.priceOutput}`,
    )
  } catch {
    const idx = listing[2].indexOf(model)
    if (idx >= 0) {
      price = listing[3][idx]
      runtime.log(`getPrice reverted — using listing.models[${idx}] parallel price`)
    }
  }
  if (!price) {
    runtime.log(`MODEL MISSING: ${model} not on Registry — reconciliation inconclusive, nothing anchored`)
    return `MODEL MISSING payment ${paymentId}: model=${model} not found on Registry (getPrice reverted, absent from listing.models) — nothing anchored`
  }
  // PIN pricing formula (integer floor via bigint division):
  // expected = (cached*cachedIn + (prompt-cached)*input + completion*output) // 1e6
  const promptTokens = BigInt(receipt.message.promptTokens)
  const cachedTokens = BigInt(receipt.message.cachedTokens)
  const completionTokens = BigInt(receipt.message.completionTokens)
  const expected =
    (cachedTokens * price.priceCachedIn +
      (promptTokens - cachedTokens) * price.priceInput +
      completionTokens * price.priceOutput) /
    1_000_000n
  const delta = receiptAmount > expected ? receiptAmount - expected : expected - receiptAmount
  const verdict = delta <= 1n ? 1 : 2 // 1=MATCH, 2=MISMATCH (Contract enum order)
  runtime.log(
    `Compare: model=${model} expected=${expected} receipt=${receiptAmount} (onchain settled=${actualAmount}) → ${verdict === 1 ? "MATCH" : `MISMATCH (delta=${delta})`}`,
  )

  // 5. WRITE — ABI-encoded audit payload routed to ReceiptAnchor.onReport
  //    through the CRE writeReport consensus path: DON signs the report
  //    (runtime.report), then the EVM capability submits it to the forwarder.
  //    The DON verdict (step 4) is carried IN the report; ReceiptAnchor
  //    anchors it verbatim instead of recomputing settled-vs-receipt, because
  //    those two comparisons legitimately diverge — see cre/README.md
  //    "Verdict semantics" (Escrow clamping + price-finality race).
  const report: Hex = encodeAbiParameters(
    parseAbiParameters(
      "uint256 paymentId, uint256 settledAmount, uint256 receiptAmount, bytes32 receiptHash, string upstreamHost, string model, uint8 verdict",
    ),
    [
      paymentId,
      actualAmount,
      receiptAmount,
      keccak256(receipt.signature as Hex),
      receipt.message.upstreamHost,
      receipt.message.model,
      verdict,
    ],
  )
  const metadata: Hex = encodeAbiParameters(parseAbiParameters("uint256 workflowVersion"), [1n])
  const signedReport = runtime
    .report(
      prepareReportRequest(
        encodeFunctionData({
          abi: onReportAbi,
          functionName: "onReport",
          args: [metadata, report],
        }),
      ),
    )
    .result()
  const writeReply = evm
    .writeReport(runtime, {
      receiver: config.anchorAddress as Address,
      report: signedReport,
    })
    .result()
  if (writeReply.txStatus !== TxStatus.SUCCESS) {
    throw new Error(`Anchor write failed: ${writeReply.errorMessage ?? writeReply.txStatus}`)
  }
  const txHash = writeReply.txHash ? bytesToHex(writeReply.txHash) : "0x (dry-run — add --broadcast)"
  runtime.log(`ReceiptAnchored on ${config.anchorAddress} tx=${txHash} verdict=${verdict}`)

  return verdict === 1
    ? `AUDIT OK payment ${paymentId}: model=${model} expected=${expected} receipt=${receiptAmount} (settled=${actualAmount}) tx=${txHash}`
    : `DISCREPANCY payment ${paymentId}: model=${model} expected=${expected} receipt=${receiptAmount} (settled=${actualAmount}, delta=${delta}) tx=${txHash}`
}

// ─── Workflow definition ────────────────────────────────────────────────────
function initWorkflow(config: Config) {
  const network = getNetwork({
    chainFamily: "evm",
    chainSelectorName: config.chainSelectorName,
    isTestnet: true,
  })
  if (!network) throw new Error(`Network not found: ${config.chainSelectorName}`)

  const evm = new EVMClient(network.chainSelector.selector)
  return [
    handler(
      evm.logTrigger(
        logTriggerConfig({
          addresses: [config.escrowAddress as Hex],
          topics: [[SETTLED_TOPIC0]], // topic0 only: any Escrow.Settled
        }),
      ),
      onSettled,
    ),
  ]
}

export async function main() {
  const runner = await Runner.newRunner<Config>({ configSchema })
  await runner.run(initWorkflow)
}

main()
