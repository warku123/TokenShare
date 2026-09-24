// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title ReceiptAnchor — TokenShare CRE settlement-audit consumer contract.
/// @notice Receives Chainlink CRE workflow reports (via the Keystone forwarder)
///         and anchors an immutable audit record of every settled payment:
///         the EIP-712 relay receipt hash + the DON's MATCH/MISMATCH verdict.
/// @dev    The forwarder address is passed to the constructor and enforced in
///         `onReport`. For Monad testnet local simulation use the tenant
///         MockKeystoneForwarder:
///             0xB9F79d863261869B234c481D1f9A7af84AeAd192
///         (verify with `cre workflow supported-chains` for your tenant).
///         Swap in the production KeystoneForwarder before real deployments.
contract ReceiptAnchor {
    // ── Types ────────────────────────────────────────────────────────────

    /// @notice Verdict emitted by the CRE workflow.
    /// @dev    1 = MATCH, 2 = MISMATCH.
    enum Verdict {
        None,
        Match,
        Mismatch
    }

    /// @notice Immutable audit record for one settled payment.
    struct AuditRecord {
        uint256 paymentId;
        uint256 settledAmount; // on-chain Escrow.Settled actualAmount (6 dp)
        uint256 receiptAmount; // relay EIP-712 receipt actualAmount (6 dp)
        bytes32 receiptHash;   // keccak256(receipt.signature)
        string upstreamHost;   // e.g. api.kimi.com — auditability anchor
        string model;          // model actually served
        Verdict verdict;
        uint64 anchoredAt;
    }

    // ── Storage ──────────────────────────────────────────────────────────

    address public immutable forwarder;

    /// @notice paymentId => latest audit record.
    mapping(uint256 => AuditRecord) public records;
    /// @notice Total records anchored (both matches and mismatches).
    uint256 public totalAnchored;
    /// @notice Total mismatches flagged (the trust-gap signal).
    uint256 public totalMismatched;

    // ── Events ───────────────────────────────────────────────────────────

    event ReceiptAnchored(
        uint256 indexed paymentId,
        bytes32 indexed receiptHash,
        uint256 settledAmount,
        uint256 receiptAmount,
        uint8 verdict
    );

    /// @notice Emitted when the DON decides receipt amount != settled amount.
    event DiscrepancyFlagged(
        uint256 indexed paymentId,
        uint256 onchainAmount,
        uint256 receiptAmount
    );

    // ── Errors ───────────────────────────────────────────────────────────

    error Unauthorized(address caller, address forwarder);
    error BadReport();

    // ── Construction ─────────────────────────────────────────────────────

    /// @param _forwarder Keystone/MockKeystone forwarder allowed to deliver
    ///        CRE reports. Cannot be zero.
    constructor(address _forwarder) {
        require(_forwarder != address(0), "forwarder=0");
        forwarder = _forwarder;
    }

    // ── CRE receiver entry point ─────────────────────────────────────────

    /// @notice IReceiver-style entry point called by the Keystone forwarder.
    /// @param metadata Opaque CRE report metadata (forwarder-managed).
    /// @param report   ABI-encoded payload from the workflow:
    ///                 (uint256 paymentId, uint256 settledAmount,
    ///                  uint256 receiptAmount, bytes32 receiptHash,
    ///                  string upstreamHost, string model, uint8 verdict)
    function onReport(bytes calldata metadata, bytes calldata report) external {
        if (msg.sender != forwarder) revert Unauthorized(msg.sender, forwarder);
        if (report.length == 0) revert BadReport();

        (
            uint256 paymentId,
            uint256 settledAmount,
            uint256 receiptAmount,
            bytes32 receiptHash,
            string memory upstreamHost,
            string memory model,
            uint8 rawVerdict
        ) = _decodeReport(report);

        Verdict verdict = _verdict(rawVerdict);

        records[paymentId] = AuditRecord({
            paymentId: paymentId,
            settledAmount: settledAmount,
            receiptAmount: receiptAmount,
            receiptHash: receiptHash,
            upstreamHost: upstreamHost,
            model: model,
            verdict: verdict,
            anchoredAt: uint64(block.timestamp)
        });
        totalAnchored += 1;

        emit ReceiptAnchored(paymentId, receiptHash, settledAmount, receiptAmount, uint8(verdict));
        if (verdict == Verdict.Mismatch) {
            totalMismatched += 1;
            emit DiscrepancyFlagged(paymentId, settledAmount, receiptAmount);
        }
    }

    // ── Views ────────────────────────────────────────────────────────────

    /// @notice True if the DON confirmed receipt == settled for this payment.
    function isVerified(uint256 paymentId) external view returns (bool) {
        return records[paymentId].verdict == Verdict.Match;
    }

    // ── Internals ────────────────────────────────────────────────────────

    function _decodeReport(bytes calldata report)
        internal
        pure
        returns (
            uint256 paymentId,
            uint256 settledAmount,
            uint256 receiptAmount,
            bytes32 receiptHash,
            string memory upstreamHost,
            string memory model,
            uint8 verdict
        )
    {
        // The workflow encodes exactly 7 fields; the signature is folded into
        // receiptHash upstream, so the tail here is (string, string, uint8).
        if (report.length < 7 * 32) revert BadReport();
        (paymentId, settledAmount, receiptAmount, receiptHash, upstreamHost, model, verdict) =
            abi.decode(report, (uint256, uint256, uint256, bytes32, string, string, uint8));
    }

    /// @dev Trusts the DON verdict carried inside the report (1 = Match,
    ///      2 = Mismatch) instead of recomputing settled-vs-receipt on-chain.
    ///      The two comparisons legitimately diverge: (1) Escrow settlement
    ///      can clamp actualAmount to maxAmount while the relay receipt stays
    ///      unclamped, and (2) the DON prices at the trigger block, so a
    ///      Registry.updateModelPrice landing after settle must not flip the
    ///      verdict. See cre/README.md "Verdict semantics".
    function _verdict(uint8 raw) internal pure returns (Verdict) {
        if (raw == uint8(Verdict.Match) || raw == uint8(Verdict.Mismatch)) {
            return Verdict(raw);
        }
        revert BadReport();
    }
}
