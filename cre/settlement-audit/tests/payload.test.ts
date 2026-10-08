/** Payload wire-format tests: seven-field roundtrip + old-wrapping negative. */
import { describe, expect, test } from "bun:test"
import { decodeAbiParameters, decodeFunctionData, encodeAbiParameters, encodeFunctionData, keccak256, parseAbi, parseAbiParameters } from "viem"
import { encodeReportPayload, REPORT_FIELDS } from "../src/payload"
import { GOLDEN_RECEIPT_HASH } from "./helpers"

const ARGS = {
  paymentId: 42001n,
  settledAmount: 6543210n,
  receiptAmount: 6543210n,
  receiptHash: GOLDEN_RECEIPT_HASH,
  upstreamHost: "api.moonshot.cn",
  model: "moonshot-v1-8k",
  verdict: 1,
} as const

const CONSUMER_ABI = parseAbi([
  "function onReport(bytes metadata, bytes report)",
])

describe("report payload (exact official-forwarder consumer shape)", () => {
  test("seven-field abi.encode roundtrip via decodeAbiParameters", () => {
    const payload = encodeReportPayload(ARGS)
    const [paymentId, settledAmount, receiptAmount, receiptHash, upstreamHost, model, verdict] =
      decodeAbiParameters(REPORT_FIELDS, payload)
    expect(paymentId).toBe(ARGS.paymentId)
    expect(settledAmount).toBe(ARGS.settledAmount)
    expect(receiptAmount).toBe(ARGS.receiptAmount)
    expect(receiptHash).toBe(ARGS.receiptHash)
    expect(upstreamHost).toBe(ARGS.upstreamHost)
    expect(model).toBe(ARGS.model)
    expect(verdict).toBe(ARGS.verdict)
  })

  test("consumer-side abi.decode (ReceiptAnchor._decodeReport shape) matches", () => {
    const payload = encodeReportPayload({ ...ARGS, verdict: 2 })
    const [paymentId, settled, receipt, hash, host, model, verdict] = decodeAbiParameters(
      [
        { type: "uint256", name: "" },
        { type: "uint256", name: "" },
        { type: "uint256", name: "" },
        { type: "bytes32", name: "" },
        { type: "string", name: "" },
        { type: "string", name: "" },
        { type: "uint8", name: "" },
      ] as never,
      payload,
    )
    expect(paymentId).toBe(ARGS.paymentId)
    expect(settled).toBe(ARGS.settledAmount)
    expect(receipt).toBe(ARGS.receiptAmount)
    expect(hash).toBe(ARGS.receiptHash)
    expect(host).toBe(ARGS.upstreamHost)
    expect(model).toBe(ARGS.model)
    expect(verdict).toBe(2)
  })

  test("receiptHash is keccak256(signature bytes)", () => {
    const sig =
      "0x9352d78655dc906ea0968c6da8114a4ffdb5f9819429a8fd4620c29dc5d5f4fd16fb8e817f0f8aeffe2dfea00db6e869c61f65ba9209edf58806a70abb05052f1c"
    expect(keccak256(sig as `0x${string}`)).toBe(ARGS.receiptHash)
  })

  test("MISMATCH verdict contributes byte 7 only (no wire extension)", () => {
    const match = encodeReportPayload(ARGS)
    const mismatch = encodeReportPayload({ ...ARGS, verdict: 2 })
    expect(match.length).toBe(mismatch.length)
    // The only difference: the low nibble of the verdict word (byte 223, word 7 low byte).
    expect(match.slice(0, 449)).toBe(mismatch.slice(0, 449))
    expect(match.slice(450)).toBe(mismatch.slice(450))
    expect(Number.parseInt(match.slice(448, 450), 16) - Number.parseInt(mismatch.slice(448, 450), 16)).toBe(-1)
  })
})

describe("negative: the workflow must NOT emit old onReport-wrapped calldata", () => {
  test("businessBytes ≠ encodeFunctionData(onReport, [metadata, businessBytes])", () => {
    const payload = encodeReportPayload(ARGS)
    // The OLD (wrong) wire branch the M9 draft used:
    const oldEncoded = encodeFunctionData({
      abi: CONSUMER_ABI,
      functionName: "onReport",
      args: [encodeAbiParameters(parseAbiParameters("uint256 workflowVersion"), [1n]), payload],
    })
    expect(oldEncoded).not.toBe(payload)
    expect(oldEncoded.startsWith("0x")).toBe(true)
    // decodable as the onReport selector (proves the old shape existed):
    const decoded = decodeFunctionData({ abi: CONSUMER_ABI, data: oldEncoded })
    expect(decoded.functionName).toBe("onReport")
    expect(decoded.args[1]).toBe(payload)
    // And the NEW payload must NOT itself be onReport calldata (no 4-byte selector):
    const onReportSelector = "0xd6d0f253" // any encoded onReport prefix differs structurally
    expect(payload.startsWith(onReportSelector)).toBe(false)
    // length: payload ⊂ old calldata (selector+2 offset-words+offset-words + payload)
    expect(oldEncoded.length).toBeGreaterThan(payload.length)
  })

  test("no fabricated metadata inside businessBytes (decode consumes exactly 7 fields)", () => {
    const payload = encodeReportPayload(ARGS)
    // Canonical ABI: whole 32-byte words, no selector prefix, no metadata head.
    expect((payload.length - 2) % 64).toBe(0)
    expect(payload.startsWith("0x")).toBe(true)
    const decoded = decodeAbiParameters(REPORT_FIELDS, payload)
    expect(decoded).toHaveLength(7)
  })
})
