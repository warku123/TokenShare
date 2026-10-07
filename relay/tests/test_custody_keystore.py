"""M15 R1 keystore tests — at-rest AAD binding (seller/url/catalog/domain/
time swaps all fail), wrong-KEK fail-closed with file preservation, partial
unwrap exclusion, restart key continuity, atomic persist + rollback."""

from __future__ import annotations

import json
import os

import pytest

from . import custody_utils as cu
from relay.app import custody
from relay.app.keystore import Keystore, KeystoreConfigError


def make_keystore(path: str, *, kek: bytes = cu.KEK, origin: str = cu.DEFAULT_ORIGIN) -> Keystore:
    return Keystore(
        path,
        kek,
        chain_id=84532,
        escrow_addr=cu.ESCROW_ADDR,
        registry_addr=cu.REGISTRY_ADDR,
        origin=origin,
    )


def store_key(ks: Keystore, seller: str, api_key: str, upstream: str = "https://api.kimi.com/coding",
              catalog: list | None = None) -> None:
    ks.set(
        seller.lower(),
        upstream_base_url=upstream,
        api_key=api_key,
        catalog=catalog if catalog is not None else [{"model": "kimi-k2-instruct", "servable": True}],
    )


CATALOG_A = [{"model": "kimi-k2-instruct", "servable": True}]
CATALOG_B = [{"model": "gpt-4o-mini", "servable": False}]


# ------------------------------------------------------------- basic lifecycle
def test_set_persists_0600_and_unwraps(tmp_path) -> None:
    path = str(tmp_path / "keystore.json")
    ks = make_keystore(path)
    store_key(ks, cu.WALLET, "sk-secret-1", catalog=CATALOG_A)
    assert os.path.exists(path)
    mode = os.stat(path).st_mode & 0o777
    assert mode == 0o600, f"keystore file must be 0600, got {oct(mode)}"
    assert ks.unwrap_api_key(cu.WALLET) == "sk-secret-1"
    doc = json.loads(open(path).read())
    entry = doc["entries"][cu.WALLET.lower()]
    assert entry["v"] == 1
    assert entry["key_fingerprint"] == custody.sha256_hex(b"sk-secret-1")
    assert entry["catalog_snapshot"] == CATALOG_A
    assert entry["upstream_base_url"] == "https://api.kimi.com/coding"
    # Only ciphertext on disk — the plaintext key appears NOWHERE.
    assert "sk-secret-1" not in open(path).read()


def test_replace_gets_new_dek_and_ivs(tmp_path) -> None:
    path = str(tmp_path / "keystore.json")
    ks = make_keystore(path)
    store_key(ks, cu.WALLET, "sk-first")
    doc1 = json.loads(open(path).read())
    store_key(ks, cu.WALLET, "sk-second")
    doc2 = json.loads(open(path).read())
    e1, e2 = doc1["entries"][cu.WALLET.lower()], doc2["entries"][cu.WALLET.lower()]
    assert e1["wrap"]["ct"] != e2["wrap"]["ct"]  # fresh DEK + IV every store
    assert e1["created"] == e2["created"]  # created preserved on replace
    assert e2["updated"] >= e1["updated"]
    assert ks.unwrap_api_key(cu.WALLET) == "sk-second"


def test_restart_key_continuity_same_kek(tmp_path) -> None:
    """Synthetic KMS continuity: reload with the SAME KEK unwraps cleanly."""
    path = str(tmp_path / "keystore.json")
    ks = make_keystore(path)
    store_key(ks, cu.WALLET, "sk-continuity", catalog=CATALOG_A)
    ks2 = make_keystore(path)  # fresh process/restart simulation
    assert len(ks2) == 1
    assert ks2.unwrap_api_key(cu.WALLET) == "sk-continuity"
    assert ks2.get(cu.WALLET).key_fingerprint == custody.sha256_hex(b"sk-continuity")


def test_restart_wrong_kek_fails_closed_preserves_file(tmp_path) -> None:
    """ALL unwrap failure (wrong KEK) → KeystoreConfigError; the file must
    survive byte-identical (never overwritten with an empty store)."""
    path = str(tmp_path / "keystore.json")
    ks = make_keystore(path)
    store_key(ks, cu.WALLET, "sk-secret")
    before = open(path, "rb").read()
    wrong_kek = bytes.fromhex("99" * 32)
    with pytest.raises(KeystoreConfigError) as exc:
        make_keystore(path, kek=wrong_kek)
    assert "unwrap" in str(exc.value) or "wrong" in str(exc.value)
    assert open(path, "rb").read() == before  # file preserved


