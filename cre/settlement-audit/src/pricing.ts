/**
 * Tiered per-model price adjudication (pure).
 *
 * PIN formula (relay/app/pricing.py:41-51, plaintext floor):
 *   expected = (cached*priceCachedIn + (prompt-cached)*priceInput
 *               + completion*priceOutput) // 1e6
 *
 * Verdict: |receipt − expected| ≤ 1 → MATCH (1), else MISMATCH (2).
 * The ±1 unit (0.000001 USDC) absorbs the integer-division rounding slop
 * between the relay's floor and any independent recompute; ±2 is a signal,
 * not noise.
 *
 * All arithmetic is checked invariant-precision of question: inputs are
 * validated as nonnegative safe integers upstream (src/receipt.ts) and prices
 * are on-chain uint256 rows; products stay in bigint. A receipt amount above
 * maxPriceUsd6 fails the sanity ceiling outright.
 */
import type { PriceRow } from "./abi"

/** 1e6 = USDC decimals / per-1M-token price divisor (PIN). */
export const PRICE_SCALE = 1_000_000n

export type TieredUsage = {
  readonly promptTokens: bigint
  readonly cachedTokens: bigint
  readonly completionTokens: bigint
}

/** Integer floor — identical to relay compute_actual (bigint division). */
export function computeExpectedAmount(prices: PriceRow, usage: TieredUsage): bigint {
  const cachedPromptTokens =
    usage.promptTokens >= usage.cachedTokens ? usage.promptTokens - usage.cachedTokens : 0n
  return (
    (usage.cachedTokens * prices.priceCachedIn +
      cachedPromptTokens * prices.priceInput +
      usage.completionTokens * prices.priceOutput) /
    PRICE_SCALE
  )
}

export const VERDICT_MATCH = 1
export const VERDICT_MISMATCH = 2

export type VerdictResult = {
  verdict: 1 | 2
  expected: bigint
  receiptAmount: bigint
  delta: bigint
}

/**
 * Receipt-vs-estimate verdict at the DON level. A *valid, correctly-signed*
 * receipt with a price mismatch is STILL anchored — verdict MISMATCH —
 * because the anchor's job is the audit trail, not the relay's toolbar.
 */
export function assessAmount(
  prices: PriceRow,
  usage: TieredUsage,
  receiptAmount: bigint,
): VerdictResult {
  const expected = computeExpectedAmount(prices, usage)
  const delta = receiptAmount > expected ? receiptAmount - expected : expected - receiptAmount
  const verdict: 1 | 2 = delta <= 1n ? VERDICT_MATCH : VERDICT_MISMATCH
  return { verdict, expected, receiptAmount, delta }
}

/** Sanity ceiling: reject runaway receipt amounts before any anchor. */
export function exceedsCeiling(receiptAmount: bigint, ceiling: bigint): boolean {
  return receiptAmount > ceiling
}
