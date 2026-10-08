/**
 * Handler (capability-seam) tests:
 *   — all reads pin the EXACT trigger block
 *   — auth/schema failures ⇒ NO runtime.report + NO writeReport
 *   — good signed receipt anchors (MATCH and MISMATCH paths)
 *   — missing blockNumber ⇒ fail closed
 *   — report payload is the raw seven-field business bytes (no onReport wrap)
 */
import { describe, expect, test } from "bun:test"
import { decodeAbiParameters } from "viem"
import { makeOnSettled, type OnSettledDeps } from "../src/handler"
import { REPORT_FIELDS } from "../src/payload"
import {
  assertSameTriggerBlock,
  EVENT_ACTUAL,
  EVENT_PAYMENT_ID,
  EVENT_SELLER,
  GOLDEN_RECEIPT,
  GOLDEN_RECEIPT_HASH,
  makeMockEvm,
  makeMockHttp,
  makeMockRuntime,
  makeSettledLog,
  PRICES_MINUS1,
  PRICES_PLUS2,
  TEST_CONFIG,
  TRIGGER_BLOCK,
  type CapturedRequest,
  type MockRuntimeSurface,
} from "./helpers"

const DEPS = ({ evm, http }: { evm: OnSettledDeps["evm"]; http: OnSettledDeps["http"] }): OnSettledDeps => ({ evm, http })

/** Run the handler with the default golden wiring; respond with live pieces. */
async function runGolden(overrides: {
  getPrice?: "throw" | undefined
  config?: typeof TEST_CONFIG
} = {}) {
  const { evm, calls, writes } = makeMockEvm({
    getPrice: overrides.getPrice,
  })
  const http = makeMockHttp()
  const runtime = makeMockRuntime(overrides.config)
  const outcome = await makeOnSettled(DEPS({ evm, http }))(runtime, makeSettledLog())
  return { outcome, calls, writes, runtime }
}

function decodeReport(runtime: MockRuntimeSurface) {
  expect(runtime.reportRequests).toHaveLength(1)
  const req = runtime.reportRequests[0] as { encodedPayload: string; encoderName?: string }
  expect(req.encoderName).toBe("evm")
  // encodedPayload is base64 of the raw business bytes (SDK encode).
  return req
}

describe("trigger-block exactness", () => {
  test("getPayment + getPrice all reference the trigger block", async () => {
    const { outcome, calls, runtime } = await runGolden()
    expect(outcome).toContain("AUDIT OK")
    assertSameTriggerBlock(calls, TRIGGER_BLOCK)
    expect(calls.map((c) => c.kind)).toEqual(["getPayment", "getPrice"])
    expect(runtime.reportRequests).toHaveLength(1)
  })

  test("missing log.blockNumber ⇒ NO report, fail-closed reason", async () => {
    const { evm, calls, writes } = makeMockEvm()
    const runtime = makeMockRuntime()
    const outcome = await makeOnSettled(DEPS({ evm, http: makeMockHttp() }))(
      runtime,
      makeSettledLog({ blockNumber: null }),
    )
    expect(outcome.startsWith("NO ANCHOR")).toBe(true)
    expect(outcome).toContain("blockNumber")
    expect(runtime.reportRequests).toHaveLength(0)
    expect(writes).toHaveLength(0)
    expect(calls).toHaveLength(0)
  })

  test("removed log (reorg) ⇒ NO report", async () => {
    const { evm } = makeMockEvm()
    const runtime = makeMockRuntime()
    const outcome = await makeOnSettled(DEPS({ evm, http: makeMockHttp() }))(
      runtime,
      makeSettledLog({ removed: true }),
    )
    expect(outcome.startsWith("NO ANCHOR")).toBe(true)
    expect(runtime.reportRequests).toHaveLength(0)
  })
})

