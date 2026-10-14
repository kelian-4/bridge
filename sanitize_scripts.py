#!/usr/bin/env python3
"""Remplace les secrets codés en dur dans script/*.py par des lectures de
variables d'environnement.

Cas traités:
  ACCESS_TOKEN = "syt_..."                      -> os.environ["MATRIX_ACCESS_TOKEN"]
  os.environ.get("X", "syt_...")                -> os.environ["X"]   (aussi sur plusieurs lignes)
  PG_DSN = "dbname=... password=..."            -> os.environ["PG_DSN"]

Par défaut: DRY-RUN (affiche ce qui changerait). --execute pour écrire.
Un fichier .bak est créé à côté de chaque fichier modifié (à ne PAS commit,
il est dans le .gitignore fourni).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

RULES: list[tuple[str, re.Pattern[str], str]] = [
    (
        "ACCESS_TOKEN littéral",
        re.compile(r'^ACCESS_TOKEN\s*=\s*"syt_[^"]+"', re.M),
        'ACCESS_TOKEN = os.environ["MATRIX_ACCESS_TOKEN"]',
    ),
    (
        "os.environ.get avec token par défaut",
        re.compile(r'os\.environ\.get\(\s*"(\w+)"\s*,\s*"syt_[^"]+"\s*\)'),
        r'os.environ["\1"]',
    ),
    (
        "PG_DSN avec mot de passe",
        re.compile(r'^PG_DSN\s*=\s*".*"', re.M),
        'PG_DSN = os.environ["PG_DSN"]',
    ),
]

LEFTOVER = re.compile(r"syt_[A-Za-z0-9_]{8,}")


def ensure_import_os(text: str) -> str:
    if re.search(r"^import os\b", text, re.M):
        return text
    m = re.search(r"^import \w+", text, re.M)
    if not m:
        return "import os\n" + text
    return text[: m.start()] + "import os\n" + text[m.start():]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("directory", nargs="?", default="script")
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    root = Path(args.directory)
    if not root.is_dir():
        print(f"Dossier introuvable: {root}")
        return 1

    if not args.execute:
        print("=== DRY-RUN: rien n'est écrit. Utilise --execute pour appliquer. ===")

    bad = 0
    for path in sorted(root.glob("*.py")):
        original = path.read_text()
        text = original
        applied: list[str] = []
        for label, pattern, repl in RULES:
            text, n = pattern.subn(repl, text)
            if n:
                applied.append(f"{label} x{n}")
        if applied:
            text = ensure_import_os(text)
            print(f"[MODIF] {path}: " + ", ".join(applied))
            if args.execute:
                path.with_suffix(path.suffix + ".bak").write_text(original)
                path.write_text(text)
        left = LEFTOVER.findall(text)
        if left:
            bad += 1
            print(f"[ATTENTION] {path}: il reste {len(left)} token(s) syt_ non traité(s), à retirer à la main")

    if bad:
        print(f"{bad} fichier(s) à vérifier manuellement.")
        return 2
    print("Terminé.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
