/**
 * Shared helpers + pinned EIP-712 fixture for settlement-audit tests.
 *
 * The fixture uses a SYNTHETIC key only (the public anvil dev key
 * 0xac09…f2ff80 — not real secret material). Its signature was produced with
 * relay/app/receipt.py's exact eth_account encode_typed_data pipeline
 * (Account.sign_message) and recovered in TS to anvil 0xf39F…92266.
 */
import {
  bigintToProtoBigInt,
  TxStatus,
  type EVMLog,
  type Runtime,
} from "@chainlink/cre-sdk"
import {
  encodeAbiParameters,
  encodeFunctionData,
  hexToBytes,
  pad,
  parseAbiParameters,
  toHex,
  stringToBytes,
  type Address,
  type Hex,
} from "viem"
import type { Config } from "../src/config"
import type { ChainEvmSeam, HttpSeam } from "../src/handler"
import {
  ESCROW_ABI,
  PAYMENT_ABI_PARAMS,
  REGISTRY_ABI,
  SETTLED_TOPIC0,
  type PriceRow,
} from "../src/abi"
import { encodeReportPayload, REPORT_FIELDS } from "../src/payload"

export { encodeReportPayload, REPORT_FIELDS }

// ─── On-chain fixture constants ─────────────────────────────────────────────
export const EVENT_PAYMENT_ID = 42001n
export const EVENT_BUYER: Address = "0x1111111111111111111111111111111111111111"
export const EVENT_SELLER: Address = "0xaBCDef0123456789abcdEF01234567890ABcDeF0"
export const EVENT_ACTUAL = 6543210n
export const EVENT_REFUNDED = 0n
export const TRIGGER_BLOCK = 189288n

export type LogArgs = {
  paymentId?: bigint
  buyer?: Address
  seller?: Address
  actualAmount?: bigint
  refundedAmount?: bigint
  /** null ⇒ omit blockNumber entirely (missing-block fail-closed case). */
  blockNumber?: bigint | null
  removed?: boolean
}

const pad32Bytes = (n: bigint): Uint8Array => hexToBytes(pad(toHex(n), { size: 32 }))

/** Build the EVMLog shape the capability hands the trigger handler. */
export function makeSettledLog(args: LogArgs = {}): EVMLog {
  const {
    paymentId = EVENT_PAYMENT_ID,
    buyer = EVENT_BUYER,
    seller = EVENT_SELLER,
    actualAmount = EVENT_ACTUAL,
    refundedAmount = EVENT_REFUNDED,
    blockNumber = TRIGGER_BLOCK,
    removed = false,
  } = args
  const topics: Hex[] = [
    SETTLED_TOPIC0,
    pad(toHex(paymentId), { size: 32 }),
    pad(buyer, { size: 32 }),
    pad(seller, { size: 32 }),
  ]
  const data = encodeAbiParameters(
    parseAbiParameters("uint256 actualAmount, uint256 refundedAmount"),
    [actualAmount, refundedAmount],
  )
  const log = {
    data: hexToBytes(data),
    topics: topics.map((t) => hexToBytes(t)),
    txHash: hexToBytes(`0x${"11".repeat(32)}`),
    blockHash: hexToBytes(`0x${"22".repeat(32)}`),
    txIndex: 0,
    index: 0,
    removed,
  }
  if (blockNumber !== null) {
    log.blockNumber = { absVal: pad32Bytes(blockNumber), sign: 1n }
  }
  return log as unknown as EVMLog
}

// ─── Config fixture ─────────────────────────────────────────────────────────

export const TEST_CONFIG: Config = {
  chainSelectorName: "monad-testnet",
  escrowAddress: "0xe4D5Eb0dBDB6DB8063C07ECF7EDFcCdDB9Ad514c",
  registryAddress: "0xeD347cDc1761750E20C024459b38dedFb1462254",
  anchorAddress: "0x5555555555555555555555555555555555555555",
  relayBaseUrl: "http://127.0.0.1:8787",
  maxPriceUsd6: "1000000000",
  workflowOwner: "0x6666666666666666666666666666666666666666",
  // Trust fixture: the synthetic anvil signer (public dev key). Tests that
  // check the *real* configured signer value use TRUSTED_SIGNER_REAL.
  receiptSignerAddress: "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
}

/** The real preconfigured shared receipt signer (public key material). */
export const TRUSTED_SIGNER_REAL = "0x49CA3eADB3b8F23623f735918A6F2b945F60fb2D"

// ─── Pinned EIP-712 golden receipt (relay/app/receipt.py pipeline) ──────────