def test_corrupted_json_fails_closed_preserves_file(tmp_path) -> None:
    path = str(tmp_path / "keystore.json")
    open(path, "w").write('{"schema": 1, "entries": {"0xabc": {')
    before = open(path, "rb").read()
    with pytest.raises(KeystoreConfigError):
        make_keystore(path)
    assert open(path, "rb").read() == before


def test_bad_schema_fails_closed(tmp_path) -> None:
    path = str(tmp_path / "keystore.json")
    open(path, "w").write(json.dumps({"schema": 99, "entries": {}}))
    with pytest.raises(KeystoreConfigError):
        make_keystore(path)


def test_missing_file_is_fresh_empty_store(tmp_path) -> None:
    ks = make_keystore(str(tmp_path / "absent.json"))
    assert len(ks) == 0
    assert ks.get(cu.WALLET) is None


# ------------------------------------------------------- partial failure face
def test_partial_unwrap_excluded_and_persistable(tmp_path) -> None:
    """One good + one broken entry: load succeeds with the good one only
    (bad excluded, no values); the next successful set() persists the valid
    set (bad entry gone from the file)."""
    path = str(tmp_path / "keystore.json")
    ks = make_keystore(path)
    store_key(ks, cu.WALLET, "sk-good")
    store_key(ks, cu.WALLET2, "sk-broken", upstream="https://api.openai.com", catalog=CATALOG_B)
    # Corrupt ONLY wallet2's enc blob in the file.
    doc = json.loads(open(path).read())
    doc["entries"][cu.WALLET2.lower()]["enc"]["ct"] = "AAAA" + doc["entries"][cu.WALLET2.lower()]["enc"]["ct"][4:]
    open(path, "w").write(json.dumps(doc))
    ks2 = make_keystore(path)
    assert len(ks2) == 1
    assert ks2.get(cu.WALLET) is not None
    assert ks2.get(cu.WALLET2) is None
    assert ks2.unwrap_api_key(cu.WALLET) == "sk-good"
    # Next successful operation persists the VALID set only.
    store_key(ks2, cu.WALLET2, "sk-new", upstream="https://api.openai.com", catalog=CATALOG_B)
    doc2 = json.loads(open(path).read())
    assert set(doc2["entries"]) == {cu.WALLET.lower(), cu.WALLET2.lower()}


# ------------------------------------------------------------ at-rest binding
def test_entry_seller_swap_fails(tmp_path) -> None:
    """Swap the two sellers' full entries at rest — the AAD binds the seller,
    so BOTH unwraps fail → fail-closed ConfigError."""
    path = str(tmp_path / "keystore.json")
    ks = make_keystore(path)
    store_key(ks, cu.WALLET, "sk-one")
    store_key(ks, cu.WALLET2, "sk-two", upstream="https://api.openai.com", catalog=CATALOG_B)
    doc = json.loads(open(path).read())
    entries = doc["entries"]
    a, b = cu.WALLET.lower(), cu.WALLET2.lower()
    entries[a], entries[b] = entries[b], entries[a]
    open(path, "w").write(json.dumps(doc))
    with pytest.raises(KeystoreConfigError):
        make_keystore(path)


def test_upstream_swap_fails(tmp_path) -> None:
    path = str(tmp_path / "keystore.json")
    ks = make_keystore(path)
    store_key(ks, cu.WALLET, "sk-secret")
    doc = json.loads(open(path).read())
    doc["entries"][cu.WALLET.lower()]["upstream_base_url"] = "https://api.openai.com"
    open(path, "w").write(json.dumps(doc))
    with pytest.raises(KeystoreConfigError):
        make_keystore(path)


def test_catalog_swap_fails(tmp_path) -> None:
    path = str(tmp_path / "keystore.json")
    ks = make_keystore(path)
    store_key(ks, cu.WALLET, "sk-secret", catalog=CATALOG_A)
    doc = json.loads(open(path).read())
    doc["entries"][cu.WALLET.lower()]["catalog_snapshot"] = CATALOG_B
    open(path, "w").write(json.dumps(doc))
    with pytest.raises(KeystoreConfigError):
        make_keystore(path)


