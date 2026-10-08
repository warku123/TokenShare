/**
 * Relay receipt verification (pure, synchronous).
 *
 * Pinned contract (verbatim, relay/app/receipt.py:30-49):
 *
 *   domain  = {name: "TokenShare Relay", version: "1", chainId: 10143}
 *   Receipt = (uint256 paymentId, uint256 promptTokens, uint256 cachedTokens,
 *              uint256 completionTokens, uint256 actualAmount, address seller,
 *              string upstreamHost, string model)
 *   X-Receipt JSON = {domain, message, signature}
 *
 * Everything here is NO-IO: schema/semantic validation and signature recovery
 * take plain values in and return decoded results out. The handler wires them
 * to the runtime; tests run them directly.
 *
 * Crypto: viem `recoverTypedDataAddress` is ASYNC (dynamic-imports
 * @noble/curves), which the CRE runtime cannot use. This module performs the
 * recovery SYNCHRONOUSLY with the exact same primitives already installed:
 * viem `hashTypedData` (sync EIP-712 digest) + @noble/curves secp256k1
 * `Signature.fromCompact(...).addRecoveryBit(bit).recoverPublicKey(digest)`,
 * i.e. byte-for-byte what viem's internal recoverPublicKey/recoverAddress do.
 */
import { checksumAddress, hashTypedData, keccak256, type Address, type Hex } from "viem"
import { secp256k1 } from "@noble/curves/secp256k1"
import { sameAddress } from "./config"
import { PAYMENT_STATE_SETTLED, type PaymentRecord } from "./abi"

// ─── Pinned EIP-712 structure ────────────────────────────────────────────────

export const RELAY_DOMAIN_NAME = "TokenShare Relay"
export const RELAY_DOMAIN_VERSION = "1"
/** Monad testnet — receipts from the relay must target exactly this chain. */
export const RELAY_DOMAIN_CHAIN_ID = 10143

/** Exact field order/types of receipt.py RECEIPT_TYPES["Receipt"] (PIN). */
export const RECEIPT_TYPES = {
  EIP712Domain: [
    { name: "name", type: "string" },
    { name: "version", type: "string" },
    { name: "chainId", type: "uint256" },
  ],
  Receipt: [
    { name: "paymentId", type: "uint256" },
    { name: "promptTokens", type: "uint256" },
    { name: "cachedTokens", type: "uint256" },
    { name: "completionTokens", type: "uint256" },
    { name: "actualAmount", type: "uint256" },
    { name: "seller", type: "address" },
    { name: "upstreamHost", type: "string" },
    { name: "model", type: "string" },
  ],
} as const

/**
 * Relay receipt JSON — pin-exact contract from relay/app/receipt.py.
 * Numeric fields arrive as JSON numbers (validated to be nonnegative safe
 * integers in Double-precision range; on-chain they are uint256, the safe
 * ceiling plus off-chain sellers keep these well inside 2^53-1).
 */
export type ReceiptJSON = {
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
 * Fully validated receipt + recovered signer identity. `seller` stays the
 * receipt's own claim (checked for consistency against the chain in
 * `checkReceiptConsistency`); `signer` is the crypto identity.
 */
export type ValidatedReceipt = {
  readonly paymentId: bigint
  readonly promptTokens: bigint
  readonly cachedTokens: bigint
  readonly completionTokens: bigint
  readonly actualAmount: bigint
  readonly seller: string
  readonly upstreamHost: string
  readonly model: string
  readonly signature: Hex
  /** Recovered from `signature`; MUST equal config.receiptSignerAddress. */
  readonly signer: Address
  /** keccak256(signature)—the anchor-primary per ReceiptAnchor. */
  readonly receiptHash: Hex
}

type ReceiptSchemaError = { kind: "SCHEMA"; reason: string }
type ReceiptAuthError = { kind: "AUTH"; reason: string }
export type ReceiptError = ReceiptSchemaError | ReceiptAuthError

export type ReceiptParseResult =
  | { ok: true; receipt: ValidatedReceipt }
  | { ok: false; error: ReceiptError }

// ─── Low-level validation helpers ────────────────────────────────────────────

