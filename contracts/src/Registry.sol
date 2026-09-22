// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/**
 * @title Registry
 * @notice On-chain directory of seller relay listings for the TokenShare
 *         API-quota rental market (BUILD_SPEC v1.1 §3 M2).
 *
 * Each seller (relay operator) maintains exactly one listing:
 *   operator --register(endpoint, models, prices)--> listing active
 *   operator --updatePrice(prices)--> only while active
 *   operator --deactivate()--> listing kept, inactive; re-register replaces it
 *
 * The buyer CLI / relay reads `getListing(operator)` and its `priceCachedIn /
 * priceInput / priceOutput` fields for tiered per-token pricing; callers must
 * check the `active` flag themselves (an inactive listing is kept on-chain so
 * receipts of past requests remain auditable).
 *
 * UNITS: all prices are native USDC units, i.e. USDC's 6 decimal places.
 *         1 USDC = 1_000_000 (1e6). A zero price is legal (free tier / free
 *         model). No 18-decimal conversion happens anywhere in this contract.
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
     * @notice A seller relay listing.
     * @param operator      Relay operator address (the EOA that registered).
     * @param endpoint      Seller relay URL the buyer CLI sends requests to.
     * @param models        OpenAI model names served by this relay.
     * @param priceCachedIn Price per 1 unit of cached prompt tokens
     *                      (prompt_tokens_details.cached_tokens), in USDC
     *                      native units (6 dp; 1 USDC = 1e6).
     * @param priceInput    Price per 1 unit of (non-cached) prompt tokens, in
     *                      USDC native units (6 dp).
     * @param priceOutput   Price per 1 unit of completion tokens, in USDC
     *                      native units (6 dp).
     * @param active        False once the operator calls `deactivate`;
     *                      callers must check this before trusting the listing.
     */
    struct Listing {
        address operator;
        string endpoint;
        string[] models;
        uint256 priceCachedIn;
        uint256 priceInput;
        uint256 priceOutput;
        bool active;
    }

    /*//////////////////////////////////////////////////////////////////////////
                                     ERRORS
    //////////////////////////////////////////////////////////////////////////*/

    /// @notice An active listing already exists for this operator. Deactivate
    ///         first, then re-register.
    error AlreadyRegistered();

    /// @notice No listing has ever been registered for this operator.
    error NotRegistered();

    /// @notice The operator's listing exists but is inactive (deactivated).
    error ListingInactive();

    /// @notice The endpoint string must not be empty.
    error EmptyEndpoint();

    /// @notice The models list must contain at least one model name.
    error EmptyModels();

    /*//////////////////////////////////////////////////////////////////////////
                                     EVENTS
    //////////////////////////////////////////////////////////////////////////*/

    /// @notice A listing was created or re-created after deactivation.
    event Registered(
        address indexed operator,
        string endpoint,
        string[] models,
        uint256 priceCachedIn,
        uint256 priceInput,
        uint256 priceOutput
    );

    /// @notice The operator changed its tiered prices (endpoint/models unchanged).
    event PriceUpdated(address indexed operator, uint256 priceCachedIn, uint256 priceInput, uint256 priceOutput);

    /// @notice The operator deactivated its listing; data is retained on-chain.
    event Deactivated(address indexed operator);

    /*//////////////////////////////////////////////////////////////////////////
                                     STORAGE
    //////////////////////////////////////////////////////////////////////////*/

    /// @notice operator => its single listing. `active` distinguishes live
    ///         listings from deactivated ones; absent operator => empty Listing
    ///         (zero address, active == false).
    mapping(address => Listing) internal _listings;

    /*//////////////////////////////////////////////////////////////////////////
                                  REGISTRATION ACTIONS
    //////////////////////////////////////////////////////////////////////////*/

    /**
     * @notice Register a relay listing for `msg.sender` (the operator).
     *         Reverts if an active listing already exists; after `deactivate`
     *         this may be called again and fully replaces every field.
     * @param endpoint      Relay URL, must be non-empty.
     * @param models        Served OpenAI model names, must contain at least one.
     * @param priceCachedIn Price per cached prompt token, USDC native units (6 dp).
     * @param priceInput    Price per prompt token, USDC native units (6 dp).
     * @param priceOutput   Price per completion token, USDC native units (6 dp).
     *                      All three prices may be zero (free tier is legal).
     */
    function register(
        string calldata endpoint,
        string[] calldata models,
        uint256 priceCachedIn,
        uint256 priceInput,
        uint256 priceOutput
    ) external {
        if (bytes(endpoint).length == 0) revert EmptyEndpoint();
        if (models.length == 0) revert EmptyModels();

        if (_listings[msg.sender].active) revert AlreadyRegistered();

        _listings[msg.sender] = Listing({
            operator: msg.sender,
            endpoint: endpoint,
            models: models,
            priceCachedIn: priceCachedIn,
            priceInput: priceInput,
            priceOutput: priceOutput,
            active: true
        });

        emit Registered(msg.sender, endpoint, models, priceCachedIn, priceInput, priceOutput);
    }

    /**
     * @notice Update the operator's tiered prices. Endpoint and models are
     *         unchanged. Requires an existing AND active listing.
     * @param priceCachedIn New price per cached prompt token, USDC native units (6 dp).
     * @param priceInput    New price per prompt token, USDC native units (6 dp).
     * @param priceOutput   New price per completion token, USDC native units (6 dp).
     */
    function updatePrice(uint256 priceCachedIn, uint256 priceInput, uint256 priceOutput) external {
        Listing storage listing = _listings[msg.sender];

        // Dead-check removed (Gate A): the mapping key IS msg.sender, so
        // listing.operator == msg.sender by construction; a different caller
        // simply hits NotRegistered below via their own empty slot.
        if (listing.operator == address(0)) revert NotRegistered();
        if (!listing.active) revert ListingInactive();

        listing.priceCachedIn = priceCachedIn;
        listing.priceInput = priceInput;
        listing.priceOutput = priceOutput;

        emit PriceUpdated(msg.sender, priceCachedIn, priceInput, priceOutput);
    }

    /**
     * @notice Deactivate the operator's listing. The stored data is retained
     *         (and still returned by `getListing`), but callers must treat it
     *         as inactive. Re-registration via `register` is allowed afterwards.
     */
    function deactivate() external {
        Listing storage listing = _listings[msg.sender];

        // Dead-check removed (Gate A): see updatePrice.
        if (listing.operator == address(0)) revert NotRegistered();
        if (!listing.active) revert ListingInactive();

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
    function getListing(address operator)
        external
        view
        returns (
            address listingOperator,
            string memory endpoint,
            string[] memory models,
            uint256 priceCachedIn,
            uint256 priceInput,
            uint256 priceOutput,
            bool active
        )
    {
        Listing storage listing = _listings[operator];
        return (
            listing.operator,
            listing.endpoint,
            listing.models,
            listing.priceCachedIn,
            listing.priceInput,
            listing.priceOutput,
            listing.active
        );
    }
}
