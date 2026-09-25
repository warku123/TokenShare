// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {Vm} from "forge-std/Vm.sol";

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Escrow} from "../src/Escrow.sol";
import {MockUSDC} from "./mocks/MockUSDC.sol";
import {MaliciousAttacker, MaliciousToken, ReentrantSeller} from "./mocks/ReentrancyAttackers.sol";

/**
 * @title EscrowTest
 * @notice Full behavioral suite for Escrow (BUILD_SPEC v1.1 §6.1, §5 acceptance).
 *         Runs on plain anvil/local EVM, no fork. All amounts are USDC 6-decimal
 *         native units (1 USDC = 1e6).
 */
contract EscrowTest is Test {
    // ------------------------------------------------------------------ data
    Escrow internal escrow;
    MockUSDC internal usdc;

    address internal buyer = makeAddr("buyer");
    address internal seller = makeAddr("seller");
    address internal thirdParty = makeAddr("thirdParty");
    address internal treasury = makeAddr("treasury"); // v3 fee recipient

    uint64 internal constant START_TIME = 1_000_000;

    // ------------------------------------------------------------------ setup
    function setUp() public {
        usdc = new MockUSDC();
        // Existing suite runs fee-free (feeBps = 0): zero behavioral delta.
        escrow = new Escrow(usdc, 0, treasury);
        vm.warp(START_TIME); // deterministic timestamps
    }

    // ------------------------------------------------------------------ helpers
    /// @dev Lock `amount` (buyer -> arbitrary seller) with default ttl.
    function _lockTo(address to, uint256 amount) internal returns (uint256 paymentId) {
        vm.startPrank(buyer);
        paymentId = escrow.lock(to, amount, 0);
        vm.stopPrank();
    }
    /// @dev Mint `who` USDC, approve escrow and deposit.
    function _deposit(address who, uint256 amount) internal {
        usdc.mint(who, amount);
        vm.startPrank(who);
        usdc.approve(address(escrow), amount);
        escrow.deposit(amount);
        vm.stopPrank();
    }

    /// @dev Lock `amount` (buyer -> seller) with default ttl, return paymentId.
    function _lock(uint256 amount) internal returns (uint256 paymentId) {
        vm.startPrank(buyer);
        paymentId = escrow.lock(seller, amount, 0);
        vm.stopPrank();
    }

    /// @dev Current lifecycle state of a payment.
    function _stateOf(uint256 paymentId) internal view returns (Escrow.State s) {
        (,,,, s) = escrow.getPayment(paymentId);
    }

    // =====================================================================
    // deposit
    // =====================================================================

    function test_Deposit_Happy() public {
        uint256 amount = 100e6;

        usdc.mint(buyer, amount);
        vm.startPrank(buyer);
        usdc.approve(address(escrow), amount);

        vm.expectEmit(true, false, false, true, address(escrow));
        emit Escrow.Deposited(buyer, amount);

        escrow.deposit(amount);
        vm.stopPrank();

        assertEq(escrow.balances(buyer), amount, "buyer balance");
        assertEq(usdc.balanceOf(address(escrow)), amount, "escrow holdings");
    }

    function test_RevertDeposit_ZeroAmount() public {
        vm.startPrank(buyer);
        vm.expectRevert(Escrow.ZeroAmount.selector);
        escrow.deposit(0);
        vm.stopPrank();
    }

    function test_RevertDeposit_NotApproved() public {
        // No approval -> ERC20 transferFrom reverts (SafeERC20 bubbles it).
        usdc.mint(buyer, 100e6);
        vm.prank(buyer);
        vm.expectRevert();
        escrow.deposit(100e6);
        // Nothing credited, nothing transferred.
        assertEq(escrow.balances(buyer), 0, "no balance on failed deposit");
        assertEq(usdc.balanceOf(address(escrow)), 0, "no escrow holdings");
    }

    // =====================================================================
    // lock
    // =====================================================================

    function test_Lock_Happy() public {
        _deposit(buyer, 100e6);
        uint256 maxAmount = 10e6;

        uint64 expectedExpiry = START_TIME + 600; // ttl 0 -> DEFAULT_TTL (600s)

        vm.expectEmit(true, true, true, true, address(escrow));
        emit Escrow.Locked(1, buyer, seller, maxAmount, expectedExpiry);

        vm.startPrank(buyer);
        uint256 pid1 = escrow.lock(seller, maxAmount, 0); // ttl = 0 -> default 600
        uint256 pid2 = escrow.lock(seller, maxAmount, 0);
        vm.stopPrank();

        // paymentIds start at 1 and increment
        assertEq(pid1, 1, "first paymentId");
        assertEq(pid2, 2, "second paymentId");
        assertEq(escrow.balances(buyer), 100e6 - 2 * maxAmount, "buyer balance after locks");

        (address pBuyer, address pSeller, uint256 pMax, uint64 pExpiry, Escrow.State pState) = escrow.getPayment(pid1);
        assertEq(pBuyer, buyer, "payment buyer");
        assertEq(pSeller, seller, "payment seller");
        assertEq(pMax, maxAmount);
        assertEq(pExpiry, expectedExpiry, "default ttl expiry");
        assertTrue(pState == Escrow.State.Locked, "state Locked");
    }

    function test_Lock_CustomTtl() public {
        _deposit(buyer, 100e6);
        uint64 ttl = 60;

        vm.startPrank(buyer);
        uint256 pid = escrow.lock(seller, 10e6, ttl);
        vm.stopPrank();

        (,,, uint64 expiry,) = escrow.getPayment(pid);
        assertEq(expiry, START_TIME + ttl, "custom ttl expiry");
    }

    function test_RevertLock_ZeroAmount() public {
        _deposit(buyer, 100e6);
        vm.prank(buyer);
        vm.expectRevert(Escrow.ZeroAmount.selector);
        escrow.lock(seller, 0, 0);
    }

    function test_RevertLock_InvalidSeller() public {
        _deposit(buyer, 100e6);
        vm.prank(buyer);
        vm.expectRevert(Escrow.InvalidSeller.selector);
        escrow.lock(address(0), 10e6, 0);
    }

    function test_RevertLock_SelfLock() public {
        _deposit(buyer, 100e6);
        vm.prank(buyer);
        vm.expectRevert(Escrow.SelfLock.selector);
        escrow.lock(buyer, 10e6, 0);
    }

    function test_RevertLock_InsufficientBalance() public {
        _deposit(buyer, 5e6);
        vm.prank(buyer);
        vm.expectRevert(abi.encodeWithSelector(Escrow.InsufficientBalance.selector, 10e6, 5e6));
        escrow.lock(seller, 10e6, 0);
    }

    // =====================================================================
    // settle
    // =====================================================================

    function test_Settle_Happy_Partial() public {
        _deposit(buyer, 100e6);
        uint256 maxAmount = 10e6;
        uint256 pid = _lock(maxAmount);

        uint256 actual = 4e6; // relay-priced usage
        uint256 expectedRefund = maxAmount - actual;

        vm.expectEmit(true, true, true, true, address(escrow));
        emit Escrow.Settled(pid, buyer, seller, actual, expectedRefund);

        vm.prank(seller);
        escrow.settle(pid, actual);

        assertEq(escrow.balances(seller), actual, "seller credited actual");
        assertEq(escrow.balances(buyer), 90e6 + expectedRefund, "buyer credited diff");
        assertTrue(_stateOf(pid) == Escrow.State.Settled, "state Settled");
    }

    function test_Settle_FullAmount() public {
        _deposit(buyer, 100e6);
        uint256 maxAmount = 10e6;
        uint256 pid = _lock(maxAmount);

        vm.prank(seller);
        escrow.settle(pid, maxAmount);

        assertEq(escrow.balances(seller), maxAmount, "seller got all");
        assertEq(escrow.balances(buyer), 90e6, "buyer diff zero");
    }

    function test_Settle_ZeroActual() public {
        _deposit(buyer, 100e6);
        uint256 maxAmount = 10e6;
        uint256 pid = _lock(maxAmount);

        vm.prank(seller);
        escrow.settle(pid, 0);

        assertEq(escrow.balances(seller), 0, "seller got nothing");
        assertEq(escrow.balances(buyer), 100e6, "buyer fully refunded");
        assertTrue(_stateOf(pid) == Escrow.State.Settled, "state Settled");
    }

    /// @dev Documented decision: settle is allowed while `Locked` even after ttl
    ///      (seller settles after serving the request); it races `refund`.
    function test_Settle_AfterTtl() public {
        _deposit(buyer, 100e6);
        uint256 pid = _lock(10e6);

        (,,, uint64 expiry,) = escrow.getPayment(pid);
        vm.warp(expiry + 1);

        vm.prank(seller);
        escrow.settle(pid, 4e6);

        assertEq(escrow.balances(seller), 4e6, "seller credited");
        assertTrue(_stateOf(pid) == Escrow.State.Settled, "state Settled");
    }

    function test_RevertSettle_NotSeller() public {
        _deposit(buyer, 100e6);
        uint256 pid = _lock(10e6);

        vm.startPrank(buyer); // buyer is NOT the designated seller
        vm.expectRevert(abi.encodeWithSelector(Escrow.NotSeller.selector, buyer, seller));
        escrow.settle(pid, 4e6);
        vm.stopPrank();

        vm.prank(thirdParty);
        vm.expectRevert(abi.encodeWithSelector(Escrow.NotSeller.selector, thirdParty, seller));
        escrow.settle(pid, 4e6);
    }

    function test_RevertSettle_ExceedsMaxAmount() public {
        _deposit(buyer, 100e6);
        uint256 maxAmount = 10e6;
        uint256 pid = _lock(maxAmount);

        vm.prank(seller);
        vm.expectRevert(abi.encodeWithSelector(Escrow.ExceedsMaxAmount.selector, maxAmount + 1, maxAmount));
        escrow.settle(pid, maxAmount + 1);
    }

    function test_RevertSettle_NonexistentId() public {
        vm.prank(seller);
        vm.expectRevert(abi.encodeWithSelector(Escrow.NotLocked.selector, 42, Escrow.State.None));
        escrow.settle(42, 4e6);
    }

    function test_RevertSettle_AlreadySettled() public {
        _deposit(buyer, 100e6);
        uint256 pid = _lock(10e6);

        vm.prank(seller);
        escrow.settle(pid, 4e6);

        vm.prank(seller);
        vm.expectRevert(abi.encodeWithSelector(Escrow.NotLocked.selector, pid, Escrow.State.Settled));
        escrow.settle(pid, 4e6);
    }

    /// @dev Cross-terminal transition: refund (Refunded) followed by settle reverts.
    function test_RevertSettle_AfterRefund() public {
        _deposit(buyer, 100e6);
        uint256 pid = _lock(10e6);

        (,,, uint64 expiry,) = escrow.getPayment(pid);
        vm.warp(expiry);
        vm.prank(buyer);
        escrow.refund(pid);

        vm.prank(seller);
        vm.expectRevert(abi.encodeWithSelector(Escrow.NotLocked.selector, pid, Escrow.State.Refunded));
        escrow.settle(pid, 4e6);
    }

    // =====================================================================
    // refund
    // =====================================================================

    function test_Refund_Happy_BuyerAfterTtl() public {
        _deposit(buyer, 100e6);
        uint256 maxAmount = 10e6;
        uint256 pid = _lock(maxAmount);

        (,,, uint64 expiry,) = escrow.getPayment(pid);
        vm.warp(expiry + 1);

        vm.expectEmit(true, true, false, true, address(escrow));
        emit Escrow.Refunded(pid, buyer, maxAmount, buyer);

        vm.prank(buyer);
        escrow.refund(pid);

        assertEq(escrow.balances(buyer), 100e6, "buyer fully refunded");
        assertEq(escrow.balances(seller), 0, "seller gets nothing");
        assertTrue(_stateOf(pid) == Escrow.State.Refunded, "state Refunded");
    }

    /// @dev §6.1: past ttl, ANYONE may trigger cleanup; funds still go to the buyer.
    function test_Refund_ByThirdParty() public {
        _deposit(buyer, 100e6);
        uint256 maxAmount = 10e6;
        uint256 pid = _lock(maxAmount);

        (,,, uint64 expiry,) = escrow.getPayment(pid);
        vm.warp(expiry + 1);

        // topic3 enabled: explicitly assert the caller is the third party
        vm.expectEmit(true, true, true, true, address(escrow));
        emit Escrow.Refunded(pid, buyer, maxAmount, thirdParty);

        vm.prank(thirdParty);
        escrow.refund(pid);

        assertEq(escrow.balances(buyer), 100e6, "funds still go to buyer");
        assertEq(escrow.balances(thirdParty), 0, "caller gains nothing");
        assertTrue(_stateOf(pid) == Escrow.State.Refunded, "state Refunded");
    }

    /// @dev TTL boundary is inclusive: refund is allowed at exactly expiresAt.
    function test_Refund_AtExactBoundary() public {
        _deposit(buyer, 100e6);
        uint256 pid = _lock(10e6);

        (,,, uint64 expiry,) = escrow.getPayment(pid);
        vm.warp(expiry); // exactly at expiry

        vm.prank(buyer);
        escrow.refund(pid);

        assertTrue(_stateOf(pid) == Escrow.State.Refunded, "state Refunded");
    }

    /// @dev Refund before ttl MUST revert — for anyone, including the buyer.
    function test_RevertRefund_TtlNotElapsed() public {
        _deposit(buyer, 100e6);
        uint256 pid = _lock(10e6);

        (,,, uint64 expiry,) = escrow.getPayment(pid);

        // buyer within ttl
        vm.prank(buyer);
        vm.expectRevert(abi.encodeWithSelector(Escrow.TtlNotElapsed.selector, pid, expiry));
        escrow.refund(pid);

        // third party within ttl
        vm.prank(thirdParty);
        vm.expectRevert(abi.encodeWithSelector(Escrow.TtlNotElapsed.selector, pid, expiry));
        escrow.refund(pid);

        // one second before expiry still reverts
        vm.warp(expiry - 1);
        vm.prank(buyer);
        vm.expectRevert(abi.encodeWithSelector(Escrow.TtlNotElapsed.selector, pid, expiry));
        escrow.refund(pid);
    }

    function test_RevertRefund_AlreadyRefunded() public {
        _deposit(buyer, 100e6);
        uint256 pid = _lock(10e6);

        (,,, uint64 expiry,) = escrow.getPayment(pid);
        vm.warp(expiry);
        vm.prank(buyer);
        escrow.refund(pid);

        vm.prank(buyer);
        vm.expectRevert(abi.encodeWithSelector(Escrow.NotLocked.selector, pid, Escrow.State.Refunded));
        escrow.refund(pid);
    }

    function test_RevertRefund_NonexistentId() public {
        vm.prank(buyer);
        vm.expectRevert(abi.encodeWithSelector(Escrow.NotLocked.selector, 77, Escrow.State.None));
        escrow.refund(77);
    }

    // =====================================================================
    // settlePartial (Escrow v2 per-key metering)
    // =====================================================================

    function test_SettlePartial_Happy_Single() public {
        _deposit(buyer, 100e6);
        uint256 maxAmount = 10e6;
        uint256 pid = _lock(maxAmount);

        vm.expectEmit(true, false, false, true, address(escrow));
        emit Escrow.SettlePartial(pid, 4e6, 4e6);

        vm.prank(seller);
        escrow.settlePartial(pid, 4e6);

        assertEq(escrow.balances(seller), 4e6, "seller credited immediately");
        assertEq(escrow.balances(buyer), 90e6, "buyer untouched");
        assertEq(escrow.capturedOf(pid), 4e6, "captured accumulated");
        assertTrue(_stateOf(pid) == Escrow.State.Locked, "state stays Locked");
        // ledger credit, not a token push: escrow still holds everything
        assertEq(usdc.balanceOf(address(escrow)), 100e6, "escrow holdings unchanged");
    }

    function test_SettlePartial_MultipleAccumulate() public {
        _deposit(buyer, 100e6);
        uint256 pid = _lock(10e6);

        vm.startPrank(seller);
        escrow.settlePartial(pid, 3e6);
        escrow.settlePartial(pid, 4e6);
        vm.expectEmit(true, false, false, true, address(escrow));
        emit Escrow.SettlePartial(pid, 2_500_000, 9_500_000);
        escrow.settlePartial(pid, 2_500_000);
        vm.stopPrank();

        assertEq(escrow.balances(seller), 9_500_000, "seller credited the sum");
        assertEq(escrow.capturedOf(pid), 9_500_000, "captured == sum of captures");
        assertTrue(_stateOf(pid) == Escrow.State.Locked, "still Locked");
    }

    function test_SettlePartial_UpToExactMax_StaysLocked() public {
        _deposit(buyer, 100e6);
        uint256 maxAmount = 10e6;
        uint256 pid = _lock(maxAmount);

        vm.startPrank(seller);
        escrow.settlePartial(pid, 6e6);
        escrow.settlePartial(pid, 4e6); // exact remainder
        vm.stopPrank();

        assertEq(escrow.capturedOf(pid), maxAmount, "captured == maxAmount");
        assertTrue(_stateOf(pid) == Escrow.State.Locked, "still Locked at cap");
        assertEq(escrow.balances(seller), maxAmount, "seller fully paid");

        // nothing left: even 1 unit of further capture exceeds the cap
        vm.prank(seller);
        vm.expectRevert(abi.encodeWithSelector(Escrow.ExceedsMaxAmount.selector, maxAmount + 1, maxAmount));
        escrow.settlePartial(pid, 1);
    }

    function test_RevertSettlePartial_ExceedsMaxAmount() public {
        _deposit(buyer, 100e6);
        uint256 maxAmount = 10e6;
        uint256 pid = _lock(maxAmount);

        vm.startPrank(seller);
        escrow.settlePartial(pid, 7e6);
        // attempted cumulative total 7e6 + 4e6 = 11e6 > 10e6
        vm.expectRevert(abi.encodeWithSelector(Escrow.ExceedsMaxAmount.selector, 11e6, maxAmount));
        escrow.settlePartial(pid, 4e6);
        vm.stopPrank();

        assertEq(escrow.capturedOf(pid), 7e6, "failed capture left captured untouched");
    }

    function test_RevertSettlePartial_ZeroAmount() public {
        _deposit(buyer, 100e6);
        uint256 pid = _lock(10e6);

        vm.prank(seller);
        vm.expectRevert(Escrow.ZeroAmount.selector);
        escrow.settlePartial(pid, 0);
    }

    function test_RevertSettlePartial_NotSeller() public {
        _deposit(buyer, 100e6);
        uint256 pid = _lock(10e6);

        vm.prank(buyer);
        vm.expectRevert(abi.encodeWithSelector(Escrow.NotSeller.selector, buyer, seller));
        escrow.settlePartial(pid, 4e6);

        vm.prank(thirdParty);
        vm.expectRevert(abi.encodeWithSelector(Escrow.NotSeller.selector, thirdParty, seller));
        escrow.settlePartial(pid, 4e6);
    }

    function test_RevertSettlePartial_NonexistentId() public {
        vm.prank(seller);
        vm.expectRevert(abi.encodeWithSelector(Escrow.NotLocked.selector, 42, Escrow.State.None));
        escrow.settlePartial(42, 4e6);
    }

    function test_RevertSettlePartial_AfterTerminalStates() public {
        _deposit(buyer, 100e6);
        uint256 pid = _lock(10e6);

        vm.prank(seller);
        escrow.settle(pid, 4e6);
        vm.prank(seller);
        vm.expectRevert(abi.encodeWithSelector(Escrow.NotLocked.selector, pid, Escrow.State.Settled));
        escrow.settlePartial(pid, 1e6);

        uint256 pid2 = _lock(5e6);
        (,,, uint64 expiry,) = escrow.getPayment(pid2);
        vm.warp(expiry);
        vm.prank(buyer);
        escrow.refund(pid2);
        vm.prank(seller);
        vm.expectRevert(abi.encodeWithSelector(Escrow.NotLocked.selector, pid2, Escrow.State.Refunded));
        escrow.settlePartial(pid2, 1e6);
    }

    /// @dev TTL policy aligned with `settle`: capture stays allowed while Locked,
    ///      even past expiresAt (seller may flush usage after serving; races refund).
    function test_SettlePartial_AfterTtl() public {
        _deposit(buyer, 100e6);
        uint256 pid = _lock(10e6);

        (,,, uint64 expiry,) = escrow.getPayment(pid);
        vm.warp(expiry + 1); // past expiry, before any refund

        vm.prank(seller);
        escrow.settlePartial(pid, 4e6);

        assertEq(escrow.capturedOf(pid), 4e6, "captured after ttl");
        assertTrue(_stateOf(pid) == Escrow.State.Locked, "still Locked");
    }

    /// @dev Boundary: exactly at expiresAt refund becomes available, but the
    ///      payment is still Locked — settlePartial (like settle) still allowed.
    function test_SettlePartial_AtExactTtlBoundary() public {
        _deposit(buyer, 100e6);
        uint256 pid = _lock(10e6);

        (,,, uint64 expiry,) = escrow.getPayment(pid);
        vm.warp(expiry);

        vm.prank(seller);
        escrow.settlePartial(pid, 2e6);
        assertEq(escrow.capturedOf(pid), 2e6, "captured at boundary");
    }

    /// @dev Refund after partial captures pays out only maxAmount - captured.
    function test_Refund_AfterPartial_DeductsCaptured() public {
        _deposit(buyer, 100e6);
        uint256 maxAmount = 10e6;
        uint256 pid = _lock(maxAmount);

        vm.prank(seller);
        escrow.settlePartial(pid, 4e6);

        (,,, uint64 expiry,) = escrow.getPayment(pid);
        vm.warp(expiry);

        vm.expectEmit(true, true, false, true, address(escrow));
        emit Escrow.Refunded(pid, buyer, maxAmount - 4e6, buyer);

        vm.prank(buyer);
        escrow.refund(pid);

        assertEq(escrow.balances(buyer), 90e6 + 6e6, "buyer refunded the remainder only");
        assertEq(escrow.balances(seller), 4e6, "seller keeps captured");
        assertTrue(_stateOf(pid) == Escrow.State.Refunded, "state Refunded");
    }

    /// @dev Edge: refund after the cap was fully captured transfers nothing but
    ///      still terminates the payment (settle/settlePartial then revert).
    function test_Refund_AfterFullCapture_ZeroPayout() public {
        _deposit(buyer, 100e6);
        uint256 maxAmount = 10e6;
        uint256 pid = _lock(maxAmount);

        vm.startPrank(seller);
        escrow.settlePartial(pid, 10e6);
        vm.stopPrank();

        (,,, uint64 expiry,) = escrow.getPayment(pid);
        vm.warp(expiry);

        vm.expectEmit(true, true, false, true, address(escrow));
        emit Escrow.Refunded(pid, buyer, 0, thirdParty);

        vm.prank(thirdParty);
        escrow.refund(pid);

        assertEq(escrow.balances(buyer), 90e6, "buyer balance unchanged");
        assertEq(escrow.balances(seller), maxAmount, "seller keeps all captured");
        assertTrue(_stateOf(pid) == Escrow.State.Refunded, "terminated");

        vm.prank(seller);
        vm.expectRevert(abi.encodeWithSelector(Escrow.NotLocked.selector, pid, Escrow.State.Refunded));
        escrow.settle(pid, maxAmount);
    }

    // --- settle x settlePartial interplay --------------------------------

    /// @dev `actual` is the CUMULATIVE total owed to the seller: closing at
    ///      exactly `captured` pays no top-up and refunds the remainder.
    function test_Settle_AfterPartial_CloseOutAtCaptured() public {
        _deposit(buyer, 100e6);
        uint256 maxAmount = 10e6;
        uint256 pid = _lock(maxAmount);

        vm.startPrank(seller);
        escrow.settlePartial(pid, 4e6);
        vm.expectEmit(true, true, true, true, address(escrow));
        emit Escrow.Settled(pid, buyer, seller, 4e6, 6e6);
        escrow.settle(pid, 4e6);
        vm.stopPrank();

        assertEq(escrow.balances(seller), 4e6, "no double pay: only the capture");
        assertEq(escrow.balances(buyer), 96e6, "buyer refunded the remainder");
        assertEq(escrow.capturedOf(pid), maxAmount, "captured bumped to maxAmount");
        assertTrue(_stateOf(pid) == Escrow.State.Settled, "terminal Settled");
    }

    /// @dev Top-up: settle declares a cumulative `actual` above `captured`.
    function test_Settle_AfterPartial_TopUp() public {
        _deposit(buyer, 100e6);
        uint256 pid = _lock(10e6);

        vm.startPrank(seller);
        escrow.settlePartial(pid, 3e6);
        escrow.settle(pid, 8e6); // cumulative 8e6: +5e6 top-up
        vm.stopPrank();

        assertEq(escrow.balances(seller), 8e6, "seller total == cumulative actual");
        assertEq(escrow.balances(buyer), 92e6, "buyer gets maxAmount - actual");
        assertTrue(_stateOf(pid) == Escrow.State.Settled, "terminal Settled");
    }

    function test_RevertSettle_ActualBelowCaptured() public {
        _deposit(buyer, 100e6);
        uint256 pid = _lock(10e6);

        vm.startPrank(seller);
        escrow.settlePartial(pid, 5e6);
        vm.expectRevert(abi.encodeWithSelector(Escrow.BelowCaptured.selector, pid, 4e6, 5e6));
        escrow.settle(pid, 4e6);
        vm.stopPrank();

        assertTrue(_stateOf(pid) == Escrow.State.Locked, "revert left state untouched");
        assertEq(escrow.capturedOf(pid), 5e6, "captured untouched");
    }

    /// @dev Mixed sequence across payments: partials, close-out settle at a
    ///      cumulative actual, refund with captured deduction, ongoing partially
    ///      captured lock, withdraw. Token conservation must hold throughout:
    ///      holdings == Σbalances + Σ(Locked: maxAmount - captured).
    function test_Invariant_MixedPartialFlow_TokenConservation() public {
        address buyer2 = makeAddr("buyer2");
        _deposit(buyer, 100e6);
        _deposit(buyer2, 50e6);

        vm.startPrank(buyer);
        uint256 p1 = escrow.lock(seller, 10e6, 0);
        uint256 p2 = escrow.lock(seller, 8e6, 0);
        vm.stopPrank();
        vm.startPrank(buyer2);
        uint256 p3 = escrow.lock(seller, 20e6, 0);
        vm.stopPrank();

        // p1: two partial captures, then close-out settle at cumulative 9e6
        vm.startPrank(seller);
        escrow.settlePartial(p1, 4e6);
        escrow.settlePartial(p1, 3e6);
        escrow.settle(p1, 9e6); // +2e6 top-up
        // p2: single capture, stays Locked
        escrow.settlePartial(p2, 5e6);
        vm.stopPrank();

        // p3: capture 6e6, then refund returns only 14e6 to the buyer
        vm.startPrank(seller);
        escrow.settlePartial(p3, 6e6);
        vm.stopPrank();
        (,,, uint64 p3Expiry,) = escrow.getPayment(p3);
        vm.warp(p3Expiry);
        vm.prank(thirdParty);
        escrow.refund(p3);

        // ongoing uncaptured lock
        vm.startPrank(buyer2);
        uint256 p4 = escrow.lock(seller, 7e6, 0);
        vm.stopPrank();

        // seller cashes out everything credited so far: 4+3+2 (p1) +5 (p2) +6 (p3) = 20e6
        vm.startPrank(seller);
        escrow.withdraw(20e6);
        vm.stopPrank();

        // ledger expectations
        //  buyer: 100 -10(p1) -8(p2) +1(p1 settle remainder) = 83e6
        assertEq(escrow.balances(buyer), 83e6, "buyer ledger");
        //  buyer2: 50 -20(p3) +14(p3 refund) -7(p4 lock) = 37e6
        assertEq(escrow.balances(buyer2), 37e6, "buyer2 ledger");
        assertEq(escrow.balances(seller), 0, "seller fully withdrawn");
        assertEq(escrow.balances(thirdParty), 0, "refund caller gains nothing");

        // conservation: p2 Locked (captured 5e6), p4 Locked (captured 0)
        uint256 sumBalances = escrow.balances(buyer) + escrow.balances(buyer2) + escrow.balances(seller);
        uint256 sumLockedRemaining = (8e6 - 5e6) + (7e6 - 0);
        assertEq(usdc.balanceOf(address(escrow)), sumBalances + sumLockedRemaining, "token conservation");

        assertTrue(_stateOf(p1) == Escrow.State.Settled, "p1 settled");
        assertTrue(_stateOf(p2) == Escrow.State.Locked, "p2 locked");
        assertTrue(_stateOf(p3) == Escrow.State.Refunded, "p3 refunded");
        assertTrue(_stateOf(p4) == Escrow.State.Locked, "p4 locked");
        assertEq(escrow.capturedOf(p2), 5e6, "p2 captured");
    }

    /// @dev settlePartial is ledger-only like settle/refund: no external token
    ///      call, hence no reentrancy surface.
    function test_SettlePartial_NoExternalTokenCalls() public {
        MaliciousToken token = new MaliciousToken();
        escrow = new Escrow(IERC20(address(token)), 0, treasury);

        address b = makeAddr("b");
        token.mint(b, 100e6);
        vm.startPrank(b);
        token.approve(address(escrow), 100e6);
        escrow.deposit(100e6);
        uint256 pid = escrow.lock(seller, 10e6, 0);
        vm.stopPrank();

        vm.prank(seller);
        escrow.settlePartial(pid, 4e6);

        assertEq(token.transferCalls(), 0, "no token push during partial capture");
        assertEq(token.transferFromCalls(), 1, "only deposit pulled tokens");
        assertEq(escrow.balances(seller), 4e6, "seller ledger credited");
    }

    // =====================================================================
    // withdraw
    // =====================================================================

    function test_Withdraw_Happy() public {
        _deposit(buyer, 100e6);

        vm.expectEmit(true, false, false, true, address(escrow));
        emit Escrow.Withdrawn(buyer, 100e6);

        vm.prank(buyer);
        escrow.withdraw(100e6);

        assertEq(escrow.balances(buyer), 0, "balance cleared");
        assertEq(usdc.balanceOf(address(escrow)), 0, "escrow emptied");
        assertEq(usdc.balanceOf(buyer), 100e6, "tokens returned to buyer");
    }

    function test_RevertWithdraw_ZeroAmount() public {
        _deposit(buyer, 100e6);
        vm.prank(buyer);
        vm.expectRevert(Escrow.ZeroAmount.selector);
        escrow.withdraw(0);
    }

    function test_RevertWithdraw_InsufficientBalance() public {
        _deposit(buyer, 100e6);
        vm.prank(buyer);
        vm.expectRevert(abi.encodeWithSelector(Escrow.InsufficientBalance.selector, 101e6, 100e6));
        escrow.withdraw(101e6);
    }

    /// @dev Funds moved into a Locked payment are NOT withdrawable.
    function test_RevertWithdraw_LockedFundsNotWithdrawable() public {
        _deposit(buyer, 10e6);
        _lock(8e6); // only 2e6 remains withdrawable

        vm.startPrank(buyer);
        vm.expectRevert(abi.encodeWithSelector(Escrow.InsufficientBalance.selector, 10e6, 2e6));
        escrow.withdraw(10e6); // trying to pull locked funds reverts

        escrow.withdraw(2e6); // unlocked remainder is fine
        vm.stopPrank();

        assertEq(escrow.balances(buyer), 0, "withdrawable balance zero");
        assertEq(usdc.balanceOf(address(escrow)), 8e6, "locked funds still escrowed");
    }

    // =====================================================================
    // isValid
    // =====================================================================

    function test_IsValid_True() public {
        _deposit(buyer, 100e6);
        uint256 pid = _lock(10e6);

        assertTrue(escrow.isValid(pid, seller, 10e6), "valid for designated seller");
        assertTrue(escrow.isValid(pid, seller, 1), "valid with smaller minAmount");
        assertTrue(escrow.isValid(pid, seller, 0), "valid with minAmount 0");
    }

    function test_IsValid_False_SellerMismatch() public {
        _deposit(buyer, 100e6);
        uint256 pid = _lock(10e6);

        assertFalse(escrow.isValid(pid, thirdParty, 10e6), "wrong seller");
        assertFalse(escrow.isValid(pid, buyer, 10e6), "buyer is not the seller");
    }

    function test_IsValid_False_MinAmountExceedsMax() public {
        _deposit(buyer, 100e6);
        uint256 pid = _lock(10e6);

        assertFalse(escrow.isValid(pid, seller, 10e6 + 1), "minAmount > maxAmount");
    }

    function test_IsValid_False_Expired() public {
        _deposit(buyer, 100e6);
        uint256 pid = _lock(10e6);

        (,,, uint64 expiry,) = escrow.getPayment(pid);
        vm.warp(expiry); // inclusive boundary: invalid the moment expiry is reached

        assertFalse(escrow.isValid(pid, seller, 10e6), "expired (boundary)");
    }

    function test_IsValid_False_NonexistentId() public view {
        assertFalse(escrow.isValid(42, seller, 1), "unknown id");
    }

    /// @dev Settled/Refunded payments are no longer valid for the relay.
    function test_IsValid_False_AfterTerminalState() public {
        _deposit(buyer, 100e6);
        uint256 pid = _lock(10e6);

        vm.prank(seller);
        escrow.settle(pid, 4e6);
        assertFalse(escrow.isValid(pid, seller, 1), "settled is not valid");

        uint256 pid2 = _lock(5e6);
        (,,, uint64 expiry,) = escrow.getPayment(pid2);
        vm.warp(expiry);
        vm.prank(buyer);
        escrow.refund(pid2);
        assertFalse(escrow.isValid(pid2, seller, 1), "refunded is not valid");
    }

    // =====================================================================
    // reentrancy
    // =====================================================================

    /// @dev Attacker + malicious token wired together; a second participant has
    ///      deposited 2e6 so the escrow holds >= 1 token (needed to make an inner
    ///      withdraw business-executable absent the guard).
    function _setupAttack() internal returns (MaliciousToken token, MaliciousAttacker attacker) {
        token = new MaliciousToken();
        escrow = new Escrow(IERC20(address(token)), 0, treasury);
        attacker = new MaliciousAttacker(escrow, IERC20(address(token)));
        token.setHook(address(attacker)); // the attacker is the hook, NOT the escrow

        token.mint(address(attacker), 101e6); // 100e6 to operate, 1e6 for the re-enter deposit

        address other = makeAddr("other");
        token.mint(other, 2e6);
        vm.startPrank(other);
        token.approve(address(escrow), 2e6);
        escrow.deposit(2e6);
        vm.stopPrank();
    }

    /// @dev Re-entrant withdraw(1) as the ATTACKER during the token pull of the
    ///      attacker's own deposit. Business-legal absent the guard: the ledger
    ///      is credited (CEI) before the pull, and the escrow holds >= 1 token.
    ///      Inner call is a raw call; only the exact ReentrancyGuardReentrantCall
    ///      selector (0x3ee5aeb5) counts as "blocked". Proven discriminative by
    ///      mutation: removing nonReentrant flips reentrySucceeded -> test red.
    function test_ReentrantDeposit_Blocked() public {
        (MaliciousToken token, MaliciousAttacker attacker) = _setupAttack();

        attacker.setMode(uint8(MaliciousAttacker.Mode.ReenterWithdraw));
        attacker.depositThenReenter(); // deposit 100e6, inner withdraw(1) mid-pull

        assertFalse(attacker.reentrySucceeded(), "reentry must not succeed");
        assertTrue(attacker.reentryBlocked(), "guard must block reentry");
        // exact revert data: ReentrancyGuardReentrantCall() selector
        assertTrue(attacker.lastRevertData().length >= 4, "revert data present");
        assertEq(bytes4(attacker.lastRevertData()), bytes4(0x3ee5aeb5), "exact guard selector");

        // invariant: only the outer deposit is reflected, nothing else moved
        assertEq(escrow.balances(address(attacker)), 100e6, "attacker ledger = deposit only");
        assertEq(escrow.balances(address(escrow)), 0, "no self-credited balance");
        assertEq(token.balanceOf(address(escrow)), 102e6, "escrow holdings = 100e6 + 2e6");
    }

    /// @dev Re-entrant deposit(1) as the ATTACKER during the token push of the
    ///      attacker's own withdraw. Business-legal absent the guard: attacker
    ///      holds 1e6 tokens and a max approval. Exact guard selector asserted.
    function test_ReentrantWithdraw_Blocked() public {
        (MaliciousToken token, MaliciousAttacker attacker) = _setupAttack();

        // fund escrow ledger normally (no reentry mode)
        attacker.depositThenReenter(); // mode None: plain deposit of 100e6

        // arm: re-enter deposit(1) during escrow's outgoing transfer
        attacker.setMode(uint8(MaliciousAttacker.Mode.ReenterDeposit));
        attacker.withdrawThenReenter(); // withdraw 100e6

        assertFalse(attacker.reentrySucceeded(), "reentry must not succeed");
        assertTrue(attacker.reentryBlocked(), "reentry must be blocked by the guard");
        assertTrue(attacker.lastRevertData().length >= 4, "revert data captured");
        assertEq(bytes4(attacker.lastRevertData()), bytes4(0x3ee5aeb5), "exact guard selector");

        // invariant: withdraw completed exactly once, re-entry contributed nothing
        assertEq(escrow.balances(address(attacker)), 0, "attacker ledger zero");
        assertEq(token.balanceOf(address(attacker)), 101e6, "attacker holds 1e6 kept + 100e6 withdrawn");
        assertEq(token.balanceOf(address(escrow)), 2e6, "escrow holds only the other participant's deposit");
    }

    /// @dev settle/refund move NO tokens and make NO external calls (pure ledger
    ///      updates) — there is no callback surface to re-enter from. The only
    ///      "re-entry" possible is a call after completion, which the state machine
    ///      blocks (Locked -> Settled/Refunded terminal transitions).
    function test_SettleRefund_NoExternalTokenCalls() public {
        MaliciousToken token = new MaliciousToken();
        escrow = new Escrow(IERC20(address(token)), 0, treasury);
        // no hook registered: the token behaves as a plain ERC-20 here

        address b = makeAddr("b");
        token.mint(b, 100e6);
        vm.startPrank(b);
        token.approve(address(escrow), 100e6);
        escrow.deposit(100e6);
        uint256 pid = escrow.lock(seller, 10e6, 0);
        vm.stopPrank();

        vm.prank(seller);
        escrow.settle(pid, 4e6); // ledger-only

        vm.startPrank(b);
        uint256 pid2 = escrow.lock(seller, 5e6, 0);
        vm.stopPrank();

        (,,, uint64 expiry,) = escrow.getPayment(pid2);
        vm.warp(expiry);
        vm.prank(b);
        escrow.refund(pid2); // ledger-only

        // deposit contributed exactly one transferFrom; lock/settle/refund made zero token calls
        assertEq(token.transferFromCalls(), 1, "only deposit pulled tokens");
        assertEq(token.transferCalls(), 0, "no token transfer during lock/settle/refund");
        assertEq(escrow.balances(seller), 4e6, "seller credited");
        assertEq(escrow.balances(b), 96e6, "buyer credited");
    }

    /// @dev Malicious seller contract: settle completes, an immediate second settle
    ///      in the same tx is rejected by the state machine (NotLocked).
    function test_ReentrantSeller_DoubleSettleBlocked() public {
        ReentrantSeller attackerSeller = new ReentrantSeller(escrow);
        _deposit(buyer, 100e6);

        vm.startPrank(buyer);
        uint256 pid = escrow.lock(address(attackerSeller), 10e6, 0);
        vm.stopPrank();

        attackerSeller.attackSettle(pid, 4e6);

        assertTrue(attackerSeller.doubleSettleBlocked(), "second settle must revert");
        assertEq(attackerSeller.settles(), 1, "exactly one settle succeeded");
        assertEq(escrow.balances(address(attackerSeller)), 4e6, "seller credited once");
        assertTrue(_stateOf(pid) == Escrow.State.Settled, "terminal state");
    }

    // =====================================================================
    // global accounting invariant
    // =====================================================================

    /**
     * @dev Multiple buyers, multiple payments, mixed operations (partial settle,
     *      full settle, zero settle, refund, ongoing lock, withdraws by every
     *      party) — after everything, the escrow's token holdings must equal
     *      the sum of all ledger balances PLUS the maxAmount of every payment
     *      that has NOT reached a terminal state.
     *
     *      b1: deposit 100e6, lock p1=10e6 (sellerA), lock p2=6e6 (sellerB)
     *      b2: deposit 80e6,  lock p3=20e6 (sellerA)
     *      actions:
     *        settle p1 actual 4e6        (partial: sellerA +4, b1 +6)
     *        settle p2 actual 0          (full refund: b1 back to 100e6)
     *        settle p3 actual 20e6       (full: sellerA +20)
     *        lock p4 = 15e6 (b2 -> sellerB), refund p4 after ttl
     *        withdraws: b1 40e6, b2 25e6, sellerA 24e6
     *      unterminated payments left: p5 (b1 -> sellerB, 6e6)
     *
     *      expected escrow holdings = Σbalances + Σunterminated.maxAmount
     */
    function test_Invariant_MixedOperations_TokenConservation() public {
        address sellerB = makeAddr("sellerB");
        address buyer1 = makeAddr("buyer1");
        address buyer2 = makeAddr("buyer2");

        _deposit(buyer1, 100e6);
        _deposit(buyer2, 80e6);

        // --- locks
        vm.startPrank(buyer1);
        uint256 p1 = escrow.lock(seller, 10e6, 0);
        uint256 p2 = escrow.lock(sellerB, 6e6, 0);
        vm.stopPrank();

        vm.startPrank(buyer2);
        uint256 p3 = escrow.lock(seller, 20e6, 0);
        uint256 p4 = escrow.lock(sellerB, 15e6, 0);
        vm.stopPrank();

        // --- settles: partial, zero-actual (full refund), full
        vm.prank(seller);
        escrow.settle(p1, 4e6);
        vm.prank(sellerB);
        escrow.settle(p2, 0);
        vm.prank(seller);
        escrow.settle(p3, 20e6);

        // --- refund p4 past ttl (triggered by a third party)
        (,,, uint64 p4Expiry,) = escrow.getPayment(p4);
        vm.warp(p4Expiry);
        vm.prank(thirdParty);
        escrow.refund(p4);

        // --- one more unterminated lock (stays Locked)
        vm.startPrank(buyer1);
        uint256 p5 = escrow.lock(sellerB, 6e6, 0);
        vm.stopPrank();

        // --- withdraws by ledger participants
        vm.startPrank(buyer1);
        escrow.withdraw(40e6);
        vm.stopPrank();

        vm.startPrank(buyer2);
        escrow.withdraw(25e6);
        vm.stopPrank();

        vm.startPrank(seller);
        escrow.withdraw(24e6); // 4 (p1) + 20 (p3)
        vm.stopPrank();

        // --- ledger expectations
        //  buyer1: 100 -10(p1) -6(p2) +6(p1 settle) +6(p2 settle) -6(p5 lock) -40 = 50e6
        assertEq(escrow.balances(buyer1), 50e6, "buyer1 ledger");
        //  buyer2: 80 -20(p3) -15(p4 lock) +15(p4 refund) -25 = 35e6
        assertEq(escrow.balances(buyer2), 35e6, "buyer2 ledger");
        //  seller: 4 + 20 - 24 = 0
        assertEq(escrow.balances(seller), 0, "seller ledger");
        //  sellerB: 0 (never settled, only refunded/locked against)
        assertEq(escrow.balances(sellerB), 0, "sellerB ledger");
        //  thirdParty: 0 (refund caller gains nothing)
        assertEq(escrow.balances(thirdParty), 0, "third party ledger");

        // --- the invariant
        uint256 sumBalances = escrow.balances(buyer1) + escrow.balances(buyer2)
            + escrow.balances(seller) + escrow.balances(sellerB) + escrow.balances(thirdParty);

        uint256[] memory pids = new uint256[](5);
        pids[0] = p1;
        pids[1] = p2;
        pids[2] = p3;
        pids[3] = p4;
        pids[4] = p5;
        uint256 sumLocked = _unterminatedMax(pids);

        assertEq(usdc.balanceOf(address(escrow)), sumBalances + sumLocked, "token conservation");
        assertEq(sumLocked, 6e6, "only p5 unterminated");
    }

    /// @dev Sum of maxAmount over payments that have not reached a terminal state.
    function _unterminatedMax(uint256[] memory pids) internal view returns (uint256 total) {
        for (uint256 i = 0; i < pids.length; i++) {
            (, , uint256 maxAmount, , Escrow.State s) = escrow.getPayment(pids[i]);
            if (s == Escrow.State.Locked) total += maxAmount;
        }
    }

    // =====================================================================
    // protocol fee (Escrow v3)
    // =====================================================================

    /// @dev Deploy a fee-charging escrow against the shared MockUSDC and make
    ///      it the active `escrow` so _deposit/_lock/_stateOf keep working.
    function _newFeeEscrow(uint16 feeBps) internal returns (Escrow) {
        escrow = new Escrow(usdc, feeBps, treasury);
        return escrow;
    }

    /// @dev True if any log in `logs` is a FeeTaken event.
    function _hasFeeTaken(Vm.Log[] memory logs) internal view returns (bool) {
        bytes32 topic0 = keccak256("FeeTaken(uint256,uint256,uint256)");
        for (uint256 i = 0; i < logs.length; i++) {
            if (logs[i].emitter == address(escrow) && logs[i].topics[0] == topic0) return true;
        }
        return false;
    }

    // --- constructor validation -----------------------------------------

    function test_RevertConstructor_FeeBpsAbove10000() public {
        vm.expectRevert(Escrow.BadFeeConfig.selector);
        new Escrow(usdc, 10_001, treasury);
    }

    function test_Constructor_FeeBpsUpperBound10000Allowed() public {
        Escrow e = new Escrow(usdc, 10_000, treasury);
        assertEq(e.FEE_BPS(), 10_000, "upper bound accepted");
        assertEq(e.FEE_RECIPIENT(), treasury, "recipient stored");
    }

    function test_RevertConstructor_ZeroFeeRecipient() public {
        vm.expectRevert(Escrow.BadFeeConfig.selector);
        new Escrow(usdc, 100, address(0));
    }

    function test_Constructor_FeeConfigImmutables() public {
        Escrow e = new Escrow(usdc, 250, treasury);
        assertEq(e.FEE_BPS(), 250, "FEE_BPS exposed");
        assertEq(e.FEE_RECIPIENT(), treasury, "FEE_RECIPIENT exposed");
    }

    // --- settle fee -------------------------------------------------------

    /// @dev 100 bps = 1%: settle(actual 4e6) -> fee 40_000, seller 3_960_000,
    ///      buyer refund share (6e6) NOT charged.
    function test_Fee_Settle_100Bps() public {
        _newFeeEscrow(100);
        _deposit(buyer, 100e6);
        uint256 pid = _lock(10e6);

        uint256 actual = 4e6;
        uint256 fee = 40_000; // 4e6 * 100 / 10_000

        vm.expectEmit(true, false, false, true, address(escrow));
        emit Escrow.FeeTaken(pid, fee, actual - fee);
        vm.expectEmit(true, true, true, true, address(escrow));
        emit Escrow.Settled(pid, buyer, seller, actual, 6e6);

        vm.prank(seller);
        escrow.settle(pid, actual);

        assertEq(escrow.balances(seller), actual - fee, "seller gets amount - fee");
        assertEq(escrow.balances(treasury), fee, "treasury credited the fee");
        assertEq(escrow.balances(buyer), 96e6, "buyer refund fee-free (90 + 6)");
        assertTrue(_stateOf(pid) == Escrow.State.Settled, "terminal Settled");
    }

    /// @dev Rounding: fee is FLOORED. 123_456 * 100 / 10_000 = 1_234 (not 1_235).
    function test_Fee_Settle_RoundsDown() public {
        _newFeeEscrow(100);
        _deposit(buyer, 100e6);
        uint256 pid = _lock(10e6);

        vm.prank(seller);
        escrow.settle(pid, 123_456);

        assertEq(escrow.balances(treasury), 1_234, "fee floored");
        assertEq(escrow.balances(seller), 123_456 - 1_234, "seller gets the remainder");
        assertEq(escrow.balances(seller) + escrow.balances(treasury), 123_456, "no dust lost");
    }

    /// @dev Extreme: feeBps = 10_000 (100%) sends the entire credit to the
    ///      treasury; the seller's ledger is untouched.
    function test_Fee_Settle_FullFee_10000Bps() public {
        _newFeeEscrow(10_000);
        _deposit(buyer, 100e6);
        uint256 pid = _lock(10e6);

        vm.expectEmit(true, false, false, true, address(escrow));
        emit Escrow.FeeTaken(pid, 10e6, 0);
        vm.prank(seller);
        escrow.settle(pid, 10e6);

        assertEq(escrow.balances(seller), 0, "seller gets nothing");
        assertEq(escrow.balances(treasury), 10e6, "treasury gets everything");
        assertEq(escrow.balances(buyer), 90e6, "buyer refund untouched (full actual settled)");
    }

    /// @dev Cumulative semantics preserved: fee is charged only on the top-up
    ///      (actual - captured); advances already paid their fee at capture.
    function test_Fee_Settle_TopUpChargedOnIncrementOnly() public {
        _newFeeEscrow(100);
        _deposit(buyer, 100e6);
        uint256 pid = _lock(10e6);

        vm.startPrank(seller);
        escrow.settlePartial(pid, 3e6); // fee 30_000 -> seller 2_970_000
        escrow.settle(pid, 8e6); // top-up 5e6: fee 50_000 -> +4_950_000
        vm.stopPrank();

        assertEq(escrow.balances(seller), 2_970_000 + 4_950_000, "seller credited net per credit");
        assertEq(escrow.balances(treasury), 30_000 + 50_000, "treasury = sum of both fees");
        assertEq(escrow.balances(buyer), 92e6, "buyer gets maxAmount - actual");
    }

    /// @dev Close-out at exactly `captured`: top-up 0 -> no fee, no FeeTaken,
    ///      and the buyer's remainder is uncharged.
    function test_Fee_Settle_CloseOutAtCaptured_NoSecondFee() public {
        _newFeeEscrow(100);
        _deposit(buyer, 100e6);
        uint256 pid = _lock(10e6);

        vm.startPrank(seller);
        escrow.settlePartial(pid, 4e6); // fee 40_000
        escrow.settle(pid, 4e6); // top-up 0
        vm.stopPrank();

        assertEq(escrow.balances(seller), 4e6 - 40_000, "only the capture credit");
        assertEq(escrow.balances(treasury), 40_000, "no extra fee on close-out");
        assertEq(escrow.balances(buyer), 96e6, "remainder fee-free");
        assertTrue(_stateOf(pid) == Escrow.State.Settled, "terminal Settled");
    }

    /// @dev actual == 0 with captured == 0: nothing credited, no fee at all.
    function test_Fee_Settle_ZeroActual_NoFee() public {
        _newFeeEscrow(100);
        _deposit(buyer, 100e6);
        uint256 pid = _lock(10e6);

        vm.recordLogs();
        vm.prank(seller);
        escrow.settle(pid, 0);
        Vm.Log[] memory logs = vm.getRecordedLogs();

        assertFalse(_hasFeeTaken(logs), "no FeeTaken on zero top-up");
        assertEq(escrow.balances(seller), 0, "seller untouched");
        assertEq(escrow.balances(treasury), 0, "treasury untouched");
        assertEq(escrow.balances(buyer), 100e6, "buyer fully refunded");
    }

    // --- settlePartial fee -------------------------------------------------

    /// @dev Each capture is charged independently (gross captured, net credited).
    function test_Fee_SettlePartial_PerCaptureFee() public {
        _newFeeEscrow(100);
        _deposit(buyer, 100e6);
        uint256 pid = _lock(10e6);

        vm.startPrank(seller);
        vm.expectEmit(true, false, false, true, address(escrow));
        emit Escrow.FeeTaken(pid, 30_000, 2_970_000);
        vm.expectEmit(true, false, false, true, address(escrow));
        emit Escrow.SettlePartial(pid, 3e6, 3e6);
        escrow.settlePartial(pid, 3e6);

        vm.expectEmit(true, false, false, true, address(escrow));
        emit Escrow.FeeTaken(pid, 40_000, 3_960_000);
        vm.expectEmit(true, false, false, true, address(escrow));
        emit Escrow.SettlePartial(pid, 4e6, 7e6);
        escrow.settlePartial(pid, 4e6);
        vm.stopPrank();

        assertEq(escrow.balances(seller), 6_930_000, "seller credited net per capture");
        assertEq(escrow.balances(treasury), 70_000, "treasury credited both fees");
        assertEq(escrow.capturedOf(pid), 7e6, "captured tracks GROSS");
        assertTrue(_stateOf(pid) == Escrow.State.Locked, "still Locked");
    }

    /// @dev Sub-unit fee rounds to zero: capture of 1 at 100 bps pays no fee,
    ///      emits no FeeTaken, and the seller is credited the full unit.
    function test_Fee_SettlePartial_TinyAmount_FeeRoundsToZero() public {
        _newFeeEscrow(100);
        _deposit(buyer, 100e6);
        uint256 pid = _lock(10e6);

        vm.recordLogs();
        vm.prank(seller);
        escrow.settlePartial(pid, 1); // fee = 1 * 100 / 10_000 = 0
        Vm.Log[] memory logs = vm.getRecordedLogs();

        assertFalse(_hasFeeTaken(logs), "zero fee emits nothing");
        assertEq(escrow.balances(seller), 1, "seller credited in full");
        assertEq(escrow.balances(treasury), 0, "treasury untouched");
        assertEq(escrow.capturedOf(pid), 1, "captured is gross");
    }

    // --- refund: never charged ---------------------------------------------

    /// @dev Refund pays the buyer the FULL remaining escrow, regardless of the
    ///      fee configuration; only captured advances (and their fees) left.
    function test_Fee_Refund_NoFee() public {
        _newFeeEscrow(100);
        _deposit(buyer, 100e6);
        uint256 maxAmount = 10e6;
        uint256 pid = _lock(maxAmount);

        vm.prank(seller);
        escrow.settlePartial(pid, 4e6); // fee 40_000 is the ONLY treasury income

        (,,, uint64 expiry,) = escrow.getPayment(pid);
        vm.warp(expiry);

        vm.expectEmit(true, true, false, true, address(escrow));
        emit Escrow.Refunded(pid, buyer, maxAmount - 4e6, buyer);
        vm.prank(buyer);
        escrow.refund(pid);

        assertEq(escrow.balances(buyer), 96e6, "buyer refunded the full remainder");
        assertEq(escrow.balances(seller), 4e6 - 40_000, "seller keeps net capture");
        assertEq(escrow.balances(treasury), 40_000, "refund took no fee");
    }

    /// @dev Uncaptured lock refunded entirely: treasury gains zero.
    function test_Fee_Refund_UncapturedLock_TreasuryGainsNothing() public {
        _newFeeEscrow(100);
        _deposit(buyer, 100e6);
        uint256 pid = _lock(10e6);

        (,,, uint64 expiry,) = escrow.getPayment(pid);
        vm.warp(expiry);

        vm.prank(thirdParty);
        escrow.refund(pid);

        assertEq(escrow.balances(buyer), 100e6, "buyer fully refunded");
        assertEq(escrow.balances(treasury), 0, "no fee on refund");
        assertEq(escrow.balances(seller), 0, "seller untouched");
    }

    // --- feeBps = 0: free path ----------------------------------------------

    /// @dev With feeBps = 0 the settle/settlePartial flow is byte-identical to
    ///      v2: full credit, zero treasury, and NO FeeTaken event.
    function test_Fee_ZeroBps_NoFeeTaken() public {
        _newFeeEscrow(0);
        _deposit(buyer, 100e6);
        uint256 pid = _lock(10e6);

        vm.recordLogs();
        vm.startPrank(seller);
        escrow.settlePartial(pid, 4e6);
        escrow.settle(pid, 8e6);
        vm.stopPrank();
        Vm.Log[] memory logs = vm.getRecordedLogs();

        assertFalse(_hasFeeTaken(logs), "zero-fee config emits no FeeTaken");
        assertEq(escrow.balances(seller), 8e6, "seller credited gross");
        assertEq(escrow.balances(treasury), 0, "treasury untouched");
    }

    // --- conservation ----------------------------------------------------

    /// @dev Fee-aware token conservation: escrow holdings == Σbalances
    ///      (treasury included) + Σ(Locked: maxAmount - gross captured).
    function test_Fee_Conservation_MixedFlow() public {
        _newFeeEscrow(100);
        _deposit(buyer, 100e6);

        vm.startPrank(buyer);
        uint256 p1 = escrow.lock(seller, 10e6, 0);
        uint256 p2 = escrow.lock(seller, 8e6, 0);
        vm.stopPrank();

        // p1: capture 4e6 (fee 40k), settle cumulative 9e6 (top-up 5e6, fee 50k)
        vm.startPrank(seller);
        escrow.settlePartial(p1, 4e6);
        escrow.settle(p1, 9e6);
        // p2: capture 5e6 (fee 50k), stays Locked
        escrow.settlePartial(p2, 5e6);
        vm.stopPrank();

        // p2 refund: buyer gets 8e6 - 5e6 = 3e6, fee-free
        (,,, uint64 p2Expiry,) = escrow.getPayment(p2);
        vm.warp(p2Expiry);
        vm.prank(thirdParty);
        escrow.refund(p2);

        // seller cashes out: (4e6 - 40k) + (5e6 - 50k) + (9e6 - 4e6 - 50k)
        uint256 sellerNet = 3_960_000 + 4_950_000 + 4_950_000;
        vm.startPrank(seller);
        escrow.withdraw(sellerNet);
        vm.stopPrank();

        //  buyer: 100 -10(p1) -8(p2) +1(p1 remainder) +3(p2 refund) = 86e6
        assertEq(escrow.balances(buyer), 86e6, "buyer ledger");
        assertEq(escrow.balances(seller), 0, "seller fully withdrawn");
        assertEq(escrow.balances(treasury), 140_000, "treasury = 40k + 50k + 50k");

        uint256 sumBalances = escrow.balances(buyer) + escrow.balances(treasury) + escrow.balances(seller);
        assertEq(usdc.balanceOf(address(escrow)), sumBalances, "holdings == balances (no Locked left)");
        assertTrue(_stateOf(p1) == Escrow.State.Settled, "p1 settled");
        assertTrue(_stateOf(p2) == Escrow.State.Refunded, "p2 refunded");
    }
}
