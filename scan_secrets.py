#!/usr/bin/env python3
"""Scanne un dossier à la recherche de secrets avant un premier push git.

Usage:
    python3 scan_secrets.py [dossier]        # défaut: .

Code de sortie 0 = rien trouvé, 1 = secrets trouvés (ne pas push).
Ignore .git/ et les fichiers binaires. Affiche uniquement fichier:ligne et
le type de secret, jamais la valeur complète.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("token Matrix (syt_)", re.compile(r"syt_[A-Za-z0-9_\-]{8,}")),
    ("mot de passe dans DSN postgres", re.compile(r"password=[^\s\"']{6,}")),
    ("as_token / hs_token", re.compile(r"\b(as_token|hs_token)\s*[:=]\s*\S{8,}")),
    ("secret Synapse", re.compile(
        r"\b(registration_shared_secret|macaroon_secret_key|form_secret)\s*[:=]\s*[\"']?[^\s\"']{8,}")),
    ("mot de passe en clair", re.compile(r"\b(password|passwd|pw)\s*[:=]\s*[\"'][^\"']{4,}[\"']", re.I)),
    ("clé privée", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("token GitHub", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
]

# Valeurs qui ressemblent à un secret mais n'en sont pas (placeholders)
SAFE_HINTS = ("CHANGE_ME", "changeme", "your_", "<", "${", "os.environ", "strenv(", "$(ask ", "example", "xxxx")

SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules"}
MAX_SIZE = 2_000_000


def is_text(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return b"\0" not in f.read(2048)
    except OSError:
        return False


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    hits = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or SKIP_DIRS & set(path.parts):
            continue
        if path.stat().st_size > MAX_SIZE or not is_text(path):
            continue
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            if any(h in line for h in SAFE_HINTS):
                continue
            for label, pat in PATTERNS:
                if pat.search(line):
                    hits += 1
                    print(f"{path}:{i}: {label}")
                    break

    if hits:
        print(f"\n{hits} occurrence(s) suspecte(s). NE PAS push avant de les corriger.")
        return 1
    print("Rien de suspect trouvé.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