const ADDRESS_RE = /^0x[0-9a-fA-F]{40}$/
const HEX64 = /^0x[0-9a-fA-F]{128}$/
/**
 * Hostname pin: relay sends uri.hostname (relay/app/main.py:_upstream_host) —
 * dot-separated DNS labels or a single label/local IP, no scheme/path/port.
 */
const HOSTNAME_EDGE = /^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$/
const HOSTNAME_RE =
  /^(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$/
const isHostname = (s: string): boolean => HOSTNAME_RE.test(s) || HOSTNAME_EDGE.test(s)

/** 65-byte EIP-191/712 ECDSA signature: r(32) || s(32) || v(1), 0x-prefixed. */
export const isSignature65 = (sig: string): boolean =>
  typeof sig === "string" && sig.startsWith("0x") && HEX64.test(sig.slice(0, 130)) &&
  /^[0-9a-fA-F]{2}$/.test(sig.slice(130))

/**
 * EIP-191 recovery bits — eth_account always emits v ∈ {27,28}; {0,1} is the
 * yParity form. Anything else (including EIP-155 chain-derived v) is invalid
 * for a 65-byte personal-free typed-data signature.
 */
const recoveryBitFromV = (v: number): number => {
  if (v === 0 || v === 1) return v
  if (v === 27) return 0
  if (v === 28) return 1
  return -1
}

/**
 * Synchronous EIP-712 signature recovery.
 *
 * viem's `recoverTypedDataAddress` is async (it awaits a dynamic
 * `import('@noble/curves/secp256k1')`) — unusable in the CRE runtime. This
 * helper performs IDENTICAL steps synchronously:
 *   digest = viem hashTypedData({domain, message, primaryType, types})
 *   Signature.fromCompact(r||s).addRecoveryBit(v_bit).recoverPublicKey(digest)
 *   address = checksumAddress(keccak256(pub[1:])[12:])
 * Throws on recover failure (curve rejects out-of-range r/s etc.).
 */
export function recoverTypedDataAddressSync(parameters: {
  domain: { name?: string; version?: string; chainId?: number }
  message: Record<string, unknown>
  primaryType: string
  types: Record<string, { name: string; type: string }[] | undefined>
  signature: string
}): Address {
  const { domain, message, primaryType, types, signature } = parameters
  if (!isSignature65(signature)) {
    throw new Error("invalid 65-byte typed-data signature")
  }
  const digest: Hex = hashTypedData({ domain, message, primaryType, types })
  const sigHex = signature.toLowerCase()
  const bit = recoveryBitFromV(parseInt(sigHex.slice(130), 16))
  if (bit === -1) throw new Error(`invalid signature recovery id: v=0x${sigHex.slice(130)}`)
  // Mirrors viem recoverPublicKey (non-object branch):
  const sigI = secp256k1.Signature.fromCompact(sigHex.slice(2, 130)).addRecoveryBit(bit)
  // noble toHex(false) → 130 hex chars = "04" + full 64-byte key (no 0x).
  const publicKey = sigI.recoverPublicKey(digest.slice(2)).toHex(false)
  // Mirrors viem publicKeyToAddress: keccak over the 64-byte key only
  // (drop the 0x04 marker), take the low 20 bytes, checksum.
  const address = keccak256(`0x${publicKey.slice(2)}`).substring(26)
  return checksumAddress(`0x${address}`) as Address
}

// ─── Receipt validation ──────────────────────────────────────────────────────

type Path = { [key: string]: unknown }

const numberField = (obj: Path, key: string, issues: string[]): number => {
  const v = obj[key]
  if (typeof v !== "number") {
    issues.push(`message.${key} must be a JSON number (no strings/BigInts)`)
    return -1
  }
  if (!Number.isSafeInteger(v) || v < 0) {
    issues.push(`message.${key} must be a nonnegative safe integer (0..2^53-1)`)
    return -1
  }
  return v
}

const stringField = (obj: Path, key: string, issues: string[]): string => {
  const v = obj[key]
  if (typeof v !== "string" || v.length === 0) {
    issues.push(`message.${key} must be a nonempty string`)
    return ""
  }
  return v
}

/**
 * Parse + validate + authenticate an X-Receipt JSON (as returned by the
 * relay's GET /receipt/{paymentId}). Failure ⇒ NO report, NO anchoring.
 *
 * Auth model: the signature must recover to `trustedSigner` from CONFIG —
 * the preconfigured shared relay key. The receipt is never allowed to
 * nominate its own verifier.
 *
 * Unknown-key policy (deliberate ACCEPTANCE, not laxness): top-level or
 * message keys beyond the pinned shape are tolerated because every REQUIRED
 * SIGNED field is schema-validated below (type / nonnegative safe integer /
 * address / host / cached ≤ prompt) AND the EIP-712 digest is recomputed
 * over exactly those pinned fields. An unknown key therefore cannot change
 * what the DON adjudicates, and any tampering of a signed field breaks the
 * signature (AUTH) — there is no path to an anchored decision using
 * unvalidated data.
 */
export function parseAndVerifyReceipt(
  raw: unknown,
  trustedSigner: string,
): ReceiptParseResult {
  if (typeof raw !== "object" || raw === null) {
    return { ok: false, error: { kind: "SCHEMA", reason: "receipt is not a JSON object" } }
  }
  const root = raw as Path

  // ── domain ──
  const domainRaw = root.domain as Path
  if (typeof domainRaw !== "object" || domainRaw === null) {
    return { ok: false, error: { kind: "SCHEMA", reason: "domain must be an object" } }
  }
  if (domainRaw.name !== RELAY_DOMAIN_NAME || domainRaw.version !== RELAY_DOMAIN_VERSION) {
    return {
      ok: false,
      error: {
        kind: "SCHEMA",
        reason: `domain must be exactly ${RELAY_DOMAIN_NAME} / ${RELAY_DOMAIN_VERSION}`,
      },
    }
  }
  const chainIdNumber = domainRaw.chainId
  const chainId =
    typeof chainIdNumber === "number" && Number.isSafeInteger(chainIdNumber) && chainIdNumber >= 0
      ? chainIdNumber
      : -1
  if (chainId !== RELAY_DOMAIN_CHAIN_ID) {
    return {
      ok: false,
      error: {
        kind: "SCHEMA",
        reason: `domain.chainId must be exactly the JSON number ${RELAY_DOMAIN_CHAIN_ID}`,
      },
    }
  }

  // ── message (schema) ──
  const message = root.message
  if (typeof message !== "object" || message === null) {
    return { ok: false, error: { kind: "SCHEMA", reason: "message must be an object" } }
  }
  const issues: string[] = []
  const paymentId = numberField(message as Path, "paymentId", issues)
  const promptTokens = numberField(message as Path, "promptTokens", issues)
  const cachedTokens = numberField(message as Path, "cachedTokens", issues)
  const completionTokens = numberField(message as Path, "completionTokens", issues)
  const actualAmount = numberField(message as Path, "actualAmount", issues)
  let seller = stringField(message as Path, "seller", issues)
  const upstreamHost = stringField(message as Path, "upstreamHost", issues)
  const model = stringField(message as Path, "model", issues)
  if (issues.length > 0) {
    return { ok: false, error: { kind: "SCHEMA", reason: issues.join("; ") } }
  }
  const sellerField = (message as Path).seller
  if (typeof sellerField !== "string" || !ADDRESS_RE.test(sellerField)) {
    return {
      ok: false,
      error: { kind: "SCHEMA", reason: "message.seller must be a 0x-prefixed 20-byte hex address" },
    }
  }
  if (!isHostname(upstreamHost)) {
    return {
      ok: false,
      error: { kind: "SCHEMA", reason: `message.upstreamHost is not a valid hostname: ${upstreamHost}` },
    }
  }
  // cached ≤ prompt (OpenAI usage semantics; negative delta would corrupt the
  // tiered price formula silently)
  if (cachedTokens > promptTokens) {
    return {
      ok: false,
      error: {
        kind: "SCHEMA",
        reason: `message.cachedTokens (${cachedTokens}) must be ≤ message.promptTokens (${promptTokens})`,
      },
    }
  }
  seller = seller.toLowerCase()

  // ── signature (schema + AUTH) ──
  const signature = root.signature
  if (typeof signature !== "string") {
    return { ok: false, error: { kind: "SCHEMA", reason: "signature must be a hex string" } }
  }
  if (!isSignature65(signature)) {
    return {
      ok: false,
      error: {
        kind: "SCHEMA",
        reason: `signature must be a 0x-prefixed 65-byte hex string (r||s||v), got ${
          signature.length % 2 === 0 ? (signature.length - 2) / 2 : NaN
        } bytes`,
      },
    }
  }

  // ── AUTH: synchronous EIP-712 recovery over the validated content ──
  let signer: Address
  try {
    signer = recoverTypedDataAddressSync({
      domain: { name: RELAY_DOMAIN_NAME, version: RELAY_DOMAIN_VERSION, chainId },
      message: {
        paymentId,
        promptTokens,
        cachedTokens,
        completionTokens,
        actualAmount,
        seller: sellerField,
        upstreamHost,
        model,
      },
      primaryType: "Receipt",
      types: { ...RECEIPT_TYPES } as unknown as {
        [k: string]: { name: string; type: string }[] | undefined
      },
      signature,
    })
  } catch (e) {
    return {
      ok: false,
      error: {
        kind: "AUTH",
        reason: `signature recovery failed: ${e instanceof Error ? e.message : String(e)}`,
      },
    }
  }
  if (!sameAddress(signer, trustedSigner)) {
    return {
      ok: false,
      error: {
        kind: "AUTH",
        reason: `receipt signer ${signer} is not the configured trusted signer ${trustedSigner}`,
      },
    }
  }

  return {
    ok: true,
    receipt: {
      paymentId: BigInt(paymentId),
      promptTokens: BigInt(promptTokens),
      cachedTokens: BigInt(cachedTokens),
      completionTokens: BigInt(completionTokens),
      actualAmount: BigInt(actualAmount),
      seller,
      upstreamHost,
      model,
      signature: signature.toLowerCase() as Hex,
      signer,
      receiptHash: keccak256(signature.toLowerCase() as Hex),
    },
  }
}

// ─── Cross-consistency (chain context) ───────────────────────────────────────

export type ChainContext = {
  /** paymentId from the Settled log (topic1 index). */
  paymentId: bigint
  eventSeller: string
  eventBuyer: string
  /** Trigger-block Escrow.getPayment(paymentId). */
  payment: PaymentRecord
}

export type ConsistencyResult =
  | { ok: true }
  | { ok: false; kind: "AUTH" | "SCHEMA"; reason: string }

/**
 * Chain-side consistency for a schema-valid, auth-valid receipt. Any failure
 * is fail-closed: the caller emits NO report.
 */
export function checkReceiptConsistency(
  receipt: ValidatedReceipt,
  ctx: ChainContext,
): ConsistencyResult {
  // receipt.paymentId == trigger paymentId
  if (ctx.paymentId > BigInt(Number.MAX_SAFE_INTEGER)) {
    // A uint256 paymentId that cannot round-trip through the relay's JSON
    // number types can never be evidenced properly — fail closed.
    return {
      ok: false,
      kind: "SCHEMA",
      reason: `paymentId ${ctx.paymentId} exceeds JSON safe-integer range`,
    }
  }
  if (receipt.paymentId !== ctx.paymentId) {
    return {
      ok: false,
      kind: "SCHEMA",
      reason: `receipt.paymentId ${receipt.paymentId} ≠ trigger paymentId ${ctx.paymentId}`,
    }
  }
  // receipt.seller == event.seller == getPayment.seller
  if (
    !sameAddress(receipt.seller, ctx.eventSeller) ||
    !sameAddress(receipt.seller, ctx.payment.seller)
  ) {
    return {
      ok: false,
      kind: "SCHEMA",
      reason: `seller mismatch: receipt=${receipt.seller} event=${ctx.eventSeller} getPayment=${ctx.payment.seller}`,
    }
  }
  // event.buyer == getPayment.buyer
  if (!sameAddress(ctx.eventBuyer, ctx.payment.buyer)) {
    return {
      ok: false,
      kind: "SCHEMA",
      reason: `buyer mismatch: event=${ctx.eventBuyer} getPayment=${ctx.payment.buyer}`,
    }
  }
  // getPayment.state == Settled
  if (ctx.payment.state !== PAYMENT_STATE_SETTLED) {
    return {
      ok: false,
      kind: "SCHEMA",
      reason: `payment ${ctx.paymentId} state=${ctx.payment.state}, expected Settled (${PAYMENT_STATE_SETTLED})`,
    }
  }
  return { ok: true }
}
