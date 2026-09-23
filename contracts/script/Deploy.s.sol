// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console2} from "forge-std/Script.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Escrow} from "../src/Escrow.sol";
import {Registry} from "../src/Registry.sol";
import {MockUSDC} from "../src/MockUSDC.sol";

/**
 * @title Deploy
 * @notice Single deployment script (BUILD_SPEC §5): `--sig run(string)` takes a
 *         network name and emits `deployed.json` for the relay/CLI/e2e to
 *         consume. Zero chain-specific hardcoding — the network name is only a
 *         label recorded into the artifact; every chain value (RPC, chainId,
 *         USDC address) comes from the environment / broadcast context.
 *
 * Usage (foundry.toml provides [rpc_endpoints] base_sepolia / monad_testnet):
 *
 *   # Base Sepolia with official testnet USDC:
 *   USDC_ADDR=0x036CbD53842c5426634e7929541eC2318f3dCF7e \
 *     forge script script/Deploy.s.sol --sig run(base_sepolia) \
 *     --rpc-url https://sepolia.base.org --broadcast
 *
 *   # Monad testnet with official Circle USDC:
 *   USDC_ADDR=0x534b2f3A21130d7a60830c2Df862319e593943A3 \
 *     forge script script/Deploy.s.sol --sig run(monad_testnet) \
 *     --rpc-url https://testnet-rpc.monad.xyz --broadcast
 *
 *   # Local anvil fork (USDC_ADDR empty -> deploys MockUSDC, usdcIsMock=true):
 *   forge script script/Deploy.s.sol --sig run(base_sepolia) \
 *     --rpc-url http://127.0.0.1:8545 --broadcast
 *
 * Env:
 *   USDC_ADDR (optional) — existing ERC-20 USDC (6dp) to use as the settlement
 *   token. When empty/absent, a fresh MockUSDC is deployed and the artifact
 *   records `usdcIsMock: true`.
 *
 * Output: contracts/deployed.json
 *   {network, chainId, escrow, registry, usdc, usdcIsMock, deployer, deployedAt}
 */
contract Deploy is Script {
    /// @dev Default artifact path, relative to the Foundry project root.
    string constant ARTIFACT_PATH = "deployed.json";

    function run(string calldata network) external {
        uint256 chainId = block.chainid;
        address deployer = msg.sender;

        // ------------------------------------------------------------------
        // Settlement token: env USDC_ADDR, or a fresh MockUSDC when empty.
        // ------------------------------------------------------------------
        address usdc = vm.envOr("USDC_ADDR", address(0));
        bool usdcIsMock = false;

        vm.startBroadcast();
        if (usdc == address(0)) {
            MockUSDC mock = new MockUSDC();
            usdc = address(mock);
            usdcIsMock = true;
        }
        // Constructor params per src/ signatures: Escrow(IERC20 usdc_), Registry().
        Escrow escrow = new Escrow(IERC20(usdc));
        Registry registry = new Registry();
        vm.stopBroadcast();

        // ------------------------------------------------------------------
        // Artifact for relay/CLI/e2e (fs_permissions grants write access).
        // ------------------------------------------------------------------
        string memory json = "deployed";
        vm.serializeString(json, "network", network);
        vm.serializeUint(json, "chainId", chainId);
        vm.serializeAddress(json, "escrow", address(escrow));
        vm.serializeAddress(json, "registry", address(registry));
        vm.serializeAddress(json, "usdc", usdc);
        vm.serializeBool(json, "usdcIsMock", usdcIsMock);
        vm.serializeAddress(json, "deployer", deployer);
        string memory artifact = vm.serializeUint(json, "deployedAt", block.timestamp);
        vm.writeJson(artifact, ARTIFACT_PATH);

        console2.log("=== TokenShare deploy complete ===");
        console2.log("network:      ", network);
        console2.log("chainId:      ", chainId);
        console2.log("escrow:       ", address(escrow));
        console2.log("registry:     ", address(registry));
        console2.log("usdc:         ", usdc);
        console2.log("usdcIsMock:   ", usdcIsMock);
        console2.log("deployer:     ", deployer);
        console2.log("artifact:     ", ARTIFACT_PATH);
    }
}
