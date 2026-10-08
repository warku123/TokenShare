/**
 * Config schema for the settlement-audit workflow, validated with zod at
 * Runner startup (the SDK feeds runtime.config from config.*.json).
 *
 * Trusted receipt signer (R2 shared signer model):
 *   The relay signs EIP-712 receipts with ONE preconfigured shared key —
 *   receiptSignerAddress — which is NOT a seller. The workflow recovers the
 *   signer from each receipt signature and compares against THIS config key.
 *   The trusted signer is never taken from the receipt itself (that would let
 *   the relay nominate its own verifier).
 */
import { z } from "zod"

const ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
const ADDRESS_RE = /^0x[0-9a-fA-F]{40}$/

/**
 * Typed-free address slot. anchorAddress / workflowOwner are still filled in
 * by the deploy step (config.staging.json ships placeholders), so they are
 * checked only for basic non-emptiness here.
 */
const populatedString = z.string().min(1)

const addressString = z
  .string()
  .refine((v) => ADDRESS_RE.test(v), { message: "expected 0x-prefixed 40-hex-char address" })

const nonZeroAddressString = addressString.refine((v) => v.toLowerCase() !== ZERO_ADDRESS, {
  message: "address must not be the zero address",
})

const httpUrl = z
  .string()
  .refine((v) => /^https?:\/\//i.test(v), {
    message: "relayBaseUrl must start with http:// or https://",
  })

const decimalString = z
  .string()
  .refine((v) => /^\d{1,20}$/.test(v) && BigInt(v) > 0n, {
    message: "maxPriceUsd6 must be a positive decimal integer (6-dp USDC) string",
  })

export const configSchema = z.object({
  chainSelectorName: populatedString,
  escrowAddress: addressString,
  registryAddress: addressString,
  /** ReceiptAnchor consumer (cre/contracts/src/ReceiptAnchor.sol). */
  anchorAddress: populatedString,
  /** TokenShare relay base URL, e.g. http://127.0.0.1:8787 (M5b listing). */
  relayBaseUrl: httpUrl,
  /** Sanity ceiling on receipt amounts, 6-dp USDC (1000 USDC default). */
  maxPriceUsd6: decimalString,
  /** Workflow owner EOA — required to scope Vault DON secret requests. */
  workflowOwner: populatedString,
  /**
   * Preconfigured EIP-712 receipt signer address (R2 shared signer).
   * MUST be a real 20-byte hex address and not the zero address. Every
   * receipt signature MUST recover to exactly this address; receipts are
   * never allowed to name their own verifier.
   */
  receiptSignerAddress: nonZeroAddressString,
})

export type Config = z.infer<typeof configSchema>

/**
 * Case-insensitive address comparison. Inputs upstream of this helper are
 * already validated (receipt schema/chain decode), so this is a pure
 * lowercase equality — no additional checksum ceremony.
 */
export const sameAddress = (a: string, b: string): boolean =>
  typeof a === "string" &&
  typeof b === "string" &&
  a.length === 42 &&
  b.length === 42 &&
  a.toLowerCase() === b.toLowerCase()
