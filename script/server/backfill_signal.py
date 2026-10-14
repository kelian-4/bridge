#!/usr/bin/env python3
"""Parcourt tous les salons WhatsApp existants (avec @whatsappbot) et corrige
leur état Signal:

- Salon SANS @signalbot du tout -> flow complet: invite signalbot, wa
  set-relay, invite le ghost du compte relais dédié, create-group,
  signal set-relay.
- Salon AVEC @signalbot mais SANS le ghost du compte relais dédié -> invite
  le ghost, puis signal set-relay (le groupe existe déjà, pas besoin de le
  recréer).
- Salon avec @signalbot ET le ghost déjà présents -> rien à faire, ignoré.

Par défaut tourne en DRY-RUN (liste les actions prévues sans les exécuter).
Relancer avec --execute pour vraiment agir.
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
SIGNAL_BOT_MXID = os.environ.get("SIGNAL_BOT_MXID", "@signalbot:local.bridge")

SIGNAL_RELAY_LOGIN_ID = os.environ["SIGNAL_RELAY_LOGIN_ID"]
SIGNAL_RELAY_GHOST_MXID = os.environ.get(
    "SIGNAL_RELAY_GHOST_MXID", f"@signal_{SIGNAL_RELAY_LOGIN_ID}:local.bridge"
)

BOT_RESPONSE_TIMEOUT = int(os.environ.get("BOT_RESPONSE_TIMEOUT", "30"))
ROOM_PROCESSING_DELAY = float(os.environ.get("ROOM_PROCESSING_DELAY", "2"))

ERROR_KEYWORDS = ("error", "failed", "⚠", "not found", "invalid")
CREATE_GROUP_SUCCESS_MARKER = "successfully created group"

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("backfill_signal")


class BridgeCommandError(Exception):
    pass


class BridgeCommandTimeout(Exception):
    pass


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

    def invite(self, room_id: str, user_id: str) -> None:
        resp = self._request(
            "POST",
            self._url(f"/_matrix/client/v3/rooms/{room_id}/invite"),
            json={"user_id": user_id},
        )
        if resp.status_code == 400 and "already in the room" in resp.text.lower():
            log.info("  %s est déjà dans le salon, on continue.", user_id)
            return
        if resp.status_code == 403 and "already invited" in resp.text.lower():
            log.info("  %s est déjà invité, on continue.", user_id)
            return
        resp.raise_for_status()

    def send_message(self, room_id: str, body: str) -> str:
        txn_id = str(int(time.time() * 1000))
        resp = self._request(
            "PUT",
            self._url(f"/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn_id}"),
            json={"msgtype": "m.text", "body": body},
        )
        resp.raise_for_status()
        return resp.json()["event_id"]

    def get_power_levels(self, room_id: str) -> dict[str, Any]:
        resp = self._request("GET", self._url(f"/_matrix/client/v3/rooms/{room_id}/state/m.room.power_levels/"))
        resp.raise_for_status()
        return resp.json()

    def set_power_level(self, room_id: str, user_id: str, level: int) -> None:
        content = self.get_power_levels(room_id)
        users = content.setdefault("users", {})
        users[user_id] = level
        put = self._request(
            "PUT",
            self._url(f"/_matrix/client/v3/rooms/{room_id}/state/m.room.power_levels/"),
            json=content,
        )
        put.raise_for_status()

    def sync(self, since: str | None = None, timeout_ms: int = 30_000) -> dict[str, Any]:
        params: dict[str, Any] = {"timeout": timeout_ms, "set_presence": "online"}
        if since:
            params["since"] = since
        resp = self._request(
            "GET", self._url("/_matrix/client/v3/sync"), params=params, timeout=(timeout_ms / 1000) + 15
        )
        resp.raise_for_status()
        return resp.json()


def wait_for_bot_reply(
    client: MatrixClient,
    room_id: str,
    expected_sender: str,
    after_sync_token: str,
    timeout: int = BOT_RESPONSE_TIMEOUT,
    reply_required: bool = True,
    require_substring: str | None = None,
) -> str:
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
                log.info("  Réponse de %s: %s", expected_sender, body.strip()[:200])
                lowered = body.lower()
                if any(kw in lowered for kw in ERROR_KEYWORDS):
                    raise BridgeCommandError(f"{expected_sender} a répondu avec une erreur: {body}")
                if require_substring and require_substring.lower() not in lowered:
                    continue
                return since

    if reply_required:
        raise BridgeCommandTimeout(f"Pas de réponse valide de {expected_sender} dans {room_id} après {timeout}s")
    return since


def run_bridge_command(
    client: MatrixClient,
    room_id: str,
    command: str,
    expected_sender: str,
    sync_token: str,
    reply_required: bool = True,
    require_substring: str | None = None,
) -> str:
    log.info("  -> %s", command)
    client.send_message(room_id, command)
    return wait_for_bot_reply(
        client, room_id, expected_sender, sync_token,
        reply_required=reply_required, require_substring=require_substring,
    )


def full_signal_setup(client: MatrixClient, room_id: str, room_name: str, sync_token: str) -> str:
    """Salon sans @signalbot du tout: flow complet."""
    log.info("  [Cas A] Aucun Signal -> flow complet")

    log.info("  Invitation de %s", SIGNAL_BOT_MXID)
    client.invite(room_id, SIGNAL_BOT_MXID)

    bot_deadline = time.monotonic() + 20
    while time.monotonic() < bot_deadline:
        members = set(client.get_room_members(room_id))
        if SIGNAL_BOT_MXID in members:
            break
        time.sleep(1)
    else:
        raise BridgeCommandTimeout(f"{SIGNAL_BOT_MXID} n'a pas rejoint {room_id} après 20s, abandon.")

    log.info("  Attribution du power level 100 à %s", SIGNAL_BOT_MXID)
    client.set_power_level(room_id, SIGNAL_BOT_MXID, 100)

    sync_token = run_bridge_command(client, room_id, "!wa set-relay", WHATSAPP_BOT_MXID, sync_token)

    log.info("  Invitation de %s", SIGNAL_RELAY_GHOST_MXID)
    client.invite(room_id, SIGNAL_RELAY_GHOST_MXID)

    sync_token = run_bridge_command(
        client, room_id, "!signal create-group", SIGNAL_BOT_MXID, sync_token,
        reply_required=True, require_substring=CREATE_GROUP_SUCCESS_MARKER,
    )

    sync_token = run_bridge_command(
        client, room_id, f"!signal set-relay {SIGNAL_RELAY_LOGIN_ID}", SIGNAL_BOT_MXID, sync_token
    )
    return sync_token


def fix_missing_relay(client: MatrixClient, room_id: str, room_name: str, sync_token: str) -> str:
    """Salon avec @signalbot et groupe déjà créé, mais sans le ghost relais dédié."""
    log.info("  [Cas B] Signal déjà configuré mais relais dédié absent -> ajout")

    log.info("  Invitation de %s", SIGNAL_RELAY_GHOST_MXID)
    client.invite(room_id, SIGNAL_RELAY_GHOST_MXID)

    sync_token = run_bridge_command(
        client, room_id, f"!signal set-relay {SIGNAL_RELAY_LOGIN_ID}", SIGNAL_BOT_MXID, sync_token
    )
    return sync_token


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute", action="store_true",
        help="Exécute réellement les actions. Sans ce flag, mode dry-run.",
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

    to_fix: list[tuple[str, str, str]] = []  # (room_id, room_name, case)
    for room_id in room_ids:
        try:
            members = set(client.get_room_members(room_id))
        except requests.RequestException as e:
            log.warning("Impossible de lire les membres de %s: %s", room_id, e)
            continue

        if WHATSAPP_BOT_MXID not in members:
            continue  # pas un salon WhatsApp, on ignore

        room_name = client.get_room_name(room_id) or room_id

        if SIGNAL_BOT_MXID not in members:
            to_fix.append((room_id, room_name, "A"))
        elif SIGNAL_RELAY_GHOST_MXID not in members:
            to_fix.append((room_id, room_name, "B"))
        # sinon: déjà complet, on ignore

    log.info("%d salons à corriger.", len(to_fix))
    for room_id, room_name, case in to_fix:
        log.info("  - [%s] %s (%s)", case, room_name, room_id)

    for i, (room_id, room_name, case) in enumerate(to_fix):
        if i > 0:
            time.sleep(ROOM_PROCESSING_DELAY)

        if not args.execute:
            log.info("[DRY-RUN] [%s] Corrigerait %s (%s)", case, room_name, room_id)
            continue

        log.info("Traitement de %s (%s) [cas %s]", room_name, room_id, case)
        try:
            if case == "A":
                sync_token = full_signal_setup(client, room_id, room_name, sync_token)
            else:
                sync_token = fix_missing_relay(client, room_id, room_name, sync_token)
            log.info("  Terminé.")
        except (BridgeCommandError, BridgeCommandTimeout) as e:
            log.error("  Échec sur %s: %s", room_id, e)
        except requests.RequestException as e:
            log.error("  Erreur réseau sur %s: %s", room_id, e)

    log.info("Terminé.")


if __name__ == "__main__":
    main()
