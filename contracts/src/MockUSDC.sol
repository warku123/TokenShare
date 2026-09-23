// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

/**
 * @title MockUSDC
 * @notice Development/test settlement token mimicking Circle USDC. Used when
 *         `USDC_ADDR` is NOT set at deploy time (see script/Deploy.s.sol): on
 *         a local anvil fork the forked chain may not grant meaningful testnet
 *         USDC to fresh accounts, so a mintable stand-in is deployed instead.
 *
 *         - Standard OpenZeppelin ERC20 (transfer / transferFrom / approve /
 *           balanceOf / totalSupply / Approval + Transfer events) — sufficient
 *           for the Escrow contract and the buyer CLI.
 *         - `decimals() == 6` EXACTLY like USDC: every amount crossing the
 *           Escrow/CLI/relay boundary is a USDC native unit (1 USDC = 1e6).
 *         - Permissionless `mint` (tests/local relays only; NEVER deployed to
 *           a network holding real funds).
 *
 *         Not for production. Real chains use official Circle USDC:
 *           Base Sepolia  0x036CbD53842c5426634e7929541eC2318f3dCF7e
 *           Monad testnet 0x534b2f3A21130d7a60830c2Df862319e593943A3
 *         (official addresses — for reference in tooling/config only; no
 *         source code hardcodes them).
 */
contract MockUSDC is ERC20 {
    constructor() ERC20("USD Coin", "USDC") {}

    /// @dev USDC has 6 decimals; Escrow treats all amounts in these native units.
    function decimals() public pure override returns (uint8) {
        return 6;
    }

    /// @notice Mint free mock USDC — local/testnets only.
    function mint(address to, uint256 amount) external {
        _mint(to, amount);
    }
}
