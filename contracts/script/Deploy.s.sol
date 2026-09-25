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
 *   # Registry-only redeploy (M9 Registry v2): rewrites ONLY the `registry`
 *   # field of the EXISTING deployed.json, preserving escrow/usdc/usdcIsMock
 *   # (deposits stay untouched). Requires a prior full run() artifact:
 *   forge script script/Deploy.s.sol --sig runRegistryOnly(monad_testnet) \
 *     --rpc-url https://testnet-rpc.monad.xyz --broadcast
 *
 * Env:
 *   USDC_ADDR (optional) — existing ERC-20 USDC (6dp) to use as the settlement
 *   token. When empty/absent, a fresh MockUSDC is deployed and the artifact
 *   records `usdcIsMock: true`.
 *   FEE_BPS (optional, default 0 = fee-free demo) — Escrow protocol fee in
 *   basis points, charged on every seller credit (settle top-up /
 *   settlePartial capture). Owner-adjustable on-chain afterwards via
 *   `setFee` (0..10_000).
 *   FEE_RECIPIENT (optional, default deployer) — treasury receiving the fee;
 *   defaulting to the deployer means the platform (deployer) is the treasury.
 *   The Escrow `owner` (fee-config admin) is always the deployer.
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

        // ------------------------------------------------------------------
        // Protocol fee (Escrow v3): env FEE_BPS (default 0 = fee-free demo,
        // owner-adjustable on-chain via setFee) and FEE_RECIPIENT (default
        // deployer => platform == deployer). Escrow owner = deployer.
        // ------------------------------------------------------------------
        uint16 feeBps = uint16(vm.envOr("FEE_BPS", uint256(0)));
        address feeRecipient = vm.envOr("FEE_RECIPIENT", deployer);

        vm.startBroadcast();
        if (usdc == address(0)) {
            MockUSDC mock = new MockUSDC();
            usdc = address(mock);
            usdcIsMock = true;
        }
        // Constructor params per src/ signature: Escrow(IERC20 usdc_, address owner_, uint16 feeBps_, address feeRecipient_). Registry().
        Escrow escrow = new Escrow(IERC20(usdc), deployer, feeBps, feeRecipient);
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
        console2.log("feeBps:       ", feeBps);
        console2.log("feeRecipient: ", feeRecipient);
        console2.log("artifact:     ", ARTIFACT_PATH);
    }

    /**
     * @notice Registry-only redeploy (M9 Registry v2): deploys a fresh Registry
     *         and rewrites the `registry` field of the EXISTING deployed.json,
     *         preserving escrow/usdc/usdcIsMock (Escrow untouched — deposits
     *         stay). `network` is only a label; run on the SAME chain as the
     *         original artifact (chainId is re-recorded from block.chainid).
     *         The artifact must already exist (produced by a prior full
     *         `run(string)`); otherwise vm.readFile reverts.
     */
    function runRegistryOnly(string calldata network) external {
        // ------------------------------------------------------------------
        // Preserve operational fields from the existing artifact.
        // ------------------------------------------------------------------
        string memory existing = vm.readFile(ARTIFACT_PATH);
        address escrow = vm.parseJsonAddress(existing, ".escrow");
        address usdc = vm.parseJsonAddress(existing, ".usdc");
        bool usdcIsMock = vm.parseJsonBool(existing, ".usdcIsMock");

        uint256 chainId = block.chainid;
        address deployer = msg.sender;

        vm.startBroadcast();
        Registry registry = new Registry();
        vm.stopBroadcast();

        // ------------------------------------------------------------------
        // Artifact: same field set as run(), registry swapped for the new one.
        // ------------------------------------------------------------------
        string memory json = "deployed";
        vm.serializeString(json, "network", network);
        vm.serializeUint(json, "chainId", chainId);
        vm.serializeAddress(json, "escrow", escrow);
        vm.serializeAddress(json, "registry", address(registry));
        vm.serializeAddress(json, "usdc", usdc);
        vm.serializeBool(json, "usdcIsMock", usdcIsMock);
        vm.serializeAddress(json, "deployer", deployer);
        string memory artifact = vm.serializeUint(json, "deployedAt", block.timestamp);
        vm.writeJson(artifact, ARTIFACT_PATH);

        console2.log("=== TokenShare registry-only redeploy complete ===");
        console2.log("network:      ", network);
        console2.log("chainId:      ", chainId);
        console2.log("escrow:       ", escrow);
        console2.log("registry NEW: ", address(registry));
        console2.log("usdc:         ", usdc);
        console2.log("usdcIsMock:   ", usdcIsMock);
        console2.log("deployer:     ", deployer);
        console2.log("artifact:     ", ARTIFACT_PATH);
    }

    /**
     * @notice Escrow-only redeploy (M13 Escrow v2 partial settle; M14 fee
     *         config): deploys a fresh Escrow against the SAME usdc and
     *         rewrites the `escrow` field of the EXISTING deployed.json,
     *         preserving registry/usdc/usdcIsMock. Escrow balances do NOT
     *         carry over — buyers must re-deposit against the new Escrow.
     *         Fee config is owner-adjustable on-chain (setFee/setFeeRecipient);
     *         initial values come from the CURRENT env (defaults: 0 bps
     *         fee-free demo, deployer recipient+owner), NOT from the old
     *         artifact. Run on the SAME chain as the
     *         original artifact; the artifact must already exist.
     */
    function runEscrowOnly(string calldata network) external {
        string memory existing = vm.readFile(ARTIFACT_PATH);
        address registry = vm.parseJsonAddress(existing, ".registry");
        address usdc = vm.parseJsonAddress(existing, ".usdc");
        bool usdcIsMock = vm.parseJsonBool(existing, ".usdcIsMock");

        uint256 chainId = block.chainid;
        address deployer = msg.sender;

        uint16 feeBps = uint16(vm.envOr("FEE_BPS", uint256(0)));
        address feeRecipient = vm.envOr("FEE_RECIPIENT", deployer);

        vm.startBroadcast();
        Escrow escrow = new Escrow(IERC20(usdc), deployer, feeBps, feeRecipient);
        vm.stopBroadcast();

        string memory json = "deployed";
        vm.serializeString(json, "network", network);
        vm.serializeUint(json, "chainId", chainId);
        vm.serializeAddress(json, "escrow", address(escrow));
        vm.serializeAddress(json, "registry", registry);
        vm.serializeAddress(json, "usdc", usdc);
        vm.serializeBool(json, "usdcIsMock", usdcIsMock);
        vm.serializeAddress(json, "deployer", deployer);
        string memory artifact = vm.serializeUint(json, "deployedAt", block.timestamp);
        vm.writeJson(artifact, ARTIFACT_PATH);

        console2.log("=== TokenShare escrow-only redeploy complete ===");
        console2.log("network:       ", network);
        console2.log("chainId:       ", chainId);
        console2.log("escrow NEW:    ", address(escrow));
        console2.log("registry:      ", registry);
        console2.log("usdc:          ", usdc);
        console2.log("usdcIsMock:    ", usdcIsMock);
        console2.log("deployer:      ", deployer);
        console2.log("feeBps:        ", feeBps);
        console2.log("feeRecipient:  ", feeRecipient);
        console2.log("artifact:      ", ARTIFACT_PATH);
    }
}
