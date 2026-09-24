// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/**
 * @title Registry
 * @notice On-chain directory of seller relay listings for the TokenShare
 *         API-quota rental market (BUILD_SPEC v1.1 §3 M2; v2 = M9 per-model
 *         pricing — prices move from one triple per seller to one triple per
 *         model, keyed by the listing's `models` array; v3 = M10 on-chain
 *         seller enumeration — `sellerCount()`/`getSellers()` replace
 *         consumer-side `Registered` log scanning; v4 = M12 model-level
 *         delisting — `removeModel` retires ONE model without touching the
 *         rest of the listing).
 *
 * Each seller (relay operator) maintains exactly one listing:
 *   operator --register(endpoint, models, prices)--> listing active
 *   operator --updateModelPrice(model, price)--> only while active, per model
 *   operator --deactivate()--> listing kept, inactive; re-register replaces it
 *
 * `prices` is a PARALLEL array to `models`: prices[i] applies to models[i].
 * Adding models = deactivate + re-register; removing one model = `removeModel`
 * (v4).
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

    /// @notice The caller's listing has only one model left, so it cannot be
    ///         removed — deactivate the whole listing instead ({deactivate}).
    error RemoveLastModel();

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

    /// @notice The operator removed ONE model from its listing (stopped
    ///         serving it). The `models`/`prices` parallel arrays were both
    ///         shortened by one entry; survivors keep their own prices.
    event ModelRemoved(address indexed operator, string model);

    /*//////////////////////////////////////////////////////////////////////////
                                      STORAGE
    //////////////////////////////////////////////////////////////////////////*/

    /// @notice operator => its single listing. `active` distinguishes live
    ///         listings from deactivated ones; absent operator => empty Listing
    ///         (zero address, active == false).
    mapping(address => Listing) internal _listings;

    /// @dev Sentinel returned by {_modelIndex} when the model is absent.
    uint256 private constant NOT_FOUND = type(uint256).max;

    /// @notice Maximum number of sellers returned by a single {getSellers}
    ///         call. Public so clients (and tests) can read the real page
    ///         limit instead of hardcoding it. Requests larger than this are
    ///         clamped down to it (see {getSellers}).
    uint256 public constant SELLER_PAGE_LIMIT = 500;

    /// @notice Every operator that has EVER registered, in first-registration
    ///         order. Append-only: entries are never removed — deactivation
    ///         keeps the seller in the directory (callers filter via
    ///         `getListing(seller).active` themselves). Enables O(1) on-chain
    ///         discovery instead of event scanning.
    address[] private _sellers;

    /// @notice operator => 1-based position in {_sellers}; 0 = never
    ///         registered. Makes re-registration after deactivation a no-op
    ///         on the array (no duplicates, no index churn).
    mapping(address => uint256) private _sellerIndex;

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

        // v3 enumeration: first-ever registration appends the operator to the
        // seller directory (one-time ~23k gas SSTORE). Deactivate ->
        // re-register hits the `_sellerIndex` guard and is NOT appended again:
        // the directory is append-only and duplicate-free.
        if (_sellerIndex[msg.sender] == 0) {
            _sellerIndex[msg.sender] = _sellers.length + 1;
            _sellers.push(msg.sender);
        }

        emit Registered(msg.sender, endpoint, models);
    }

    /**
     * @notice Update the tiered price of ONE existing model. The endpoint and
     *         the model set are unchanged (adding a model = deactivate +
     *         re-register; removing ONE model = {removeModel}). Requires an
     *         active listing.
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
     * @notice Remove ONE model from the caller's listing — from now on the
     *         operator does NOT serve that model. The `models` and `prices`
     *         parallel arrays are shortened by one entry at the SAME index
     *         (the price travels with its model): the removed index is filled
     *         with the former last element (swap-and-pop) and both arrays are
     *         popped, keeping the arrays dense and parallel. The relative
     *         order of the surviving models is therefore unspecified —
     *         consumers must read `getListing` instead of caching indices.
     *
     * No `active` requirement: an inactive listing may also be cleaned up
     * model by model.
     *
     * SETTLEMENT SEMANTICS: removal only stops FUTURE service. A payment
     * already locked against this model is settled by the relay via
     * `getPrice`, which now reverts with `ModelNotFound` — the settle tx
     * fails (`settle-failed`, no receipt) and the buyer takes the normal
     * refund path after TTL. No escrow state is touched here (Escrow is a
     * separate contract).
     *
     * @param model Model name currently present in the caller's listing.
     */
    function removeModel(string calldata model) external {
        Listing storage listing = _listings[msg.sender];

        uint256 idx = _modelIndex(listing, model);
        if (idx == NOT_FOUND) revert ModelNotFound();

        // idx is a hit => length >= 1; last == 0 means it is the ONLY model.
        uint256 last = listing.models.length - 1;
        if (last == 0) revert RemoveLastModel();

        // Swap-and-pop, BOTH arrays at the same index. When idx == last the
        // assignments are self-assign no-ops and this is a plain pop.
        listing.models[idx] = listing.models[last];
        listing.prices[idx] = listing.prices[last];
        listing.models.pop();
        listing.prices.pop();

        emit ModelRemoved(msg.sender, model);
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

    /**
     * @notice Number of sellers in the on-chain directory — every operator
     *         that has EVER called {register}, in first-registration order.
     * @dev Append-only and never pruned: deactivated sellers stay counted.
     *      Consumers filter for activeness via `getListing(seller).active`.
     */
    function sellerCount() external view returns (uint256) {
        return _sellers.length;
    }

    /**
     * @notice Slice of the seller directory: `{getSellers(start, count)}`
     *         returns `_sellers[start .. start+count)` with clamping, never
     *         reverting:
     *         - `start >= sellerCount()`  -> empty array
     *         - `count > {SELLER_PAGE_LIMIT}` -> clamped to the limit (500)
     *         - `start + count > sellerCount()` -> truncated to the end
     *
     * WHY ON-CHAIN ENUMERATION (v3 economics): storing each seller costs one
     * ~23k-gas SSTORE at first registration — a one-time, seller-paid fee.
     * The alternative is every consumer (web market page, CLI `listings`)
     * reconstructing the directory by scanning `Registered` logs from block
     * zero: an O(chain-age) `eth_getLogs` walk that gets slower every day and
     * trips public-RPC range limits (Monad caps windows at 100-1000 blocks).
     * This is the standard Uniswap V2 Factory `allPairs`/`allPairsLength` and
     * Curve `pool_list`/`pool_count` pattern: a tiny constant write at
     * registration buys every reader an O(page) `eth_call` instead of an
     * unbounded log scan. gas-for-latency trade is strictly favourable at
     * market scale (thousands of reads per single 23k-gas write).
     *
     * @param start Directory index to begin at (0-based).
     * @param count Maximum entries to return (clamped as above).
     */
    function getSellers(uint256 start, uint256 count) external view returns (address[] memory) {
        uint256 len = _sellers.length;
        if (start >= len) return new address[](0);
        if (count > SELLER_PAGE_LIMIT) count = SELLER_PAGE_LIMIT;

        uint256 end = start + count; // no overflow: start < len (array len ≤ 2^64-1), count ≤ 500
        if (end > len) end = len;

        address[] memory page = new address[](end - start);
        for (uint256 i = start; i < end; i++) {
            page[i - start] = _sellers[i];
        }
        return page;
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
