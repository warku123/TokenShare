// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";

import {Registry} from "../src/Registry.sol";

/**
 * @title RegistryTest
 * @notice Behavioral suite for the Registry v2 listing contract (BUILD_SPEC
 *         v1.1 §3 M2 + M9 per-model pricing PIN). Plain anvil/local EVM, no
 *         fork. All prices are USDC 6-decimal native units per 1M tokens
 *         (1 USDC = 1e6).
 *
 * IMPLEMENTATION NOTE: unlike v1's 7-tuple (which needed raw returndata
 * decoding to dodge "Stack too deep"), v2 `getListing` returns a single
 * `Listing` struct — forge/solc decode that natively, so tests read fields
 * directly.
 */
contract RegistryTest is Test {
    // ------------------------------------------------------------------ data
    Registry internal registry;

    address internal operatorA = makeAddr("operatorA");
    address internal operatorB = makeAddr("operatorB");
    address internal stranger = makeAddr("stranger");

    string internal constant ENDPOINT = "https://relay.example.com:8787";

    // ------------------------------------------------------------------ setup
    function setUp() public {
        registry = new Registry();
    }

    // ------------------------------------------------------------------ helpers
    /// @dev Register a listing as `who` with the given parallel arrays.
    function _register(address who, string memory endpoint, string[] memory models, Registry.Price[] memory prices)
        internal
    {
        vm.startPrank(who);
        registry.register(endpoint, models, prices);
        vm.stopPrank();
    }

    /// @dev Build the suite's default two-model price list: m0 = (1,2,3)e6,
    ///      m1 = (4,5,6)e6 — distinct per model on purpose.
    function _defaultPrices() internal pure returns (Registry.Price[] memory prices) {
        prices = new Registry.Price[](2);
        prices[0] = Registry.Price({cachedIn: 1e6, input: 2e6, output: 3e6});
        prices[1] = Registry.Price({cachedIn: 4e6, input: 5e6, output: 6e6});
    }

    /// @dev Register `who` with the suite's default listing
    ///      (["gpt-4o", "gpt-4o-mini"] + _defaultPrices()).
    function _registerDefault(address who) internal {
        string[] memory models = new string[](2);
        models[0] = "gpt-4o";
        models[1] = "gpt-4o-mini";
        _register(who, ENDPOINT, models, _defaultPrices());
    }

    /// @dev Single-model helpers for revert tests.
    function _oneModel(string memory name) internal pure returns (string[] memory) {
        string[] memory models = new string[](1);
        models[0] = name;
        return models;
    }

    function _onePrice(uint256 cachedIn, uint256 input, uint256 output)
        internal
        pure
        returns (Registry.Price[] memory)
    {
        Registry.Price[] memory prices = new Registry.Price[](1);
        prices[0] = Registry.Price({cachedIn: cachedIn, input: input, output: output});
        return prices;
    }

    // =====================================================================
    // register
    // =====================================================================

    function test_Register_Happy() public {
        string[] memory models = new string[](2);
        models[0] = "gpt-4o";
        models[1] = "gpt-4o-mini";
        Registry.Price[] memory prices = _defaultPrices();

        // Registered event: topic1 = indexed operator; data = endpoint, models
        vm.expectEmit(true, false, false, true, address(registry));
        emit Registry.Registered(operatorA, ENDPOINT, models);

        _register(operatorA, ENDPOINT, models, prices);

        Registry.Listing memory listing = registry.getListing(operatorA);
        assertEq(listing.operator, operatorA, "operator");
        assertEq(listing.endpoint, ENDPOINT, "endpoint");
        assertEq(listing.models.length, 2, "models length");
        assertEq(listing.models[0], "gpt-4o", "models[0]");
        assertEq(listing.models[1], "gpt-4o-mini", "models[1]");
        // prices fall to storage as a parallel array, asserted per field
        assertEq(listing.prices.length, 2, "prices length");
        assertEq(listing.prices[0].cachedIn, 1e6, "prices[0].cachedIn");
        assertEq(listing.prices[0].input, 2e6, "prices[0].input");
        assertEq(listing.prices[0].output, 3e6, "prices[0].output");
        assertEq(listing.prices[1].cachedIn, 4e6, "prices[1].cachedIn");
        assertEq(listing.prices[1].input, 5e6, "prices[1].input");
        assertEq(listing.prices[1].output, 6e6, "prices[1].output");
        assertTrue(listing.active, "active flag");

        // getPrice agrees with the stored parallel array
        Registry.Price memory p0 = registry.getPrice(operatorA, "gpt-4o");
        assertEq(p0.cachedIn, 1e6, "getPrice m0 cachedIn");
        Registry.Price memory p1 = registry.getPrice(operatorA, "gpt-4o-mini");
        assertEq(p1.output, 6e6, "getPrice m1 output");
    }

    function test_RevertRegister_AlreadyRegistered() public {
        _registerDefault(operatorA);

        // still-active duplicate registration reverts
        vm.prank(operatorA);
        vm.expectRevert(Registry.AlreadyRegistered.selector);
        registry.register("https://other.example.com", _oneModel("o1-preview"), _onePrice(0, 0, 0));
    }

    function test_RevertRegister_EmptyModels() public {
        string[] memory models = new string[](0);
        Registry.Price[] memory prices = new Registry.Price[](0);

        vm.prank(operatorA);
        vm.expectRevert(Registry.EmptyModels.selector);
        registry.register(ENDPOINT, models, prices);
    }

    /// @dev Both mismatch directions revert: models longer or prices longer.
    function test_RevertRegister_LengthMismatch() public {
        // 2 models, 1 price
        string[] memory models = new string[](2);
        models[0] = "gpt-4o";
        models[1] = "gpt-4o-mini";

        vm.prank(operatorA);
        vm.expectRevert(Registry.LengthMismatch.selector);
        registry.register(ENDPOINT, models, _onePrice(1e6, 2e6, 3e6));

        // 1 model, 2 prices
        Registry.Price[] memory prices = new Registry.Price[](2);
        prices[0] = Registry.Price({cachedIn: 1e6, input: 2e6, output: 3e6});
        prices[1] = Registry.Price({cachedIn: 4e6, input: 5e6, output: 6e6});

        vm.prank(operatorA);
        vm.expectRevert(Registry.LengthMismatch.selector);
        registry.register(ENDPOINT, _oneModel("gpt-4o"), prices);
    }

    /// @dev After deactivate, re-register succeeds and FULLY replaces every
    ///      field (endpoint, a fresh models list, a fresh prices array — no
    ///      stale elements) and re-activates, emitting Registered again.
    function test_ReRegister_AfterDeactivate() public {
        _registerDefault(operatorA);
        vm.prank(operatorA);
        registry.deactivate();

        string[] memory newModels = _oneModel("o1-preview");

        vm.expectEmit(true, false, false, true, address(registry));
        emit Registry.Registered(operatorA, "https://relay2.example.com", newModels);

        _register(operatorA, "https://relay2.example.com", newModels, _onePrice(0, 1, 2));

        Registry.Listing memory listing = registry.getListing(operatorA);
        assertTrue(listing.active, "re-activated");
        assertEq(listing.endpoint, "https://relay2.example.com", "endpoint replaced");
        assertEq(listing.models.length, 1, "models replaced");
        assertEq(listing.models[0], "o1-preview", "new model present");
        assertEq(listing.prices.length, 1, "prices replaced");
        assertEq(listing.prices[0].cachedIn, 0, "price cachedIn replaced");
        assertEq(listing.prices[0].input, 1, "price input replaced");
        assertEq(listing.prices[0].output, 2, "price output replaced");
    }

    // =====================================================================
    // updateModelPrice
    // =====================================================================

    function test_UpdateModelPrice_Happy() public {
        _registerDefault(operatorA);

        vm.expectEmit(true, false, false, true, address(registry));
        emit Registry.PriceUpdated(operatorA, "gpt-4o", 10e6, 11e6, 12e6);

        vm.startPrank(operatorA);
        registry.updateModelPrice("gpt-4o", Registry.Price({cachedIn: 10e6, input: 11e6, output: 12e6}));
        vm.stopPrank();

        Registry.Listing memory listing = registry.getListing(operatorA);
        // target model updated
        assertEq(listing.prices[0].cachedIn, 10e6, "m0 cachedIn updated");
        assertEq(listing.prices[0].input, 11e6, "m0 input updated");
        assertEq(listing.prices[0].output, 12e6, "m0 output updated");
        // other model untouched
        assertEq(listing.prices[1].cachedIn, 4e6, "m1 cachedIn unchanged");
        assertEq(listing.prices[1].input, 5e6, "m1 input unchanged");
        assertEq(listing.prices[1].output, 6e6, "m1 output unchanged");
        // no model added or removed
        assertEq(listing.models.length, 2, "models count unchanged");
        assertEq(listing.models[0], "gpt-4o", "models[0] unchanged");
        assertEq(listing.models[1], "gpt-4o-mini", "models[1] unchanged");
        assertEq(listing.endpoint, ENDPOINT, "endpoint unchanged");
        assertTrue(listing.active, "still active");
    }

    function test_RevertUpdateModelPrice_ModelNotFound() public {
        _registerDefault(operatorA);

        vm.prank(operatorA);
        vm.expectRevert(Registry.ModelNotFound.selector);
        registry.updateModelPrice("unknown-model", Registry.Price({cachedIn: 1e6, input: 2e6, output: 3e6}));
    }

    function test_RevertUpdateModelPrice_NotActive() public {
        _registerDefault(operatorA);
        vm.startPrank(operatorA);
        registry.deactivate();

        vm.expectRevert(Registry.NotActive.selector);
        registry.updateModelPrice("gpt-4o", Registry.Price({cachedIn: 10e6, input: 11e6, output: 12e6}));
        vm.stopPrank();
    }

    // =====================================================================
    // getPrice
    // =====================================================================

    function test_GetPrice_Happy() public {
        _registerDefault(operatorA);

        Registry.Price memory p0 = registry.getPrice(operatorA, "gpt-4o");
        assertEq(p0.cachedIn, 1e6, "m0 cachedIn");
        assertEq(p0.input, 2e6, "m0 input");
        assertEq(p0.output, 3e6, "m0 output");

        Registry.Price memory p1 = registry.getPrice(operatorA, "gpt-4o-mini");
        assertEq(p1.cachedIn, 4e6, "m1 cachedIn");
        assertEq(p1.input, 5e6, "m1 input");
        assertEq(p1.output, 6e6, "m1 output");
    }

    function test_RevertGetPrice_ModelNotFound() public {
        _registerDefault(operatorA);

        vm.prank(stranger);
        vm.expectRevert(Registry.ModelNotFound.selector);
        registry.getPrice(operatorA, "unknown-model");
    }

    function test_RevertGetPrice_NotActive() public {
        _registerDefault(operatorA);
        vm.prank(operatorA);
        registry.deactivate();

        // inactive listing: even a listed model reverts
        vm.prank(stranger);
        vm.expectRevert(Registry.NotActive.selector);
        registry.getPrice(operatorA, "gpt-4o");

        // never-registered operator is inactive too
        vm.prank(stranger);
        vm.expectRevert(Registry.NotActive.selector);
        registry.getPrice(operatorB, "gpt-4o");
    }

    // =====================================================================
    // deactivate
    // =====================================================================

    function test_Deactivate_Happy() public {
        _registerDefault(operatorA);

        vm.expectEmit(true, false, false, true, address(registry));
        emit Registry.Deactivated(operatorA);

        vm.prank(operatorA);
        registry.deactivate();

        Registry.Listing memory listing = registry.getListing(operatorA);
        assertFalse(listing.active, "active flag cleared");
        assertEq(listing.operator, operatorA, "operator retained");
        assertEq(listing.endpoint, ENDPOINT, "endpoint retained");
        assertEq(listing.models.length, 2, "models retained");
        assertEq(listing.models[0], "gpt-4o", "models[0] retained");
        assertEq(listing.prices.length, 2, "prices retained");
        assertEq(listing.prices[0].cachedIn, 1e6, "prices[0].cachedIn retained");
        assertEq(listing.prices[1].output, 6e6, "prices[1].output retained");
    }

    // =====================================================================
    // getListing
    // =====================================================================

    /// @dev Struct shape: operator is the FIRST field, prices stay parallel to
    ///      models (same length, per-index triples), active last.
    function test_GetListing_StructShape() public {
        _registerDefault(operatorA);

        Registry.Listing memory listing = registry.getListing(operatorA);
        // field order: operator, endpoint, models, prices, active
        assertEq(listing.operator, operatorA, "operator is field 0");
        assertEq(listing.endpoint, ENDPOINT, "endpoint is field 1");
        assertEq(listing.models.length, 2, "models is field 2");
        assertEq(listing.prices.length, listing.models.length, "prices parallel to models");
        for (uint256 i = 0; i < listing.models.length; i++) {
            Registry.Price memory p = registry.getPrice(operatorA, listing.models[i]);
            assertEq(listing.prices[i].cachedIn, p.cachedIn, "parallel cachedIn");
            assertEq(listing.prices[i].input, p.input, "parallel input");
            assertEq(listing.prices[i].output, p.output, "parallel output");
        }
        assertTrue(listing.active, "active is last field");
    }

    /// @dev An unregistered operator returns the default (empty) struct:
    ///      zero operator, empty strings/arrays, active == false.
    function test_GetListing_Unregistered() public view {
        Registry.Listing memory listing = registry.getListing(operatorB);
        assertEq(listing.operator, address(0), "no operator");
        assertEq(bytes(listing.endpoint).length, 0, "no endpoint");
        assertEq(listing.models.length, 0, "no models");
        assertEq(listing.prices.length, 0, "no prices");
        assertFalse(listing.active, "not active");
    }

    // =====================================================================
    // multiple operators
    // =====================================================================

    /// @dev A and B each have exactly one listing; price updates and
    ///      deactivation of one never touch the other.
    function test_MultipleOperators_Independent() public {
        _registerDefault(operatorA);
        _registerDefault(operatorB);

        // A updates its m0 price and deactivates
        vm.startPrank(operatorA);
        registry.updateModelPrice("gpt-4o", Registry.Price({cachedIn: 9e6, input: 9e6, output: 9e6}));
        registry.deactivate();
        vm.stopPrank();

        // B is untouched: still active, original prices
        assertTrue(registry.getListing(operatorB).active, "B still active");
        Registry.Price memory pb = registry.getPrice(operatorB, "gpt-4o");
        assertEq(pb.cachedIn, 1e6, "B m0 cachedIn unchanged");
        assertEq(pb.input, 2e6, "B m0 input unchanged");
        assertEq(pb.output, 3e6, "B m0 output unchanged");
        Registry.Listing memory listingB = registry.getListing(operatorB);
        assertEq(listingB.endpoint, ENDPOINT, "B endpoint unchanged");
        assertEq(listingB.models.length, 2, "B models unchanged");

        // A is inactive with its new price retained
        Registry.Listing memory listingA = registry.getListing(operatorA);
        assertFalse(listingA.active, "A deactivated");
        assertEq(listingA.prices[0].cachedIn, 9e6, "A updated price retained");

        // B can still update its own prices (no cross-effect)
        vm.prank(operatorB);
        registry.updateModelPrice("gpt-4o", Registry.Price({cachedIn: 4e6, input: 4e6, output: 4e6}));
        pb = registry.getPrice(operatorB, "gpt-4o");
        assertEq(pb.cachedIn, 4e6, "B updated its own price");
        assertFalse(registry.getListing(operatorA).active, "A still inactive");
    }
}
