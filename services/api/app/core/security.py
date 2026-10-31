from __future__ import annotations

import logging
import os
import platform
import stat
import subprocess
from pathlib import Path
from typing import Any
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import DATA_DIR, settings

logger = logging.getLogger(__name__)


class MasterKeyError(RuntimeError):
    """Raised when the master key is missing, corrupt, or has been lost."""


class SecretDecryptError(ValueError):
    """Raised when a ciphertext cannot be decrypted. Never carries the ciphertext."""


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

    MASTER_KEY_FILENAME = ".master.key"
    MARKER_FILENAME = ".master.key.expected"

    def __init__(self, base_dir: Path | None = None) -> None:
        self._base_dir = Path(base_dir) if base_dir is not None else DATA_DIR
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

        key_file: Path = self._base_dir / self.MASTER_KEY_FILENAME
        marker_file: Path = self._base_dir / self.MARKER_FILENAME

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
                write_res = subprocess.run(
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
                if write_res.returncode != 0:
                    logger.warning(
                        "Keychain write failed (returncode=%s): %s; falling back to key file backend",
                        write_res.returncode,
                        write_res.stderr,
                    )
                else:
                    read_res = subprocess.run(
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
                    read_back = read_res.stdout.strip() if read_res.returncode == 0 else None
                    if read_back == new_key:
                        return self._persist_marker(new_key.encode("utf-8"), marker_file)
                    logger.warning(
                        "Keychain read-back verification failed (returncode=%s): %s; "
                        "falling back to key file backend",
                        read_res.returncode,
                        read_res.stderr,
                    )
            except Exception as exc:
                logger.warning("Keychain backend unavailable: %s; falling back to key file backend", exc)

        if key_file.exists():
            try:
                raw = key_file.read_bytes().strip()
                if not raw:
                    raise MasterKeyError(
                        f"Master key file {key_file} is empty or corrupt. "
                        "Restore it from a backup; it must not be regenerated."
                    )
                return self._validate_key(raw, f"master key file {key_file}")
            except MasterKeyError:
                raise
            except (OSError, ValueError) as exc:
                raise MasterKeyError(
                    f"Master key file {key_file} is corrupt ({exc}). "
                    "Restore it from a backup; it was left unchanged."
                ) from exc

        if marker_file.exists():
            raise MasterKeyError(
                "The master key was lost: no key found in TERMINAL_MASTER_KEY, the macOS Keychain, "
                f"or {key_file}, but encrypted secrets exist (marker {marker_file} present). "
                "Existing encrypted secrets cannot be decrypted without the original key; "
                "restore the master key from a backup before starting. "
                "A new key will NOT be silently generated, because that would leave all "
                "previously encrypted credentials permanently undecryptable."
            )

        return self._create_key_file(key_file, marker_file)

    def _create_key_file(self, key_file: Path, marker_file: Path) -> bytes:
        """Persist a freshly generated key to a 0600 file, atomically, without overwriting."""
        generated = Fernet.generate_key()
        self._base_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(key_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, generated)
        finally:
            os.close(fd)
        os.chmod(key_file, 0o600)
        if os.name == "posix":
            mode = stat.S_IMODE(os.stat(key_file).st_mode)
            if mode != 0o600:
                raise MasterKeyError(
                    f"Master key file {key_file} has mode {oct(mode)} instead of 0o600; refusing to store a key there."
                )
        return self._persist_marker(generated, marker_file)

    def _persist_marker(self, key: bytes, marker_file: Path) -> bytes:
        """After the first successful persist, drop an empty marker recording that secrets exist."""
        try:
            marker_file.touch(exist_ok=True)
        except OSError as exc:
            logger.warning("Could not create master-key marker %s: %s", marker_file, exc)
        return key

    def encrypt(self, plaintext: str | None) -> str | None:
        if not plaintext:
            return None
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, ciphertext: str | None) -> str | None:
        if not ciphertext:
            return None
        try:
            return self._fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise SecretDecryptError(
                "Decryption failed: the ciphertext is invalid or was encrypted with a different master key."
            ) from exc


secret_vault = SecretVault()
