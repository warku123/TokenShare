"""Local dispute records for failed receipt verification.

Default location: ~/.tokenshare/disputes.json (override with the global
--disputes-file option). Layout: { "<paymentId>": [ {record}, ... ] }.

Documented in the app --help so it is discoverable (BUILD_SPEC §6.3 / M4).
"""

import json
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DISPUTES_PATH = Path.home() / ".tokenshare" / "disputes.json"

# Dispute reasons (receipt verification failures only; settle-failed is a
# normal expiry-refund path and is NOT a dispute).
REASON_RECOVER_MISMATCH = "receipt-recover-mismatch"
REASON_SELLER_MISMATCH = "receipt-seller-mismatch"
REASON_PAYMENT_ID_MISMATCH = "receipt-paymentid-mismatch"
REASON_DECODE_FAILED = "receipt-decode-failed"


def resolve_path(override: str | None) -> Path:
    return Path(override) if override else DEFAULT_DISPUTES_PATH


def load_disputes(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def save_disputes(path: Path, disputes: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(disputes, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def record_dispute(
    path: Path,
    payment_id: int,
    reason: str,
    expected_seller: str | None,
    recovered: str | None,
    receipt_seller: str | None = None,
    detail: str | None = None,
) -> None:
    """Append a dispute record for `payment_id` (atomic append-and-save)."""
    disputes = load_disputes(path)
    key = str(int(payment_id))
    entries = disputes.setdefault(key, [])
    entries.append(
        {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "reason": reason,
            "expected_seller": expected_seller,
            "receipt_seller": receipt_seller,
            "recovered": recovered,
            "detail": detail,
        }
    )
    save_disputes(path, disputes)


def format_disputes(disputes: dict) -> str:
    if not disputes:
        return "No disputes recorded."
    lines = []
    for payment_id in sorted(disputes, key=lambda k: int(k) if k.isdigit() else 0):
        entries = disputes[payment_id]
        lines.append(f"paymentId {payment_id}: {len(entries)} record(s)")
        for entry in entries:
            lines.append(
                f"  - {entry.get('time')}  reason={entry.get('reason')}  "
                f"expected_seller={entry.get('expected_seller')}  "
                f"recovered={entry.get('recovered')}"
            )
    return "\n".join(lines)
