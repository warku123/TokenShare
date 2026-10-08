/**
 * onSettled handler — reads, receipt verification, adjudication, report.
 *
 * Design rules (fail-closed, single-payment scope):
 *   1. EXACT TRIGGER BLOCK — every EVM read (getPayment, getPrice) references
 *      the Settled log's own blockNumber (that block's END state — the exact
 *      state the settlement priced against). A log without blockNumber, or a
 *      removed/reorged log, fails closed: no report. There is deliberately NO
 *      fallback to LAST_FINALIZED (that race is precisely what fabricates
 *      phantom MISMATCHes) and no broad catch silently substitutes a
 *      different height. Registry.getListing is NOT read at all: its price
 *      fallback was REMOVED — strict fail-closed (ora39 2026-10-08).
 *   2. ONE payment in, at most ONE report out. Any validation, auth, read or
 *      ceiling failure ⇒ the handler logs a NO ANCHOR reason and anchors
 *      nothing. No partial-payment anchoring, no cross-payment cumulative
 *      audit — the trigger set stays exactly one (Escrow.Settled).
 *   3. A good signed receipt with an amount mismatch IS anchored with
 *      verdict=MISMATCH (the audit trail is the point). Auth/schema failures
 *      are NEVER anchored, never reported.
 *   4. Report = runtime.report(prepareReportRequest(businessBytes)) where
 *      businessBytes = src/payload.encodeReportPayload(...) — the seven
 *      abi-encoded business fields, NOT encodeFunctionData(onReport)
 *      calldata. The official KeystoneForwarder
 *      (contracts/cre/src/v1/KeystoneForwarder.sol) routes
 *      abi.encodeCall(onReport, (rawReport[45:109], rawReport[109:])) to the
 *      consumer, whose abi.decode must receive exactly those seven fields.
 *
 * Capability seam for tests: makeOnSettled(deps) accepts minimal structural
 * implementations of the EVM + confidential HTTP clients, so bun tests
 * assert trigger-block equality and no-report-on-failure without the CRE
 * runtime. main.ts wires the real clients.
 */
import {
  bigintToProtoBigInt,
  bytesToHex,
  ConfidentialHTTPClient,
  encodeCallMsg,
  json,
  ok,
  prepareReportRequest,
  protoBigIntToBigint,
  TxStatus,
  type EVMClient,
  type EVMLog,
  type Runtime,
} from "@chainlink/cre-sdk"
import {
  decodeAbiParameters,
  decodeEventLog,
  encodeFunctionData,
  type Address,
  type Hex,
} from "viem"
import type { Config } from "./config"
import { sameAddress as sameChainAddress } from "./config"
import {
  ESCROW_ABI,
  PAYMENT_ABI_PARAMS,
  PAYMENT_STATE_SETTLED,
  PRICE_ROW_ABI_PARAMS,
  REGISTRY_ABI,
  SETTLED_TOPIC0,
  type PaymentRecord,
  type PriceRow,
} from "./abi"
import {
  checkReceiptConsistency,
  parseAndVerifyReceipt,
  type ChainContext,
  type ValidatedReceipt,
} from "./receipt"
import { assessAmount, exceedsCeiling, type TieredUsage } from "./pricing"
import { encodeReportPayload } from "./payload"

/** Structural surface of the real EVMClient used here (mock-friendly). */
export type ChainEvmSeam = Pick<EVMClient, "callContract" | "writeReport">
/** Structural surface of the confidential HTTP client. */
export type HttpSeam = Pick<ConfidentialHTTPClient, "sendRequest">

export type OnSettledDeps = {
  evm: ChainEvmSeam
  http: HttpSeam
}

export type SettledArgs = {
  paymentId: bigint
  buyer: `0x${string}`
  seller: `0x${string}`
  actualAmount: bigint
  refundedAmount: bigint
}

/** Structural surface of the confidential HTTP response (result() value). */
export type ConfResponse = ReturnType<ReturnType<HttpSeam["sendRequest"]>["result"]>

const ZERO_FROM = "0x0000000000000000000000000000000000000000" as Address

/**
 * The Settled handler. Returns a human summary; failures BEGIN with
 * "NO ANCHOR"/"MODEL MISSING" and guarantee zero runtime.report and zero
 * evm.writeReport calls.
 */
