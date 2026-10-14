#!/usr/bin/env python3
"""Parcourt tous les salons existants où @user est membre, détecte ceux qui
ont le bot Telegram (@telegrambot) dedans, et les dé-bridge avec `!tg unbridge`.

Par défaut tourne en DRY-RUN (n'envoie aucune commande, liste juste ce qui
serait fait). Relancer avec --execute pour vraiment agir.

La commande `!tg unbridge` demande une confirmation (retaper la commande une
seconde fois). Ce script gère ça automatiquement en envoyant la commande deux
fois de suite avec un court délai, et vérifie la réponse du bot à chaque étape.
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

TELEGRAM_BOT_MXID = os.environ.get("TELEGRAM_BOT_MXID", "@telegrambot:local.bridge")
WHATSAPP_BOT_MXID = os.environ.get("WHATSAPP_BOT_MXID", "@whatsappbot:local.bridge")

BOT_RESPONSE_TIMEOUT = int(os.environ.get("BOT_RESPONSE_TIMEOUT", "20"))
ROOM_PROCESSING_DELAY = float(os.environ.get("ROOM_PROCESSING_DELAY", "2"))
CONFIRM_DELAY = float(os.environ.get("CONFIRM_DELAY", "2"))

ERROR_KEYWORDS = ("error", "failed", "⚠", "not found", "invalid")

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("unbridge_telegram")


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

    def send_message(self, room_id: str, body: str) -> str:
        txn_id = str(int(time.time() * 1000))
        resp = self._request(
            "PUT",
            self._url(f"/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn_id}"),
            json={"msgtype": "m.text", "body": body},
        )
        resp.raise_for_status()
        return resp.json()["event_id"]

    def sync(self, since: str | None = None, timeout_ms: int = 0) -> dict[str, Any]:
        params: dict[str, Any] = {"timeout": timeout_ms}
        if since:
            params["since"] = since
        resp = self._request("GET", self._url("/_matrix/client/v3/sync"), params=params, timeout=(timeout_ms / 1000) + 15)
        resp.raise_for_status()
        return resp.json()


def wait_for_bot_reply(
    client: MatrixClient,
    room_id: str,
    expected_sender: str,
    after_sync_token: str,
    timeout: int = BOT_RESPONSE_TIMEOUT,
) -> tuple[str, str | None]:
    """Retourne (nouveau sync_token, corps du message du bot ou None si timeout)."""
    deadline = time.monotonic() + timeout
    since = after_sync_token

    while time.monotonic() < deadline:
        remaining_ms = max(1000, int((deadline - time.monotonic()) * 1000))
        data = client.sync(since=since, timeout_ms=min(remaining_ms, 10_000))
        since = data.get("next_batch", since)

        room_data = data.get("rooms", {}).get("join", {}).get(room_id)
        if room_data:
            for evt in room_data.get("timeline", {}).get("events", []):
                if evt.get("type") != "m.room.message":
                    continue
                if evt.get("sender") != expected_sender:
                    continue
                body = evt.get("content", {}).get("body", "")
                return since, body

    return since, None


def unbridge_room(client: MatrixClient, room_id: str, room_name: str, sync_token: str, execute: bool) -> str:
    if not execute:
        log.info("[DRY-RUN] Dé-bridgerait Telegram dans %s (%s)", room_name, room_id)
        return sync_token

    log.info("Dé-bridge Telegram dans %s (%s)", room_name, room_id)

    log.info("  -> !tg unbridge (1ère fois, demande de confirmation attendue)")
    client.send_message(room_id, "!tg unbridge")
    sync_token, body = wait_for_bot_reply(client, room_id, TELEGRAM_BOT_MXID, sync_token)
    if body is None:
        log.error("  Pas de réponse du bot Telegram après !tg unbridge, on passe au salon suivant.")
        return sync_token
    log.info("  Réponse: %s", body.strip()[:200])

    if any(kw in body.lower() for kw in ERROR_KEYWORDS):
        log.error("  Le bot a répondu avec une erreur, on passe au salon suivant.")
        return sync_token

    time.sleep(CONFIRM_DELAY)

    log.info("  -> !tg confirm-unbridge (confirmation)")
    client.send_message(room_id, "!tg confirm-unbridge")
    sync_token, body = wait_for_bot_reply(client, room_id, TELEGRAM_BOT_MXID, sync_token)
    if body is None:
        log.info(
            "  Pas de message texte après confirmation — probablement un succès "
            "(le bot et les ghosts Telegram quittent le salon sans forcément "
            "envoyer de message texte). Vérifie manuellement si besoin."
        )
    else:
        log.info("  Réponse: %s", body.strip()[:200])

    return sync_token


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute", action="store_true",
        help="Exécute réellement les unbridge. Sans ce flag, mode dry-run (rien n'est envoyé).",
    )
    args = parser.parse_args()

    if not args.execute:
        log.info("=== MODE DRY-RUN — aucune commande ne sera envoyée. Utilise --execute pour agir. ===")

    client = MatrixClient(HOMESERVER_URL, ACCESS_TOKEN, USER_ID)

    log.info("Récupération de la liste des salons...")
    room_ids = client.get_joined_rooms()
    log.info("%d salons trouvés, analyse en cours...", len(room_ids))

    initial_sync = client.sync(since=None, timeout_ms=0)
    sync_token = initial_sync["next_batch"]

    to_unbridge: list[tuple[str, str]] = []
    for room_id in room_ids:
        try:
            members = set(client.get_room_members(room_id))
        except requests.RequestException as e:
            log.warning("Impossible de lire les membres de %s: %s", room_id, e)
            continue
        if TELEGRAM_BOT_MXID in members and WHATSAPP_BOT_MXID in members:
            room_name = client.get_room_name(room_id) or room_id
            to_unbridge.append((room_id, room_name))

    log.info("%d salons WhatsApp<->Telegram (ponts) détectés.", len(to_unbridge))
    for room_id, room_name in to_unbridge:
        log.info("  - %s (%s)", room_name, room_id)

    for i, (room_id, room_name) in enumerate(to_unbridge):
        if i > 0:
            time.sleep(ROOM_PROCESSING_DELAY)
        try:
            sync_token = unbridge_room(client, room_id, room_name, sync_token, args.execute)
        except requests.RequestException as e:
            log.error("Erreur réseau sur %s: %s", room_id, e)

    log.info("Terminé.")


if __name__ == "__main__":
    main()
