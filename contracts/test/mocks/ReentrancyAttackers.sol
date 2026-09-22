// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Escrow} from "../../src/Escrow.sol";

/// @dev Hook interface: the malicious token calls this on `to == hook` transfers.
interface MaliciousHook {
    function onTokenTransfer() external;
}

/**
 * @title MaliciousToken
 * @notice Test-only token shaped like an ERC-20. During `transfer`/`transferFrom`
 *         it notifies the registered hook so the hook can attempt a re-entrant
 *         call back into the Escrow mid-flight. It behaves like a normal ERC-20
 *         otherwise (no allowance needed when from == msg.sender, mirroring
 *         OpenZeppelin's _spendAllowance).
 */
contract MaliciousToken {
    address public hook;

    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    uint256 public transferCalls;
    uint256 public transferFromCalls;

    function setHook(address hook_) external {
        hook = hook_;
    }

    function mint(address to, uint256 amount) external {
        totalSupply += amount;
        balanceOf[to] += amount;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        transferCalls++;
        require(balanceOf[msg.sender] >= amount, "balance");
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
        _notify(msg.sender, to);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        transferFromCalls++;
        if (from != msg.sender) {
            uint256 allowed = allowance[from][msg.sender];
            if (allowed != type(uint256).max) {
                require(allowed >= amount, "allowance");
                allowance[from][msg.sender] = allowed - amount;
            }
        }
        require(balanceOf[from] >= amount, "balance");
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        _notify(from, to);
        return true;
    }

    /// @dev Fires when the hook is a party to the movement — covering both the
    ///      deposit pull (from == hook) and the withdraw push (to == hook).
    function _notify(address from, address to) internal {
        if (hook != address(0) && (from == hook || to == hook)) {
            MaliciousHook(hook).onTokenTransfer();
        }
    }
}

/**
 * @title MaliciousAttacker
 * @notice Reentrancy attacker whose inner calls are BUSINESS-LEGITIMATE: the
 *         only thing that can stop them is Escrow's ReentrancyGuard.
 *
 *   - depositThenReenter(): attacker deposits 100e6. Effects run first (ledger
 *     credited), so during the token pull the attacker's escrow ledger is
 *     >= 1 and the escrow holds >= 1 token (funded by a second participant) —
 *     the re-entrant `withdraw(1)` as the ATTACKER is fully executable absent
 *     the guard.
 *   - withdrawThenReenter(): attacker withdraws 100e6 (keeps 1e6 tokens and a
 *     max approval). During the outgoing transfer it re-enters `deposit(1)`
 *     as the ATTACKER — allowance and balance are both satisfied, so absent
 *     the guard it must succeed.
 *
 * The inner call is a raw `call`; outcomes are recorded precisely:
 *   - ok                  -> reentrySucceeded (guard absent -> tests MUST fail)
 *   - revert selector
 *     0x3ee5aeb5          -> reentryBlocked  (ReentrancyGuardReentrantCall)
 *   - anything else       -> whole tx reverts (loud failure, not silent pass)
 */
contract MaliciousAttacker is MaliciousHook {
    /// @dev keccak256("ReentrancyGuardReentrantCall()") — OpenZeppelin v5 guard error.
    bytes4 internal constant REENTRANCY_GUARD_SELECTOR = 0x3ee5aeb5;

    enum Mode {
        None,
        ReenterWithdraw,
        ReenterDeposit
    }

    Escrow public escrow;
    IERC20 public token;
    Mode public mode;

    bool public reentrySucceeded;
    bool public reentryBlocked;
    bytes public lastRevertData;

    error UnexpectedRevertData(bytes data);

    constructor(Escrow escrow_, IERC20 token_) {
        escrow = escrow_;
        token = token_;
        token.approve(address(escrow), type(uint256).max);
    }

    function setMode(uint8 mode_) external {
        mode = Mode(mode_);
    }

    /// @dev Called by the token while Escrow is mid-flight (checks-effects done).
    function onTokenTransfer() external {
        require(msg.sender == address(token), "only token");
        Mode m = mode;
        mode = Mode.None;
        if (m == Mode.ReenterWithdraw) {
            // msg.sender is the ATTACKER here: ledger was already credited by
            // the outer deposit, so this withdraw is legal absent the guard.
            (bool ok, bytes memory ret) = address(escrow).call(abi.encodeCall(Escrow.withdraw, (1)));
            _record(ok, ret);
        } else if (m == Mode.ReenterDeposit) {
            // msg.sender is the ATTACKER: holds tokens + max approval, so this
            // deposit is legal absent the guard.
            (bool ok, bytes memory ret) = address(escrow).call(abi.encodeCall(Escrow.deposit, (1)));
            _record(ok, ret);
        }
    }

    function _record(bool ok, bytes memory ret) internal {
        if (ok) {
            reentrySucceeded = true;
        } else {
            lastRevertData = ret;
            // bytes4 cast is safe: length >= 4 was checked above
            // forge-lint: disable-next-line(unsafe-typecast)
            if (ret.length >= 4 && bytes4(ret) == REENTRANCY_GUARD_SELECTOR) {
                reentryBlocked = true;
            } else {
                revert UnexpectedRevertData(ret);
            }
        }
    }

    /// @dev Deposit 100e6, re-entering withdraw(1) during the token pull.
    function depositThenReenter() external {
        escrow.deposit(100e6);
    }

    /// @dev Withdraw 100e6, re-entering deposit(1) during the token push.
    function withdrawThenReenter() external {
        escrow.withdraw(100e6);
    }
}

/**
 * @title ReentrantSeller
 * @notice A contract designated as the lock's seller. Demonstrates that `settle`
 *         performs NO external calls (pure ledger update): there is no callback
 *         surface during settle, so the only "re-entry" possible is a call AFTER
 *         the first settle completes — which the state machine (Locked -> Settled)
 *         makes revert with NotLocked. If Escrow ever adds an external call to
 *         settle, this contract should be extended with a mid-flight hook.
 */
contract ReentrantSeller {
    Escrow public target;
    uint256 public settles;
    bool public doubleSettleBlocked;

    constructor(Escrow escrow_) {
        target = escrow_;
    }

    function attackSettle(uint256 paymentId, uint256 actual) external {
        target.settle(paymentId, actual);
        settles++;
        // Second settle attempt in the same tx must be rejected (payment now Settled).
        try target.settle(paymentId, actual) {
            settles++;
        } catch {
            doubleSettleBlocked = true;
        }
    }
}
