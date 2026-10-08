/**
 * Receipt verification tests — schema negatives, domain/signature/auth trust
 * boundaries, and the exact EIP-712 golden fixture.
 */
import { describe, expect, test } from "bun:test"
import {
  checkReceiptConsistency,
  parseAndVerifyReceipt,
  RECEIPT_TYPES,
  RELAY_DOMAIN_CHAIN_ID,
  RELAY_DOMAIN_NAME,
  RELAY_DOMAIN_VERSION,
  type ChainContext,
  type ReceiptError,
  type ValidatedReceipt,
} from "../src/receipt"
import {
  EVENT_BUYER,
  EVENT_PAYMENT_ID,
  EVENT_SELLER,
  GOLDEN_RECEIPT,
  GOLDEN_RECEIPT_HASH,
  GOLDEN_SIGNER,
  TEST_CONFIG,
  TRUSTED_SIGNER_REAL,
} from "./helpers"

const GOOD = GOLDEN_RECEIPT
const TRUSTED = GOLDEN_SIGNER // the synthetic key the fixture was signed with

const parse = (raw: unknown, trusted: string = TRUSTED) => parseAndVerifyReceipt(raw, trusted)
const fail = (raw: unknown, kind: "SCHEMA" | "AUTH"): ReceiptError => {
  const res = parse(raw)
  if (res.ok) throw new Error(`expected ${kind} failure, got ok: ${JSON.stringify(raw)}`)
  expect(res.error.kind).toBe(kind)
  return res.error
}
const clone = (overrides: Partial<typeof GOOD.message> | Record<string, unknown>): unknown => ({
  ...GOOD,
  message: { ...GOOD.message, ...overrides },
})
const cloneRoot = (delta: Record<string, unknown>): unknown => ({ ...GOOD, ...delta })

// ─── 1. Golden fixture (valid ⟂ exact EIP-712) ──────────────────────────────

describe("golden EIP-712 fixture", () => {
  test("parses, authenticates and hashes the pinned receipt", () => {
    const res = parse(GOOD)
    expect(res.ok).toBe(true)
    if (!res.ok) throw new Error(res.error.reason)
    const r: ValidatedReceipt = res.receipt
    expect(r.signer).toBe(GOLDEN_SIGNER)
    expect(r.receiptHash).toBe(GOLDEN_RECEIPT_HASH)
    expect(r.paymentId).toBe(42001n)
    expect(r.actualAmount).toBe(6543210n)
    // seller claim is preserved (lower-cased for the context checks below)
    expect(r.seller).toBe("0xabcdef0123456789abcdef01234567890abcdef0")
    expect(r.upstreamHost).toBe("api.moonshot.cn")
    expect(r.model).toBe("moonshot-v1-8k")
  })

  test("passes full chain-context consistency", () => {
    const res = parse(GOOD)
    if (!res.ok) throw new Error("fixture must parse")
    const ctx: ChainContext = {
      paymentId: EVENT_PAYMENT_ID,
      eventSeller: EVENT_SELLER.toLowerCase(),
      eventBuyer: EVENT_BUYER,
      payment: {
        buyer: EVENT_BUYER,
        seller: EVENT_SELLER.toLowerCase(),
        maxAmount: 12000000n,
        expiresAt: 9999999999n,
        state: 2,
      },
    }
    const okCtx = checkReceiptConsistency(res.receipt, ctx)
    expect(okCtx.ok).toBe(true)
  })

  test("a lowercased-seller receipt is the SAME signed message (address bytes are case-insensitive)", () => {
    // Both eth_account and viem ABI-encode addresses by bytes, so re-casing
    // the address in the JSON does not change the EIP-712 digest. The relay
    // canonicalizes to checksummed (receipt.py to_checksum_address) either way.
    const lowercased: unknown = cloneRoot({
      message: { ...GOOD.message, seller: GOOD.message.seller.toLowerCase() },
    })
    const res = parse(lowercased)
    expect(res.ok).toBe(true)
    if (!res.ok) throw new Error(res.error.reason)
    expect(res.receipt.signer).toBe(GOLDEN_SIGNER)
    expect(res.receipt.seller).toBe("0xabcdef0123456789abcdef01234567890abcdef0")
  })
})

