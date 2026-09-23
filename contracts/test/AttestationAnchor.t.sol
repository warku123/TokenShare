// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";

import {AttestationAnchor} from "../src/AttestationAnchor.sol";

/**
 * @title AttestationAnchorTest
 * @notice Behavioral suite for the M7-A TEE quote anchor: digest stored under
 *         the caller (appId = msg.sender), Anchored event, overwrite
 *         semantics, and per-appId isolation. Plain anvil EVM, no fork.
 */
contract AttestationAnchorTest is Test {
    AttestationAnchor internal anchorContract;

    address internal sellerA = makeAddr("sellerA");
    address internal sellerB = makeAddr("sellerB");

    // A realistic quote digest: keccak256 of raw quote bytes.
    bytes32 internal digestA = keccak256(abi.encodePacked("tdx-quote-bytes-A"));
    bytes32 internal digestB = keccak256(abi.encodePacked("tdx-quote-bytes-B"));

    function setUp() public {
        anchorContract = new AttestationAnchor();
    }

    function test_AnchorStoresDigestUnderCaller() public {
        vm.prank(sellerA);
        anchorContract.anchor(digestA);

        assertEq(anchorContract.digests(sellerA), digestA, "digest anchored under appId");
    }

    function test_AnchorEmitsEvent() public {
        vm.prank(sellerA);
        vm.expectEmit(true, true, false, true);
        emit AttestationAnchor.Anchored(sellerA, digestA);
        anchorContract.anchor(digestA);
    }

    function test_ReanchorOverwritesLatestWins() public {
        vm.startPrank(sellerA);
        anchorContract.anchor(digestA);
        anchorContract.anchor(digestB);
        vm.stopPrank();

        assertEq(anchorContract.digests(sellerA), digestB, "latest anchor wins");
    }

    function test_AppIdsAreIsolated() public {
        vm.prank(sellerA);
        anchorContract.anchor(digestA);
        vm.prank(sellerB);
        anchorContract.anchor(digestB);

        assertEq(anchorContract.digests(sellerA), digestA, "sellerA keeps its digest");
        assertEq(anchorContract.digests(sellerB), digestB, "sellerB keeps its digest");
    }

    function test_UnsetDigestIsZero() public view {
        assertEq(anchorContract.digests(sellerA), bytes32(0), "no anchor -> zero digest");
    }
}
