/** Config schema tests incl. the trusted-signer key boundaries. */
import { describe, expect, test } from "bun:test"
import { configSchema } from "../src/config"
import { TRUSTED_SIGNER_REAL } from "./helpers"
import { readFileSync } from "node:fs"

const BASE = {
  chainSelectorName: "monad-testnet",
  escrowAddress: "0xe4D5Eb0dBDB6DB8063C07ECF7EDFcCdDB9Ad514c",
  registryAddress: "0xeD347cDc1761750E20C024459b38dedFb1462254",
  anchorAddress: "0x5555555555555555555555555555555555555555",
  relayBaseUrl: "https://relay.example",
  maxPriceUsd6: "1000000000",
  workflowOwner: "0x6666666666666666666666666666666666666666",
  receiptSignerAddress: TRUSTED_SIGNER_REAL,
}

describe("configSchema", () => {
  test("accepts the full documented config", () => {
    expect(configSchema.parse(BASE)).toBeTruthy()
    // checksummed address preserved (case-sensitive schema only validates)
    expect(configSchema.parse(BASE).receiptSignerAddress).toBe(TRUSTED_SIGNER_REAL)
  })

  test("receiptSignerAddress: malformed hex ⇒ reject", () => {
    for (const bad of ["not-an-address", "0x1234", "49CA3eADB3b8F23623f735918A6F2b945F60fb2D"]) {
      expect(() =>
        configSchema.parse({ ...BASE, receiptSignerAddress: bad }),
      ).toThrowError(/40-hex/)
    }
  })

  test("receiptSignerAddress: zero address ⇒ reject", () => {
    expect(() =>
      configSchema.parse({
        ...BASE,
        receiptSignerAddress: "0x0000000000000000000000000000000000000000",
      }),
    ).toThrowError(/zero address/)
  })

  test("receiptSignerAddress: missing ⇒ reject (no receipt trust without pin)", () => {
    const { receiptSignerAddress: _drop, ...rest } = BASE
    expect(() => configSchema.parse(rest)).toThrowError()
  })

  test("escrow/registry must be hex addresses ⇒ reject arbitrary strings", () => {
    expect(() =>
      configSchema.parse({ ...BASE, escrowAddress: "0xYOUR_ESCROW_PLACEHOLDER" }),
    ).toThrowError(/40-hex/)
  })

  test("anchorAddress / workflowOwner stay deploy-time free slots (placeholders pass)", () => {
    expect(configSchema.parse({ ...BASE, anchorAddress: "0xYOUR_RECEIPT_ANCHOR_CONTRACT_ADDRESS" })).toBeTruthy()
    expect(
      configSchema.parse({ ...BASE, workflowOwner: "0xYOUR_WORKFLOW_OWNER_ADDRESS" }),
    ).toBeTruthy()
  })

  test("relayBaseUrl: scheme required", () => {
    expect(() => configSchema.parse({ ...BASE, relayBaseUrl: "ftp://relay" })).toThrowError(
      /http/,
    )
    expect(() => configSchema.parse({ ...BASE, relayBaseUrl: "YOUR_PUBLIC_RELAY_ENDPOINT" })).toThrowError(
      /http/,
    )
    expect(configSchema.parse({ ...BASE, relayBaseUrl: "http://127.0.0.1:8787" }).relayBaseUrl).toBe(
      "http://127.0.0.1:8787",
    )
  })

  test("maxPriceUsd6: positive decimal integer string", () => {
    expect(() => configSchema.parse({ ...BASE, maxPriceUsd6: "0" })).toThrowError()
    expect(() => configSchema.parse({ ...BASE, maxPriceUsd6: "-1" })).toThrowError()
    expect(() => configSchema.parse({ ...BASE, maxPriceUsd6: "0x10" })).toThrowError()
    expect(configSchema.parse({ ...BASE, maxPriceUsd6: "1" }).maxPriceUsd6).toBe("1")
  })
})

describe("real config files on disk", () => {
  test("config.staging.json is schema-valid with real trusted signer + public relay URL", () => {
    const cfg = JSON.parse(readFileSync(new URL("../config.staging.json", import.meta.url), "utf8"))
    const parsed = configSchema.parse(cfg)
    expect(parsed.receiptSignerAddress).toBe(TRUSTED_SIGNER_REAL)
    expect(parsed.relayBaseUrl).toBe(
      "https://c30652f4833465adda9bdc7ac557e8b45e8e66c6-8787.dstack-pha-prod5.phala.network",
    )
    expect(parsed.chainSelectorName).toBe("monad-testnet")
  })

  test("config.production.json keeps placeholder relay URL but the real trusted signer", () => {
    const cfg = JSON.parse(readFileSync(new URL("../config.production.json", import.meta.url), "utf8"))
    const parsed = configSchema.parse(cfg)
    expect(parsed.receiptSignerAddress).toBe(TRUSTED_SIGNER_REAL)
    expect(parsed.relayBaseUrl).toBe("https://YOUR_PUBLIC_RELAY_ENDPOINT")
  })
})