describe("no report on auth/schema failures", () => {
  test("wrong trusted signer in config ⇒ NO report", async () => {
    const cfg = { ...TEST_CONFIG, receiptSignerAddress: "0x49CA3eADB3b8F23623f735918A6F2b945F60fb2D" }
    const { outcome, runtime, calls } = await runGolden({ config: cfg })
    expect(outcome.startsWith("NO ANCHOR")).toBe(true)
    expect(outcome).toContain("trusted signer")
    expect(runtime.reportRequests).toHaveLength(0)
    expect(calls.map((c) => c.kind)).toEqual(["getPayment"])
  })

  test("relay schema violation (negative amount) ⇒ NO report", async () => {
    const bad = {
      ...GOLDEN_RECEIPT,
      message: { ...GOLDEN_RECEIPT.message, actualAmount: -6543210 },
    }
    const { evm, writes } = makeMockEvm()
    const runtime = makeMockRuntime()
    const outcome = await makeOnSettled(DEPS({ evm, http: makeMockHttp({ body: bad }) }))(
      runtime,
      makeSettledLog(),
    )
    expect(outcome.startsWith("NO ANCHOR")).toBe(true)
    expect(outcome).toContain("safe integer")
    expect(runtime.reportRequests).toHaveLength(0)
    expect(writes).toHaveLength(0)
  })

  test("tampered signature ⇒ NO report", async () => {
    const bad = {
      ...GOLDEN_RECEIPT,
      signature:
        "0x9352d78655dc906ea0968c6da8114a4ffdb5f9819429a8fd4620c29dc5d5f4fd16fb8e817f0f8aeffe2dfea00db6e869c61f65ba9209edf58806a70abb05052ff",
    }
    const { evm, writes } = makeMockEvm()
    const runtime = makeMockRuntime()
    const outcome = await makeOnSettled(DEPS({ evm, http: makeMockHttp({ body: bad }) }))(
      runtime,
      makeSettledLog(),
    )
    expect(outcome.startsWith("NO ANCHOR")).toBe(true)
    expect(runtime.reportRequests).toHaveLength(0)
    expect(writes).toHaveLength(0)
  })

  test("HTTP failure ⇒ NO report", async () => {
    const { evm, writes } = makeMockEvm()
    const runtime = makeMockRuntime()
    const outcome = await makeOnSettled(DEPS({ evm, http: makeMockHttp({ statusCode: 404 }) }))(
      runtime,
      makeSettledLog(),
    )
    expect(outcome.startsWith("NO ANCHOR")).toBe(true)
    expect(outcome).toContain("404")
    expect(runtime.reportRequests).toHaveLength(0)
    expect(writes).toHaveLength(0)
  })

  test("SDK sendRequest JSON shape: timeout is protobuf-JSON STRING '5s', not an object (sim-dryrun regression)", async () => {
    const captured: CapturedRequest[] = []
    const { evm, writes } = makeMockEvm()
    const runtime = makeMockRuntime()
    const outcome = await makeOnSettled(DEPS({ evm, http: makeMockHttp({ capture: captured }) }))(
      runtime,
      makeSettledLog(),
    )
    expect(outcome).toContain("AUDIT OK")
    expect(captured).toHaveLength(1)
    const req = captured[0].request as CapturedRequest["request"]
    // the exact runtime failure from sim-dryrun: {seconds,nanos} object sent
    // to google.protobuf.Duration ⇒ "cannot decode … from JSON: object".
    // Regression fixes the WIRE shape, not just business success:
    expect(typeof req.timeout).toBe("string")
    expect(req.timeout).toBe("5s")
    expect(req.timeout).toMatch(/^\d+s$/)
    // and the rest of the SDK JSON envelope stays structurally valid:
    expect(req.method).toBe("GET")
    expect(String(req.url)).toContain("/receipt/")
  })

  test("payment state ≠ Settled ⇒ NO report", async () => {
    const { evm, writes } = makeMockEvm({ payment: { state: 1 } })
    const runtime = makeMockRuntime()
    const outcome = await makeOnSettled(DEPS({ evm, http: makeMockHttp() }))(
      runtime,
      makeSettledLog(),
    )
    expect(outcome.startsWith("NO ANCHOR")).toBe(true)
    expect(outcome).toContain("Settled")
    expect(runtime.reportRequests).toHaveLength(0)
    expect(writes).toHaveLength(0)
  })

  test("getPrice failure (revert/transport/decode/model-absent) ⇒ NO report — STRICT, no listing fallback", async () => {
    const { evm, calls, writes } = makeMockEvm({ getPrice: "throw" })
    const runtime = makeMockRuntime()
    const outcome = await makeOnSettled(DEPS({ evm, http: makeMockHttp() }))(
      runtime,
      makeSettledLog(),
    )
    expect(outcome.startsWith("NO ANCHOR")).toBe(true)
    expect(outcome).toContain("getPrice")
    expect(outcome).toContain("NO fallback")
    expect(runtime.reportRequests).toHaveLength(0)
    expect(writes).toHaveLength(0)
    // exactly TWO on-chain reads happened: no getListing fallback read
    expect(calls.map((c) => c.kind)).toEqual(["getPayment", "getPrice"])
  })

  test("getPayment read failure at trigger block ⇒ NO report", async () => {
    const { evm, calls, writes } = makeMockEvm({ failOn: "getPayment" })
    const runtime = makeMockRuntime()
    const outcome = await makeOnSettled(DEPS({ evm, http: makeMockHttp() }))(
      runtime,
      makeSettledLog(),
    )
    expect(outcome.startsWith("NO ANCHOR")).toBe(true)
    expect(outcome).toContain("trigger block")
    expect(runtime.reportRequests).toHaveLength(0)
    expect(writes).toHaveLength(0)
    expect(calls.filter((c) => c.kind === "getPayment")).toHaveLength(1)
  })
})

