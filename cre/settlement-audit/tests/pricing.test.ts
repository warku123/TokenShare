/** Adjudication math: PIN floor formula + ±1 tolerance + safety ceiling. */
import { describe, expect, test } from "bun:test"
import {
  assessAmount,
  computeExpectedAmount,
  exceedsCeiling,
  PRICE_SCALE,
  VERDICT_MATCH,
  VERDICT_MISMATCH,
} from "../src/pricing"
import type { PriceRow } from "../src/abi"
import { PRICES_EXACT, PRICES_MINUS1, PRICES_PLUS2 } from "./helpers"

const USAGE = {
  promptTokens: 1234n,
  cachedTokens: 234n,
  completionTokens: 567n,
} as const

const row = (a: bigint, b: bigint, c: bigint): PriceRow => ({
  priceCachedIn: a,
  priceInput: b,
  priceOutput: c,
})

describe("PIN pricing formula (integer 6dp floor, preserved)", () => {
  test("exact relay formula: (cached*a + (prompt-cached)*b + completion*c) // 1e6", () => {
    // Hand-computed: 234*0 + 1000*6543210000 + 567*0 = 6.543210e12; floor/1e6
    expect(computeExpectedAmount(PRICES_EXACT, USAGE)).toBe(6543210n)
    expect(computeExpectedAmount(PRICES_EXACT, USAGE)).toBe((234n * 0n + 1000n * 6543210000n) / PRICE_SCALE)
  })

  test("non-cached tier = prompt − cached only", () => {
    expect(computeExpectedAmount(row(1n, 1n, 1n), { promptTokens: 10n, cachedTokens: 10n, completionTokens: 0n })).toBe(
      0n, // 10*1e6-ish: (10 in cached tier + 0 non-cached)*... /1e6 → 0
    )
    expect(computeExpectedAmount(row(0n, 1n, 0n), { promptTokens: 999999n, cachedTokens: 0n, completionTokens: 0n })).toBe(
      0n, // 999999*1 // 1e6 = 0 (floor preserves the under-scale remainder)
    )
    expect(computeExpectedAmount(row(0n, 1n, 0n), { promptTokens: 1000000n, cachedTokens: 0n, completionTokens: 0n })).toBe(
      1n, // 1000000*1 // 1e6 = 1
    )
  })

  test("CEIL is never applied — remainder is floor-dropped (PIN parity with relay)", () => {
    // (1000 * 6543210001) / 1e6: mantissa 6543210.001, must floor to 6543210
    expect(computeExpectedAmount(row(0n, 6543210001n, 0n), USAGE)).toBe(6543210n)
  })

  test("cached/prompt/completion all weighted", () => {
    // (4*1e6 cached + 6*1e6 non-cached + 6*1e6 completion) // 1e6 = 4+6+6 = 16
    expect(
      computeExpectedAmount(row(1_000_000n, 1_000_000n, 1_000_000n), {
        promptTokens: 10n,
        cachedTokens: 4n,
        completionTokens: 6n,
      }),
    ).toBe(16n)
  })
})

describe("±1 tolerance vs MISMATCH", () => {
  const receiptAmount = 6543210n

  test("δ = 0 ⇒ MATCH", () => {
    expect(assessAmount(PRICES_EXACT, USAGE, receiptAmount).verdict).toBe(VERDICT_MATCH)
  })
  test("δ = 1 ⇒ MATCH (still anchored, verdict 1)", () => {
    const res = assessAmount(PRICES_MINUS1, USAGE, receiptAmount)
    expect(res.verdict).toBe(VERDICT_MATCH)
    expect(res.expected).toBe(6543211n)
    expect(res.delta).toBe(1n)
  })
  test("δ = 2 ⇒ MISMATCH", () => {
    const res = assessAmount(PRICES_PLUS2, USAGE, receiptAmount)
    expect(res.verdict).toBe(VERDICT_MISMATCH)
    expect(res.expected).toBe(6543212n)
    expect(res.delta).toBe(2n)
  })
  test("δ = 9 ⇒ MISMATCH with exact delta", () => {
    const res = assessAmount(row(0n, 6543219000n, 0n), USAGE, receiptAmount)
    expect(res.verdict).toBe(VERDICT_MISMATCH)
    expect(res.delta).toBe(9n)
  })
})

describe("safety ceiling", () => {
  test("over ceiling rejected", () => {
    expect(exceedsCeiling(1000000001n, 1000000000n)).toBe(true)
    expect(exceedsCeiling(1000000000n, 1000000000n)).toBe(false)
  })

  test("verdict uses bigint — no float drift at scale", () => {
    // 1.6 USDC per 1M tokens × 1M tokens = 1.6 USDC = 1_600_000 native units:
    const big = row(0n, 1_600_000n, 0n)
    const expected = computeExpectedAmount(big, { promptTokens: 1_000_000n, cachedTokens: 0n, completionTokens: 0n })
    expect(expected).toBe(1_600_000n)
    expect(assessAmount(big, { promptTokens: 1_000_000n, cachedTokens: 0n, completionTokens: 0n }, expected).verdict).toBe(
      VERDICT_MATCH,
    )
  })
})
