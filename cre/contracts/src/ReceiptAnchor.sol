// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title ReceiptAnchor — TokenShare CRE settlement-audit consumer contract.
/// @notice Receives Chainlink CRE workflow reports via the constructor-set
///         forwarder and anchors an audit record per reported payment: the
///         EIP-712 relay receipt hash + the verdict (MATCH/MISMATCH)
///         computed by the workflow. `records[paymentId]` holds only the
///         LATEST record — a later report for the same payment overwrites
///         it; the report/event trail is append-only (the events do NOT
///         repeat the model/host fields, though the signed business report
///         payload itself carries both, and they land in `records`).
/// @dev    For Monad testnet local simulation use the tenant
///         MockKeystoneForwarder:
///             0xB9F79d863261869B234c481D1f9A7af84AeAd192
///         (verify with `cre workflow supported-chains` for your tenant).
///         That mock is permissionless and zero-validation: it accepts
///         reports without DON consensus checks, so simulation anchors do
///         NOT prove production DON authenticity. Production deployments
///         must re-deploy with the real KeystoneForwarder.
contract ReceiptAnchor {
    // ── Types ────────────────────────────────────────────────────────────

    /// @notice Verdict emitted by the CRE workflow.
    /// @dev    1 = MATCH, 2 = MISMATCH.
    enum Verdict {
        None,
        Match,
        Mismatch
    }

    /// @notice Latest audit record for one reported payment (overwritten by
    ///         each new report; the append-only report/event trail is the
    ///         history, but the EVENTS do NOT repeat the model/host fields
    ///         — the signed business report payload carries both, and they
    ///         land in `records`).
    struct AuditRecord {
        uint256 paymentId;
        uint256 settledAmount; // on-chain Escrow.Settled actualAmount (6 dp)
        uint256 receiptAmount; // relay EIP-712 receipt actualAmount (6 dp)
        bytes32 receiptHash;   // keccak256(receipt.signature)
        string upstreamHost;   // e.g. api.kimi.com — auditability anchor
        string model;          // model claimed by the signed receipt
        Verdict verdict;
        uint64 anchoredAt;
    }

    // ── Storage ──────────────────────────────────────────────────────────

    address public immutable forwarder;

    /// @notice paymentId => latest audit record.
    mapping(uint256 => AuditRecord) public records;
    /// @notice Total reports anchored, MATCH or MISMATCH (counts reports, not
    ///         unique payments — re-reports increment it).
    uint256 public totalAnchored;
    /// @notice Total MISMATCH verdicts received (counts reports, not unique
    ///         payments — the trust-gap signal).
    uint256 public totalMismatched;

    // ── Events ───────────────────────────────────────────────────────────

    event ReceiptAnchored(
        uint256 indexed paymentId,
        bytes32 indexed receiptHash,
        uint256 settledAmount,
        uint256 receiptAmount,
        uint8 verdict
    );

    /// @notice Emitted when a report's verdict is MISMATCH (the receipt amount
    ///         deviates from the on-chain per-model estimate). `onchainAmount`
    ///         / `receiptAmount` are the settled and receipt amounts for
    ///         context; they can legitimately differ WITHOUT a discrepancy
    ///         (relay-side budget clamping, cumulative vs per-call amounts).
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

    /// @notice IReceiver-style entry point called by the forwarder set in the
    ///         constructor.
    /// @param metadata Opaque CRE report metadata (forwarder-managed).
    /// @param report   ABI-encoded payload from the workflow:
    ///                 (uint256 paymentId, uint256 settledAmount,
    ///                  uint256 receiptAmount, bytes32 receiptHash,
    ///                  string upstreamHost, string model, uint8 verdict)
    /// @dev    The sole authenticity boundary is the constructor forwarder.
    ///         THROUGH THE SIMULATION MOCK (zero validation) any caller can
    ///         reach reports, so anchored records from simulation carry no
    ///         production-DON guarantee. `records[paymentId]` is overwritten
    ///         on re-report; the event trail is append-only but does not
    ///         repeat the record's model/host fields.
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

    /// @notice True if the LATEST report for this payment carried a MATCH
    ///         verdict (receipt amount within ±1 of the Registry per-model
    ///         estimate at the trigger block). NOT a settled-vs-receipt
    ///         equality check.
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

    /// @dev Trusts the verdict carried inside the report (1 = Match,
    ///      2 = Mismatch) instead of re-deriving it on-chain. MATCH means the
    ///      receipt's pricing arithmetic is consistent with the Registry's
    ///      per-model prices at the TRIGGER block (±1 native unit); it does
    ///      not prove the token counts, the upstream/model truth, the
    ///      buyer's lock-time price, or cumulative settlement equality. A
    ///      settledAmount-vs-receiptAmount comparison is deliberately
    ///      skipped: Escrow v3.2 reverts over-max settles (no contract
    ///      clamp — the relay clamps to the remaining budget while receipts
    ///      stay unclamped), `Settled.actualAmount` is cumulative across
    ///      settlePartial advances while the record's receiptAmount is the
    ///      latest per-call receipt, and an on-chain recompute would read
    ///      current Registry state instead of the trigger-block finality.
    ///      See cre/README.md "Verdict semantics".
    function _verdict(uint8 raw) internal pure returns (Verdict) {
        if (raw == uint8(Verdict.Match) || raw == uint8(Verdict.Mismatch)) {
            return Verdict(raw);
        }
        revert BadReport();
    }
}