describe("good signed receipt anchors (verdicts)", () => {
  test("MATCH (δ=0): exactly one report + write, seven-field payload, tx hashes surface", async () => {
    const { outcome, runtime, writes } = await runGolden()
    expect(outcome).toContain("AUDIT OK")
    expect(outcome).toContain("6543210")
    const req = decodeReport(runtime)
    // encodedPayload = base64(businessBytes): assert decodes to the exact pinned fields
    const reqAny = req as { encodedPayload: string }
    const payloadBytes = fromBase64(reqAny.encodedPayload)
    const [paymentId, settledAmount, receiptAmount, receiptHash, upstreamHost, model, verdict] =
      decodeAbiParameters(REPORT_FIELDS, payloadBytes)
    expect(paymentId).toBe(EVENT_PAYMENT_ID)
    expect(settledAmount).toBe(EVENT_ACTUAL)
    expect(receiptAmount).toBe(6543210n)
    expect(receiptHash).toBe(GOLDEN_RECEIPT_HASH)
    expect(upstreamHost).toBe("api.moonshot.cn")
    expect(model).toBe("moonshot-v1-8k")
    expect(verdict).toBe(1)
    expect(writes).toHaveLength(1)
    expect(writes[0].receiver).toBe(TEST_CONFIG.anchorAddress.toLowerCase())
    expect(writes[0].report.toLowerCase()).toBe("0xdonsigned")
    expect(runtime.logs.some((l) => l.includes("ReceiptAnchored"))).toBe(true)
  })

  test("δ = 1 (PRICES_MINUS1) ⇒ MATCH", async () => {
    const { evm, writes } = makeMockEvm({ getPrice: PRICES_MINUS1 })
    const runtime = makeMockRuntime()
    const outcome = await makeOnSettled(DEPS({ evm, http: makeMockHttp() }))(
      runtime,
      makeSettledLog(),
    )
    expect(outcome).toContain("AUDIT OK")
    expect(runtime.reportRequests).toHaveLength(1)
    expect(writes).toHaveLength(1)
  })

  test("δ = 2 (PRICES_PLUS2) ⇒ MISMATCH still anchors with verdict=2", async () => {
    const { evm, writes } = makeMockEvm({ getPrice: PRICES_PLUS2 })
    const runtime = makeMockRuntime()
    const outcome = await makeOnSettled(DEPS({ evm, http: makeMockHttp() }))(
      runtime,
      makeSettledLog(),
    )
    expect(outcome).toContain("DISCREPANCY")
    expect(outcome).toContain("delta=2")
    expect(runtime.reportRequests).toHaveLength(1)
    expect(writes).toHaveLength(1)
    const [,,,, , , verdict] = decodeAbiParameters(REPORT_FIELDS, fromBase64((runtime.reportRequests[0] as { encodedPayload: string }).encodedPayload))
    expect(verdict).toBe(2)
  })

  test("trigger-block invariant holds up to the last read before the report", async () => {
    const { evm, calls, writes } = makeMockEvm()
    const runtime = makeMockRuntime()
    const outcome = await makeOnSettled(DEPS({ evm, http: makeMockHttp() }))(
      runtime,
      makeSettledLog(),
    )
    expect(outcome).toContain("AUDIT OK")
    expect(calls.map((c) => c.kind)).toEqual(["getPayment", "getPrice"])
    assertSameTriggerBlock(calls, TRIGGER_BLOCK)
    expect(writes).toHaveLength(1)
  })

  test("writeReport non-SUCCESS ⇒ ANCHOR FAILED (DON signed, not anchored)", async () => {
    const { evm, writes } = makeMockEvm({ writeTxStatus: 1 })
    const runtime = makeMockRuntime()
    const outcome = await makeOnSettled(DEPS({ evm, http: makeMockHttp() }))(
      runtime,
      makeSettledLog(),
    )
    expect(outcome.startsWith("ANCHOR FAILED")).toBe(true)
    expect(runtime.reportRequests).toHaveLength(1)
    expect(writes).toHaveLength(1)
  })
})

