// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/**
 * @title AttestationAnchor
 * @notice Minimal on-chain anchor for TEE attestation quotes (M7-A, Phala
 *         Cloud / dstack TDX relay).
 *
 * The relay running inside a Phala Cloud CVM serves its TDX quote at
 * GET /attestation. Off-chain verification (dstack-verifier / Phala cloud
 * API) proves the quote cryptographically; this contract only TIMESTAMPES and
 * BINDS the quote digest to an application identity, so buyers can later
 * compare a quote's digest against the anchored value (CLI
 * `verify-attestation --anchor <addr>`).
 *
 * `appId` = msg.sender: with the relay's TEE-derived seller key only the
 * relay itself can write its own row — no constructor parameters needed.
 *
 * Digest convention (shared with the CLI and relay/README-docker.md):
 *     digest = keccak256(raw quote bytes)   // the /attestation `quote` hex
 *
 * Deliberately NOT part of Registry.sol (that would force a Registry
 * redeploy + CLI churn for an optional feature).
 */
contract AttestationAnchor {
    /// @dev appId (msg.sender at anchor time, i.e. the relay seller address)
    ///      => digest of the last anchored quote.
    mapping(address appId => bytes32 digest) public digests;

    /// @notice Emitted on every anchor(); the latest quote wins.
    event Anchored(address indexed appId, bytes32 indexed digest);

    /// @notice Anchor `digest` (keccak256 of the raw TDX quote bytes) under
    ///         the caller's address. Re-anchoring overwrites (latest wins).
    function anchor(bytes32 digest) external {
        digests[msg.sender] = digest;
        emit Anchored(msg.sender, digest);
    }
}
