# custody_vector.json — synthetic custody crypto/protocol vector (M15 R1)

**Note from the file itself: `synthetic test keys, never production`.** All
key material below is random test data; none of it may be copied into
`.env`, deployed systems or docs. READ-ONLY for lanes other than the relay
custody lane (this file is regenerated only by the relay custody tests).

## Schema (v1)

| field | type | meaning |
|---|---|---|
| `note` | string | synthetic-data marker, must stay |
| `schema` | int | 1 |
| `alg` | string | `ECDH-P256-HKDF-SHA256-A256GCM` |
| `salt_seed_ascii` | string | `tokenshare-custody-salt-v1` (HKDF salt = SHA256 of this ASCII) |
| `info_ascii` | string | `tokenshare-custody-aesgcm-v1` (HKDF info) |
| `upload_priv_hex` | `0x`+64hex | relay-side P-256 scalar (ECDH static key) |
| `upload_pub_b64url` | b64url(65B) | uncompressed `0x04\|\|X\|\|Y` of `upload_priv` |
| `eph_priv_hex` | `0x`+64hex | sender-side ephemeral P-256 scalar |
| `plaintext_json` | ASCII | compact `{"api_key":"..."}` — envelope payload |
| `message` | ASCII single line | EXACT submit message (EIP-191 text) |
| `aad_ascii` | ASCII single line | EXACT upload AAD — independent string, NO body hash |
| `iv_b64url` | b64url(12B) | pinned AES-GCM nonce (96-bit) |
| `aad_fields` | object | seller/chain_id/escrow_addr/registry_addr/origin/upstream_base_url/nonce/issued/expires — inputs that rebuild `aad_ascii` and `message` |
| `raw_body` | ASCII | the EXACT canonical compact POST body (`nonce,issued_at,expires_at,upstream_base_url,envelope{alg,epk,iv,ct}` — that key order). Final field name per the web interop script (L8–L10). |
| `body_sha256` | 64hex | SHA256 of the exact `raw_body` bytes (no `0x`) — final field name per the web interop script |
| `eoa_priv_hex` | `0x`+64hex | synthetic secp256k1 EOA key — its address IS the custody seller |
| `signer` | 0x…40hex | EIP-55 CHECKSUMMED address of `eoa_priv` == custody seller. Kept checksummed so `ethers.verifyMessage(message, signature)` output compares `=== signer` directly (final field name per the web interop script L11) |
| `signature` | `0x`+130hex | EIP-191 personal-sign signature of `eoa_priv` over `message` — `ethers.verifyMessage(message, signature) === signer` (final field name per the web interop script L11) |
| `expected` | object | `epk_b64url`/`iv_b64url`/`ct_b64url` — byte-exact re-encryption target (same eph scalar + IV) |

## Invariants (all independently verified by relay/tests/test_custody_vector.py)

1. `upload_pub_b64url` == base64url(uncompressed pubkey of `upload_priv`).
2. `decrypt_envelope(upload_priv, expected, aad_ascii)` == `plaintext_json` bytes.
3. Re-encrypting `plaintext_json` with `eph_priv` + `iv` yields byte-identical
   `expected.{epk,iv,ct}` (deterministic reproducibility).
4. `raw_body` is compact ASCII, canonical key order; its independent
   SHA256 == `body_sha256`; `message` binds exactly that hash.
5. `aad_ascii` == `build_upload_aad(**aad_fields)`; contains NO body hash and
   never shares the "TokenShare key custody" message class.
6. `recover(signature, message)` == `signer` == `aad_fields.seller`;
   the signature fails over any other message (e.g. action=revoke).

## Web/console interop usage

`raw_body` is the exact bytes to send as POST /sellers/keys with
headers `X-Tokenshare-Seller: signer`, `X-Tokenshare-Signature: signature`
(plus an allowed `Origin`). The relay must return 200 with
`stored:true` and `key_fingerprint == SHA256(api_key)` — proven by the test
`test_vector_drives_a_real_enrollment` against a mocked upstream.

Ethers verification mirror (web script L8–L11):

```js
const vector = JSON.parse(fs.readFileSync("relay/tests/vectors/custody_vector.json"));
// L8–L10: real raw-body hash check
const bodyHash = ethers.sha256(ethers.toUtf8Bytes(vector.raw_body));
assert(bodyHash === vector.body_sha256);
// L11: EIP-191 personal-sign verification
const recovered = ethers.verifyMessage(vector.message, vector.signature);
assert(recovered === vector.signer); // both EIP-55 checksummed
```

