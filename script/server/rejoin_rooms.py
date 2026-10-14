#!/usr/bin/env python3
"""Récupère les memberships de @user après une désactivation/réactivation de
compte qui a fait quitter (leave) tous ses salons.

Utilise l'API admin Synapse pour rejoindre chaque salon où @user a un
historique de membership 'join' dans room_memberships, mais qui n'est plus
dans le state courant (donc plus visible côté /sync).

La liste des room_id à rejoindre doit être fournie via un fichier texte
(un room_id par ligne), généré depuis la requête SQL:

    sudo -u postgres psql synapse -tAc \\
      "SELECT DISTINCT room_id FROM room_memberships WHERE user_id = '@user:local.bridge' AND membership = 'join';" \\
      > /tmp/rooms_to_rejoin.txt

Par défaut tourne en DRY-RUN. Relancer avec --execute pour vraiment agir.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

HOMESERVER_URL = os.environ.get("MATRIX_HOMESERVER_URL", "http://localhost:8008")
ACCESS_TOKEN = os.environ["MATRIX_ACCESS_TOKEN"]
USER_ID = os.environ.get("MATRIX_USER_ID", "@user:local.bridge")

# Token du bot WhatsApp (obtenu via l'API admin /users/<id>/login), utilisé
# pour ré-inviter @user dans les salons où il n'est plus membre, avant que
# @user ne les rejoigne lui-même avec son propre token.
INVITER_ACCESS_TOKEN = os.environ["INVITER_ACCESS_TOKEN"]
INVITER_USER_ID = os.environ.get("INVITER_USER_ID", "@whatsappbot:local.bridge")

ROOM_LIST_FILE = os.environ.get("ROOM_LIST_FILE", "/tmp/rooms_to_rejoin.txt")
ROOM_PROCESSING_DELAY = float(os.environ.get("ROOM_PROCESSING_DELAY", "1"))

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("rejoin_rooms")


@dataclass
class MatrixClient:
    homeserver_url: str
    access_token: str
    user_id: str
    session: requests.Session = field(default_factory=requests.Session)

    def __post_init__(self) -> None:
        self.session.headers.update({"Authorization": f"Bearer {self.access_token}"})

    def _url(self, path: str) -> str:
        return f"{self.homeserver_url}{path}"

    def _request(self, method: str, url: str, max_retries: int = 5, **kwargs: Any) -> requests.Response:
        for attempt in range(max_retries + 1):
            resp = self.session.request(method, url, **kwargs)
            if resp.status_code != 429:
                return resp
            try:
                retry_after_ms = resp.json().get("retry_after_ms", 1000)
            except ValueError:
                retry_after_ms = 1000
            wait_s = max(retry_after_ms / 1000, 0.5)
            log.warning("  429, attente %.1fs (tentative %d/%d)", wait_s, attempt + 1, max_retries)
            time.sleep(wait_s)
        return resp

    def join_room(self, room_id: str) -> requests.Response:
        return self._request("POST", self._url(f"/_matrix/client/v3/join/{room_id}"))

    def invite(self, room_id: str, user_id: str) -> requests.Response:
        return self._request(
            "POST",
            self._url(f"/_matrix/client/v3/rooms/{room_id}/invite"),
            json={"user_id": user_id},
        )


def load_room_list(path: str) -> list[str]:
    p = Path(path)
    if not p.exists():
        log.error("Fichier introuvable: %s", path)
        log.error("Génère-le d'abord avec la requête SQL documentée en haut de ce script.")
        sys.exit(1)
    rooms = [line.strip() for line in p.read_text().splitlines() if line.strip()]
    return rooms


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute", action="store_true",
        help="Exécute réellement les join. Sans ce flag, mode dry-run.",
    )
    args = parser.parse_args()

    if not args.execute:
        log.info("=== MODE DRY-RUN — aucune commande ne sera envoyée. Utilise --execute pour agir. ===")

    room_ids = load_room_list(ROOM_LIST_FILE)
    log.info("%d salons à rejoindre trouvés dans %s.", len(room_ids), ROOM_LIST_FILE)

    client = MatrixClient(HOMESERVER_URL, ACCESS_TOKEN, USER_ID)
    inviter = MatrixClient(HOMESERVER_URL, INVITER_ACCESS_TOKEN, INVITER_USER_ID)

    succeeded = 0
    failed: list[tuple[str, str]] = []

    for i, room_id in enumerate(room_ids):
        if i > 0:
            time.sleep(ROOM_PROCESSING_DELAY)

        if not args.execute:
            log.info("[DRY-RUN] Inviterait puis rejoindrait %s", room_id)
            continue

        # 1. Le bot WhatsApp (toujours membre de la plupart des salons)
        #    invite @user. Si @user est déjà invité ou déjà membre,
        #    ce n'est pas bloquant, on continue quand même vers le join.
        try:
            inv_resp = inviter.invite(room_id, USER_ID)
            if inv_resp.status_code not in (200,) and "already in the room" not in inv_resp.text.lower() and "already invited" not in inv_resp.text.lower():
                log.warning("  Invite échouée (%d) sur %s: %s", inv_resp.status_code, room_id, inv_resp.text[:150])
        except requests.RequestException as e:
            log.warning("  Erreur réseau lors de l'invite sur %s: %s", room_id, e)

        # 2. @user rejoint le salon avec son propre token.
        try:
            resp = client.join_room(room_id)
            if resp.status_code == 200:
                log.info("  OK: %s", room_id)
                succeeded += 1
            else:
                log.error("  Échec (%d): %s -- %s", resp.status_code, room_id, resp.text[:200])
                failed.append((room_id, f"{resp.status_code}: {resp.text[:200]}"))
        except requests.RequestException as e:
            log.error("  Erreur réseau: %s -- %s", room_id, e)
            failed.append((room_id, str(e)))

    if args.execute:
        log.info("Terminé: %d réussis, %d échoués sur %d.", succeeded, len(failed), len(room_ids))
        if failed:
            log.info("Salons en échec:")
            for room_id, reason in failed:
                log.info("  - %s: %s", room_id, reason)


if __name__ == "__main__":
    main()
