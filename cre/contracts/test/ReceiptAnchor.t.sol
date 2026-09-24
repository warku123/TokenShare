// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Test} from "forge-std/Test.sol";
import {ReceiptAnchor} from "../src/ReceiptAnchor.sol";

contract ReceiptAnchorTest is Test {
    ReceiptAnchor anchor;
    address forwarder = makeAddr("forwarder");
    address attacker = makeAddr("attacker");

    uint8 constant V_MATCH = 1;
    uint8 constant V_MISMATCH = 2;

    // (paymentId=2, settled=730, receipt=730, hash, host, model, verdict=Match)
    bytes reportMatch = abi.encode(
        uint256(2), uint256(730), uint256(730), keccak256("sig"), "api.kimi.com", "kimi-k2.6", V_MATCH
    );
    // (paymentId=3, settled=700, receipt=900, hash, host, model, verdict=Mismatch)
    bytes reportMismatch = abi.encode(
        uint256(3), uint256(700), uint256(900), keccak256("sig2"), "api.kimi.com", "kimi-k2.6", V_MISMATCH
    );

    function setUp() public {
        anchor = new ReceiptAnchor(forwarder);
    }

    function getRecord(uint256 paymentId)
        internal
        view
        returns (ReceiptAnchor.AuditRecord memory)
    {
        (
            uint256 paymentId_,
            uint256 settledAmount,
            uint256 receiptAmount,
            bytes32 receiptHash,
            string memory upstreamHost,
            string memory model,
            ReceiptAnchor.Verdict verdict,
            uint64 anchoredAt
        ) = anchor.records(paymentId);
        return ReceiptAnchor.AuditRecord(paymentId_, settledAmount, receiptAmount, receiptHash, upstreamHost, model, verdict, anchoredAt);
    }

    function test_constructorRevertsOnZeroForwarder() public {
        vm.expectRevert("forwarder=0");
        new ReceiptAnchor(address(0));
    }

    function test_onReportRevertsForNonForwarder() public {
        vm.prank(attacker);
        vm.expectRevert(abi.encodeWithSelector(ReceiptAnchor.Unauthorized.selector, attacker, forwarder));
        anchor.onReport(bytes(""), reportMatch);
    }

    function test_onReportAnchorsMatch() public {
        vm.prank(forwarder);
        anchor.onReport(bytes("meta"), reportMatch);
        ReceiptAnchor.AuditRecord memory r = getRecord(2);
        assertEq(r.paymentId, 2);
        assertEq(r.settledAmount, 730);
        assertEq(r.receiptAmount, 730);
        assertEq(r.upstreamHost, "api.kimi.com");
        assertTrue(r.verdict == ReceiptAnchor.Verdict.Match);
        assertTrue(anchor.isVerified(2));
        assertEq(anchor.totalAnchored(), 1);
        assertEq(anchor.totalMismatched(), 0);
    }

    function test_onReportFlagsMismatch() public {
        vm.expectEmit(true, true, false, true);
        emit ReceiptAnchor.DiscrepancyFlagged(3, 700, 900);
        vm.prank(forwarder);
        anchor.onReport(bytes("meta"), reportMismatch);
        ReceiptAnchor.AuditRecord memory r = getRecord(3);
        assertTrue(r.verdict == ReceiptAnchor.Verdict.Mismatch);
        assertFalse(anchor.isVerified(3));
        assertEq(anchor.totalAnchored(), 1);
        assertEq(anchor.totalMismatched(), 1);
    }

    /// The contract trusts the DON verdict carried in the report, so a
    /// MISMATCH is anchored even when settled == receipt (e.g. the workflow
    /// flagged receipt-vs-estimate drift while the settle amount happened to
    /// equal the receipt amount).
    function test_onReportVerdictFieldMismatchWinsWhenSettledEqualsReceipt() public {
        bytes memory report = abi.encode(
            uint256(5), uint256(730), uint256(730), keccak256("sig5"), "api.kimi.com", "kimi-k2.6", V_MISMATCH
        );
        vm.prank(forwarder);
        anchor.onReport(bytes(""), report);
        assertTrue(getRecord(5).verdict == ReceiptAnchor.Verdict.Mismatch);
        assertFalse(anchor.isVerified(5));
        assertEq(anchor.totalMismatched(), 1);
    }

    /// Escrow clamping case: settle may clamp actualAmount to maxAmount while
    /// the relay receipt stays unclamped (settled=700 < receipt=900). The DON
    /// verdict (receipt-vs-estimate = Match) must NOT be flipped into a
    /// MISMATCH by the on-chain settled-vs-receipt delta — no
    /// DiscrepancyFlagged is emitted.
    function test_onReportVerdictFieldMatchSurvivesSettledClamping() public {
        bytes memory report = abi.encode(
            uint256(6), uint256(700), uint256(900), keccak256("sig6"), "api.kimi.com", "kimi-k2.6", V_MATCH
        );
        vm.prank(forwarder);
        anchor.onReport(bytes(""), report);
        assertTrue(getRecord(6).verdict == ReceiptAnchor.Verdict.Match);
        assertTrue(anchor.isVerified(6));
        assertEq(anchor.totalAnchored(), 1);
        assertEq(anchor.totalMismatched(), 0);
    }

    /// The ±1 native-unit tolerance lives in the workflow now; the contract
    /// no longer applies it to settled-vs-receipt when the report says
    /// MISMATCH.
    function test_onReportNoLongerAppliesOneUnitToleranceOnchain() public {
        bytes memory report = abi.encode(
            uint256(7), uint256(730), uint256(731), keccak256("sig7"), "api.kimi.com", "kimi-k2.6", V_MISMATCH
        );
        vm.prank(forwarder);
        anchor.onReport(bytes(""), report);
        assertTrue(getRecord(7).verdict == ReceiptAnchor.Verdict.Mismatch);
        assertFalse(anchor.isVerified(7));
    }

    /// Verdict values outside {Match, Mismatch} (0=None, 3, …) are rejected.
    function test_onReportRevertsOnInvalidVerdict(uint8 raw) public {
        vm.assume(raw != V_MATCH && raw != V_MISMATCH);
        bytes memory report = abi.encode(
            uint256(8), uint256(730), uint256(730), keccak256("sig8"), "api.kimi.com", "kimi-k2.6", raw
        );
        vm.prank(forwarder);
        vm.expectRevert(ReceiptAnchor.BadReport.selector);
        anchor.onReport(bytes(""), report);
    }

    function test_onReportRevertsOnEmptyReport() public {
        vm.prank(forwarder);
        vm.expectRevert(ReceiptAnchor.BadReport.selector);
        anchor.onReport(bytes(""), bytes(""));
    }

    function test_onReportEmitsEvents() public {
        vm.prank(forwarder);
        vm.expectEmit(true, true, false, true);
        emit ReceiptAnchor.ReceiptAnchored(2, keccak256("sig"), 730, 730, uint8(ReceiptAnchor.Verdict.Match));
        anchor.onReport(bytes(""), reportMatch);
    }
}
