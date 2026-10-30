#!/usr/bin/env python3
from __future__ import annotations

import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

HOMESERVER_URL = os.environ.get("MATRIX_HOMESERVER_URL", "http://localhost:8008")
ACCESS_TOKEN = os.environ["MATRIX_ACCESS_TOKEN"]
USER_ID = os.environ.get("MATRIX_USER_ID", "@user:local.bridge")
DOMAIN = USER_ID.split(":", 1)[1] if ":" in USER_ID else "local.bridge"

WHATSAPP_BOT_MXID = os.environ.get("WHATSAPP_BOT_MXID", f"@whatsappbot:{DOMAIN}")
TELEGRAM_BOT_MXID = os.environ.get("TELEGRAM_BOT_MXID", f"@telegrambot:{DOMAIN}")
TELEGRAM_RELAY_LOGIN_ID = os.environ.get("TELEGRAM_RELAY_LOGIN_ID", "").strip()
TELEGRAM_RELAY_GHOST_MXID = os.environ.get(
    "TELEGRAM_RELAY_GHOST_MXID",
    f"@telegram_{TELEGRAM_RELAY_LOGIN_ID}:{DOMAIN}" if TELEGRAM_RELAY_LOGIN_ID else "",
)
TELEGRAM_GHOSTS_TO_INVITE = [
    g.strip() for g in os.environ.get("TELEGRAM_GHOSTS_TO_INVITE", "").split(",") if g.strip()
]
TELEGRAM_GROUP_COMMAND = os.environ.get(
    "TELEGRAM_GROUP_COMMAND",
    f"!tg create-group {TELEGRAM_RELAY_LOGIN_ID} group" if TELEGRAM_RELAY_LOGIN_ID else "!tg create-group group",
)
TELEGRAM_SET_RELAY_COMMAND = os.environ.get("TELEGRAM_SET_RELAY_COMMAND", "!tg set-relay")

BOT_RESPONSE_TIMEOUT = int(os.environ.get("BOT_RESPONSE_TIMEOUT", "30"))
ROOM_PROCESSING_DELAY = float(os.environ.get("ROOM_PROCESSING_DELAY", "2"))
SYNC_TOKEN_FILE = os.environ.get("SYNC_TOKEN_FILE", "/var/lib/auto-bridge/telegram_next_batch")

ERROR_KEYWORDS = ("error", "failed", "⚠", "not found", "invalid")
CREATE_SUCCESS_MARKER = os.environ.get("TELEGRAM_CREATE_MARKER", "successfully created group")
CONFIGURED_STATE_TYPE = "fr.kelian.auto_bridge_telegram"
SYNC_TIMEOUT_MS = 30_000

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("auto_bridge")


class BridgeCommandError(Exception):
    pass


class BridgeCommandTimeout(Exception):
    pass


class InvalidSyncToken(Exception):
    pass


class TelegramFloodWait(Exception):
    def __init__(self, message: str, wait_seconds: int | None = None) -> None:
        super().__init__(message)
        self.wait_seconds = wait_seconds


