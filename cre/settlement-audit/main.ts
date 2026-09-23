/**
 * TokenShare — Settlement Audit Anchor (Chainlink CRE workflow, TypeScript)
 *
 * Bounty track: "Best Workflow with CRE" (Chainlink, $3k). Flow = M7 素材节
 * 构想 #1 + #3 合一 (settlement audit anchor + receipt notary):
 *
 *   1. TRIGGER (index 0): Escrow.Settled log on Monad testnet (chain 10143).
 *   2. EVM READ: Escrow.getPayment(paymentId) → buyer/max/expiresAt/state,
 *                Registry.getListing(seller) → 3-tier pricing rows
 *                (priceCachedIn / priceInput / priceOutput, 6-dp USDC).
 *   3. CONFIDENTIAL HTTP: GET {relayBaseUrl}/receipt/{paymentId} — the relay's
 *                EIP-712 signed receipt. The bearer credential is template-
 *                resolved inside the enclave via {{.receipts_bearer}}
 *                (simulation: value injected from .env; deployed: Vault DON).
 *   4. VERIFY  : receipt.message.actualAmount vs on-chain settled actualAmount
 *                (±1 native-unit tolerance = 0.000001 USDC rounding slop).
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
 *   registry 0x3a44dB7696306DFB08266721aE660C2334FCAA93
 */
import {
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
const registryAbi = parseAbi([
  "function getListing(address operator) view returns (address listingOperator, string endpoint, string[] models, uint256 priceCachedIn, uint256 priceInput, uint256 priceOutput, bool active)",
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
  const listing = decodeAbiParameters(
    parseAbiParameters(
      "address listingOperator, string endpoint, string[] models, uint256 priceCachedIn, uint256 priceInput, uint256 priceOutput, bool active",
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
  // listing tuple (7 outputs, mirrors registryAbi.getListing): [0]=operator,
  // [1]=endpoint, [2]=models, [3]=priceCachedIn, [4]=priceInput,
  // [5]=priceOutput, [6]=active
  runtime.log(
    `getListing: cached=${listing[3]} input=${listing[4]} output=${listing[5]} active=${listing[6]}`,
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
  const receiptAmount = BigInt(receipt.message.actualAmount)
  const ceiling = BigInt(config.maxPriceUsd6)
  if (receiptAmount > ceiling) {
    throw new Error(`Receipt amount ${receiptAmount} exceeds sanity ceiling ${ceiling} — refusing to anchor`)
  }
  const delta = receiptAmount > actualAmount ? receiptAmount - actualAmount : actualAmount - receiptAmount
  const verdict = delta <= 1n ? 1 : 2 // 1=MATCH, 2=MISMATCH (Contract enum order)
  runtime.log(
    `Compare: onchain=${actualAmount} receipt=${receiptAmount} → ${verdict === 1 ? "MATCH" : `MISMATCH (delta=${delta})`}`,
  )

  // 5. WRITE — ABI-encoded audit payload routed to ReceiptAnchor.onReport
  //    through the CRE writeReport consensus path: DON signs the report
  //    (runtime.report), then the EVM capability submits it to the forwarder.
  const report: Hex = encodeAbiParameters(
    parseAbiParameters(
      "uint256 paymentId, uint256 settledAmount, uint256 receiptAmount, bytes32 receiptHash, string upstreamHost, string model",
    ),
    [
      paymentId,
      actualAmount,
      receiptAmount,
      keccak256(receipt.signature as Hex),
      receipt.message.upstreamHost,
      receipt.message.model,
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
    ? `AUDIT OK payment ${paymentId}: onchain=${actualAmount} receipt=${receiptAmount} tx=${txHash}`
    : `DISCREPANCY payment ${paymentId}: onchain=${actualAmount} receipt=${receiptAmount} tx=${txHash}`
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
