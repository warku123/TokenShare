// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

/**
 * @title Escrow
 * @notice Per-call payment escrow for the TokenShare API-quota rental market.
 *
 * Flow (BUILD_SPEC v1.1 §6.1):
 *   buyer --deposit--> credited balance
 *   buyer --lock(seller, maxAmount, ttl)--> payment becomes `Locked`
 *     (maxAmount is moved out of the buyer's withdrawable balance into the payment)
 *   seller --settlePartial(paymentId, amount)--> seller credited `amount` at once;
 *     `captured` accumulates (captured + amount <= maxAmount) while the payment
 *     STAYS `Locked` so further captures remain possible
 *   seller --settle(paymentId, actual)--> seller credited up to a cumulative
 *     `actual` (partial captures count toward it), buyer credited the difference
 *     (maxAmount - actual); payment becomes `Settled`
 *   anyone --refund(paymentId) after ttl elapsed--> buyer credited back
 *     the remaining maxAmount - captured; payment becomes `Refunded`
 *   user --withdraw(amount)--> USDC transferred out of the contract
 *
 * UNITS: all amounts are native USDC units, i.e. USDC's 6 decimal places.
 *         1 USDC = 1_000_000 (1e6). No 18-decimal conversion happens anywhere
 *         in this contract.
 *
 * CHAIN-AGNOSTIC (BUILD_SPEC §2.5): the USDC token address is injected via the
 * constructor — no USDC address, chainId or other chain-specific value is
 * hardcoded anywhere in this file. The same bytecode is deployed unchanged on
 * Base Sepolia and Monad testnet.
 *
 * NON-UPGRADEABLE: plain constructor deployment, no proxy, no owner/admin,
 * no privileged entry points. Every mutating function is permissionless except
 * `settle` and `settlePartial`, which are restricted to the seller designated
 * at lock time.
 */