def parse_flood_wait_seconds(body: str) -> int | None:
    h = re.search(r"(\d+)\s*hour", body)
    m = re.search(r"(\d+)\s*minute", body)
    s = re.search(r"(\d+)\s*second", body)
    if not (h or m or s):
        return None
    return (int(h.group(1)) if h else 0) * 3600 + (int(m.group(1)) if m else 0) * 60 + (int(s.group(1)) if s else 0)


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
            log.warning("Limite de débit, attente %.1fs (%d/%d)", wait_s, attempt + 1, max_retries)
            time.sleep(wait_s)
        return resp

    def sync(self, since: str | None = None, timeout_ms: int = SYNC_TIMEOUT_MS) -> dict[str, Any]:
        params: dict[str, Any] = {"timeout": timeout_ms, "set_presence": "online"}
        if since:
            params["since"] = since
        resp = self._request(
            "GET", self._url("/_matrix/client/v3/sync"), params=params, timeout=(timeout_ms / 1000) + 15
        )
        if resp.status_code == 400 and since is not None:
            raise InvalidSyncToken(resp.text)
        resp.raise_for_status()
        return resp.json()

    def join_room(self, room_id: str) -> None:
        self._request("POST", self._url(f"/_matrix/client/v3/join/{room_id}")).raise_for_status()

    def disable_own_ratelimit(self) -> None:
        resp = self._request(
            "POST",
            self._url(f"/_synapse/admin/v1/users/{self.user_id}/override_ratelimit"),
            json={"messages_per_second": 0, "burst_count": 0},
        )
        resp.raise_for_status()

    def make_room_admin(self, room_id: str, user_id: str | None = None) -> None:
        resp = self._request(
            "POST",
            self._url(f"/_synapse/admin/v1/rooms/{room_id}/make_room_admin"),
            json={"user_id": user_id or self.user_id},
        )
        resp.raise_for_status()

    def invite(self, room_id: str, user_id: str) -> None:
        resp = self._request(
            "POST", self._url(f"/_matrix/client/v3/rooms/{room_id}/invite"), json={"user_id": user_id}
        )
        if resp.status_code == 400 and "already in the room" in resp.text.lower():
            return
        resp.raise_for_status()

    def send_message(self, room_id: str, body: str) -> str:
        txn_id = str(time.time_ns())
        resp = self._request(
            "PUT",
            self._url(f"/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn_id}"),
            json={"msgtype": "m.text", "body": body},
        )
        resp.raise_for_status()
        return resp.json()["event_id"]

    def get_state_event(self, room_id: str, event_type: str, state_key: str = "") -> dict[str, Any] | None:
        resp = self._request("GET", self._url(f"/_matrix/client/v3/rooms/{room_id}/state/{event_type}/{state_key}"))
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def set_state_event(self, room_id: str, event_type: str, content: dict[str, Any], state_key: str = "") -> None:
        resp = self._request(
            "PUT", self._url(f"/_matrix/client/v3/rooms/{room_id}/state/{event_type}/{state_key}"), json=content
        )
        resp.raise_for_status()

    def get_room_name(self, room_id: str) -> str | None:
        resp = self._request("GET", self._url(f"/_matrix/client/v3/rooms/{room_id}/state/m.room.name/"))
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json().get("name")

    def get_room_members(self, room_id: str) -> list[str]:
        resp = self._request("GET", self._url(f"/_matrix/client/v3/rooms/{room_id}/joined_members"))
        resp.raise_for_status()
        return list(resp.json().get("joined", {}).keys())

    def get_power_levels(self, room_id: str) -> dict[str, Any]:
        resp = self._request("GET", self._url(f"/_matrix/client/v3/rooms/{room_id}/state/m.room.power_levels/"))
        resp.raise_for_status()
        return resp.json()

    def set_power_level(self, room_id: str, user_id: str, level: int) -> None:
        content = self.get_power_levels(room_id)
        content.setdefault("users", {})[user_id] = level
        resp = self._request(
            "PUT", self._url(f"/_matrix/client/v3/rooms/{room_id}/state/m.room.power_levels/"), json=content
        )
        resp.raise_for_status()


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
        if not room_data:
            continue
        for evt in room_data.get("timeline", {}).get("events", []):
            if evt.get("type") != "m.room.message" or evt.get("sender") != expected_sender:
                continue
            body = evt.get("content", {}).get("body", "")
            log.info("Réponse de %s : %s", expected_sender, body.strip()[:200])
            lowered = body.lower()
            if "flood" in lowered and "wait" in lowered:
                raise TelegramFloodWait(body, parse_flood_wait_seconds(body))
            if any(kw in lowered for kw in ERROR_KEYWORDS):
                raise BridgeCommandError(body.strip()[:200])
            if require_substring and require_substring.lower() not in lowered:
                continue
            return since
    if reply_required:
        raise BridgeCommandTimeout(f"Pas de réponse de {expected_sender} après {timeout}s")
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
    log.info("> %s", command)
    client.send_message(room_id, command)
    return wait_for_bot_reply(
        client, room_id, expected_sender, sync_token,
        reply_required=reply_required, require_substring=require_substring,
    )