// ─── 2. Finite trust boundary: signer comes from config, not the receipt ────

describe("trusted-signer enforcement", () => {
  test("non-trusted signer ⇒ AUTH (signer never taken from the receipt)", () => {
    // The production shared signer is a different key than the fixture's —
    // the golden receipt must FAIL against it.
    const err = parseAndVerifyReceipt(GOOD, TRUSTED_SIGNER_REAL)
    if (err.ok) throw new Error("golden must not pass against the production signer")
    expect(err.error.kind).toBe("AUTH")
    expect(err.error.reason).toContain(TRUSTED_SIGNER_REAL)
  })

  test("valid content signed by the tested signer identity passes", () => {
    // TEST_CONFIG pins the fixture's own signer as the trusted one: the
    // golden receipt must validate END-TO-END there.
    const res = parseAndVerifyReceipt(GOOD, TEST_CONFIG.receiptSignerAddress)
    expect(res.ok).toBe(true)
  })
})

// ─── 3. Domain pin ───────────────────────────────────────────────────────────

describe("domain pin (EXACT TokenShare Relay / 1 / 10143)", () => {
  test("wrong domain name ⇒ SCHEMA", () => {
    const raw = cloneRoot({ domain: { name: "TokenShare relay", version: "1", chainId: 10143 } })
    const err = fail(raw, "SCHEMA")
    expect(err.reason).toContain(RELAY_DOMAIN_NAME)
  })
  test("wrong domain version ⇒ SCHEMA", () => {
    const raw = cloneRoot({ domain: { name: RELAY_DOMAIN_NAME, version: "2", chainId: 10143 } })
    fail(raw, "SCHEMA")
  })
  test("wrong chainId (1) ⇒ SCHEMA", () => {
    const raw = cloneRoot({ domain: { ...GOOD.domain, chainId: 1 } })
    const err = fail(raw, "SCHEMA")
    expect(err.reason).toContain(RELAY_DOMAIN_CHAIN_ID.toString())
  })
  test("chainId as string ⇒ SCHEMA", () => {
    const raw = cloneRoot({ domain: { ...GOOD.domain, chainId: "10143" } })
    fail(raw, "SCHEMA")
  })
  test("missing domain ⇒ SCHEMA", () => {
    fail({ message: GOOD.message, signature: GOOD.signature }, "SCHEMA")
  })
})

// ─── 4. Schema negatives per field ──────────────────────────────────────────

