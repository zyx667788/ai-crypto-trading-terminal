from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path
from typing import Any
from cryptography.fernet import Fernet

from app.core.config import DATA_DIR, settings


SENSITIVE_KEY_SUBSTRINGS = (
    "secret",
    "passphrase",
    "password",
    "private_key",
    "signature",
    "api_key",
    "apikey",
    "token",
    "encrypted_key",
    "encrypted_secret",
    "encrypted_passphrase",
)


def mask_secret(value: str | None) -> str:
    if not value:
        return "****"
    cleaned = value.strip()
    if len(cleaned) <= 8:
        return f"{cleaned[:2]}****"
    return f"{cleaned[:4]}****{cleaned[-4:]}"


def redact_sensitive_data(data: Any) -> Any:
    """Recursively scrub secrets from dicts, lists, and strings before logging, DB snapshots, or AI prompts."""
    if isinstance(data, dict):
        sanitized: dict[str, Any] = {}
        for k, v in data.items():
            key_lower = str(k).lower()
            if any(marker in key_lower for marker in SENSITIVE_KEY_SUBSTRINGS):
                if key_lower.endswith("_masked"):
                    sanitized[k] = v
                else:
                    sanitized[k] = "[REDACTED]"
            else:
                sanitized[k] = redact_sensitive_data(v)
        return sanitized
    if isinstance(data, list):
        return [redact_sensitive_data(item) for item in data]
    if isinstance(data, tuple):
        return tuple(redact_sensitive_data(item) for item in data)
    return data


class SecretVault:
    """
    Encrypts exchange API credentials at rest.
    Priority:
    1. Environment override TERMINAL_MASTER_KEY (for deterministic testing)
    2. macOS Keychain via `/usr/bin/security` when running on Darwin
    3. Local restricted key file in DATA_DIR / '.master.key'
    """

    def __init__(self) -> None:
        self._fernet = Fernet(self._load_or_create_master_key())

    @staticmethod
    def _validate_key(key: bytes, source: str) -> bytes:
        """
        Fail loudly and actionably on a malformed master key.

        A silently regenerated key would make every stored API secret permanently
        undecryptable, so a bad key must never be ignored.
        """
        try:
            Fernet(key)
        except Exception as exc:
            raise ValueError(
                f"Invalid {source}: a Fernet master key must be 32 url-safe base64-encoded bytes. "
                f"Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            ) from exc
        return key

    def _load_or_create_master_key(self) -> bytes:
        env_key = os.environ.get("TERMINAL_MASTER_KEY")
        if env_key:
            return self._validate_key(env_key.encode("utf-8"), "TERMINAL_MASTER_KEY")

        if platform.system() == "Darwin":
            try:
                res = subprocess.run(
                    [
                        "/usr/bin/security",
                        "find-generic-password",
                        "-s",
                        settings.KEYCHAIN_SERVICE_NAME,
                        "-a",
                        settings.KEYCHAIN_ACCOUNT_NAME,
                        "-w",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                if res.returncode == 0 and res.stdout.strip():
                    return self._validate_key(res.stdout.strip().encode("utf-8"), "macOS Keychain entry")

                new_key = Fernet.generate_key().decode("utf-8")
                subprocess.run(
                    [
                        "/usr/bin/security",
                        "add-generic-password",
                        "-U",
                        "-s",
                        settings.KEYCHAIN_SERVICE_NAME,
                        "-a",
                        settings.KEYCHAIN_ACCOUNT_NAME,
                        "-w",
                        new_key,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                return new_key.encode("utf-8")
            except Exception:
                pass

        key_file: Path = DATA_DIR / ".master.key"
        if key_file.exists():
            raw = key_file.read_bytes().strip()
            if raw:
                return raw
        generated = Fernet.generate_key()
        key_file.write_bytes(generated)
        return generated

    def encrypt(self, plaintext: str | None) -> str | None:
        if not plaintext:
            return None
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, ciphertext: str | None) -> str | None:
        if not ciphertext:
            return None
        return self._fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")


secret_vault = SecretVault()
