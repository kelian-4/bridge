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
import urllib.parse
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
STATUS_BROADCAST_NAME = "WhatsApp Status Broadcast"
RELAY_JOIN_WAIT = int(os.environ.get("BACKFILL_RELAY_JOIN_WAIT", "20"))
ZERO_LOGIN_ID = "00000000-0000-0000-0000-000000000000"

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


def signal_channel(client: MatrixClient, room_id: str) -> str | None:
    resp = client._request("GET", client._url(f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id, safe='')}/state"))
    resp.raise_for_status()
    for event in resp.json():
        if event.get("type") != "m.bridge":
            continue
        content = event.get("content") or {}
        if (content.get("protocol") or {}).get("id") == "signal":
            return (content.get("channel") or {}).get("id")
    return None


def existing_group_setup(client: MatrixClient, room_id: str, room_name: str, sync_token: str) -> str:
    log.info("  [Cas C] Groupe Signal existant, bot absent -> invitation sans recréation")

    log.info("  Invitation de %s", SIGNAL_BOT_MXID)
    client.invite(room_id, SIGNAL_BOT_MXID)

    bot_deadline = time.monotonic() + 20
    while time.monotonic() < bot_deadline:
        if SIGNAL_BOT_MXID in set(client.get_room_members(room_id)):
            break
        time.sleep(1)
    else:
        raise BridgeCommandTimeout(f"{SIGNAL_BOT_MXID} n'a pas rejoint {room_id} après 20s, abandon.")

    client.set_power_level(room_id, SIGNAL_BOT_MXID, 100)
    return fix_missing_relay(client, room_id, room_name, sync_token)


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

    join_deadline = time.monotonic() + RELAY_JOIN_WAIT
    joined = False
    while time.monotonic() < join_deadline:
        if SIGNAL_RELAY_GHOST_MXID in set(client.get_room_members(room_id)):
            joined = True
            break
        time.sleep(2)
    relay_id = SIGNAL_RELAY_LOGIN_ID
    if joined:
        log.info("  Le compte relais est dans le salon.")
    else:
        main_id = os.environ.get("SIGNAL_MAIN_LOGIN_ID", "")
        log.warning("  Le compte relais n'a pas rejoint le salon après %d s.", RELAY_JOIN_WAIT)
        if len(main_id) != 36 or main_id == ZERO_LOGIN_ID:
            log.warning("  Relais du salon laissé inchangé.")
            return sync_token
        log.warning("  Relais du salon remis sur le compte principal.")
        relay_id = main_id

    sync_token = run_bridge_command(
        client, room_id, f"!signal set-relay {relay_id}", SIGNAL_BOT_MXID, sync_token
    )
    return sync_token


def finalise(client: MatrixClient, execute: bool, limit: int) -> None:
    log.info("Récupération de la liste des salons...")
    sync_token = client.sync(since=None, timeout_ms=0)["next_batch"]
    targets: list[tuple[str, str]] = []
    for room_id in client.get_joined_rooms():
        try:
            members = set(client.get_room_members(room_id))
        except requests.RequestException as e:
            log.warning("Impossible de lire les membres de %s: %s", room_id, e)
            continue
        if not {WHATSAPP_BOT_MXID, SIGNAL_BOT_MXID, SIGNAL_RELAY_GHOST_MXID} <= members:
            continue
        room_name = client.get_room_name(room_id) or room_id
        if room_name == STATUS_BROADCAST_NAME or not signal_channel(client, room_id):
            continue
        targets.append((room_id, room_name))
    if limit > 0:
        targets = targets[:limit]
    log.info("%d salons où le compte relais est entré.", len(targets))
    for i, (room_id, room_name) in enumerate(targets):
        if not execute:
            log.info("[DRY-RUN] set-relay dans %s (%s)", room_name, room_id)
            continue
        if i > 0:
            time.sleep(ROOM_PROCESSING_DELAY)
        try:
            sync_token = run_bridge_command(
                client, room_id, f"!signal set-relay {SIGNAL_RELAY_LOGIN_ID}", SIGNAL_BOT_MXID, sync_token
            )
        except (BridgeCommandError, BridgeCommandTimeout) as e:
            log.error("  Échec sur %s: %s", room_id, e)
        except requests.RequestException as e:
            log.error("  Erreur réseau sur %s: %s", room_id, e)
    log.info("Terminé.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute", action="store_true",
        help="Exécute réellement les actions. Sans ce flag, mode dry-run.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Traite au plus N salons.")
    parser.add_argument("--cas", choices=["A", "B", "C", "D"], help="Ne traite que les salons du cas A, B, C ou D.")
    parser.add_argument("--invitations", action="store_true", help="Cas B : invite le compte relais sans attendre ni modifier le relais.")
    parser.add_argument("--finaliser", action="store_true", help="Envoie set-relay dans les salons où le compte relais est entré.")
    args = parser.parse_args()

    if not args.execute:
        log.info("=== MODE DRY-RUN — aucune commande ne sera envoyée. Utilise --execute pour agir. ===")

    client = MatrixClient(HOMESERVER_URL, ACCESS_TOKEN, USER_ID)

    if args.finaliser:
        finalise(client, args.execute, args.limit)
        return
    if args.invitations:
        args.cas = "B"

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
        if room_name == STATUS_BROADCAST_NAME:
            continue

        if SIGNAL_BOT_MXID not in members:
            to_fix.append((room_id, room_name, "C" if signal_channel(client, room_id) else "A"))
        elif SIGNAL_RELAY_GHOST_MXID not in members:
            to_fix.append((room_id, room_name, "B" if signal_channel(client, room_id) else "D"))
        # sinon: déjà complet, on ignore

    if args.cas:
        to_fix = [item for item in to_fix if item[2] == args.cas]
    if args.limit > 0:
        to_fix = to_fix[: args.limit]
    summary = {case: sum(1 for item in to_fix if item[2] == case) for case in "ABCD"}
    log.info("%d salons à corriger : cas A %d, cas B %d, cas C %d, cas D %d.", len(to_fix), summary["A"], summary["B"], summary["C"], summary["D"])
    for room_id, room_name, case in to_fix:
        log.info("  - [%s] %s (%s)", case, room_name, room_id)

    for i, (room_id, room_name, case) in enumerate(to_fix):
        if i > 0 and args.execute:
            time.sleep(ROOM_PROCESSING_DELAY)

        if not args.execute:
            log.info("[DRY-RUN] [%s] Corrigerait %s (%s)", case, room_name, room_id)
            continue

        log.info("Traitement de %s (%s) [cas %s]", room_name, room_id, case)
        try:
            if case in ("A", "D"):
                sync_token = full_signal_setup(client, room_id, room_name, sync_token)
            elif case == "C":
                sync_token = existing_group_setup(client, room_id, room_name, sync_token)
            elif args.invitations:
                client.invite(room_id, SIGNAL_RELAY_GHOST_MXID)
                log.info("  Invitation envoyée.")
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
