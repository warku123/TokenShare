// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";

import {Registry} from "../src/Registry.sol";

/**
 * @title RegistryTest
 * @notice Behavioral suite for the Registry listing contract (BUILD_SPEC
 *         v1.1 §3 M2 + M9 per-model pricing PIN + M10 v3 seller
 *         enumeration + M12 v4 model-level delisting). Plain anvil/local
 *         EVM, no fork. All prices are USDC 6-decimal native units per 1M
 *         tokens (1 USDC = 1e6).
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

    // =====================================================================
    // removeModel (v4 / M12)
    // =====================================================================

    /// @dev Three-model listing with distinct per-model prices:
    ///      m-alpha=(1,2,3)e6, m-beta=(4,5,6)e6, m-gamma=(7,8,9)e6.
    function _registerThreeModels(address who) internal {
        string[] memory models = new string[](3);
        models[0] = "m-alpha";
        models[1] = "m-beta";
        models[2] = "m-gamma";
        Registry.Price[] memory prices = new Registry.Price[](3);
        prices[0] = Registry.Price({cachedIn: 1e6, input: 2e6, output: 3e6});
        prices[1] = Registry.Price({cachedIn: 4e6, input: 5e6, output: 6e6});
        prices[2] = Registry.Price({cachedIn: 7e6, input: 8e6, output: 9e6});
        _register(who, ENDPOINT, models, prices);
    }

    /// @dev Assert every survivor keeps ITS OWN price and getListing stays a
    ///      consistent parallel array (models.length == prices.length, and
    ///      prices[i] == getPrice(operator, models[i]) for every i).
    function _assertParallelShape(address who) internal view {
        Registry.Listing memory listing = registry.getListing(who);
        assertEq(listing.prices.length, listing.models.length, "parallel lengths");
        for (uint256 i = 0; i < listing.models.length; i++) {
            Registry.Price memory p = registry.getPrice(who, listing.models[i]);
            assertEq(listing.prices[i].cachedIn, p.cachedIn, "parallel cachedIn");
            assertEq(listing.prices[i].input, p.input, "parallel input");
            assertEq(listing.prices[i].output, p.output, "parallel output");
        }
    }

    /// @dev Removing a MIDDLE model: swap-and-pop moves the last element into
    ///      the hole (survivor ORDER may change), but the price travels with
    ///      its model — every survivor's price is exactly its pre-removal
    ///      triple, verified both via getListing and via getPrice.
    function test_RemoveModel_MiddleSwapAndPop() public {
        _registerThreeModels(operatorA);

        vm.prank(operatorA);
        registry.removeModel("m-beta");

        Registry.Listing memory listing = registry.getListing(operatorA);
        assertEq(listing.models.length, 2, "models shortened by one");
        assertEq(listing.prices.length, 2, "prices shortened in lockstep");

        // swap-and-pop: "m-gamma" (former last) fills the hole at index 1
        assertEq(listing.models[0], "m-alpha", "models[0] untouched");
        assertEq(listing.models[1], "m-gamma", "models[1] = former last");

        // price followed its model across the swap
        assertEq(listing.prices[1].cachedIn, 7e6, "gamma cachedIn followed model");
        assertEq(listing.prices[1].input, 8e6, "gamma input followed model");
        assertEq(listing.prices[1].output, 9e6, "gamma output followed model");
        assertEq(listing.prices[0].cachedIn, 1e6, "alpha cachedIn unchanged");
        assertEq(listing.prices[0].input, 2e6, "alpha input unchanged");
        assertEq(listing.prices[0].output, 3e6, "alpha output unchanged");

        _assertParallelShape(operatorA);
        // removed model is gone from the listing
        assertFalse(
            keccak256(bytes(listing.models[0])) == keccak256(bytes("m-beta"))
                || keccak256(bytes(listing.models[1])) == keccak256(bytes("m-beta")),
            "m-beta fully removed"
        );
        assertTrue(listing.active, "removal is not deactivation");
    }

    /// @dev Removing the LAST element is a plain pop: survivor order AND all
    ///      survivor prices are bit-identical to before.
    function test_RemoveModel_LastElement() public {
        _registerThreeModels(operatorA);

        vm.prank(operatorA);
        registry.removeModel("m-gamma");

        Registry.Listing memory listing = registry.getListing(operatorA);
        assertEq(listing.models.length, 2, "models shortened by one");
        assertEq(listing.models[0], "m-alpha", "order kept (pure pop)");
        assertEq(listing.models[1], "m-beta", "order kept (pure pop)");
        assertEq(listing.prices[0].cachedIn, 1e6, "alpha cachedIn intact");
        assertEq(listing.prices[0].input, 2e6, "alpha input intact");
        assertEq(listing.prices[0].output, 3e6, "alpha output intact");
        assertEq(listing.prices[1].cachedIn, 4e6, "beta cachedIn intact");
        assertEq(listing.prices[1].input, 5e6, "beta input intact");
        assertEq(listing.prices[1].output, 6e6, "beta output intact");

        _assertParallelShape(operatorA);
    }

    /// @dev The LAST remaining model cannot be removed — RemoveLastModel
    ///      steers the operator to {deactivate}. Removing down from two works
    ///      and the resulting one-model listing is fully functional
    ///      (getPrice / updateModelPrice still operate on the survivor).
    function test_RevertRemoveModel_RemoveLastModel() public {
        _registerDefault(operatorA); // 2 models

        vm.prank(operatorA);
        registry.removeModel("gpt-4o-mini");

        // down to one model: removing the survivor reverts RemoveLastModel
        // (guard order is ModelNotFound first, so this uses the REAL survivor
        // name; an unknown name on a one-model listing is ModelNotFound —
        // covered in test_RevertRemoveModel_ModelNotFound)
        vm.prank(operatorA);
        vm.expectRevert(Registry.RemoveLastModel.selector);
        registry.removeModel("gpt-4o");

        // the one-model listing keeps working
        Registry.Price memory p = registry.getPrice(operatorA, "gpt-4o");
        assertEq(p.cachedIn, 1e6, "survivor price intact");
        vm.prank(operatorA);
        registry.updateModelPrice("gpt-4o", Registry.Price({cachedIn: 9e6, input: 9e6, output: 9e6}));
        assertEq(registry.getPrice(operatorA, "gpt-4o").cachedIn, 9e6, "update still works");

        // same guard on a fresh single-model listing
        _register(operatorB, ENDPOINT, _oneModel("solo"), _onePrice(1, 2, 3));
        vm.prank(operatorB);
        vm.expectRevert(Registry.RemoveLastModel.selector);
        registry.removeModel("solo");
    }

    /// @dev Unknown model (never listed, or already removed, or on a
    ///      never-registered operator's empty listing) reverts ModelNotFound.
    function test_RevertRemoveModel_ModelNotFound() public {
        _registerThreeModels(operatorA);

        // never existed
        vm.prank(operatorA);
        vm.expectRevert(Registry.ModelNotFound.selector);
        registry.removeModel("unknown-model");

        // on a single-model listing, an UNKNOWN name is ModelNotFound (not
        // RemoveLastModel) — the model lookup happens first
        _register(operatorB, ENDPOINT, _oneModel("solo"), _onePrice(1, 2, 3));
        vm.prank(operatorB);
        vm.expectRevert(Registry.ModelNotFound.selector);
        registry.removeModel("not-solo");

        // never-registered operator: empty listing => nothing to find
        vm.prank(stranger);
        vm.expectRevert(Registry.ModelNotFound.selector);
        registry.removeModel("m-alpha");
    }

    /// @dev No `active` requirement: a DEACTIVATED listing can still be
    ///      cleaned up model by model; the flag stays false and other fields
    ///      are retained.
    function test_RemoveModel_InactiveListing() public {
        _registerThreeModels(operatorA);
        vm.prank(operatorA);
        registry.deactivate();

        vm.prank(operatorA);
        registry.removeModel("m-beta"); // must NOT revert NotActive

        Registry.Listing memory listing = registry.getListing(operatorA);
        assertFalse(listing.active, "still inactive");
        assertEq(listing.models.length, 2, "model removed while inactive");
        assertEq(listing.prices.length, 2, "prices parallel while inactive");
        assertEq(listing.endpoint, ENDPOINT, "endpoint retained");
        // NB: no getPrice here — it correctly reverts NotActive while
        // inactive; assert the parallel arrays directly instead.
        assertEq(listing.models[0], "m-alpha", "survivor order (gamma filled hole)");
        assertEq(listing.models[1], "m-gamma", "survivor order");
        assertEq(listing.prices[1].cachedIn, 7e6, "gamma price followed model");
    }

    /// @dev After removal, `getPrice` reverts ModelNotFound for the removed
    ///      model (this is exactly the hook that makes an in-flight settle
    ///      fail and pushes the buyer to the refund path), while survivors
    ///      still price normally.
    function test_RemoveModel_GetPriceReverts() public {
        _registerThreeModels(operatorA);

        vm.prank(operatorA);
        registry.removeModel("m-beta");

        vm.prank(stranger);
        vm.expectRevert(Registry.ModelNotFound.selector);
        registry.getPrice(operatorA, "m-beta");

        // survivors unaffected
        Registry.Price memory pa = registry.getPrice(operatorA, "m-alpha");
        assertEq(pa.input, 2e6, "alpha still priced");
        Registry.Price memory pg = registry.getPrice(operatorA, "m-gamma");
        assertEq(pg.input, 8e6, "gamma still priced");
    }

    /// @dev Emits ModelRemoved(operator, model) with the operator indexed.
    function test_RemoveModel_Event() public {
        _registerThreeModels(operatorA);

        vm.expectEmit(true, false, false, true, address(registry));
        emit Registry.ModelRemoved(operatorA, "m-beta");

        vm.prank(operatorA);
        registry.removeModel("m-beta");
    }

    /// @dev Re-add path: after removing a model (then deactivating, since
    ///      register requires an inactive listing) the same model name can be
    ///      registered again with a fresh price and prices normally.
    function test_RemoveModel_ReRegisterSameModel() public {
        _registerThreeModels(operatorA);

        vm.prank(operatorA);
        registry.removeModel("m-beta");

        vm.prank(operatorA);
        registry.deactivate();

        // re-register WITH the removed model back in the set
        string[] memory models = new string[](2);
        models[0] = "m-alpha";
        models[1] = "m-beta"; // re-added
        Registry.Price[] memory prices = new Registry.Price[](2);
        prices[0] = Registry.Price({cachedIn: 1e6, input: 2e6, output: 3e6});
        prices[1] = Registry.Price({cachedIn: 40e6, input: 50e6, output: 60e6}); // new price
        _register(operatorA, ENDPOINT, models, prices);

        Registry.Listing memory listing = registry.getListing(operatorA);
        assertTrue(listing.active, "re-registered");
        assertEq(listing.models.length, 2, "model set restored");
        assertEq(listing.models[1], "m-beta", "m-beta back in the set");
        Registry.Price memory p = registry.getPrice(operatorA, "m-beta");
        assertEq(p.cachedIn, 40e6, "fresh cachedIn");
        assertEq(p.input, 50e6, "fresh input");
        assertEq(p.output, 60e6, "fresh output");
        _assertParallelShape(operatorA);
    }

    // =====================================================================
    // seller enumeration (v3 / M10)
    // =====================================================================

    /// @dev Register `who` with a minimal one-model listing (model "m").
    function _registerOneModel(address who) internal {
        vm.prank(who);
        registry.register("https://relay.example.com", _oneModel("m"), _onePrice(1e6, 2e6, 3e6));
    }

    /// @dev Fresh directory: count 0, any page query returns an empty array
    ///      (no revert).
    function test_SellerEnumeration_InitiallyEmpty() public view {
        assertEq(registry.sellerCount(), 0, "initial count");
        address[] memory sellers = registry.getSellers(0, 10);
        assertEq(sellers.length, 0, "initial page empty");
    }

    /// @dev One registration -> directory of one, correct entry, order stable
    ///      with an oversized `count` request.
    function test_SellerEnumeration_RegisterAdds() public {
        _registerOneModel(operatorA);

        assertEq(registry.sellerCount(), 1, "count after register");
        address[] memory sellers = registry.getSellers(0, 10);
        assertEq(sellers.length, 1, "page length (count>len truncates)");
        assertEq(sellers[0], operatorA, "sellers[0]");
    }

    /// @dev Deactivate -> re-register does NOT duplicate the directory entry:
    ///      the `_sellerIndex` guard makes the append a no-op.
    function test_SellerEnumeration_ReRegisterNoDuplicate() public {
        _registerDefault(operatorA);
        vm.prank(operatorA);
        registry.deactivate();
        _register(operatorA, "https://relay2.example.com", _oneModel("o1-preview"), _onePrice(0, 1, 2));

        assertEq(registry.sellerCount(), 1, "still one seller");
        address[] memory sellers = registry.getSellers(0, 10);
        assertEq(sellers.length, 1, "page length");
        assertEq(sellers[0], operatorA, "same single entry");
    }

    /// @dev Three sellers: first-registration order is preserved and any
    ///      single-entry window is addressable.
    function test_SellerEnumeration_ThreeSellersPagination() public {
        _registerOneModel(operatorA);
        _registerOneModel(operatorB);
        _registerOneModel(stranger);

        assertEq(registry.sellerCount(), 3, "three sellers");

        address[] memory second = registry.getSellers(1, 1);
        assertEq(second.length, 1, "window length");
        assertEq(second[0], operatorB, "second seller");

        address[] memory first = registry.getSellers(0, 1);
        assertEq(first[0], operatorA, "first seller");

        address[] memory third = registry.getSellers(2, 1);
        assertEq(third[0], stranger, "third seller");
    }

    /// @dev `start >= sellerCount()` returns an empty array — never reverts —
    ///      for any `count`.
    function test_SellerEnumeration_StartOutOfBounds() public {
        _registerOneModel(operatorA);

        address[] memory past = registry.getSellers(1, 10);
        assertEq(past.length, 0, "start==len -> empty");

        address[] memory far = registry.getSellers(10_000, 1);
        assertEq(far.length, 0, "start>>len -> empty");

        // start==len-1 still yields the last entry
        address[] memory last = registry.getSellers(0, 1);
        assertEq(last.length, 1, "start==0 fine");
        assertEq(last[0], operatorA, "last entry reachable");
    }

    /// @dev Page-limit constant is 500 (ABI-facing, clients read it) and the
    ///      clamp semantics hold on small data: an oversized `count` returns
    ///      min(limit, len - start) entries.
    function test_SellerEnumeration_CountClamp_SmallData() public {
        // via the ABI getter (instance call): solc 0.8.24 rejects bare
        // contract-type constant access (`Registry.SELLER_PAGE_LIMIT`) with
        // error 9582 even in assignment context, so tests read the public
        // constant the way clients do — through the deployed getter.
        uint256 limit = registry.SELLER_PAGE_LIMIT();
        assertEq(limit, 500, "public page limit");

        _registerOneModel(operatorA);
        _registerOneModel(operatorB);

        // count far above the limit: clamped to 500, then truncated to len
        address[] memory page = registry.getSellers(0, limit + 1);
        assertEq(page.length, 2, "clamp+truncate -> all sellers");
        assertEq(page[0], operatorA, "order kept");
        assertEq(page[1], operatorB, "order kept");

        // count exactly at the limit on tiny data behaves as plain truncation
        address[] memory atLimit = registry.getSellers(0, limit);
        assertEq(atLimit.length, 2, "at-limit page truncated to len");
    }

    /// @dev The ONLY way to observe the count clamp in isolation (without
    ///      len-truncation masking it): 501 sellers, request 1000 ->
    ///      exactly 500 entries, and the 501st seller is reachable via the
    ///      next window.
    function test_SellerEnumeration_CountClamp_AtLimit() public {
        uint256 limit = registry.SELLER_PAGE_LIMIT(); // ABI getter, see small-data test
        for (uint256 i = 1; i <= limit + 1; i++) {
            _registerOneModel(vm.addr(i));
        }
        assertEq(registry.sellerCount(), limit + 1, "501 registered");

        address[] memory page = registry.getSellers(0, 1000);
        assertEq(page.length, limit, "count clamped to 500 (not truncated to 501)");
        assertEq(page[0], vm.addr(1), "page starts at first");
        assertEq(page[limit - 1], vm.addr(limit), "page ends at 500th");

        address[] memory tail = registry.getSellers(limit, 10);
        assertEq(tail.length, 1, "tail window");
        assertEq(tail[0], vm.addr(limit + 1), "501st reachable");
    }

    /// @dev Paginated fetch concatenates to the full directory: no gaps, no
    ///      duplicates, last short page handled.
    function test_SellerEnumeration_PaginationConcatenation() public {
        uint256 n = 7;
        for (uint256 i = 1; i <= n; i++) {
            _registerOneModel(vm.addr(i));
        }

        address[] memory p0 = registry.getSellers(0, 3);
        address[] memory p1 = registry.getSellers(3, 3);
        address[] memory p2 = registry.getSellers(6, 3); // short final page

        assertEq(p0.length, 3, "page 0 full");
        assertEq(p1.length, 3, "page 1 full");
        assertEq(p2.length, 1, "page 2 short");

        for (uint256 i = 0; i < 3; i++) {
            assertEq(p0[i], vm.addr(i + 1), "p0 entry");
            assertEq(p1[i], vm.addr(i + 4), "p1 entry");
        }
        assertEq(p2[0], vm.addr(7), "p2 entry");
    }

    /// @dev Directory is append-only and never pruned: deactivation keeps the
    ///      entry in place; a later registration appends after it; the
    ///      consumer filters activeness via `getListing(seller).active`.
    function test_SellerEnumeration_DeactivateKeepsEntry() public {
        _registerOneModel(operatorA);
        vm.prank(operatorA);
        registry.deactivate();
        _registerOneModel(operatorB);

        assertEq(registry.sellerCount(), 2, "deactivated seller still counted");
        address[] memory sellers = registry.getSellers(0, 10);
        assertEq(sellers.length, 2, "directory length");
        assertEq(sellers[0], operatorA, "inactive seller retained");
        assertEq(sellers[1], operatorB, "new seller appended after");

        assertFalse(registry.getListing(operatorA).active, "A inactive (consumer filters)");
        assertTrue(registry.getListing(operatorB).active, "B active");

        // A re-registers: directory unchanged, order preserved
        _registerOneModel(operatorA);
        assertEq(registry.sellerCount(), 2, "no duplicate after re-register");
        sellers = registry.getSellers(0, 10);
        assertEq(sellers[0], operatorA, "order preserved");
        assertEq(sellers[1], operatorB, "order preserved");
    }
}
