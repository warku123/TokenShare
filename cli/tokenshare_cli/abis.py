"""Contract ABIs (embedded from the forge artifacts).

Extracted verbatim from:
  contracts/out/Escrow.sol/Escrow.json
  contracts/out/Registry.sol/Registry.json
  contracts/out/MockUSDC.sol/MockUSDC.json  (standard OZ ERC-20, 6 decimals)

Only the function entries the CLI actually calls are embedded. These are
public interfaces — NOT configuration — so embedding them does not violate
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

REGISTRY_ABI = [
    {
        "type": "function",
        "name": "getListing",
        "inputs": [{"name": "operator", "type": "address", "internalType": "address"}],
        "outputs": [
            {"name": "listingOperator", "type": "address", "internalType": "address"},
            {"name": "endpoint", "type": "string", "internalType": "string"},
            {"name": "models", "type": "string[]", "internalType": "string[]"},
            {"name": "priceCachedIn", "type": "uint256", "internalType": "uint256"},
            {"name": "priceInput", "type": "uint256", "internalType": "uint256"},
            {"name": "priceOutput", "type": "uint256", "internalType": "uint256"},
            {"name": "active", "type": "bool", "internalType": "bool"},
        ],
        "stateMutability": "view",
    }
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
