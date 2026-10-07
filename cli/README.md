# TokenShare Buyer CLI (`tokenshare_cli`)

 Buyer-side CLI: pick a seller from the Registry, lock USDC in Escrow, call
 the seller relay with an EIP-191 signed request, and verify the EIP-712
 `X-Receipt` against the on-chain listing operator. Env contract and receipt
 verification PIN are documented in `TokenShare-BUILD_SPEC.md` /
 `m3-m5-e2e.md`; failures are recorded in a local dispute ledger
 (`disputes` command, default `~/.tokenshare/disputes.json`).

## M15 shared mode — receipt signer pinning

Shared mode (relay operated by an independent TEE signer) keeps the receipt
PIN unchanged: same EIP-712 domain (`TokenShare Relay` / `1` / chainId), same
8-field `Receipt` struct, and `receipt.message.seller` is still the on-chain
listing operator (`listing.operator` / `payment.seller`). Only the signature
comes from an independent TEE address.

To verify such receipts, the buyer must pin the TEE signer explicitly:

- `call --expected-signer 0x…` (explicit flag, wins), or
- `EXPECTED_SIGNER=0x…` environment variable.

With a pin, `verify_receipt` requires BOTH:

1. `recover(signature) == expected_signer` (independent TEE signer), and
2. `receipt.message.seller == on-chain listing operator` (economic seller —
   never relaxed), plus the unchanged strict domain/paymentId checks.

Without a pin the CLI keeps the single-tenant behavior (recover must equal
the listing operator). The pin is strictly validated as an Ethereum address
and is NEVER auto-accepted from `/info`, relay headers, or any other
self-reported field; there is no TOFU and no test-only relaxation.

## Tests

Run the CLI suite separately (the `tests` package name collides with
relay/tests — never merge them in one pytest invocation):

```
python3 -m pytest cli/tests -q
```
