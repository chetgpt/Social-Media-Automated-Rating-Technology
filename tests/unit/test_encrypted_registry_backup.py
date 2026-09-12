from __future__ import annotations

import hashlib
import json
import os

import pytest
from cryptography.exceptions import InvalidTag

from tools import encrypted_registry_backup as backup


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI backup")


def test_windows_dpapi_roundtrip_across_streaming_chunk_boundary(tmp_path):
    source = tmp_path / "before.sqlite"
    ciphertext = tmp_path / "before.sqlite.aesgcm"
    restored = tmp_path / "restored.sqlite"
    payload = bytes(range(256)) * (33 * 1024)
    source.write_bytes(payload)

    receipt = backup.encrypt(source, ciphertext)
    verified = backup.verify(ciphertext)
    recovered = backup.verify(ciphertext, restore_to=restored)

    assert verified == recovered == {
        "verified": True,
        "plaintext_sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
    }
    assert restored.read_bytes() == source.read_bytes() == payload
    assert ciphertext.read_bytes() != payload
    assert receipt["key_protection"] == "Windows-DPAPI-current-user"
    assert json.loads(ciphertext.with_suffix(".aesgcm.json").read_text()) == receipt


@pytest.mark.parametrize("damage", ["ciphertext", "truncation", "checksum"])
def test_invalid_backup_removes_only_new_failed_restore(tmp_path, damage):
    source = tmp_path / "before.sqlite"
    ciphertext = tmp_path / "before.sqlite.aesgcm"
    restored = tmp_path / "restored.sqlite"
    payload = b"durable registry evidence\0" * 100
    source.write_bytes(payload)
    backup.encrypt(source, ciphertext)
    if damage == "ciphertext":
        encrypted = bytearray(ciphertext.read_bytes())
        encrypted[len(encrypted) // 2] ^= 1
        ciphertext.write_bytes(encrypted)
    elif damage == "truncation":
        ciphertext.write_bytes(ciphertext.read_bytes()[:-1])
    else:
        receipt_path = ciphertext.with_suffix(".aesgcm.json")
        receipt = json.loads(receipt_path.read_text())
        receipt["plaintext_sha256"] = "0" * 64
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises((InvalidTag, ValueError)):
        backup.verify(ciphertext, restore_to=restored)

    assert not restored.exists()
    assert source.read_bytes() == payload
    assert ciphertext.is_file()
    assert ciphertext.with_suffix(".aesgcm.json").is_file()


@pytest.mark.parametrize("existing", ["ciphertext", "receipt"])
def test_encryption_never_overwrites_existing_backup_files(tmp_path, existing):
    source = tmp_path / "before.sqlite"
    ciphertext = tmp_path / "before.sqlite.aesgcm"
    receipt = ciphertext.with_suffix(".aesgcm.json")
    source.write_bytes(b"source evidence")
    protected_path = ciphertext if existing == "ciphertext" else receipt
    protected_path.write_bytes(b"already retained")

    with pytest.raises(FileExistsError):
        backup.encrypt(source, ciphertext)

    assert protected_path.read_bytes() == b"already retained"
    assert source.read_bytes() == b"source evidence"
    assert not (receipt if existing == "ciphertext" else ciphertext).exists()


@pytest.mark.parametrize("destination", ["existing_file", "source", "ciphertext"])
def test_restore_never_overwrites_existing_files(tmp_path, destination):
    source = tmp_path / "before.sqlite"
    ciphertext = tmp_path / "before.sqlite.aesgcm"
    existing = tmp_path / "existing.sqlite"
    source.write_bytes(b"source evidence")
    existing.write_bytes(b"existing evidence")
    backup.encrypt(source, ciphertext)
    target = {"existing_file": existing, "source": source, "ciphertext": ciphertext}[destination]
    previous = target.read_bytes()

    with pytest.raises(FileExistsError):
        backup.verify(ciphertext, restore_to=target)

    assert target.read_bytes() == previous
    assert backup.verify(ciphertext)["verified"] is True
