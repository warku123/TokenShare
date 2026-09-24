// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/**
 * @title Registry
 * @notice On-chain directory of seller relay listings for the TokenShare
 *         API-quota rental market (BUILD_SPEC v1.1 §3 M2; v2 = M9 per-model
 *         pricing — prices move from one triple per seller to one triple per
 *         model, keyed by the listing's `models` array).
 *
 * Each seller (relay operator) maintains exactly one listing:
 *   operator --register(endpoint, models, prices)--> listing active
 *   operator --updateModelPrice(model, price)--> only while active, per model
 *   operator --deactivate()--> listing kept, inactive; re-register replaces it
 *
 * `prices` is a PARALLEL array to `models`: prices[i] applies to models[i].
 * Changing the model set = deactivate + re-register.
 *
 * The buyer CLI / relay reads `getListing(operator)` and, per request model,
 * `getPrice(operator, model)` for tiered per-token pricing; callers must check
 * the `active` flag themselves (an inactive listing is kept on-chain so
 * receipts of past requests remain auditable).
 *
 * UNITS: all price fields are USDC native units per 1M tokens, i.e. USDC's 6
 *         decimal places (1 USDC = 1e6) divided into 1,000,000 tokens. A zero
 *         price is legal (free tier / free model). No 18-decimal conversion
 *         happens anywhere in this contract.
 *
 * CHAIN-AGNOSTIC (BUILD_SPEC §2.5): pure metadata contract — no token address,
 * no chainId, no chain-specific value hardcoded anywhere. The same bytecode is
 * deployed unchanged on Base Sepolia and Monad testnet.
 *
 * NON-UPGRADEABLE: plain constructor deployment, no proxy, no owner/admin, no
 * privileged entry points. Every function is permissionless: an operator is
 * simply the caller of `register` (operator == msg.sender, one listing each).
 *
 * SAFETY: no tokens, no external calls, no reentrancy surface, no constructor
 * parameters.
 */
contract Registry {
    /*//////////////////////////////////////////////////////////////////////////
                                      TYPES
    //////////////////////////////////////////////////////////////////////////*/

    /**
     * @notice Tiered price for ONE model.
     * @param cachedIn USDC native units (6 dp; 1 USDC = 1e6) per 1M cached
     *                 prompt tokens (prompt_tokens_details.cached_tokens).
     * @param input    USDC native units (6 dp) per 1M non-cached prompt tokens.
     * @param output   USDC native units (6 dp) per 1M completion tokens.
     */
    struct Price {
        uint256 cachedIn;
        uint256 input;
        uint256 output;
    }

    /**
     * @notice A seller relay listing.
     * @param operator Relay operator address (the EOA that registered).
     * @param endpoint Seller relay URL the buyer CLI sends requests to.
     * @param models   OpenAI model names served by this relay.
     * @param prices   Parallel array to `models` (same length): prices[i] is
     *                 the Price for models[i].
     * @param active   False once the operator calls `deactivate`; callers must
     *                 check this before trusting the listing.
     */
    struct Listing {
        address operator;
        string endpoint;
        string[] models;
        Price[] prices;
        bool active;
    }

    /*//////////////////////////////////////////////////////////////////////////
                                      ERRORS
    //////////////////////////////////////////////////////////////////////////*/

    /// @notice An active listing already exists for this operator. Deactivate
    ///         first, then re-register.
    error AlreadyRegistered();

    /// @notice The caller's listing is not active (never registered, or
    ///         deactivated).
    error NotActive();

    /// @notice The requested model is not in the operator's `models` list.
    error ModelNotFound();

    /// @notice The models list must contain at least one model name.
    error EmptyModels();

    /// @notice `models` and `prices` must have the same (non-zero) length.
    error LengthMismatch();

    /*//////////////////////////////////////////////////////////////////////////
                                      EVENTS
    //////////////////////////////////////////////////////////////////////////*/

    /// @notice A listing was created or re-created after deactivation. The
    ///         per-model prices are readable via `getPrice`/`getListing`.
    event Registered(address indexed operator, string endpoint, string[] models);

    /// @notice The operator changed the tiered price of one existing model
    ///         (endpoint and the model set are unchanged).
    event PriceUpdated(address indexed operator, string model, uint256 cachedIn, uint256 input, uint256 output);

    /// @notice The operator deactivated its listing; data is retained on-chain.
    event Deactivated(address indexed operator);

    /*//////////////////////////////////////////////////////////////////////////
                                      STORAGE
    //////////////////////////////////////////////////////////////////////////*/

    /// @notice operator => its single listing. `active` distinguishes live
    ///         listings from deactivated ones; absent operator => empty Listing
    ///         (zero address, active == false).
    mapping(address => Listing) internal _listings;

    /// @dev Sentinel returned by {_modelIndex} when the model is absent.
    uint256 private constant NOT_FOUND = type(uint256).max;

    /*//////////////////////////////////////////////////////////////////////////
                                  REGISTRATION ACTIONS
    //////////////////////////////////////////////////////////////////////////*/

    /**
     * @notice Register a relay listing for `msg.sender` (the operator).
     *         Reverts if an active listing already exists; after `deactivate`
     *         this may be called again and fully replaces every field.
     * @param endpoint Relay URL (kept verbatim; may be empty).
     * @param models   Served OpenAI model names, at least one.
     * @param prices   Per-model tiered prices; MUST have the same length as
     *                 `models` (parallel arrays). Entries may be zero (free
     *                 tier is legal).
     */
    function register(string calldata endpoint, string[] calldata models, Price[] calldata prices) external {
        if (_listings[msg.sender].active) revert AlreadyRegistered();
        if (models.length == 0) revert EmptyModels();
        if (models.length != prices.length) revert LengthMismatch();

        Listing storage listing = _listings[msg.sender];
        listing.operator = msg.sender;
        listing.endpoint = endpoint;

        // Element-wise copy: solc 0.8.24 legacy codegen cannot copy a calldata
        // struct array (Price[]) to storage wholesale. delete-first keeps
        // re-register a FULL replacement (no stale models/prices).
        delete listing.models;
        delete listing.prices;
        uint256 len = models.length;
        for (uint256 i = 0; i < len; i++) {
            listing.models.push(models[i]);
            listing.prices.push(Price({cachedIn: prices[i].cachedIn, input: prices[i].input, output: prices[i].output}));
        }
        listing.active = true;

        emit Registered(msg.sender, endpoint, models);
    }

    /**
     * @notice Update the tiered price of ONE existing model. The endpoint and
     *         the model set are unchanged (adding/removing models = deactivate
     *         + re-register). Requires an active listing.
     * @param model Model name already present in the caller's listing.
     * @param price New tiered price for `model` (zero entries are legal).
     */
    function updateModelPrice(string calldata model, Price calldata price) external {
        Listing storage listing = _listings[msg.sender];
        if (!listing.active) revert NotActive();

        uint256 idx = _modelIndex(listing, model);
        if (idx == NOT_FOUND) revert ModelNotFound();

        listing.prices[idx] = price;

        emit PriceUpdated(msg.sender, model, price.cachedIn, price.input, price.output);
    }

    /**
     * @notice Deactivate the operator's listing. The stored data is retained
     *         (and still returned by `getListing`), but callers must treat it
     *         as inactive; `getPrice` reverts while inactive. Re-registration
     *         via `register` is allowed afterwards.
     */
    function deactivate() external {
        Listing storage listing = _listings[msg.sender];
        if (!listing.active) revert NotActive();

        listing.active = false;

        emit Deactivated(msg.sender);
    }

    /*//////////////////////////////////////////////////////////////////////////
                                      VIEWS
    //////////////////////////////////////////////////////////////////////////*/

    /**
     * @notice Full listing of an operator. Callers must check the `active` flag
     *         (an inactive/deactivated listing is still returned for audit) and
     *         `operator != address(0)` for "never registered".
     */
    function getListing(address operator) external view returns (Listing memory) {
        return _listings[operator];
    }

    /**
     * @notice Tiered price for `model` on `operator`'s ACTIVE listing. Reverts
     *         if the listing is inactive or the model is not listed.
     */
    function getPrice(address operator, string calldata model) external view returns (Price memory) {
        Listing storage listing = _listings[operator];
        if (!listing.active) revert NotActive();

        uint256 idx = _modelIndex(listing, model);
        if (idx == NOT_FOUND) revert ModelNotFound();

        return listing.prices[idx];
    }

    /*//////////////////////////////////////////////////////////////////////////
                                     INTERNALS
    //////////////////////////////////////////////////////////////////////////*/

    /**
     * @dev Index of `model` in `listing.models`, or NOT_FOUND. Linear scan:
     *      listings hold a handful of models, and a keccak mapping keyed by
     *      string is not expressible; correctness > gas here.
     */
    function _modelIndex(Listing storage listing, string calldata model) private view returns (uint256) {
        bytes32 needle = keccak256(bytes(model));
        string[] storage models = listing.models;
        uint256 len = models.length;
        for (uint256 i = 0; i < len; i++) {
            if (keccak256(bytes(models[i])) == needle) return i;
        }
        return NOT_FOUND;
    }
}