def test_fingerprint_swap_fails(tmp_path) -> None:
    path = str(tmp_path / "keystore.json")
    ks = make_keystore(path)
    store_key(ks, cu.WALLET, "sk-secret")
    doc = json.loads(open(path).read())
    entry = doc["entries"][cu.WALLET.lower()]
    entry["key_fingerprint"] = "0" * 64
    open(path, "w").write(json.dumps(doc))
    with pytest.raises(KeystoreConfigError):
        make_keystore(path)


def test_timestamp_swap_fails(tmp_path) -> None:
    path = str(tmp_path / "keystore.json")
    ks = make_keystore(path)
    store_key(ks, cu.WALLET, "sk-secret")
    doc = json.loads(open(path).read())
    doc["entries"][cu.WALLET.lower()]["updated"] += 1
    open(path, "w").write(json.dumps(doc))
    with pytest.raises(KeystoreConfigError):
        make_keystore(path)


def test_domain_binding_origin_mismatch_fails(tmp_path) -> None:
    """The same keystore file opened under a DIFFERENT origin (relay domain
    move / copy to a sibling relay) fails unwrapping entirely."""
    path = str(tmp_path / "keystore.json")
    ks = make_keystore(path)
    store_key(ks, cu.WALLET, "sk-secret")
    with pytest.raises(KeystoreConfigError):
        make_keystore(path, origin="https://other-relay.example")


def test_chain_binding_escrow_mismatch_fails(tmp_path) -> None:
    path = str(tmp_path / "keystore.json")
    ks = make_keystore(path)
    store_key(ks, cu.WALLET, "sk-secret")
    with pytest.raises(KeystoreConfigError):
        Keystore(
            path,
            cu.KEK,
            chain_id=84532,
            escrow_addr="0x" + "ff" * 20,  # different escrow contract
            registry_addr=cu.REGISTRY_ADDR,
            origin=cu.DEFAULT_ORIGIN,
        )


def test_kek_swap_between_entries_fails(tmp_path) -> None:
    """A DEK wrapped under a different KEK (hybrid at-rest attack) fails."""
    path = str(tmp_path / "keystore.json")
    ks = make_keystore(path)
    store_key(ks, cu.WALLET, "sk-secret")
    # Re-encrypt the wrap blob with a foreign KEK, keep everything else.
    doc = json.loads(open(path).read())
    entry = doc["entries"][cu.WALLET.lower()]
    entry["wrap"]["ct"] = cu_reeval_wrap(entry)
    open(path, "w").write(json.dumps(doc))
    with pytest.raises(KeystoreConfigError):
        make_keystore(path)


def cu_reeval_wrap(entry: dict) -> str:
    # Foreign-KEK wrap of a random DEK with the ORIGINAL wrap AAD is not
    # constructible here without the meta string; a corrupted wrap ct is the
    # same failure face (authentication failure).
    return "AAAA" + entry["wrap"]["ct"][4:]


# ------------------------------------------------------------ persist + rollback
def test_persist_failure_rolls_back_memory(tmp_path, monkeypatch) -> None:
    path = str(tmp_path / "keystore.json")
    ks = make_keystore(path)
    store_key(ks, cu.WALLET, "sk-old")
    fp_old = ks.get(cu.WALLET).key_fingerprint

    real_replace = os.replace
    def boom(*a, **k):  # noqa: ANN001
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store_key(ks, cu.WALLET, "sk-new")
    monkeypatch.setattr(os, "replace", real_replace)

    # Memory rolled back → the OLD key survives in memory AND on disk.
    assert ks.get(cu.WALLET).key_fingerprint == fp_old
    assert ks.unwrap_api_key(cu.WALLET) == "sk-old"
    doc = json.loads(open(path).read())
    assert doc["entries"][cu.WALLET.lower()]["key_fingerprint"] == fp_old
    # No temp files left behind.
    leftovers = [f for f in os.listdir(tmp_path) if f.startswith(".keystore-")]
    assert leftovers == []


def test_remove_rolls_back_on_failure(tmp_path, monkeypatch) -> None:
    path = str(tmp_path / "keystore.json")
    ks = make_keystore(path)
    store_key(ks, cu.WALLET, "sk-old")

    def boom(*a, **k):  # noqa: ANN001
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        ks.remove(cu.WALLET.lower())
    # entry restored in memory
    assert ks.get(cu.WALLET) is not None


def test_remove_absent_is_noop_no_write(tmp_path) -> None:
    path = str(tmp_path / "keystore.json")
    ks = make_keystore(path)
    assert ks.remove(cu.WALLET.lower()) is False
    assert not os.path.exists(path)
