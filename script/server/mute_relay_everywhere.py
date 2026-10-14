#!/usr/bin/env python3
"""Parcourt tous les salons WhatsApp existants (avec @whatsappbot) où le
compte relais Signal dédié (ghost) est membre, et fusionne son power level à
-1 dans m.room.power_levels — pour l'empêcher d'écrire dans Element tout en
le laissant membre du salon.

Salon sans @whatsappbot -> ignoré (pas un salon WhatsApp).
Salon avec @whatsappbot mais sans le ghost relais -> ignoré (rien à patcher).
Salon avec @whatsappbot ET le ghost relais, déjà à -1 -> ignoré (déjà bon).
Salon avec @whatsappbot ET le ghost relais, à un autre niveau -> patché.

Par défaut tourne en DRY-RUN (liste les salons qui seraient patchés sans
rien envoyer). Relancer avec --execute pour vraiment agir.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import requests

HOMESERVER_URL = os.environ.get("MATRIX_HOMESERVER_URL", "http://localhost:8008")
ACCESS_TOKEN = os.environ["MATRIX_ACCESS_TOKEN"]
USER_ID = os.environ.get("MATRIX_USER_ID", "@user:local.bridge")

WHATSAPP_BOT_MXID = os.environ.get("WHATSAPP_BOT_MXID", "@whatsappbot:local.bridge")

SIGNAL_RELAY_LOGIN_ID = os.environ["SIGNAL_RELAY_LOGIN_ID"]
SIGNAL_RELAY_GHOST_MXID = os.environ.get(
    "SIGNAL_RELAY_GHOST_MXID", f"@signal_{SIGNAL_RELAY_LOGIN_ID}:local.bridge"
)

MUTE_LEVEL = int(os.environ.get("MUTE_LEVEL", "-1"))

ROOM_PROCESSING_DELAY = float(os.environ.get("ROOM_PROCESSING_DELAY", "0.5"))

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("mute_relay_everywhere")


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

    def get_joined_rooms(self) -> list[str]:
        resp = self._request("GET", self._url("/_matrix/client/v3/joined_rooms"))
        resp.raise_for_status()
        return resp.json().get("joined_rooms", [])

    def get_room_members(self, room_id: str) -> list[str]:
        resp = self._request("GET", self._url(f"/_matrix/client/v3/rooms/{room_id}/joined_members"))
        resp.raise_for_status()
        return list(resp.json().get("joined", {}).keys())

    def get_room_name(self, room_id: str) -> str | None:
        resp = self._request("GET", self._url(f"/_matrix/client/v3/rooms/{room_id}/state/m.room.name/"))
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json().get("name")

    def get_power_levels(self, room_id: str) -> dict[str, Any]:
        resp = self._request("GET", self._url(f"/_matrix/client/v3/rooms/{room_id}/state/m.room.power_levels/"))
        resp.raise_for_status()
        return resp.json()

    def set_power_levels(self, room_id: str, content: dict[str, Any]) -> None:
        resp = self._request(
            "PUT",
            self._url(f"/_matrix/client/v3/rooms/{room_id}/state/m.room.power_levels/"),
            json=content,
        )
        resp.raise_for_status()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute", action="store_true",
        help="Exécute réellement les patchs. Sans ce flag, mode dry-run.",
    )
    args = parser.parse_args()

    if not args.execute:
        log.info("=== MODE DRY-RUN — aucune commande ne sera envoyée. Utilise --execute pour agir. ===")

    client = MatrixClient(HOMESERVER_URL, ACCESS_TOKEN, USER_ID)

    log.info("Récupération de la liste des salons...")
    room_ids = client.get_joined_rooms()
    log.info("%d salons trouvés, analyse en cours...", len(room_ids))

    to_patch: list[tuple[str, str, int | None]] = []  # (room_id, room_name, current_level)
    skipped_no_ghost = 0
    skipped_not_whatsapp = 0
    skipped_already_muted = 0

    for room_id in room_ids:
        try:
            members = set(client.get_room_members(room_id))
        except requests.RequestException as e:
            log.warning("Impossible de lire les membres de %s: %s", room_id, e)
            continue

        if WHATSAPP_BOT_MXID not in members:
            skipped_not_whatsapp += 1
            continue

        if SIGNAL_RELAY_GHOST_MXID not in members:
            skipped_no_ghost += 1
            continue

        try:
            pl = client.get_power_levels(room_id)
        except requests.RequestException as e:
            log.warning("Impossible de lire les power levels de %s: %s", room_id, e)
            continue

        current_level = pl.get("users", {}).get(SIGNAL_RELAY_GHOST_MXID)
        if current_level is not None and current_level <= MUTE_LEVEL:
            skipped_already_muted += 1
            continue

        room_name = client.get_room_name(room_id) or room_id
        to_patch.append((room_id, room_name, current_level))

    log.info(
        "Résumé: %d salons non-WhatsApp ignorés, %d salons sans le relais ignorés, "
        "%d déjà mutés, %d à patcher.",
        skipped_not_whatsapp, skipped_no_ghost, skipped_already_muted, len(to_patch),
    )
    for room_id, room_name, current_level in to_patch:
        log.info("  - %s (%s) — niveau actuel: %s", room_name, room_id, current_level)

    if not args.execute:
        log.info("[DRY-RUN] Rien n'a été envoyé. Relance avec --execute pour patcher.")
        return

    patched = 0
    failed: list[tuple[str, str]] = []

    for i, (room_id, room_name, _current_level) in enumerate(to_patch):
        if i > 0:
            time.sleep(ROOM_PROCESSING_DELAY)
        try:
            pl = client.get_power_levels(room_id)
            pl.setdefault("users", {})[SIGNAL_RELAY_GHOST_MXID] = MUTE_LEVEL
            client.set_power_levels(room_id, pl)
            log.info("  OK: %s (%s) -> %d", room_name, room_id, MUTE_LEVEL)
            patched += 1
        except requests.RequestException as e:
            log.error("  Échec sur %s (%s): %s", room_name, room_id, e)
            failed.append((room_id, str(e)))

    log.info("Terminé: %d salons patchés, %d échecs.", patched, len(failed))
    if failed:
        log.info("Salons en échec:")
        for room_id, reason in failed:
            log.info("  - %s: %s", room_id, reason)


if __name__ == "__main__":
    main()