describe("schema negatives", () => {
  test("non-object receipt ⇒ SCHEMA", () => {
    fail("not-a-receipt", "SCHEMA")
    fail(null, "SCHEMA")
    fail(42, "SCHEMA")
  })

  test("each numeric field: negative / float / string / > 2^53-1 ⇒ SCHEMA", () => {
    for (const key of [
      "paymentId",
      "promptTokens",
      "cachedTokens",
      "completionTokens",
      "actualAmount",
    ] as const) {
      expect(fail(clone({ [key]: -1 }), "SCHEMA").reason).toContain(key)
      expect(fail(clone({ [key]: 10.5 }), "SCHEMA").reason).toContain(key)
      expect(fail(clone({ [key]: "1" }), "SCHEMA").reason).toContain(key)
      expect(fail(clone({ [key]: 9007199254740992 }), "SCHEMA").reason).toContain(key)
    }
  })

  test("cachedTokens > promptTokens ⇒ SCHEMA", () => {
    const err = fail(clone({ cachedTokens: 1235, promptTokens: 1234 }), "SCHEMA")
    expect(err.reason).toContain("≤")
  })

  test("cached == prompt is legal at schema level (signature integrity then applies)", () => {
    // Content changes (promptTokens/cachedTokens here) invalidate the golden
    // signature ⇒ the receipt may pass SCHEMA but must fail AUTH. Reaching
    // AUTH (not "≤" failure) proves the cached ≤ prompt gate accepts equality.
    const raw = clone({ promptTokens: 1234, cachedTokens: 1234 })
    const res = parse(raw)
    if (res.ok) throw new Error("changed content cannot stay auth-valid")
    expect(res.error.kind).toBe("AUTH")
  })

  test("seller malformed ⇒ SCHEMA", () => {
    expect(fail(clone({ seller: "not-an-address" }), "SCHEMA").reason).toContain("seller")
    expect(fail(clone({ seller: "0x1234" }), "SCHEMA").reason).toContain("seller")
    expect(fail(clone({ seller: "0xZZZDef0123456789abcdEF01234567890ABcDeF0" }), "SCHEMA").reason).toContain(
      "seller",
    )
  })

  test("upstreamHost malformed ⇒ SCHEMA", () => {
    expect(fail(clone({ upstreamHost: "" }), "SCHEMA").reason).toContain("upstreamHost")
    expect(fail(clone({ upstreamHost: "http://api.moonshot.cn" }), "SCHEMA").reason).toContain(
      "upstreamHost",
    )
    expect(fail(clone({ upstreamHost: "api.moon shot.cn" }), "SCHEMA").reason).toContain(
      "upstreamHost",
    )
  })

  test("model empty/absent ⇒ SCHEMA", () => {
    expect(fail(clone({ model: "" }), "SCHEMA").reason).toContain("model")
  })

  test("extra keys tolerated (X-Receipt shape is additive)", () => {
    const raw = cloneRoot({ message: { ...GOOD.message, extra: "ignored" } })
    const res = parse(raw)
    // The extra key is NOT part of the signed struct; recovery still matches.
    expect(res.ok).toBe(true)
  })
})

// ─── 5. Signature negatives ─────────────────────────────────────────────────

describe("signature negatives", () => {
  test("length ≠ 65 bytes ⇒ SCHEMA", () => {
    expect(fail(cloneRoot({ signature: GOOD.signature.slice(0, -2) }), "SCHEMA").reason).toContain(
      "65-byte",
    )
    expect(
      fail(cloneRoot({ signature: GOOD.signature + "ff" }), "SCHEMA").reason,
    ).toContain("65-byte")
  })
  test("missing 0x prefix ⇒ SCHEMA", () => {
    fail(cloneRoot({ signature: GOOD.signature.slice(2) }), "SCHEMA")
  })
  test("non-hex payload ⇒ SCHEMA", () => {
    fail(cloneRoot({ signature: `0x${"zz".repeat(65)}` }), "SCHEMA")
  })
  test("tampered r ⇒ AUTH", () => {
    // flip the first r nibble (index 2; "0x" prefix stays) → different identity
    const bad = GOOD.signature.slice(0, 2) + "1" + GOOD.signature.slice(3)
    expect(fail(cloneRoot({ signature: bad }), "AUTH").kind).toBe("AUTH")
  })
  test("tampered s ⇒ AUTH", () => {
    const bad = GOOD.signature.slice(0, 70) + "00" + GOOD.signature.slice(72)
    expect(fail(cloneRoot({ signature: bad }), "AUTH").kind).toBe("AUTH")
  })
  test("invalid recovery id v=31 ⇒ AUTH (recovery throws)", () => {
    const bad = GOOD.signature.slice(0, 130) + "1f"
    const err = fail(cloneRoot({ signature: bad }), "AUTH")
    expect(err.reason).toContain("recovery")
  })
  test("absent signature ⇒ SCHEMA", () => {
    fail({ domain: GOOD.domain, message: GOOD.message }, "SCHEMA")
  })
})

// ─── 6. Chain-context consistency negatives ─────────────────────────────────