def wait_for_member(client: MatrixClient, room_id: str, user_id: str, timeout: int = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if user_id in client.get_room_members(room_id):
            return
        time.sleep(1)
    raise BridgeCommandTimeout(f"{user_id} n'a pas rejoint le salon")


def process_new_room(client: MatrixClient, room_id: str, sync_token: str) -> str:
    log.info("Salon %s", room_id)
    client.join_room(room_id)
    client.make_room_admin(room_id)

    if client.get_state_event(room_id, CONFIGURED_STATE_TYPE):
        log.info("Salon déjà configuré")
        return sync_token

    room_name = client.get_room_name(room_id) or room_id
    log.info("Nom : %s", room_name)

    client.invite(room_id, TELEGRAM_BOT_MXID)
    wait_for_member(client, room_id, TELEGRAM_BOT_MXID)
    client.set_power_level(room_id, TELEGRAM_BOT_MXID, 100)

    sync_token = run_bridge_command(client, room_id, "!wa set-relay", WHATSAPP_BOT_MXID, sync_token)

    invited: list[str] = []
    for ghost in [*TELEGRAM_GHOSTS_TO_INVITE, TELEGRAM_RELAY_GHOST_MXID]:
        if not ghost or ghost == TELEGRAM_BOT_MXID or ghost in invited:
            continue
        log.info("Invitation de %s", ghost)
        client.invite(room_id, ghost)
        invited.append(ghost)

    sync_token = run_bridge_command(
        client, room_id, TELEGRAM_GROUP_COMMAND, TELEGRAM_BOT_MXID, sync_token,
        require_substring=CREATE_SUCCESS_MARKER,
    )

    if TELEGRAM_RELAY_LOGIN_ID:
        sync_token = run_bridge_command(
            client, room_id, f"{TELEGRAM_SET_RELAY_COMMAND} {TELEGRAM_RELAY_LOGIN_ID}",
            TELEGRAM_BOT_MXID, sync_token,
        )

    client.set_state_event(
        room_id, CONFIGURED_STATE_TYPE, {"configured": True, "configured_at": int(time.time())}
    )
    log.info("Salon %s configuré", room_id)
    return sync_token


def load_sync_token() -> str | None:
    p = Path(SYNC_TOKEN_FILE)
    if p.exists():
        return p.read_text().strip() or None
    return None


def save_sync_token(token: str) -> None:
    p = Path(SYNC_TOKEN_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(token)


def reset_sync(client: MatrixClient) -> str:
    Path(SYNC_TOKEN_FILE).unlink(missing_ok=True)
    since = client.sync(since=None, timeout_ms=0)["next_batch"]
    save_sync_token(since)
    return since


def main() -> None:
    client = MatrixClient(HOMESERVER_URL, ACCESS_TOKEN, USER_ID)
    log.info("Démarrage : %s, %s", HOMESERVER_URL, USER_ID)

    try:
        client.disable_own_ratelimit()
    except requests.RequestException:
        log.warning("Limite de débit non désactivée")

    since = load_sync_token() or reset_sync(client)

    while True:
        try:
            data = client.sync(since=since, timeout_ms=SYNC_TIMEOUT_MS)
        except InvalidSyncToken:
            log.warning("Jeton de synchronisation périmé, repositionnement")
            since = reset_sync(client)
            continue
        except requests.RequestException:
            log.warning("Synapse injoignable, nouvelle tentative dans 5s")
            time.sleep(5)
            continue

        since = data.get("next_batch", since)
        save_sync_token(since)

        for i, room_id in enumerate(data.get("rooms", {}).get("invite", {})):
            if i > 0:
                time.sleep(ROOM_PROCESSING_DELAY)
            try:
                since = process_new_room(client, room_id, since)
                save_sync_token(since)
            except TelegramFloodWait as e:
                log.error("Telegram impose une attente : %s. Salon %s non configuré", e.wait_seconds or "durée inconnue", room_id)
                save_sync_token(since)
                break
            except (BridgeCommandError, BridgeCommandTimeout) as e:
                log.error("Salon %s non configuré : %s", room_id, e)
            except requests.RequestException as e:
                log.error("Salon %s : erreur réseau", room_id)


if __name__ == "__main__":
    main()
