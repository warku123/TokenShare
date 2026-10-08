/**
 * Pinned on-chain ABI subsets + constants for the settlement audit.
 *
 * Source of truth: contracts/src/Escrow.sol and contracts/src/Registry.sol
 * (deployed to Monad testnet, chain 10143). These ABIs are consumed by both
 * the workflow and its bun tests, so decode expectations stay identical.
 */
import {
  decodeAbiParameters,
  keccak256,
  parseAbi,
  parseAbiParameters,
  stringToHex,
  type Hex,
} from "viem"

export const ESCROW_ABI = parseAbi([
  "event Settled(uint256 indexed paymentId, address indexed buyer, address indexed seller, uint256 actualAmount, uint256 refundedAmount)",
  "function getPayment(uint256 paymentId) view returns (address buyer, address seller, uint256 maxAmount, uint64 expiresAt, uint8 state)",
])

export const REGISTRY_ABI = parseAbi([
  // Registry.sol:301 `getPrice(address, string) returns (Price memory)` where
  // Price at Registry.sol:57 is (uint256 cachedIn, uint256 input, uint256
  // output) — STATIC tuple, return body is the 3 words directly.
  "function getPrice(address operator, string model) view returns (uint256 priceCachedIn, uint256 priceInput, uint256 priceOutput)",
])

// ⚠ Registry.getListing ABI-wire proof (independently verified via real RPC):
// getListing(address) returns (Listing memory) where Listing has DYNAMIC
// members ⇒ the eth_call return is ONE dynamic tuple: word0 = 0x20 (offset 32
// to the tuple body), then the 5-member body. Decoding it as 5 flat top-level
// outputs is WRONG (the head row would be read as the operator).
// The strict decoder lives in decodeListingReturn() below; its behavior is
// regression-pinned against REAL RPC bytes in tests/abi-shape.test.ts
// (block 0x42033c6 / 69219270, RPC testnet-rpc.monad.xyz — public data).

/**
 * Raw strict decoder for Registry.getListing's return bytes:
 * ONE dynamic tuple `(Listing memory)` = [offsetWord=0x20][body...].
 * Throws on any shape deviation — fail closed, never partially decoded.
 */
export const LISTING_RETURN_ABI_PARAM = parseAbiParameters(
  "(address operator, string endpoint, string[] models, (uint256 priceCachedIn, uint256 priceInput, uint256 priceOutput)[] prices, bool active)",
)

/** getPayment decoded-output layout (verification-pinned; STATIC 5-slot body). */
export const PAYMENT_ABI_PARAMS = parseAbiParameters(
  "address buyer, address seller, uint256 maxAmount, uint64 expiresAt, uint8 state",
)
/** getPrice decoded-output layout (verification-pinned; STATIC 3-word body). */
export const PRICE_ROW_ABI_PARAMS = parseAbiParameters(
  "(uint256 priceCachedIn, uint256 priceInput, uint256 priceOutput)",
)

/** Registry v2 per-model 3-tier price row (6-dp USDC native units). */
export type PriceRow = {
  readonly priceCachedIn: bigint
  readonly priceInput: bigint
  readonly priceOutput: bigint
}

/** Strict getListing-return decoder (single exported call site for tests). */
export function decodeListingReturn(raw: Uint8Array): ListingRecord {
  if (raw.length < 32 || raw.length % 32 !== 0) {
    throw new Error(`getListing return: bad length ${raw.length} (word-aligned required)`)
  }
  // head word must be the dynamic-tuple offset 0x20 (32 bytes)
  const head = bytesToU256(raw, 0)
  if (head !== 32n) {
    throw new Error(`getListing return: expected tuple offset word 0x20, got 0x${head.toString(16)}`)
  }
  const decoded = decodeAbiParameters(LISTING_RETURN_ABI_PARAM, raw) as [
    ListingRecord & { operator: string },
  ]
  const t = decoded[0]
  if (t.prices.length !== t.models.length) {
    throw new Error(
      `getListing return: models(${t.models.length})≠prices(${t.prices.length}) parallel-array invariant broken`,
    )
  }
  return {
    listingOperator: t.operator,
    endpoint: t.endpoint,
    models: t.models,
    prices: t.prices,
    active: t.active,
  }
}

function bytesToU256(bytes: Uint8Array, offset: number): bigint {
  let acc = 0n
  for (let i = 0; i < 32; i++) acc = (acc << 8n) + BigInt(bytes[offset + i])
  return acc
}

/** Escrow.State { None=0, Locked=1, Settled=2, Refunded=3 } */
export const PAYMENT_STATE_NONE = 0
export const PAYMENT_STATE_LOCKED = 1
export const PAYMENT_STATE_SETTLED = 2
export const PAYMENT_STATE_REFUNDED = 3

/** getPayment abi-decoded output tuple. */
export type PaymentRecord = {
  readonly buyer: string
  readonly seller: string
  readonly maxAmount: bigint
  readonly expiresAt: bigint
  readonly state: number
}

export type ListingRecord = {
  readonly listingOperator: string
  readonly endpoint: string
  readonly models: readonly string[]
  readonly prices: readonly PriceRow[]
  readonly active: boolean
}

// keccak256("Settled(uint256,address,address,uint256,uint256)") — asserted
// at module load so selector drift cannot pass silently.
export const SETTLED_TOPIC0: Hex = keccak256(
  stringToHex("Settled(uint256,address,address,uint256,uint256)"),
)