export type GoldenReceipt = {
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

/**
 * Signed via the exact relay pipeline over the pinned domain/type set —
 * eth_account encode_typed_data + Account.sign_message
 * (relay/app/receipt.py:95-105).
 */
export const GOLDEN_RECEIPT: GoldenReceipt = {
  domain: { name: "TokenShare Relay", version: "1", chainId: 10143 },
  message: {
    paymentId: 42001,
    promptTokens: 1234,
    cachedTokens: 234,
    completionTokens: 567,
    actualAmount: 6543210,
    seller: "0xaBCDef0123456789abcdEF01234567890ABcDeF0",
    upstreamHost: "api.moonshot.cn",
    model: "moonshot-v1-8k",
  },
  signature:
    "0x9352d78655dc906ea0968c6da8114a4ffdb5f9819429a8fd4620c29dc5d5f4fd16fb8e817f0f8aeffe2dfea00db6e869c61f65ba9209edf58806a70abb05052f1c",
}
export const GOLDEN_SIGNER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
/** keccak256(GOLDEN_RECEIPT.signature) — computed relay-side (eth_hash). */
export const GOLDEN_RECEIPT_HASH =
  "0x91f6e45cdf54794f32e6c682fbcb7f3998eef91ffcdba9ca32ebe8bf5a08cb9d"

// ─── Pricing rows (calibrated to GOLDEN usage: cached 234, prompt 1000,
//     completion 567 → non-cached prompt = 1000 tokens) ───────────────────────

export const PRICES_EXACT: PriceRow = { priceCachedIn: 0n, priceInput: 6543210000n, priceOutput: 0n }
export const PRICES_MINUS1: PriceRow = { priceCachedIn: 0n, priceInput: 6543211000n, priceOutput: 0n }
export const PRICES_PLUS2: PriceRow = { priceCachedIn: 0n, priceInput: 6543212000n, priceOutput: 0n }

// ─── Mock capability seam ───────────────────────────────────────────────────

export type RecordedCall = {
  to: string
  data: Hex
  blockNumber: unknown
  kind: "getPayment" | "getListing" | "getPrice" | "other"
}

const SHAM_ADDR: Address = "0x0000000000000000000000000000000000000064"
const SELECTORS = {
  getPayment: encodeFunctionData({
    abi: ESCROW_ABI,
    functionName: "getPayment",
    args: [1n],
  }).slice(0, 10),
  getListing: "0x084af0b2", // real on-wire selector (eth_call fixture provenance)
  getPrice: encodeFunctionData({
    abi: REGISTRY_ABI,
    functionName: "getPrice",
    args: [SHAM_ADDR, "x"],
  }).slice(0, 10),
}

const kindForCall = (data: Hex): RecordedCall["kind"] =>
  (Object.keys(SELECTORS) as Array<keyof typeof SELECTORS>).find(
    (k) => data.startsWith(SELECTORS[k]),
  ) ?? "other"

/** SDK encodeCallMsg byte fields are base64; the mocks compare in hex. */
const b64ToHex = (b64: string): string =>
  `0x${Buffer.from(b64, "base64").toString("hex")}`
const dataB64ToHex = (b64: string | undefined): Hex =>
  b64ToHex(b64 ?? "") as Hex
const addrB64ToHex = (b64: string): string => {
  const hex = b64ToHex(b64)
  return hex === "0x" || hex === "0x0000000000000000000000000000000000000000"
    ? hex
    : hex // 20-byte address as plain hex
}

export type MockEvmOptions = {
  payment?: Partial<{ buyer: Address; seller: Address; maxAmount: bigint; expiresAt: bigint; state: number }>
  /** getPrice router: default → PRICES_EXACT; "throw" → revert. */
  getPrice?: PriceRow | "throw"
  /** Throw on a chosen read (transport/revert simulation for fail-closed). */
  failOn?: "getPayment" | "getListing" | "getPrice"
  writeTxStatus?: number
}

export type MockHttpOptions = {
  statusCode?: number
  /** Receipt JSON or arbitrary body; default = GOLDEN_RECEIPT. */
  body?: unknown
  /** Appends every raw sendRequest input (see CapturedRequest). */
  capture?: CapturedRequest[]
}

/**
 * Raw sendRequest input as the capability receives it (SDK JSON shape).
 * Capturing THIS is the regression surface for protobuf field-type bugs —
 * e.g. `timeout` must be the DurationJson STRING "5s", not an object
 * ({seconds, nanos}) which fails at runtime with
 * "cannot decode google.protobuf.Duration from JSON: object"
 * (sim-dryrun 2026-10-08).
 */
export type CapturedRequest = {
  request: {
    url?: string
    method?: string
    timeout?: unknown
  }
  vaultDonSecrets?: unknown
}

export type MockRuntimeSurface = Runtime<Config> & {
  config: Config
  logs: string[]
  reportRequests: Array<{ encodedPayload: string; encoderName?: string }>
  report: (req: unknown) => { result: () => string }
}

export function makeMockRuntime(config: Config = TEST_CONFIG): MockRuntimeSurface {
  const logs: string[] = []
  const reportRequests: Array<{ encodedPayload: string }> = []
  return {
    config,
    logs,
    reportRequests,
    log: (m: string) => logs.push(m),
    report: (req: unknown) => {
      reportRequests.push(req as { encodedPayload: string })
      return { result: () => "0xdonsigned" as Hex }
    },
  } as unknown as MockRuntimeSurface
}

export type WriteRecord = { receiver: string; report: string; txStatus: number }

export type WireResult = {
  evm: ChainEvmSeam
  http: HttpSeam
  calls: RecordedCall[]
  writes: WriteRecord[]
}

export function makeMockEvm(opts: MockEvmOptions = {}): Pick<WireResult, "evm" | "calls" | "writes"> {
  const calls: RecordedCall[] = []
  const writes: WriteRecord[] = []
  const payment = {
    buyer: EVENT_BUYER,
    seller: EVENT_SELLER,
    maxAmount: 12_000_000n,
    expiresAt: 9999999999n,
    state: 2,
    ...opts.payment,
  }
  const evm: ChainEvmSeam = {
    callContract: (_runtime, input) => {
      const bag = input as { call: { to?: string; data?: string }; blockNumber?: unknown }
      // SDK encodeCallMsg returns base64-encoded byte fields (from/to/data);
      // decode back to hex so kind routing + assertions stay hex-shaped.
      const data = dataB64ToHex(bag.call.data)
      const kind = kindForCall(data)
      calls.push({ to: addrB64ToHex(bag.call.to ?? ""), data, blockNumber: bag.blockNumber, kind })
      if (kind !== "other" && opts.failOn === kind) {
        throw new Error(`mock ${kind} reverted`)
      }
      let out: Hex
      if (kind === "getPayment") {
        out = encodeAbiParameters(PAYMENT_ABI_PARAMS, [
          payment.buyer,
          payment.seller,
          payment.maxAmount,
          payment.expiresAt,
          payment.state,
        ])
      } else if (kind === "getPrice") {
        if (opts.getPrice === "throw") throw new Error("mock getPrice reverted (NotActive)")
        const p = opts.getPrice ?? PRICES_EXACT
        out = encodeAbiParameters(
          parseAbiParameters("(uint256 priceCachedIn, uint256 priceInput, uint256 priceOutput)"),
          [{ priceCachedIn: p.priceCachedIn, priceInput: p.priceInput, priceOutput: p.priceOutput }],
        )
      } else {
        throw new Error(`mock cannot serve call: ${data.slice(0, 10)}`)
      }
      return { result: () => ({ data: hexToBytes(out) }) } as unknown as { result: () => { data: Uint8Array } }
    },
    writeReport: (_runtime, input) => {
      const bag = input as unknown as { receiver: string; report: string }
      writes.push({ receiver: bag.receiver, report: bag.report, txStatus: opts.writeTxStatus ?? TxStatus.SUCCESS })
      return {
        result: () => ({
          txStatus: opts.writeTxStatus ?? TxStatus.SUCCESS,
          txHash: hexToBytes(`0x${"ab".repeat(32)}`),
          errorMessage: "",
        }),
      } as unknown as { result: () => { txStatus: number; txHash: Uint8Array; errorMessage: string } }
    },
  }
  return { evm, calls, writes }
}

/**
 * Raw sendRequest input as the capability receives it (SDK JSON shape).
 * Capturing THIS is the regression surface for protobuf field-type bugs —
 * e.g. `timeout` must be the DurationJson STRING "5s", not an object
 * ({seconds, nanos}) which fails at runtime with
 * "cannot decode google.protobuf.Duration from JSON: object"
 * (sim-dryrun 2026-10-08).
 */
export type CapturedRequest = {
  request: {
    url?: string
    method?: string
    timeout?: unknown
  }
  vaultDonSecrets?: unknown
}

export function makeMockHttp(opts: MockHttpOptions = {}): HttpSeam {
  return {
    sendRequest: (_runtime, input) => {
      // capture the raw edge input for SDK-JSON shape assertions
      opts.capture?.push(input as unknown as CapturedRequest)
      if (opts.statusCode !== undefined && opts.statusCode >= 400) {
        return {
          result: () => ({ statusCode: opts.statusCode, body: stringToBytes('{"error":"boom"}') }),
        }
      }
      const bodyObj = opts.body ?? GOLDEN_RECEIPT
      return {
        result: () => ({
          statusCode: opts.statusCode ?? 200,
          body: stringToBytes(typeof bodyObj === "string" ? bodyObj : JSON.stringify(bodyObj)),
        }),
      }
    },
  } as unknown as HttpSeam
}

/**
 * Block-reference invariant: every read call's blockNumber serializes to the
 * bigintToProtoBigInt(triggerBlock) JSON shape.
 */
export function assertSameTriggerBlock(calls: RecordedCall[], triggerBlock: bigint) {
  const expected = JSON.stringify(bigintToProtoBigInt(triggerBlock))
  const refs = calls.map((c) => JSON.stringify(c.blockNumber))
  // getPayment + getPrice at minimum.
  expect(refs.length).toBeGreaterThanOrEqual(2)
  for (const ref of refs) {
    expect(ref).toBe(expected)
  }
}

export function encodeBody(value: unknown): Uint8Array {
  return stringToBytes(JSON.stringify(value))
}
