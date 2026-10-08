/**
 * Report payload construction + wire-format guarantees (pure).
 *
 * Wire contract (PIN, tidied 2026-10):
 *   runtime.report(prepareReportRequest(EXACTLY these business bytes)) —
 *   the raw abi.encode of the seven business fields, built with
 *   encodeAbiParameters. The official KeystoneForwarder
 *   (contracts/cre/src/v1/KeystoneForwarder.sol) routes a report to the
 *   consumer as abi.encodeCall(onReport, (rawReport[45:109], rawReport[109:])),
 *   so the consumer's abi.decode MUST receive the seven fields and nothing
 *   else. Anything extra (e.g. a fabricated metadata field, or an
 *   encodeFunctionData(onReport...) calldata wrapper) lands inside the report
 *   span and breaks ReceiptAnchor._decodeReport.
 *
 * Field order/type pin = cre/contracts/src/ReceiptAnchor.sol:_decodeReport:
 *   (uint256 paymentId, uint256 settledAmount, uint256 receiptAmount,
 *    bytes32 receiptHash, string upstreamHost, string model, uint8 verdict)
 */
import {
  encodeAbiParameters,
  parseAbiParameters,
  type Hex,
} from "viem"

export const REPORT_FIELDS = parseAbiParameters(
  "uint256 paymentId, uint256 settledAmount, uint256 receiptAmount, bytes32 receiptHash, string upstreamHost, string model, uint8 verdict",
)

export type ReportPayloadArgs = {
  paymentId: bigint
  settledAmount: bigint
  receiptAmount: bigint
  receiptHash: Hex
  upstreamHost: string
  model: string
  verdict: 1 | 2 | bigint
}

/**
 * Business bytes handed to runtime.report(prepareReportRequest(...)).
 * The EVM writeReport capability wraps DON-signed metadata around these; the
 * forwarder extracts (receiver, report=THESE BYTES) and calls onReport on it.
 */
export function encodeReportPayload(args: ReportPayloadArgs): Hex {
  return encodeAbiParameters(REPORT_FIELDS, [
    args.paymentId,
    args.settledAmount,
    args.receiptAmount,
    args.receiptHash,
    args.upstreamHost,
    args.model,
    typeof args.verdict === "bigint" ? Number(args.verdict) : args.verdict,
  ])
}
