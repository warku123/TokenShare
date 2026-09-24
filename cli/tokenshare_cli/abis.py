"""Contract ABIs (embedded from the forge artifacts).

Extracted verbatim from:
  contracts/out/Escrow.sol/Escrow.json
  contracts/out/Registry.sol/Registry.json
  contracts/out/MockUSDC.sol/MockUSDC.json  (standard OZ ERC-20, 6 decimals)

Only the function/event entries the CLI actually calls are embedded (Registry
v3 = M9 ABI PIN per-model pricing + M10 ABI PIN on-chain enumeration). These
are public interfaces — NOT configuration — so embedding them does not violate
the zero-hardcode rule (no addresses, no chain ids appear here).
"""

ESCROW_ABI = [
    {
        "type": "function",
        "name": "balances",
        "inputs": [{"name": "", "type": "address", "internalType": "address"}],
        "outputs": [{"name": "", "type": "uint256", "internalType": "uint256"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "deposit",
        "inputs": [{"name": "amount", "type": "uint256", "internalType": "uint256"}],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
    {
        "type": "function",
        "name": "getPayment",
        "inputs": [
            {"name": "paymentId", "type": "uint256", "internalType": "uint256"}
        ],
        "outputs": [
            {"name": "buyer", "type": "address", "internalType": "address"},
            {"name": "seller", "type": "address", "internalType": "address"},
            {"name": "maxAmount", "type": "uint256", "internalType": "uint256"},
            {"name": "expiresAt", "type": "uint64", "internalType": "uint64"},
            {"name": "state", "type": "uint8", "internalType": "enum Escrow.State"},
        ],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "isValid",
        "inputs": [
            {"name": "paymentId", "type": "uint256", "internalType": "uint256"},
            {"name": "seller", "type": "address", "internalType": "address"},
            {"name": "minAmount", "type": "uint256", "internalType": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool", "internalType": "bool"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "lock",
        "inputs": [
            {"name": "seller", "type": "address", "internalType": "address"},
            {"name": "maxAmount", "type": "uint256", "internalType": "uint256"},
            {"name": "ttl", "type": "uint64", "internalType": "uint64"},
        ],
        "outputs": [{"name": "paymentId", "type": "uint256", "internalType": "uint256"}],
        "stateMutability": "nonpayable",
    },
    {
        "type": "function",
        "name": "nextPaymentId",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256", "internalType": "uint256"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "refund",
        "inputs": [{"name": "paymentId", "type": "uint256", "internalType": "uint256"}],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
    {
        "type": "event",
        "name": "Locked",
        "anonymous": False,
        "inputs": [
            {"name": "paymentId", "type": "uint256", "indexed": True, "internalType": "uint256"},
            {"name": "buyer", "type": "address", "indexed": True, "internalType": "address"},
            {"name": "seller", "type": "address", "indexed": True, "internalType": "address"},
            {"name": "maxAmount", "type": "uint256", "indexed": False, "internalType": "uint256"},
            {"name": "expiresAt", "type": "uint64", "indexed": False, "internalType": "uint64"},
        ],
    },
    {
        "type": "event",
        "name": "Refunded",
        "anonymous": False,
        "inputs": [
            {"name": "paymentId", "type": "uint256", "indexed": True, "internalType": "uint256"},
            {"name": "buyer", "type": "address", "indexed": True, "internalType": "address"},
            {"name": "amount", "type": "uint256", "indexed": False, "internalType": "uint256"},
            {"name": "caller", "type": "address", "indexed": True, "internalType": "address"},
        ],
    },
]

# Registry v3 (M9 ABI PIN 「M9 ABI PIN（Registry v2）」 + M10 ABI PIN 「M10」,
# verbatim — no drift): v2 surface unchanged, v3 ADDS on-chain enumeration.
#   struct Price { uint256 cachedIn; uint256 input; uint256 output; }
#   struct Listing { address operator; string endpoint; string[] models;
#                    Price[] prices; bool active; }   // prices parallel to models
# Pricing is PER MODEL: the three-tier unit prices for a request come from
# getPrice(operator, model); the pricing FORMULA is unchanged.
_PRICE_COMPONENTS = [
    {"name": "cachedIn", "type": "uint256", "internalType": "uint256"},
    {"name": "input", "type": "uint256", "internalType": "uint256"},
    {"name": "output", "type": "uint256", "internalType": "uint256"},
]

REGISTRY_ABI = [
    {
        "type": "function",
        "name": "register",
        "inputs": [
            {"name": "endpoint", "type": "string", "internalType": "string"},
            {"name": "models", "type": "string[]", "internalType": "string[]"},
            {
                "name": "prices",
                "type": "tuple[]",
                "internalType": "struct Registry.Price[]",
                "components": _PRICE_COMPONENTS,
            },
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
    {
        "type": "function",
        "name": "updateModelPrice",
        "inputs": [
            {"name": "model", "type": "string", "internalType": "string"},
            {
                "name": "price",
                "type": "tuple",
                "internalType": "struct Registry.Price",
                "components": _PRICE_COMPONENTS,
            },
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
    {
        "type": "function",
        "name": "deactivate",
        "inputs": [],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
    {
        "type": "function",
        "name": "getListing",
        "inputs": [{"name": "operator", "type": "address", "internalType": "address"}],
        # v2 returns `Listing memory` — a SINGLE struct. solc encodes a
        # single-struct return as one (dynamic) outer tuple with a head
        # offset, so the outputs must be the wrapped tuple. web3 unwraps
        # single-tuple outputs itself, so chain.get_listing()'s five-field
        # unpack is unchanged. (Flat field outputs misread the outer offset
        # → BadFunctionCallOutput on real chain bytes.)
        "outputs": [
            {
                "name": "listing",
                "type": "tuple",
                "internalType": "struct Registry.Listing",
                "components": [
                    {"name": "listingOperator", "type": "address", "internalType": "address"},
                    {"name": "endpoint", "type": "string", "internalType": "string"},
                    {"name": "models", "type": "string[]", "internalType": "string[]"},
                    {
                        "name": "prices",
                        "type": "tuple[]",
                        "internalType": "struct Registry.Price[]",
                        "components": _PRICE_COMPONENTS,
                    },
                    {"name": "active", "type": "bool", "internalType": "bool"},
                ],
            }
        ],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "getPrice",
        "inputs": [
            {"name": "operator", "type": "address", "internalType": "address"},
            {"name": "model", "type": "string", "internalType": "string"},
        ],
        "outputs": [
            {
                "name": "",
                "type": "tuple",
                "internalType": "struct Registry.Price",
                "components": _PRICE_COMPONENTS,
            }
        ],
        "stateMutability": "view",
    },
    # M10 Registry v3 on-chain enumeration (ABI PIN 「M10」, verbatim — no
    # drift). O(1) discovery, replaces the old eth_getLogs Registered scan:
    #   sellerCount() -> total registered sellers (append-only, never shrinks)
    #   getSellers(start, count) -> clamp semantics: start >= len -> empty;
    #     count > 500 -> 500 (公开常量便于测); start+count > len -> truncated.
    # deactivate -> re-register never re-appends a seller. The Registered
    # EVENT still exists on-chain (signature unchanged), but the CLI no
    # longer consumes it — the entry was dropped with the scan path.
    {
        "type": "function",
        "name": "sellerCount",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256", "internalType": "uint256"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "getSellers",
        "inputs": [
            {"name": "start", "type": "uint256", "internalType": "uint256"},
            {"name": "count", "type": "uint256", "internalType": "uint256"},
        ],
        "outputs": [{"name": "", "type": "address[]", "internalType": "address[]"}],
        "stateMutability": "view",
    },
    {
        "type": "event",
        "name": "PriceUpdated",
        "anonymous": False,
        "inputs": [
            {"name": "operator", "type": "address", "indexed": True, "internalType": "address"},
            {"name": "model", "type": "string", "indexed": False, "internalType": "string"},
            {"name": "cachedIn", "type": "uint256", "indexed": False, "internalType": "uint256"},
            {"name": "input", "type": "uint256", "indexed": False, "internalType": "uint256"},
            {"name": "output", "type": "uint256", "indexed": False, "internalType": "uint256"},
        ],
    },
    {
        "type": "event",
        "name": "Deactivated",
        "anonymous": False,
        "inputs": [
            {"name": "operator", "type": "address", "indexed": True, "internalType": "address"},
        ],
    },
]

# Minimal standard ERC-20 surface (MockUSDC is OZ ERC20 with 6 decimals).
ERC20_ABI = [
    {
        "type": "function",
        "name": "allowance",
        "inputs": [
            {"name": "owner", "type": "address", "internalType": "address"},
            {"name": "spender", "type": "address", "internalType": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256", "internalType": "uint256"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "approve",
        "inputs": [
            {"name": "spender", "type": "address", "internalType": "address"},
            {"name": "value", "type": "uint256", "internalType": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool", "internalType": "bool"}],
        "stateMutability": "nonpayable",
    },
    {
        "type": "function",
        "name": "balanceOf",
        "inputs": [{"name": "account", "type": "address", "internalType": "address"}],
        "outputs": [{"name": "", "type": "uint256", "internalType": "uint256"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "transfer",
        "inputs": [
            {"name": "to", "type": "address", "internalType": "address"},
            {"name": "value", "type": "uint256", "internalType": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool", "internalType": "bool"}],
        "stateMutability": "nonpayable",
    },
]

# AttestationAnchor (M7-A): read-only surface for `verify-attestation --anchor`
# (mapping(address appId => bytes32 digest)).
ANCHOR_ABI = [
    {
        "type": "function",
        "name": "digests",
        "inputs": [{"name": "appId", "type": "address", "internalType": "address"}],
        "outputs": [{"name": "", "type": "bytes32", "internalType": "bytes32"}],
        "stateMutability": "view",
    },
    {
        "type": "event",
        "name": "Anchored",
        "anonymous": False,
        "inputs": [
            {"name": "appId", "type": "address", "indexed": True, "internalType": "address"},
            {"name": "digest", "type": "bytes32", "indexed": True, "internalType": "bytes32"},
        ],
    },
]