export const makeOnSettled =
  (deps: OnSettledDeps) =>
  async (runtime: Runtime<Config>, log: EVMLog): Promise<string> => {
    const config = runtime.config
    const noAnchor = (label: string, reason: string): string => {
      const message = `NO ANCHOR ${label}: ${reason}`
      runtime.log(`NO ANCHOR ${label} — ${reason}`)
      return message
    }

    // ── 1. Decode the trigger surface ──────────────────────────────────────
    if (!log.topics || log.topics.length < 4) {
      return noAnchor("trigger", "Settled log malformed: missing indexed topics")
    }
    const eventTopics = log.topics.map((t) => bytesToHex(t))
    const topic0 = eventTopics[0] as Hex
    if (topic0.toLowerCase() !== SETTLED_TOPIC0.toLowerCase()) {
      return noAnchor(
        "trigger",
        `unexpected topic0 ${topic0} (expected ${SETTLED_TOPIC0}) — refusing to anchor`,
      )
    }
    let decodedArgs: SettledArgs
    try {
      decodedArgs = decodeEventLog({
        abi: ESCROW_ABI,
        eventName: "Settled" as const,
        data: bytesToHex(log.data ?? new Uint8Array()),
        topics: eventTopics as [Hex, ...Hex[]],
      }).args as unknown as SettledArgs
    } catch (e) {
      return noAnchor(
        "trigger",
        `Settled log does not decode against the pinned Escrow ABI — refusing to anchor (${
          e instanceof Error ? e.message : String(e)
        })`,
      )
    }
    const { paymentId, seller, buyer, actualAmount } = decodedArgs
    runtime.log(
      `Settled: paymentId=${paymentId} buyer=${buyer} seller=${seller} actualAmount=${actualAmount}`,
    )

    if (log.removed === true) {
      return noAnchor(`payment ${paymentId}`, "Settled log was removed (reorg) — refusing to anchor")
    }

    // ── 2. EXACT TRIGGER BLOCK (fail closed when absent) ───────────────────
    if (log.blockNumber == null) {
      return noAnchor(
        `payment ${paymentId}`,
        "Settled log carries no blockNumber — reads without an exact block reference are forbidden (no fallback height)",
      )
    }
    const triggerBlock = protoBigIntToBigint(log.blockNumber)
    const blockNumberArg = bigintToProtoBigInt(triggerBlock)
    runtime.log(`Trigger block: ${triggerBlock} — all reads at this exact height`)

    // ── 3. Reads (all at the exact trigger block) ───────────────────────────
    const read = (to: string, data: Hex, what: string): Uint8Array => {
      let raw: Uint8Array
      try {
        raw = deps.evm
          .callContract(runtime, {
            call: encodeCallMsg({
              from: ZERO_FROM,
              to: to as Address,
              data,
            }),
            blockNumber: blockNumberArg,
          })
          .result().data
      } catch (e) {
        throw new Error(
          `${what} failed at exact trigger block ${triggerBlock} — refusing to anchor (${
            e instanceof Error ? e.message : String(e)
          })`,
        )
      }
      runtime.log(`${what}: ok at block ${triggerBlock}`)
      return raw
    }

    let paymentRecord: PaymentRecord
    try {
      paymentRecord = decodePayment(
        read(
          config.escrowAddress,
          encodeFunctionData({
            abi: ESCROW_ABI,
            functionName: "getPayment",
            args: [paymentId],
          }),
          `getPayment(${paymentId})`,
        ),
      )
    } catch (e) {
      return noAnchor(`payment ${paymentId}`, e instanceof Error ? e.message : String(e))
    }
    if (paymentRecord.state !== PAYMENT_STATE_SETTLED) {
      return noAnchor(
        `payment ${paymentId}`,
        `getPayment.state=${paymentRecord.state} ≠ Settled (${PAYMENT_STATE_SETTLED}) — refusing to anchor`,
      )
    }
    runtime.log(
      `getPayment: buyer=${paymentRecord.buyer} seller=${paymentRecord.seller} maxAmount=${paymentRecord.maxAmount} expiresAt=${paymentRecord.expiresAt} state=${paymentRecord.state}`,
    )

    // NOTE: Registry.getListing is NOT read by this workflow. Rationale:
    // (a) it served only as a price-revert fallback, which is now REMOVED in
    // favor of strict fail-closed behavior (ora39 2026-10-08); (b) its return
    // `bytes` shape (ONE dynamic Listing tuple, head word 0x20) is a decode
    // trap that malformed reads would turn into a full decode failure. The
    // strict shape proof stays as a regression test (tests/abi-shape.test.ts,
    // real RPC bytes @ block 0x42033c6) instead of a runtime read.

    // Context consistency that needs NO receipt (event vs trigger-block state):
    if (!sameChainAddress(buyer, paymentRecord.buyer)) {
      return noAnchor(
        `payment ${paymentId}`,
        `buyer mismatch: event=${buyer} getPayment=${paymentRecord.buyer}`,
      )
    }
    if (!sameChainAddress(seller, paymentRecord.seller)) {
      return noAnchor(
        `payment ${paymentId}`,
        `seller mismatch: event=${seller} getPayment=${paymentRecord.seller}`,
      )
    }

    // ── 4. Receipt fetch (confidential HTTP) ────────────────────────────────
    // NOTE: GET /receipt/{id} is an ANONYMOUS 200 endpoint — the relay does
    // NOT verify the Authorization header. The bearer is carried purely as
    // simulation/deploy plumbing (Vault DON secret injection stays exercised):
    // it is NOT HTTP authentication and must never be described as such.
    // Receipt authenticity comes exclusively from its EIP-712 signature,
    // verified against the PRECONFIGURED trusted signer in src/receipt.ts.
    const receiptUrl = `${config.relayBaseUrl}/receipt/${paymentId}`
    let response: ConfResponse
    try {
      response = deps.http
        .sendRequest(runtime, {
          request: {
            url: receiptUrl,
            method: "GET" as const,
            multiHeaders: {
              Authorization: { values: ["Bearer {{.receipts_bearer}}"] },
            },
            // SDK type = `timeout?: DurationJson` — a protobuf-JSON STRING
            // ("5s"), never an object: sending {seconds,nanos} fails at
            // runtime with "cannot decode … google.protobuf.Duration from
            // JSON: object" (fixed from sim-dryrun log, 2026-10-08).
            // Protocol max is 10s; 5s is ample for a relay round-trip.
            timeout: "5s",
            // encryptOutput stays false: this workflow itself must read the
            // receipt to adjudicate it. See cre/README notes for the
            // external-buyer-decrypt variant.
            encryptOutput: false,
          },
          vaultDonSecrets: [{ key: "receipts_bearer", owner: config.workflowOwner }],
        })
        .result()
    } catch (e) {
      return noAnchor(
        `payment ${paymentId}`,
        `receipt request threw (${
          e instanceof Error ? e.message : String(e)
        })`,
      )
    }
    if (!ok(response)) {
      // HTTPResponse pb type declares statusCode: number — direct access, no
      // type-erase casts (such casts hide real field-type drift, e.g. the
      // DurationJson timeout object bug — see sim-dryrun regression test).
      return noAnchor(
        `payment ${paymentId}`,
        `receipt fetch failed: HTTP ${response.statusCode} for ${receiptUrl}`,
      )
    }
    let receiptRaw: unknown
    try {
      receiptRaw = json(response)
    } catch (e) {
      return noAnchor(
        `payment ${paymentId}`,
        `receipt body is not JSON (${
          e instanceof Error ? e.message : String(e)
        })`,
      )
    }

    // ── 5. Receipt schema + AUTH (trusted signer from config, never receipt) ─
    const verified = parseAndVerifyReceipt(receiptRaw, config.receiptSignerAddress)
    if (!verified.ok) {
      const headline =
        verified.error.kind === "AUTH"
          ? "receipt signer is not the configured trusted signer"
          : "receipt schema validation failed"
      return noAnchor(`payment ${paymentId}`, `${headline}: ${verified.error.reason}`)
    }
    const receipt: ValidatedReceipt = verified.receipt
    runtime.log(
      `Receipt auth: signature recovered ${receipt.signer} == trusted ${config.receiptSignerAddress}`,
    )
    runtime.log(
      `Receipt: paymentId=${receipt.paymentId} actualAmount=${receipt.actualAmount} prompt=${receipt.promptTokens} cached=${receipt.cachedTokens} completion=${receipt.completionTokens} upstreamHost=${receipt.upstreamHost} model=${receipt.model}`,
    )

    // ── 6. Chain-context consistency (receipt vs event vs getPayment) ───────
    const ctx: ChainContext = {
      paymentId,
      eventSeller: seller.toLowerCase(),
      eventBuyer: buyer.toLowerCase(),
      payment: paymentRecord,
    }
    const consistent = checkReceiptConsistency(receipt, ctx)
    if (!consistent.ok) {
      return noAnchor(`payment ${paymentId}`, `${consistent.kind}: ${consistent.reason}`)
    }

    // ── 7. Price resolution (receipt's model, exact trigger block) ──────────
    // STRICT FAIL-CLOSED (ora39 2026-10-08): transport error, revert (incl.
    // NotActive/ModelNotFound), decode error or any other read failure ⇒
    // NO report, NO anchoring. There is NO listing fallback and NO fallback
    // block height — silent substitution of ANY price source would mask real
    // mispricing instead of flagging it. getListing is not read at all.
    let price: PriceRow
    try {
      price = decodePriceRow(
        read(
          config.registryAddress,
          encodeFunctionData({
            abi: REGISTRY_ABI,
            functionName: "getPrice",
            args: [seller, receipt.model],
          }),
          `getPrice(${receipt.model})`,
        ),
      )
    } catch (e) {
      return noAnchor(
        `payment ${paymentId}`,
        `getPrice(${receipt.model}) failed at exact trigger block ${triggerBlock} — no price row, NO fallback (strict fail-closed): ${
          e instanceof Error ? e.message : String(e)
        }`,
      )
    }
    runtime.log(
      `getPrice(${receipt.model}): cachedIn=${price.priceCachedIn} input=${price.priceInput} output=${price.priceOutput}`,
    )

    // ── 8. Ceiling + adjudication (MISMATCH still anchors) ──────────────────
    const ceiling = BigInt(config.maxPriceUsd6)
    if (exceedsCeiling(receipt.actualAmount, ceiling)) {
      return noAnchor(
        `payment ${paymentId}`,
        `receipt amount ${receipt.actualAmount} exceeds sanity ceiling ${ceiling}`,
      )
    }
    const usage: TieredUsage = {
      promptTokens: receipt.promptTokens,
      cachedTokens: receipt.cachedTokens,
      completionTokens: receipt.completionTokens,
    }
    const assessment = assessAmount(price, usage, receipt.actualAmount)
    const verdict = assessment.verdict
    runtime.log(
      `Compare: model=${receipt.model} expected=${assessment.expected} receipt=${assessment.receiptAmount} (onchain settled=${actualAmount}) → ${
        verdict === 1 ? "MATCH" : `MISMATCH (delta=${assessment.delta})`
      }`,
    )

    // ── 9. WRITE — seven-field business bytes, no onReport wrapper ──────────
    const reportPayload = encodeReportPayload({
      paymentId,
      settledAmount: actualAmount,
      receiptAmount: receipt.actualAmount,
      receiptHash: receipt.receiptHash,
      upstreamHost: receipt.upstreamHost,
      model: receipt.model,
      verdict,
    })
    const signedReport = runtime
      .report(prepareReportRequest(reportPayload))
      .result() as unknown as Hex
    const writeReply = deps.evm
      .writeReport(runtime, {
        receiver: config.anchorAddress as Address,
        report: signedReport,
      })
      .result()
    if (writeReply.txStatus !== TxStatus.SUCCESS) {
      return `ANCHOR FAILED payment ${paymentId}: writeReport ${
        writeReply.errorMessage ?? writeReply.txStatus
      } (verdict=${verdict}; DON signed the report, anchor delivery failed)`
    }
    const txHash = writeReply.txHash
      ? bytesToHex(writeReply.txHash)
      : "0x (dry-run — add --broadcast)"
    runtime.log(`ReceiptAnchored on ${config.anchorAddress} tx=${txHash} verdict=${verdict}`)

    return verdict === 1
      ? `AUDIT OK payment ${paymentId}: model=${receipt.model} expected=${assessment.expected} receipt=${assessment.receiptAmount} (settled=${actualAmount}) tx=${txHash}`
      : `DISCREPANCY payment ${paymentId}: model=${receipt.model} expected=${assessment.expected} receipt=${assessment.receiptAmount} (settled=${actualAmount}, delta=${assessment.delta}) tx=${txHash}`
  }

const decodePayment = (raw: Uint8Array): PaymentRecord => {
  const [buyer, seller, maxAmount, expiresAt, state] = decodeAbiParameters(PAYMENT_ABI_PARAMS, raw)
  return { buyer, seller, maxAmount, expiresAt, state }
}

const decodePriceRow = (raw: Uint8Array): PriceRow =>
  (decodeAbiParameters(PRICE_ROW_ABI_PARAMS, raw) as [PriceRow])[0]
