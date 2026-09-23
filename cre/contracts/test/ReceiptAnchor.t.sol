// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Test} from "forge-std/Test.sol";
import {ReceiptAnchor} from "../src/ReceiptAnchor.sol";

contract ReceiptAnchorTest is Test {
    ReceiptAnchor anchor;
    address forwarder = makeAddr("forwarder");
    address attacker = makeAddr("attacker");

    // (paymentId=2, settled=730, receipt=730, hash, hosts, model, sig)
    bytes reportMatch = abi.encode(
        uint256(2), uint256(730), uint256(730), keccak256("sig"), "api.kimi.com", "kimi-k2.6", bytes("sig")
    );
    bytes reportMismatch = abi.encode(
        uint256(3), uint256(700), uint256(900), keccak256("sig2"), "api.kimi.com", "kimi-k2.6", bytes("sig2")
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
        vm.prank(forwarder);
        anchor.onReport(bytes("meta"), reportMismatch);
        ReceiptAnchor.AuditRecord memory r = getRecord(3);
        assertTrue(r.verdict == ReceiptAnchor.Verdict.Mismatch);
        assertFalse(anchor.isVerified(3));
        assertEq(anchor.totalAnchored(), 1);
        assertEq(anchor.totalMismatched(), 1);
    }

    function test_onReportOneUnitToleranceIsMatch() public {
        bytes memory report = abi.encode(
            uint256(4), uint256(730), uint256(731), keccak256("sig3"), "api.kimi.com", "kimi-k2.6", bytes("s")
        );
        vm.prank(forwarder);
        anchor.onReport(bytes(""), report);
        assertTrue(anchor.isVerified(4));
        assertEq(anchor.totalMismatched(), 0);
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