contract Escrow is ReentrancyGuard {
    using SafeERC20 for IERC20;

    /*//////////////////////////////////////////////////////////////////////////
                                     TYPES
    //////////////////////////////////////////////////////////////////////////*/

    /// @notice Lifecycle state of a payment.
    ///         None = id never issued; Locked = escrowed and claimable by the
    ///         seller; Settled = seller paid; Refunded = rolled back to buyer.
    enum State {
        None,
        Locked,
        Settled,
        Refunded
    }

    /// @notice A per-call payment lock.
    /// @param buyer     Address that created the lock (pays the seller).
    /// @param seller    Address allowed to settle this payment (the relay operator).
    /// @param maxAmount Upper bound the seller may settle, in USDC native units (6 dp).
    /// @param expiresAt Timestamp (inclusive) until which the lock is valid;
    ///                  after this refund becomes available.
    /// @param state     Current lifecycle state.
    /// @param captured  Cumulative amount already credited to the seller via
    ///                  `settlePartial` (v2). Always <= maxAmount; deducted from
    ///                  the payout on `refund` and counted toward `settle`'s
    ///                  cumulative `actual`. Appended at the tail of the struct —
    ///                  fresh v2 deployments only, no storage migration needed.
    struct Payment {
        address buyer;
        address seller;
        uint256 maxAmount;
        uint64 expiresAt;
        State state;
        uint256 captured;
    }

    /*//////////////////////////////////////////////////////////////////////////
                                   CONSTANTS
    //////////////////////////////////////////////////////////////////////////*/

    /// @notice Default lock TTL in seconds, used when `lock` is called with ttl = 0.
    uint64 public constant DEFAULT_TTL = 600;

    /*//////////////////////////////////////////////////////////////////////////
                                   IMMUTABLES
    //////////////////////////////////////////////////////////////////////////*/

    /// @notice The settlement token (USDC, 6 decimals). Set once at deployment.
    IERC20 public immutable USDC;

    /*//////////////////////////////////////////////////////////////////////////
                                    STORAGE
    //////////////////////////////////////////////////////////////////////////*/

    /// @notice Withdrawable USDC balance per account, in USDC native units (6 dp).
    ///         Amounts locked into payments are NOT included here (they moved
    ///         into the `Payment` record), so `withdraw` can never drain escrowed
    ///         funds. Public mapping getter serves as the spec's `balances(addr)`.
    mapping(address => uint256) public balances;

    /// @notice paymentId => payment. paymentIds are unique, monotonically
    ///         increasing integers starting at 1 (id 0 means "no payment").
    mapping(uint256 => Payment) internal _payments;

    /// @notice Counter for paymentId generation; starts at 1 so that id 0 stays invalid.
    uint256 private _nextPaymentId = 1;

    /*//////////////////////////////////////////////////////////////////////////
                                     ERRORS
    //////////////////////////////////////////////////////////////////////////*/

    /// @notice Amount must be greater than zero.
    error ZeroAmount();

    /// @notice Seller address must not be zero.
    error InvalidSeller();

    /// @notice A buyer cannot lock funds to themselves.
    error SelfLock();

    /// @notice Caller tried to lock/settle/withdraw more than their available balance.
    /// @param requested The amount requested.
    /// @param available The caller's currently withdrawable balance.
    error InsufficientBalance(uint256 requested, uint256 available);

    /// @notice paymentId does not exist or is not in the `Locked` state.
    /// @param paymentId The payment id.
    /// @param state     The state actually found on-chain.
    error NotLocked(uint256 paymentId, State state);

    /// @notice Only the seller designated at lock time may settle.
    /// @param caller  Address that attempted the settle.
    /// @param seller  The designated seller.
    error NotSeller(address caller, address seller);

    /// @notice Settlement amount exceeds the locked maximum.
    /// @param actual Attempted settle amount.
    /// @param maxAmount The locked maximum.
    error ExceedsMaxAmount(uint256 actual, uint256 maxAmount);

    /// @notice Refund attempted before the TTL has fully elapsed.
    /// @param paymentId The payment id.
    /// @param expiresAt The timestamp at which refund becomes available.
    error TtlNotElapsed(uint256 paymentId, uint64 expiresAt);

    /// @notice Settle declared a cumulative `actual` lower than the amount
    ///         already captured via `settlePartial` (advances cannot be clawed back).
    /// @param paymentId The payment id.
    /// @param actual    The cumulative total declared by `settle`.
    /// @param captured  The amount already captured for this payment.
    error BelowCaptured(uint256 paymentId, uint256 actual, uint256 captured);

    /*//////////////////////////////////////////////////////////////////////////
                                     EVENTS
    //////////////////////////////////////////////////////////////////////////*/

    /// @notice Buyer deposited USDC into the escrow.
    event Deposited(address indexed account, uint256 amount);

    /// @notice Buyer locked `maxAmount` for a specific seller; funds are now escrowed.
    event Locked(
        uint256 indexed paymentId,
        address indexed buyer,
        address indexed seller,
        uint256 maxAmount,
        uint64 expiresAt
    );

    /// @notice Seller settled the payment: `actualAmount` credited to the seller,
    ///         the remainder `refundedAmount` returned to the buyer's balance.
    event Settled(
        uint256 indexed paymentId,
        address indexed buyer,
        address indexed seller,
        uint256 actualAmount,
        uint256 refundedAmount
    );

    /// @notice Seller captured part of a locked payment (Escrow v2 metering).
    ///         The payment stays `Locked`; `captured` is the new cumulative total.
    event SettlePartial(uint256 indexed paymentId, uint256 amount, uint256 captured);

    /// @notice An expired lock was refunded to the buyer's balance.
    event Refunded(uint256 indexed paymentId, address indexed buyer, uint256 amount, address indexed caller);

    /// @notice A user withdrew USDC out of the escrow.
    event Withdrawn(address indexed account, uint256 amount);

    /*//////////////////////////////////////////////////////////////////////////
                                   MODIFIERS
    //////////////////////////////////////////////////////////////////////////*/

    /// @dev Reverts unless `paymentId` is currently `Locked`.
    modifier onlyLocked(uint256 paymentId) {
        _onlyLocked(paymentId);
        _;
    }

    function _onlyLocked(uint256 paymentId) internal view {
        State state = _payments[paymentId].state;
        if (state != State.Locked) revert NotLocked(paymentId, state);
    }

    /*//////////////////////////////////////////////////////////////////////////
                                  CONSTRUCTOR
    //////////////////////////////////////////////////////////////////////////*/

    /// @param usdc_ ERC-20 settlement token (USDC, 6 decimals). Chain-specific
    ///              address is supplied by the deployer configuration, never hardcoded.
    constructor(IERC20 usdc_) {
        USDC = usdc_;
    }

    /*//////////////////////////////////////////////////////////////////////////
                              STATE-MACHINE ACTIONS
    //////////////////////////////////////////////////////////////////////////*/

    /**
     * @notice Buyer deposits USDC into the escrow, crediting `balances[msg.sender]`.
     * @dev Caller must have approved this contract to spend `amount` USDC.
     * @param amount USDC to deposit, in USDC native units (6 dp; 1 USDC = 1e6).
     */
    function deposit(uint256 amount) external nonReentrant {
        _requireNonZero(amount);

        // Effects first: credit before moving tokens (checks-effects-interactions).
        balances[msg.sender] += amount;

        // Interaction: pull USDC in.
        USDC.safeTransferFrom(msg.sender, address(this), amount);

        emit Deposited(msg.sender, amount);
    }

    /**
     * @notice Buyer locks up to `maxAmount` (taken from their own balance) for a
     *         designated seller and receives a unique paymentId.
     * @param seller    Address that will be allowed to settle this payment.
     * @param maxAmount Upper bound for settlement, in USDC native units (6 dp).
     *                  Taken out of the buyer's withdrawable balance immediately.
     * @param ttl       Lock validity in seconds. Pass 0 to use DEFAULT_TTL (600s).
     * @return paymentId Unique id of the newly locked payment (starts at 1).
     */
    function lock(address seller, uint256 maxAmount, uint64 ttl) external nonReentrant returns (uint256 paymentId) {
        _requireNonZero(maxAmount);
        if (seller == address(0)) revert InvalidSeller();
        if (seller == msg.sender) revert SelfLock();

        uint256 available = balances[msg.sender];
        if (maxAmount > available) revert InsufficientBalance(maxAmount, available);

        uint64 expiry = ttl == 0 ? DEFAULT_TTL : ttl;
        uint64 expiresAt = uint64(block.timestamp) + expiry; // overflow-safe for uint64*2

        // Effects: move maxAmount out of the buyer's withdrawable balance into
        // the escrowed payment record. paymentId is unique (counter) and event-traceable.
        paymentId = _nextPaymentId++;
        balances[msg.sender] = available - maxAmount;
        _payments[paymentId] = Payment({
            buyer: msg.sender,
            seller: seller,
            maxAmount: maxAmount,
            expiresAt: expiresAt,
            state: State.Locked,
            captured: 0
        });

        emit Locked(paymentId, msg.sender, seller, maxAmount, expiresAt);
    }

    /**
     * @notice Settle a locked payment. Only the seller designated at lock time
     *         may call. `actual` is the CUMULATIVE total owed to the seller for
     *         this payment: any amount already paid via `settlePartial` counts
     *         toward it, so only the un-advanced remainder (`actual - captured`)
     *         is credited here, and the buyer receives `maxAmount - actual`.
     *         With `captured == 0` this is exactly the original one-shot behavior.
     * @dev A settle is allowed as long as the payment is still `Locked` — even
     *      after `expiresAt` — since the seller only calls this after having
     *      served the request. It races against (and is atomic with) `refund`.
     *      Reverts with `BelowCaptured` if `actual < captured` (advances cannot
     *      be clawed back). On success the payment reaches the terminal `Settled`
     *      state and `captured` is bumped to `maxAmount` (fully consumed).
     * @param paymentId Id returned by `lock`.
     * @param actual    Cumulative amount owed to the seller, in USDC native
     *                  units (6 dp); must satisfy captured <= actual <= maxAmount.
     *                  actual == 0 (and captured == 0) pays the seller nothing
     *                  and returns the full maxAmount to the buyer.
     */
    function settle(uint256 paymentId, uint256 actual)
        external
        nonReentrant
        onlyLocked(paymentId)
    {
        Payment storage payment = _payments[paymentId];
        if (msg.sender != payment.seller) revert NotSeller(msg.sender, payment.seller);
        if (actual > payment.maxAmount) revert ExceedsMaxAmount(actual, payment.maxAmount);
        if (actual < payment.captured) revert BelowCaptured(paymentId, actual, payment.captured);

        // Effects only: the tokens already sit in this contract since `lock`
        // moved the buyer's balance into the payment. No token transfer here.
        // Partial captures were already credited; only the top-up is paid now.
        uint256 sellerTopUp = actual - payment.captured;
        uint256 refunded = payment.maxAmount - actual;

        payment.state = State.Settled;
        payment.captured = payment.maxAmount; // fully consumed (terminal consistency)
        if (sellerTopUp > 0) balances[payment.seller] += sellerTopUp;
        if (refunded > 0) balances[payment.buyer] += refunded;

        emit Settled(paymentId, payment.buyer, payment.seller, actual, refunded);
    }

    /**
     * @notice Capture part of a locked payment (Escrow v2 per-key metering).
     *         Only the seller designated at lock time may call. `amount` is
     *         credited to the seller's withdrawable balance immediately and
     *         accumulated in `captured`; the payment STAYS `Locked` so further
     *         captures — as well as the one-shot `settle` and the `refund` —
     *         remain available.
     * @dev TTL policy matches `settle`: allowed while `Locked`, even after
     *      `expiresAt` (the seller may flush usage after serving a request; it
     *      races `refund` atomically). Ledger-only like `settle`: no external
     *      token call, the seller pulls via `withdraw`. Requires
     *      `captured + amount <= maxAmount` (reverts `ExceedsMaxAmount` with the
     *      attempted total) and `amount > 0` (reverts `ZeroAmount` — a zero
     *      capture is a meaningless no-op here, unlike `settle(actual=0)`).
     * @param paymentId Id returned by `lock`.
     * @param amount    Amount to capture now, in USDC native units (6 dp).
     */
    function settlePartial(uint256 paymentId, uint256 amount)
        external
        nonReentrant
        onlyLocked(paymentId)
    {
        Payment storage payment = _payments[paymentId];
        if (msg.sender != payment.seller) revert NotSeller(msg.sender, payment.seller);
        _requireNonZero(amount);
        if (payment.captured + amount > payment.maxAmount) {
            revert ExceedsMaxAmount(payment.captured + amount, payment.maxAmount);
        }

        // Effects only: state stays Locked; the credit is ledger-only.
        payment.captured += amount;
        balances[payment.seller] += amount;

        emit SettlePartial(paymentId, amount, payment.captured);
    }

    /**
     * @notice Refund an expired lock back to the buyer's withdrawable balance.
     *         The buyer may call it once the TTL has elapsed; past that point
     *         ANYONE may call it (permissionless cleanup of expired locks —
     *         gas is paid by the caller, funds always go to the buyer).
     * @dev Reverts while `block.timestamp < expiresAt` (TTL not yet elapsed).
     *      Pays out the REMAINING escrow `maxAmount - captured`: anything the
     *      seller already captured via `settlePartial` stays with the seller.
     * @param paymentId Id returned by `lock`.
     */
    function refund(uint256 paymentId) external nonReentrant onlyLocked(paymentId) {
        Payment storage payment = _payments[paymentId];
        // TTL boundary is inclusive: refund is allowed once the full ttl has
        // elapsed, i.e. block.timestamp >= expiresAt. Strictly before that it reverts.
        if (block.timestamp < payment.expiresAt) revert TtlNotElapsed(paymentId, payment.expiresAt);

        address buyer = payment.buyer;
        uint256 amount = payment.maxAmount - payment.captured;

        // Effect: transition before any accounting (terminal state, no interaction).
        payment.state = State.Refunded;
        balances[buyer] += amount;

        emit Refunded(paymentId, buyer, amount, msg.sender);
    }

    /**
     * @notice Withdraw `amount` of the caller's withdrawable (unlocked) USDC.
     * @param amount USDC to withdraw, in USDC native units (6 dp).
     */
    function withdraw(uint256 amount) external nonReentrant {
        _requireNonZero(amount);

        uint256 available = balances[msg.sender];
        if (amount > available) revert InsufficientBalance(amount, available);

        // Effects first: debit before the token transfer
        // (checks-effects-interactions; reentrancy additionally blocked by guard).
        balances[msg.sender] = available - amount;

        // Interaction: push USDC out.
        USDC.safeTransfer(msg.sender, amount);

        emit Withdrawn(msg.sender, amount);
    }

    /*//////////////////////////////////////////////////////////////////////////
                                     VIEWS
    //////////////////////////////////////////////////////////////////////////*/

    /**
     * @notice Payment-validity check used by the relay before forwarding a request
     *         (BUILD_SPEC §6.2): true only if the payment is `Locked`, has not
     *         expired, designates `seller`, and its `maxAmount` can cover
     *         `minAmount` (the per-request cost upper bound).
     * @param paymentId Id returned by `lock`.
     * @param seller    Caller's expected designated seller address.
     * @param minAmount Minimum required locked amount, in USDC native units (6 dp).
     */
    function isValid(uint256 paymentId, address seller, uint256 minAmount) external view returns (bool) {
        Payment storage payment = _payments[paymentId];
        return payment.state == State.Locked && block.timestamp < payment.expiresAt
            && payment.seller == seller && payment.maxAmount >= minAmount;
    }

    /**
     * @notice Full record of a payment, for relays/CLIs (e.g. to compute when
     *         `refund` becomes available).
     */
    function getPayment(uint256 paymentId)
        external
        view
        returns (address buyer, address seller, uint256 maxAmount, uint64 expiresAt, State state)
    {
        Payment storage payment = _payments[paymentId];
        return (payment.buyer, payment.seller, payment.maxAmount, payment.expiresAt, payment.state);
    }

    /**
     * @notice Cumulative amount already captured for a payment via `settlePartial`
     *         (Escrow v2). The remaining escrow — refundable after TTL, or
     *         payable on a cumulative `settle` — is `maxAmount - capturedOf(id)`.
     *         Additive view: `getPayment` keeps its original 5-value ABI shape
     *         so existing relay/CLI/web consumers stay source-compatible.
     */
    function capturedOf(uint256 paymentId) external view returns (uint256) {
        return _payments[paymentId].captured;
    }

    /**
     * @notice Id that the next `lock` call will return.
     */
    function nextPaymentId() external view returns (uint256) {
        return _nextPaymentId;
    }

    /*//////////////////////////////////////////////////////////////////////////
                                   INTERNAL
    //////////////////////////////////////////////////////////////////////////*/

    function _requireNonZero(uint256 amount) private pure {
        if (amount == 0) revert ZeroAmount();
    }
}
