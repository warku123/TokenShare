/**
 * Registry/Escrow decode-shape regression tests.
 *
 * EVIDENCE STANDARD (ora39 2026-10-08): the getListing shape is pinned
 * against RAW RPC BYTES captured from the real on-chain Registry — the
 * fixture is NOT generated with the workflow's own ABI encoders (those are
 * circular as evidence). getPayment/getPrice ABI shapes were independently
 * verified by ora39 (static returns); the tests below are DECODER
 * regressions, not ABI proofs.
 */
import { describe, expect, test } from "bun:test"
import { decodeAbiParameters, encodeAbiParameters, hexToBytes, parseAbiParameters } from "viem"
import {
  decodeListingReturn,
  PAYMENT_ABI_PARAMS,
  PRICE_ROW_ABI_PARAMS,
  type PriceRow,
} from "../src/abi"
import { EVENT_BUYER, EVENT_SELLER } from "./helpers"

// ─── Independent real-contract fixture (public data, read-only eth_call) ────
//
//   eth_call  Registry 0xeD347cDc1761750E20C024459b38dedFb1462254
//   data      0x084af0b2 getListing(address)
//             + 0x38c26E1782b7E6F656Aa35EbF473e2ff0D718Dd6 (seller)
//   block     0x42033c6 (69219270) — FIXED historical height
//   rpc       https://testnet-rpc.monad.xyz (public, France 2026-10-08)
//   return    768 bytes: word0 = 0x20 ⇒ ONE dynamic (Listing memory) tuple
//
// Values below were read off the raw hex BY HAND (operator word, endpoint
// string, models array count + strings, prices rows, active word) and only
// then pinned — the workflow decoder is the side under test.

/** Seller/operator of the pinned listing. */
export const FIXTURE_OPERATOR = "0x38c26e1782b7e6f656aa35ebf473e2ff0d718dd6"
/** Pinned endpoint string (word count 0x35 = 53 bytes, unpadded here). */
export const FIXTURE_ENDPOINT =
  "https://c30652f4833465adda9bdc7ac557e8b45e8e66c6-8787.dstack-pha-prod5.phala.network"
/** models array length = 2 (word 0x2 offset 0x120 region). */
export const FIXTURE_MODEL_COUNT = 2

const FIXTURE_REG_LISTING_RAW =
"0x" +
  "0000000000000000000000000000000000000000000000000000000000000020" +
  "00000000000000000000000038c26e1782b7e6f656aa35ebf473e2ff0d718dd6" +
  "00000000000000000000000000000000000000000000000000000000000000a0" +
  "0000000000000000000000000000000000000000000000000000000000000120" +
  "0000000000000000000000000000000000000000000000000000000000000200" +
  "0000000000000000000000000000000000000000000000000000000000000001" +
  "0000000000000000000000000000000000000000000000000000000000000054" +
  "68747470733a2f2f633330363532663438333334363561646461396264633761" +
  "633535376538623435653865363663362d383738372e64737461636b2d706861" +
  "2d70726f64352e7068616c612e6e6574776f726b000000000000000000000000" +
  "0000000000000000000000000000000000000000000000000000000000000002" +
  "0000000000000000000000000000000000000000000000000000000000000040" +
  "0000000000000000000000000000000000000000000000000000000000000080" +
  "0000000000000000000000000000000000000000000000000000000000000007" +
  "6b332d3235366b00000000000000000000000000000000000000000000000000" +
  "0000000000000000000000000000000000000000000000000000000000000019" +
  "6b696d692d666f722d636f64696e672d68696768737065656400000000000000" +
  "0000000000000000000000000000000000000000000000000000000000000002" +
  "00000000000000000000000000000000000000000000000000000000002dc6c0" +
  "00000000000000000000000000000000000000000000000000000000005b8d80" +
  "0000000000000000000000000000000000000000000000000000000000895440" +
  "000000000000000000000000000000000000000000000000000000000016e360" +
  "00000000000000000000000000000000000000000000000000000000002dc6c0" +
  "000000000000000000000000000000000000000000000000000000000044aa20"

const FIXTURE_ACTIVE_WORD =
  "0x0000000000000000000000000000000000000000000000000000000000000001"

const FIXTURE_HEAD_OFFSET_WORD =
  "0x0000000000000000000000000000000000000000000000000000000000000020"

