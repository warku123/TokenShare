// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";

import {Registry} from "../src/Registry.sol";

/**
 * @title RegistryTest
 * @notice Behavioral suite for the Registry listing contract (BUILD_SPEC v1.1
 *         §3 M2, §5 per-function happy+revert acceptance). Plain anvil/local
 *         EVM, no fork. All prices are USDC 6-decimal native units (1 USDC = 1e6).
 *
 * IMPLEMENTATION NOTE: `getListing` returns a 7-tuple with two dynamic types.
 * Direct tuple destructuring hits "Stack too deep" in solc 0.8.24 legacy
 * codegen, and decoding the raw returndata as a whole struct reverts on
 * non-empty listings. We therefore read the tuple per-field: scalars straight
 * from head words, dynamic fields decoded from their own offset slices
 * (abi-encoding is standard: head offsets are relative to the tuple start).
 */
contract RegistryTest is Test {
    // ------------------------------------------------------------------ data
    Registry internal registry;

    address internal operatorA = makeAddr("operatorA");
    address internal operatorB = makeAddr("operatorB");
    address internal stranger = makeAddr("stranger");

    // ------------------------------------------------------------------ setup
    function setUp() public {
        registry = new Registry();
    }

    // ------------------------------------------------------------------ helpers
    /// @dev Register a listing as `who` with the given fields.
    function _register(address who, string memory endpoint, string[] memory models, uint256 cached, uint256 input, uint256 output)
        internal
    {
        vm.startPrank(who);
        registry.register(endpoint, models, cached, input, output);
        vm.stopPrank();
    }

    /// @dev Register `who` with the suite's default listing.
    function _registerDefault(address who) internal {
        string[] memory models = new string[](2);
        models[0] = "gpt-4o";
        models[1] = "gpt-4o-mini";
        _register(who, "https://relay.example.com:8787", models, 1e6, 2e6, 3e6);
    }

    /// @dev Raw returndata of getListing(op).
    function _rawListing(address op) internal view returns (bytes memory ret) {
        (bool ok, bytes memory data) = address(registry).staticcall(abi.encodeCall(Registry.getListing, (op)));
        require(ok, "getListing staticcall failed");
        ret = data;
    }

    /// @dev Word i of an ABI tuple (offsets/heads/values), i in 32-byte units.
    function _word(bytes memory data, uint256 i) internal pure returns (bytes32) {
        bytes32 w;
        assembly {
            w := mload(add(data, add(32, mul(i, 32))))
        }
        return w;
    }

    /// @dev data[from:] re-framed as the encoding of ONE dynamic value
    ///      ([0x20 offset][payload]) so `abi.decode(out, (T))` is valid.
    function _tail(bytes memory data, uint256 from) internal pure returns (bytes memory out) {
        uint256 len = data.length - from;
        assembly {
            out := mload(0x40)
            let payload := and(add(len, 31), not(31))
            mstore(out, add(32, payload)) // bytes length: offset word + payload
            mstore(add(out, 32), 32) // the offset word abi.decode expects
            let src := add(data, add(32, from))
            let dst := add(out, 64)
            for { let j := 0 } lt(j, len) { j := add(j, 32) } {
                mstore(add(dst, j), mload(add(src, j)))
            }
            mstore(0x40, add(out, add(64, payload)))
        }
    }

    function _listingOperator(address op) internal view returns (address) {
        return address(uint160(uint256(_word(_rawListing(op), 0))));
    }

    function _listingEndpoint(address op) internal view returns (string memory) {
        bytes memory ret = _rawListing(op);
        uint256 off = uint256(_word(ret, 1));
        if (ret.length - off == 0) return "";
        return abi.decode(_tail(ret, off), (string));
    }

    function _listingModels(address op) internal view returns (string[] memory) {
        bytes memory ret = _rawListing(op);
        uint256 off = uint256(_word(ret, 2));
        if (ret.length - off == 0) return new string[](0);
        return abi.decode(_tail(ret, off), (string[]));
    }

    function _listingPrices(address op) internal view returns (uint256 cached, uint256 input, uint256 output) {
        bytes memory ret = _rawListing(op);
        cached = uint256(_word(ret, 3));
        input = uint256(_word(ret, 4));
        output = uint256(_word(ret, 5));
    }

    function _listingActive(address op) internal view returns (bool) {
        return _word(_rawListing(op), 6) != bytes32(0);
    }

    // =====================================================================
    // register
    // =====================================================================

    function test_Register_Happy() public {
        string[] memory models = new string[](2);
        models[0] = "gpt-4o";
        models[1] = "gpt-4o-mini";

        // Registered event: topic1 = indexed operator; data = endpoint, models, 3 prices
        vm.expectEmit(true, false, false, true, address(registry));
        emit Registry.Registered(operatorA, "https://relay.example.com:8787", models, 1e6, 2e6, 3e6);

        _register(operatorA, "https://relay.example.com:8787", models, 1e6, 2e6, 3e6);

        assertEq(_listingOperator(operatorA), operatorA, "operator");
        assertEq(_listingEndpoint(operatorA), "https://relay.example.com:8787", "endpoint");
        string[] memory got = _listingModels(operatorA);
        assertEq(got.length, 2, "models length");
        assertEq(got[0], "gpt-4o", "models[0]");
        assertEq(got[1], "gpt-4o-mini", "models[1]");
        (uint256 cached, uint256 input, uint256 output) = _listingPrices(operatorA);
        assertEq(cached, 1e6, "priceCachedIn");
        assertEq(input, 2e6, "priceInput");
        assertEq(output, 3e6, "priceOutput");
        assertTrue(_listingActive(operatorA), "active flag");
    }

    function test_RevertRegister_EmptyEndpoint() public {
        string[] memory models = new string[](1);
        models[0] = "gpt-4o";

        vm.prank(operatorA);
        vm.expectRevert(Registry.EmptyEndpoint.selector);
        registry.register("", models, 1e6, 2e6, 3e6);
    }

    function test_RevertRegister_EmptyModels() public {
        string[] memory models = new string[](0);

        vm.prank(operatorA);
        vm.expectRevert(Registry.EmptyModels.selector);
        registry.register("https://relay.example.com:8787", models, 1e6, 2e6, 3e6);
    }

    function test_RevertRegister_AlreadyRegistered() public {
        _registerDefault(operatorA);

        // still-active duplicate registration reverts
        vm.prank(operatorA);
        vm.expectRevert(Registry.AlreadyRegistered.selector);
        registry.register("https://other.example.com", new string[](1), 0, 0, 0);
    }

    /// @dev After deactivate, re-register succeeds and FULLY replaces every
    ///      field (endpoint, models as a fresh list with no stale elements,
    ///      all three prices) and re-activates, emitting Registered again.
    function test_ReRegister_AfterDeactivate() public {
        _registerDefault(operatorA);
        vm.prank(operatorA);
        registry.deactivate();

        string[] memory newModels = new string[](1);
        newModels[0] = "o1-preview";

        vm.expectEmit(true, false, false, true, address(registry));
        emit Registry.Registered(operatorA, "https://relay2.example.com", newModels, 0, 1, 2);

        _register(operatorA, "https://relay2.example.com", newModels, 0, 1, 2);

        assertTrue(_listingActive(operatorA), "re-activated");
        assertEq(_listingEndpoint(operatorA), "https://relay2.example.com", "endpoint replaced");
        string[] memory got = _listingModels(operatorA);
        assertEq(got.length, 1, "models replaced");
        assertEq(got[0], "o1-preview", "new model present");
        (uint256 cached, uint256 input, uint256 output) = _listingPrices(operatorA);
        assertEq(cached, 0, "priceCachedIn replaced");
        assertEq(input, 1, "priceInput replaced");
        assertEq(output, 2, "priceOutput replaced");
    }

    // =====================================================================
    // updatePrice
    // =====================================================================

    function test_UpdatePrice_Happy() public {
        _registerDefault(operatorA);

        vm.expectEmit(true, false, false, true, address(registry));
        emit Registry.PriceUpdated(operatorA, 10e6, 11e6, 12e6);

        vm.startPrank(operatorA);
        registry.updatePrice(10e6, 11e6, 12e6);
        vm.stopPrank();

        (uint256 cached, uint256 input, uint256 output) = _listingPrices(operatorA);
        assertEq(cached, 10e6, "priceCachedIn updated");
        assertEq(input, 11e6, "priceInput updated");
        assertEq(output, 12e6, "priceOutput updated");
        assertEq(_listingEndpoint(operatorA), "https://relay.example.com:8787", "endpoint unchanged");
        assertEq(_listingModels(operatorA).length, 2, "models unchanged");
        assertTrue(_listingActive(operatorA), "still active");
    }

    /// @dev Anyone without a listing reverts with NotRegistered (the mapping
    ///      key is the caller; NotOperator was removed as dead code).
    function test_RevertUpdatePrice_NotRegistered() public {
        // operator never registered
        vm.prank(operatorA);
        vm.expectRevert(Registry.NotRegistered.selector);
        registry.updatePrice(1e6, 2e6, 3e6);

        // any other address (never registered) — same revert
        vm.prank(stranger);
        vm.expectRevert(Registry.NotRegistered.selector);
        registry.updatePrice(1e6, 2e6, 3e6);
    }

    function test_RevertUpdatePrice_ListingInactive() public {
        _registerDefault(operatorA);
        vm.startPrank(operatorA);
        registry.deactivate();

        vm.expectRevert(Registry.ListingInactive.selector);
        registry.updatePrice(10e6, 11e6, 12e6);
        vm.stopPrank();
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

        assertFalse(_listingActive(operatorA), "active flag cleared");
        assertEq(_listingOperator(operatorA), operatorA, "operator retained");
        assertEq(_listingEndpoint(operatorA), "https://relay.example.com:8787", "endpoint retained");
        assertEq(_listingModels(operatorA).length, 2, "models retained");
        (uint256 cached, uint256 input, uint256 output) = _listingPrices(operatorA);
        assertEq(cached, 1e6, "priceCachedIn retained");
        assertEq(input, 2e6, "priceInput retained");
        assertEq(output, 3e6, "priceOutput retained");
    }

    function test_RevertDeactivate_NotRegistered() public {
        vm.prank(operatorA);
        vm.expectRevert(Registry.NotRegistered.selector);
        registry.deactivate();
    }

    function test_RevertDeactivate_ListingInactive() public {
        _registerDefault(operatorA);
        vm.startPrank(operatorA);
        registry.deactivate();

        vm.expectRevert(Registry.ListingInactive.selector);
        registry.deactivate();
        vm.stopPrank();
    }

    // =====================================================================
    // getListing boundaries
    // =====================================================================

    function test_GetListing_Unregistered() public view {
        assertEq(_listingOperator(operatorB), address(0), "no operator");
        assertEq(bytes(_listingEndpoint(operatorB)).length, 0, "no endpoint");
        assertEq(_listingModels(operatorB).length, 0, "no models");
        (uint256 cached, uint256 input, uint256 output) = _listingPrices(operatorB);
        assertEq(cached, 0, "no priceCachedIn");
        assertEq(input, 0, "no priceInput");
        assertEq(output, 0, "no priceOutput");
        assertFalse(_listingActive(operatorB), "not active");
    }

    // =====================================================================
    // price boundaries
    // =====================================================================

    /// @dev A zero price on all three tiers is legal (free tier), at register
    ///      and via updatePrice.
    function test_Register_FreeTier_AllZeroPrices() public {
        string[] memory models = new string[](1);
        models[0] = "gpt-4o";

        _register(operatorA, "https://free.example.com", models, 0, 0, 0);

        (uint256 cached, uint256 input, uint256 output) = _listingPrices(operatorA);
        assertEq(cached, 0, "cached price 0");
        assertEq(input, 0, "input price 0");
        assertEq(output, 0, "output price 0");
        assertTrue(_listingActive(operatorA), "active");

        // zero-price update is legal too
        vm.prank(operatorA);
        registry.updatePrice(0, 0, 0);
        (cached, input, output) = _listingPrices(operatorA);
        assertEq(cached + input + output, 0, "all prices still zero");
    }

    /// @dev Very large prices carry no overflow semantics (uint256 storage).
    function test_Register_LargePrices() public {
        string[] memory models = new string[](1);
        models[0] = "gpt-4o";
        uint256 big = type(uint64).max; // 18,446,744,073,709,551,615

        _register(operatorA, "https://big.example.com", models, big, big, big);
        (uint256 cached, uint256 input, uint256 output) = _listingPrices(operatorA);
        assertEq(cached, big, "large cached price");
        assertEq(input, big, "large input price");
        assertEq(output, big, "large output price");

        // even type(uint256).max is storable and updatable
        vm.prank(operatorA);
        registry.updatePrice(type(uint256).max, type(uint256).max, type(uint256).max);
        (cached, input, output) = _listingPrices(operatorA);
        assertEq(cached, type(uint256).max, "max cached price");
        assertEq(input, type(uint256).max, "max input price");
        assertEq(output, type(uint256).max, "max output price");
    }

    // =====================================================================
    // multiple operators
    // =====================================================================

    /// @dev A and B each have exactly one listing; writes and deactivation of
    ///      one never touch the other.
    function test_MultipleOperators_Independent() public {
        _registerDefault(operatorA);
        _registerDefault(operatorB);

        // A updates its prices and deactivates
        vm.startPrank(operatorA);
        registry.updatePrice(9e6, 9e6, 9e6);
        registry.deactivate();
        vm.stopPrank();

        // B is untouched: still active, original prices
        assertTrue(_listingActive(operatorB), "B still active");
        (uint256 cachedB, uint256 inB, uint256 outB) = _listingPrices(operatorB);
        assertEq(cachedB, 1e6, "B cached price unchanged");
        assertEq(inB, 2e6, "B input price unchanged");
        assertEq(outB, 3e6, "B output price unchanged");
        assertEq(_listingEndpoint(operatorB), "https://relay.example.com:8787", "B endpoint unchanged");
        assertEq(_listingModels(operatorB).length, 2, "B models unchanged");

        // A is inactive with its new prices
        assertFalse(_listingActive(operatorA), "A deactivated");
        (uint256 cachedA, , ) = _listingPrices(operatorA);
        assertEq(cachedA, 9e6, "A updated price retained");

        // B can still update its own prices (no cross-effect)
        vm.prank(operatorB);
        registry.updatePrice(4e6, 5e6, 6e6);
        (cachedB, , ) = _listingPrices(operatorB);
        assertEq(cachedB, 4e6, "B updated its own price");
        assertFalse(_listingActive(operatorA), "A still inactive");
    }
}