describe("seller/buyer context fails in handler (fail-closed)", () => {
  test("event seller ≠ getPayment seller ⇒ NO report", async () => {
    const { evm, writes } = makeMockEvm({
      payment: { seller: "0x4444444444444444444444444444444444444444" },
    })
    const runtime = makeMockRuntime()
    const outcome = await makeOnSettled(DEPS({ evm, http: makeMockHttp() }))(
      runtime,
      makeSettledLog(),
    )
    expect(outcome.startsWith("NO ANCHOR")).toBe(true)
    expect(outcome).toContain("seller mismatch")
    expect(runtime.reportRequests).toHaveLength(0)
    expect(writes).toHaveLength(0)
  })

  test("event buyer ≠ getPayment buyer ⇒ NO report", async () => {
    const { evm } = makeMockEvm({
      payment: { buyer: "0x2222222222222222222222222222222222222222" },
    })
    const runtime = makeMockRuntime()
    const outcome = await makeOnSettled(DEPS({ evm, http: makeMockHttp() }))(
      runtime,
      makeSettledLog(),
    )
    expect(outcome.startsWith("NO ANCHOR")).toBe(true)
    expect(outcome).toContain("buyer mismatch")
    expect(runtime.reportRequests).toHaveLength(0)
  })

  test("trigger topic0 mismatch ⇒ NO report before any read", async () => {
    const { evm, calls } = makeMockEvm()
    const runtime = makeMockRuntime()
    const log = makeSettledLog()
    const wrongTopic = log as { topics: Uint8Array[] }
    wrongTopic.topics[0] = new Uint8Array(32).fill(9)
    const outcome = await makeOnSettled(DEPS({ evm, http: makeMockHttp() }))(runtime, wrongTopic as never)
    expect(outcome.startsWith("NO ANCHOR")).toBe(true)
    expect(outcome).toContain("topic0")
    expect(calls).toHaveLength(0)
    expect(runtime.reportRequests).toHaveLength(0)
  })
})

describe("single-payment / no-cumulative-audit scope", () => {
  test("one trigger → at most one report; verdict payload carries exactly seven fields", async () => {
    const { runtime } = await runGolden()
    expect(runtime.reportRequests).toHaveLength(1)
    const payload = decodeAbiParameters(REPORT_FIELDS, fromBase64((runtime.reportRequests[0] as { encodedPayload: string }).encodedPayload))
    expect(payload).toHaveLength(7)
  })
})

// ── tiny base64 helper (bubble-encoding used by the SDK prepareReportRequest) ─
function fromBase64(b64: string): Uint8Array {
  if (typeof Buffer !== "undefined") {
    return new Uint8Array(Buffer.from(b64, "base64"))
  }
  throw new Error("no base64 decoder in this bun runtime")
}
