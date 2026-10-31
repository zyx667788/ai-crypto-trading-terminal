"""
Pre-publish secret scan.

Anchors: this repository is about to be pushed to a PUBLIC GitHub repository.
We scan (a) every tracked file in the working tree and (b) every blob in the entire
git history, for anything that would leak credentials.

Exit code 0 = safe to publish, 1 = leak found (do NOT push).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Concrete values that must never appear anywhere.
# Supplied at run time so this scanner never becomes the leak it is guarding against:
#   set PUBLISH_SCAN_SECRETS="value1;value2"  (or pass --secret value repeatedly)
KNOWN_SECRETS: list[str] = [
    s.strip()
    for s in os.environ.get("PUBLISH_SCAN_SECRETS", "").split(";")
    if len(s.strip()) >= 12
]

# Generic patterns.
PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "OpenAI-style API key"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "GitHub token"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key id"),
    (re.compile(r"-----BEGIN (RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"), "private key block"),
    (re.compile(r"(?i)(api[_-]?secret|api[_-]?key|passphrase|password)\s*[:=]\s*['\"][^'\"\s]{16,}['\"]"), "hardcoded credential"),
]

# Paths that must never be tracked (exact match unless the entry ends with "/").
FORBIDDEN_PATHS = [".env", ".master.key", ".master.key/", "data/backups/"]
FORBIDDEN_SUFFIXES = (".db", ".db-wal", ".db-shm", ".enc", ".pem", ".key")

ALLOWLIST_SUBSTRINGS = [
    "AI_API_KEY=",          # .env.example placeholder line
    "sk-test-not-a-real-key",
    "sk-super-secret-value",
    "AKIAIOSFODNN7EXAMPLE",
    "SIMULATED-DEMO-",      # explicit non-secret placeholders for the simulators
]


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        check=False,
    ).stdout


def scan_text(where: str, text: str, hits: list[str]) -> None:
    for secret in KNOWN_SECRETS:
        if secret in text:
            hits.append(f"{where}: KNOWN SECRET leaked")
    for pattern, label in PATTERNS:
        for m in pattern.finditer(text):
            snippet = m.group(0)
            if any(a in snippet for a in ALLOWLIST_SUBSTRINGS):
                continue
            hits.append(f"{where}: {label} -> {snippet[:24]}…")


def main() -> int:
    hits: list[str] = []
    print("=" * 70)
    print("PRE-PUBLISH SECRET SCAN")
    print("=" * 70)

    # 1) Tracked paths
    tracked = [p for p in git("ls-files").splitlines() if p.strip()]
    print(f"tracked files: {len(tracked)}")
    for path in tracked:
        for forbidden in FORBIDDEN_PATHS:
            if forbidden.endswith("/"):
                if path.startswith(forbidden):
                    hits.append(f"TRACKED FORBIDDEN PATH: {path}")
            elif path == forbidden:
                hits.append(f"TRACKED FORBIDDEN PATH: {path}")
        if path.lower().endswith(FORBIDDEN_SUFFIXES) and not path.endswith(".env.example"):
            hits.append(f"TRACKED FORBIDDEN SUFFIX: {path}")

    # 2) Working tree contents
    scanned = 0
    for path in tracked:
        fp = ROOT / path
        if not fp.is_file():
            continue
        try:
            data = fp.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        scanned += 1
        scan_text(path, data, hits)
    print(f"scanned contents: {scanned}")

    # 3) Whole git history (every blob ever committed)
    revs = git("rev-list", "--all").split()
    print(f"commits: {len(revs)}")
    blob_ids = set()
    for rev in revs:
        for line in git("ls-tree", "-r", "--format=%(objectname)", rev).splitlines():
            if line.strip():
                blob_ids.add(line.strip())
    print(f"history blobs: {len(blob_ids)}")
    for blob in blob_ids:
        content = subprocess.run(
            ["git", "cat-file", "-p", blob],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
        ).stdout or ""
        scan_text(f"blob {blob[:10]}", content, hits)

    print("-" * 70)
    if hits:
        unique = sorted(set(hits))
        print(f"RESULT: {len(unique)} POTENTIAL LEAK(S) — DO NOT PUBLISH")
        for h in unique[:40]:
            print("  -", h)
        return 1

    print("RESULT: CLEAN — no secrets found in working tree or history")
    return 0


if __name__ == "__main__":
    sys.exit(main())