describe("getListing real-RPC bytes regression (strict, fail-closed decoder)", () => {
  test("fixture starts with dynamic-tuple offset word 0x20 (independent hex read)", () => {
    expect(FIXTURE_REG_LISTING_RAW.slice(0, 66)).toBe(FIXTURE_HEAD_OFFSET_WORD)
    // member4 active word (offset = 32 + 0x20*4 + tail... verified by decode):
    const bytes = hexToBytes(FIXTURE_REG_LISTING_RAW as `0x${string}`)
    expect(bytes.length % 32).toBe(0)
    expect(bytes.length).toBe(768) // rpc observed length, byte-for-byte pin
  })

  test("strict single-tuple decoder succeeds on the REAL bytes and pins semantics", () => {
    const listing = decodeListingReturn(hexToBytes(FIXTURE_REG_LISTING_RAW as `0x${string}`))
    expect(listing.listingOperator.toLowerCase()).toBe(FIXTURE_OPERATOR)
    expect(listing.endpoint).toBe(FIXTURE_ENDPOINT)
    expect(listing.models.length).toBe(FIXTURE_MODEL_COUNT)
    expect(listing.active).toBe(true)
  })

  test("old 5-flat-outputs decode of the SAME bytes would misread (proof the shape was wrong)", () => {
    // Reading the real return as 5 flat top-level outputs treats the offset
    // word (0x20) as ... the operator decode path consumes word0 as data —
    // here we show the first word IS the offset 32, not a valid address:
    expect(FIXTURE_REG_LISTING_RAW.slice(0, 66)).toBe(FIXTURE_HEAD_OFFSET_WORD)
    expect(FIXTURE_REG_LISTING_RAW.slice(0, 66)).not.toBe(
      `0x000000000000000000000000${FIXTURE_OPERATOR}`,
    )
  })

  test("corrupted/truncated return bytes ⇒ decoder throws (no partial decode)", () => {
    const good = hexToBytes(FIXTURE_REG_LISTING_RAW as `0x${string}`)
    // (a) wrong head offset word (0x20 → 0x40)
    const badHead = good.slice()
    badHead[31] = 0x40
    expect(() => decodeListingReturn(badHead)).toThrowError(/0x20/)
    // (b) truncated body
    expect(() => decodeListingReturn(good.slice(0, good.length - 32))).toThrowError()
    // (c) non-word-aligned length
    expect(() => decodeListingReturn(good.slice(1))).toThrowError(/length/)
    // (d) models length word (word index 10 ⇒ byte 351) diverged to 3 while
    //     prices claims 2 — v decode must throw, never return a partial view
    const diverged = good.slice()
    diverged[351] = 3
    expect(() => decodeListingReturn(diverged)).toThrowError()
  })

  test("parallel-array invariant: models.length == prices.length asserted post-decode", () => {
    const listing = decodeListingReturn(hexToBytes(FIXTURE_REG_LISTING_RAW as `0x${string}`))
    expect(listing.prices.length).toBe(listing.models.length)
    // AND the pinned price rows (hand-read from the RPC hex):
    const rows: PriceRow[] = listing.prices as unknown as PriceRow[]
    expect(rows[0].priceCachedIn).toBe(3_000_000n) // 0x2dc6c0
    expect(rows[0].priceInput).toBe(6_000_000n) // 0x5b8d80
    expect(rows[0].priceOutput).toBe(9_000_000n) // 0x895440
    expect(rows[1].priceCachedIn).toBe(1_500_000n) // 0x16e360
    expect(rows[1].priceInput).toBe(3_000_000n) // 0x2dc6c0
    expect(rows[1].priceOutput).toBe(4_500_000n) // 0x44aa20
    // active flag word pinned literally:
    expect(FIXTURE_ACTIVE_WORD).toBe("0x0000000000000000000000000000000000000000000000000000000000000001")
  })
})

// ─── Decoder regressions for the verification-pinned static shapes ──────────

describe("getPayment decoder regression (STATIC body; shape verified by ora39)", () => {
  test("flat 5-slot decode round-trips buyer/seller/max/expires/state", () => {
    // NOTE: this is a DECODER regression (parse+decode consistency), NOT an
    // ABI proof — the getPayment signature is independent-verified (ora39).
    const body = encodeAbiParameters(PAYMENT_ABI_PARAMS, [
      EVENT_BUYER,
      EVENT_SELLER,
      12_000_000n,
      (1n << 64n) - 1n,
      2,
    ])
    const [buyer, seller, maxAmount, expiresAt, state] = decodeAbiParameters(PAYMENT_ABI_PARAMS, body)
    expect(buyer).toBe(EVENT_BUYER)
    expect(seller).toBe(EVENT_SELLER)
    expect(maxAmount).toBe(12_000_000n)
    expect(expiresAt).toBe((1n << 64n) - 1n)
    expect(state).toBe(2)
  })
})

describe("getPrice decoder regression (STATIC body; shape verified by ora39)", () => {
  const row: PriceRow = { priceCachedIn: 700n, priceInput: 800n, priceOutput: 900n }

  test("single static-tuple decode round-trips the 3 words", () => {
    // A `Price memory` static tuple return is exactly the 3 words inline —
    // NO head wrapper word for static returns (contrast: getListing dynamic).
    const body = encodeAbiParameters(PRICE_ROW_ABI_PARAMS, [
      { priceCachedIn: row.priceCachedIn, priceInput: row.priceInput, priceOutput: row.priceOutput },
    ])
    expect((body.length - 2) / 2).toBe(96) // 3 words exactly
    const decoded = (decodeAbiParameters(PRICE_ROW_ABI_PARAMS, body) as [PriceRow])[0]
    expect(decoded.priceCachedIn).toBe(700n)
    expect(decoded.priceInput).toBe(800n)
    expect(decoded.priceOutput).toBe(900n)
  })
})