describe("chain-context consistency", () => {
  const parsedFixture = (): ValidatedReceipt => {
    const res = parse(GOOD)
    if (!res.ok) throw new Error("fixture must parse")
    return res.receipt
  }
  const baseCtx = (): ChainContext => ({
    paymentId: EVENT_PAYMENT_ID,
    eventSeller: EVENT_SELLER.toLowerCase(),
    eventBuyer: EVENT_BUYER,
    payment: { buyer: EVENT_BUYER, seller: EVENT_SELLER.toLowerCase(), maxAmount: 12000000n, expiresAt: 9999999999n, state: 2 },
  })

  test("trigger paymentId mismatch ⇒ fail", () => {
    const ctx = { ...baseCtx(), paymentId: 42002n }
    const res = checkReceiptConsistency(parsedFixture(), ctx)
    expect(res.ok).toBe(false)
    if (!res.ok) expect(res.reason).toContain("paymentId")
  })

  test("receipt seller ≠ event seller ⇒ fail", () => {
    const ctx = { ...baseCtx(), eventSeller: "0x4444444444444444444444444444444444444444" }
    const res = checkReceiptConsistency(parsedFixture(), ctx)
    expect(res.ok).toBe(false)
    if (!res.ok) expect(res.reason).toContain("seller mismatch")
  })

  test("receipt seller ≠ getPayment seller ⇒ fail", () => {
    const ctx = baseCtx()
    ctx.payment = { ...ctx.payment, seller: "0x4444444444444444444444444444444444444444" }
    const res = checkReceiptConsistency(parsedFixture(), ctx)
    expect(res.ok).toBe(false)
    if (!res.ok) expect(res.reason).toContain("seller mismatch")
  })

  test("event buyer ≠ getPayment buyer ⇒ fail", () => {
    const ctx = baseCtx()
    ctx.payment = { ...ctx.payment, buyer: "0x2222222222222222222222222222222222222222" }
    const res = checkReceiptConsistency(parsedFixture(), ctx)
    expect(res.ok).toBe(false)
    if (!res.ok) expect(res.reason).toContain("buyer mismatch")
  })

  test("payment state ≠ Settled (Locked=1) ⇒ fail", () => {
    const ctx = baseCtx()
    ctx.payment = { ...ctx.payment, state: 1 }
    const res = checkReceiptConsistency(parsedFixture(), ctx)
    expect(res.ok).toBe(false)
    if (!res.ok) expect(res.reason).toContain("state")
  })

  test("state Refunded=3 ⇒ fail", () => {
    const ctx = baseCtx()
    ctx.payment = { ...ctx.payment, state: 3 }
    const res = checkReceiptConsistency(parsedFixture(), ctx)
    if (res.ok) throw new Error("refunded must not pass")
  })

  test("uint256 paymentId beyond JSON safe range ⇒ fail closed", () => {
    const res = checkReceiptConsistency(parsedFixture(), {
      ...baseCtx(),
      paymentId: 2n ** 53n,
    })
    expect(res.ok).toBe(false)
  })

  test("case-insensitive seller matching is allowed", () => {
    const ctx = baseCtx()
    // Real decoded topics keep the 0x prefix; only the body may vary in case.
    ctx.eventSeller = ("0x" + EVENT_SELLER.slice(2).toUpperCase()) as Address
    const res = checkReceiptConsistency(parsedFixture(), ctx)
    expect(res.ok).toBe(true)
  })
})

// ─── 7. Receipt types pin (field order/type fidelity vs receipt.py:30-49) ───

describe("RECEIPT_TYPES pin", () => {
  test("Receipt field order/types exactly mirror receipt.py", () => {
    expect(RECEIPT_TYPES.Receipt.map((f) => `${f.name}:${f.type}`)).toEqual([
      "paymentId:uint256",
      "promptTokens:uint256",
      "cachedTokens:uint256",
      "completionTokens:uint256",
      "actualAmount:uint256",
      "seller:address",
      "upstreamHost:string",
      "model:string",
    ])
    expect(RECEIPT_TYPES.EIP712Domain.map((f) => f.name)).toEqual(["name", "version", "chainId"])
  })
})

// (float/over-range/`v` tampering covered in dedicated groups above)
