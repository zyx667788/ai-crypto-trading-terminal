"""Tests for SecretVault master-key persistence and loss detection (P0 fix)."""
from __future__ import annotations

import subprocess
import sys

import pytest
from cryptography.fernet import Fernet

from app.core.security import MasterKeyError, SecretDecryptError, SecretVault


@pytest.fixture(autouse=True)
def _no_env_master_key(monkeypatch):
    """Never let the session-wide TERMINAL_MASTER_KEY leak into file-backend tests."""
    monkeypatch.delenv("TERMINAL_MASTER_KEY", raising=False)


def test_key_file_created_with_mode_0600(tmp_path, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("POSIX file permission bits (0o600) are not enforced on Windows")
    monkeypatch.setattr("app.core.security.platform.system", lambda: "Linux")
    vault = SecretVault(base_dir=tmp_path)
    key_file = tmp_path / ".master.key"
    assert key_file.exists()
    import os, stat

    assert stat.S_IMODE(os.stat(key_file).st_mode) == 0o600
    vault.decrypt(vault.encrypt("mode-check"))  # key material is valid


def test_second_instantiation_reuses_same_key(tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.security.platform.system", lambda: "Linux")
    vault1 = SecretVault(base_dir=tmp_path)
    token = vault1.encrypt("hello-secret")
    vault2 = SecretVault(base_dir=tmp_path)
    assert vault2.decrypt(token) == "hello-secret"
    # second instantiation must not have overwritten the key file
    assert (tmp_path / ".master.key").exists()
    key1 = tmp_path / ".master.key"
    key1_bytes = key1.read_bytes()
    vault2 = SecretVault(base_dir=tmp_path)
    assert key1.read_bytes() == key1_bytes


def test_corrupt_key_file_raises_master_key_error_and_file_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.security.platform.system", lambda: "Linux")
    key_file = tmp_path / ".master.key"
    key_file.write_bytes(b"not-a-valid-fernet-key")
    before = key_file.read_bytes()
    with pytest.raises(MasterKeyError):
        SecretVault(base_dir=tmp_path)
    assert key_file.read_bytes() == before


def test_marker_present_with_no_key_raises_lost_key_error(tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.security.platform.system", lambda: "Linux")
    (tmp_path / ".master.key.expected").touch()
    with pytest.raises(MasterKeyError) as excinfo:
        SecretVault(base_dir=tmp_path)
    msg = str(excinfo.value)
    assert "lost" in msg.lower()
    assert "backup" in msg.lower() and "restore" in msg.lower()


def test_encrypt_decrypt_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.security.platform.system", lambda: "Linux")
    vault = SecretVault(base_dir=tmp_path)
    token = vault.encrypt("api-key-123")
    assert token and token != "api-key-123"
    assert vault.decrypt(token) == "api-key-123"
    assert vault.encrypt(None) is None
    assert vault.decrypt(None) is None


def test_decrypt_foreign_key_raises_secret_decrypt_error(tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.security.platform.system", lambda: "Linux")
    vault = SecretVault(base_dir=tmp_path)
    token = vault.encrypt("super-secret-ciphertext-xyz")
    other_dir = tmp_path / "other"
    other_vault = SecretVault(base_dir=other_dir)
    with pytest.raises(SecretDecryptError) as excinfo:
        other_vault.decrypt(token)
    assert "super-secret-ciphertext-xyz" not in str(excinfo.value)
    assert token not in str(excinfo.value)


def test_keychain_write_failure_falls_through_to_file_backend(tmp_path, monkeypatch):
    """Simulated `security add-generic-password` failure must fall back to the file
    backend and the persisted key must equal the key actually used."""
    calls = []

    class FakeCompleted:
        def __init__(self, returncode, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if "add-generic-password" in cmd:
            return FakeCompleted(45, stderr="SecKeychainSearch copy: The specified item already exists.")
        # any find-generic-password read fails too
        return FakeCompleted(44, stderr="The specified item could not be found.")

    monkeypatch.setattr("app.core.security.platform.system", lambda: "Darwin")
    monkeypatch.setattr(subprocess, "run", fake_run)
    vault = SecretVault(base_dir=tmp_path)
    assert any("add-generic-password" in c for c in calls), "keychain write should have been attempted"
    key_file = tmp_path / ".master.key"
    assert key_file.exists(), "must fall through to the file backend"
    persisted = key_file.read_bytes().strip()
    # the persisted key must be usable by this very vault (i.e. equal to the used key)
    assert vault.decrypt(vault.encrypt("roundtrip")) == "roundtrip"
    assert Fernet(persisted).decrypt(vault.encrypt("x"))  # same key material
