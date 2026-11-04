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
TELEGRAM_MAIN_LOGIN_ID = os.environ.get("TELEGRAM_MAIN_LOGIN_ID", "").strip()
TELEGRAM_MAIN_GHOST_MXID = os.environ.get(
    "TELEGRAM_MAIN_GHOST_MXID",
    f"@telegram_{TELEGRAM_MAIN_LOGIN_ID}:{DOMAIN}" if TELEGRAM_MAIN_LOGIN_ID else "",
)
TELEGRAM_EXTRA_GHOSTS = [
    g.strip() for g in os.environ.get("TELEGRAM_EXTRA_GHOSTS", "").split(",") if g.strip()
]
EXTRA_GHOST_WAIT = int(os.environ.get("EXTRA_GHOST_WAIT", "20"))
TELEGRAM_GHOSTS_TO_INVITE = [
    g.strip() for g in os.environ.get("TELEGRAM_GHOSTS_TO_INVITE", "").split(",") if g.strip()
]
UPGRADE_MODE = os.environ.get("TELEGRAM_CREATE_MODE", "direct").strip().lower() == "upgrade"
TELEGRAM_RELAY_GHOST_MXID = os.environ.get(
    "TELEGRAM_RELAY_GHOST_MXID",
    f"@telegram_{TELEGRAM_RELAY_LOGIN_ID}:{DOMAIN}" if TELEGRAM_RELAY_LOGIN_ID else "",
)
CREATOR_LOGIN_ID = TELEGRAM_MAIN_LOGIN_ID if UPGRADE_MODE else TELEGRAM_RELAY_LOGIN_ID
PARTICIPANT_GHOST_MXID = TELEGRAM_RELAY_GHOST_MXID if UPGRADE_MODE else TELEGRAM_MAIN_GHOST_MXID
TELEGRAM_GROUP_COMMAND = os.environ.get(
    "TELEGRAM_GROUP_COMMAND",
    f"!tg create-group {CREATOR_LOGIN_ID} group" if CREATOR_LOGIN_ID else "!tg create-group group",
)
TELEGRAM_UPGRADE_COMMAND = os.environ.get("TELEGRAM_UPGRADE_COMMAND", "!tg upgrade")
UPGRADE_WAIT = int(os.environ.get("UPGRADE_WAIT", "8"))
TELEGRAM_SET_RELAY_COMMAND = os.environ.get("TELEGRAM_SET_RELAY_COMMAND", "!tg set-relay")
CREATE_DELAY = float(os.environ.get("TELEGRAM_CREATE_DELAY", "30"))
RATE_LIMIT_MARGIN = float(os.environ.get("RATE_LIMIT_MARGIN", "5"))
RATE_LIMIT_DEFAULT_WAIT = int(os.environ.get("RATE_LIMIT_DEFAULT_WAIT", "600"))
RATE_LIMIT_RETRIES = int(os.environ.get("RATE_LIMIT_RETRIES", "5"))
BLOCKED_PAUSE = int(os.environ.get("TELEGRAM_BLOCKED_PAUSE", "3600"))
RELAY_RETRIES = int(os.environ.get("RELAY_RETRIES", "10"))
RELAY_RETRY_DELAY = float(os.environ.get("RELAY_RETRY_DELAY", "3"))

BOT_RESPONSE_TIMEOUT = int(os.environ.get("BOT_RESPONSE_TIMEOUT", "30"))
ROOM_PROCESSING_DELAY = float(os.environ.get("ROOM_PROCESSING_DELAY", "2"))
SYNC_TOKEN_FILE = os.environ.get("SYNC_TOKEN_FILE", "/var/lib/auto-bridge/telegram_next_batch")

ERROR_KEYWORDS = ("error", "failed", "⚠", "not found", "invalid")
CREATE_SUCCESS_MARKER = os.environ.get("TELEGRAM_CREATE_MARKER", "successfully created group")
RELAY_SUCCESS_MARKER = os.environ.get("TELEGRAM_RELAY_MARKER", "will now be relayed")
WA_RELAY_MARKER = os.environ.get("WHATSAPP_RELAY_MARKER", "will now be relayed")
PLUMB_SUCCESS_MARKER = os.environ.get("TELEGRAM_BRIDGE_MARKER", "successfully plumbed")
MAX_ROOM_ATTEMPTS = int(os.environ.get("MAX_ROOM_ATTEMPTS", "3"))
ROOM_RETRY_DELAY = float(os.environ.get("ROOM_RETRY_DELAY", "300"))
CONFIGURED_STATE_TYPE = "fr.kelian.auto_bridge_telegram"
WHATSAPP_PROTOCOL = os.environ.get("WHATSAPP_PROTOCOL_ID", "whatsapp")
BRIDGE_INFO_TYPES = ("m.bridge", "uk.half-shot.bridge")
PROTOCOL_WAIT = int(os.environ.get("PROTOCOL_WAIT", "15"))
MERGE_WAIT = int(os.environ.get("MERGE_WAIT", "10"))
SYNC_TIMEOUT_MS = 30_000

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("auto_bridge")


class BridgeCommandError(Exception):
    def __init__(self, message: str, sync_token: str | None = None) -> None:
        super().__init__(message)
        self.sync_token = sync_token


class BridgeCommandTimeout(Exception):
    def __init__(self, message: str, sync_token: str | None = None) -> None:
        super().__init__(message)
        self.sync_token = sync_token


class InvalidSyncToken(Exception):
    pass


class TelegramRateLimit(Exception):
    def __init__(self, message: str, wait_seconds: int | None = None, sync_token: str | None = None) -> None:
        super().__init__(message)
        self.wait_seconds = wait_seconds
        self.sync_token = sync_token


class RoomReplaced(Exception):
    def __init__(self, replacement: str, sync_token: str | None = None) -> None:
        super().__init__(f"Salon remplacé par {replacement}")
        self.replacement = replacement
        self.sync_token = sync_token


class TelegramBlocked(Exception):
    def __init__(self, message: str, sync_token: str | None = None) -> None:
        super().__init__(message)
        self.sync_token = sync_token


FLOOD_PATTERNS = (re.compile(r"a wait of (\d+) seconds"), re.compile(r"flood_wait[ _(]+(\d+)"))
BLOCKED_MARKERS = ("spamreported", "can't create channels or chats", "too many channels", "channels_too_much", "user_restricted")
RATE_MARKERS = ("too many requests", "peer_flood")


def telegram_limit_error(body: str, sync_token: str | None = None) -> Exception | None:
    lowered = body.lower()
    text = body.strip()[:200]
    if any(marker in lowered for marker in BLOCKED_MARKERS):
        return TelegramBlocked(text, sync_token)
    for pattern in FLOOD_PATTERNS:
        found = pattern.search(lowered)
        if found:
            return TelegramRateLimit(text, int(found.group(1)), sync_token)
    if any(marker in lowered for marker in RATE_MARKERS):
        return TelegramRateLimit(text, None, sync_token)
    return None


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
        if resp.status_code in (400, 403) and "already in the room" in resp.text.lower():
            return
        resp.raise_for_status()

    def send_message(self, room_id: str, body: str, msgtype: str = "m.text") -> str:
        txn_id = str(time.time_ns())
        resp = self._request(
            "PUT",
            self._url(f"/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn_id}"),
            json={"msgtype": msgtype, "body": body},
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

    def get_room_state(self, room_id: str) -> list[dict[str, Any]]:
        resp = self._request("GET", self._url(f"/_matrix/client/v3/rooms/{room_id}/state"))
        resp.raise_for_status()
        return resp.json()

    def joined_rooms(self) -> list[str]:
        resp = self._request("GET", self._url("/_matrix/client/v3/joined_rooms"))
        resp.raise_for_status()
        return list(resp.json().get("joined_rooms", []))

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
    strict: bool = False,
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
            if expected_sender == TELEGRAM_BOT_MXID:
                limit = telegram_limit_error(body, since)
                if limit:
                    raise limit
            if any(kw in lowered for kw in ERROR_KEYWORDS):
                raise BridgeCommandError(body.strip()[:200], since)
            if require_substring and require_substring.lower() not in lowered:
                if strict:
                    raise BridgeCommandError(body.strip()[:200], since)
                continue
            return since
    if reply_required:
        raise BridgeCommandTimeout(f"Pas de réponse de {expected_sender} après {timeout}s", since)
    return since


def run_bridge_command(
    client: MatrixClient,
    room_id: str,
    command: str,
    expected_sender: str,
    sync_token: str,
    reply_required: bool = True,
    require_substring: str | None = None,
    strict: bool = False,
    timeout: int = BOT_RESPONSE_TIMEOUT,
) -> str:
    log.info("> %s", command)
    client.send_message(room_id, command, "m.notice")
    return wait_for_bot_reply(
        client, room_id, expected_sender, sync_token, timeout=timeout,
        reply_required=reply_required, require_substring=require_substring, strict=strict,
    )


def wait_for_member(client: MatrixClient, room_id: str, user_id: str, timeout: int = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if user_id in client.get_room_members(room_id):
            return
        time.sleep(1)
    raise BridgeCommandTimeout(f"{user_id} n'a pas rejoint le salon")


def set_relay_when_ready(client: MatrixClient, room_id: str, sync_token: str, check_tombstone: bool = True) -> str:
    command = f"{TELEGRAM_SET_RELAY_COMMAND} {TELEGRAM_RELAY_LOGIN_ID}"
    for attempt in range(1, RELAY_RETRIES + 1):
        if check_tombstone:
            info = room_info(client, room_id, wait=0)
            if info.tombstoned and info.replacement:
                raise RoomReplaced(info.replacement, sync_token)
        try:
            return run_bridge_command(
                client, room_id, command, TELEGRAM_BOT_MXID, sync_token,
                require_substring=RELAY_SUCCESS_MARKER, strict=True,
            )
        except (BridgeCommandError, BridgeCommandTimeout) as e:
            migrating = isinstance(e, BridgeCommandError) and "split portals" in str(e).lower()
            if not (migrating or isinstance(e, BridgeCommandTimeout)) or attempt == RELAY_RETRIES:
                raise
            sync_token = e.sync_token or sync_token
            log.info("Relais pas encore possible, nouvelle tentative (%d/%d)", attempt, RELAY_RETRIES)
            time.sleep(RELAY_RETRY_DELAY)
    return sync_token


@dataclass
class RoomInfo:
    protocols: set[str]
    space: bool = False
    tombstoned: bool = False
    replacement: str = ""
    telegram_chat: str = ""


def room_info(client: MatrixClient, room_id: str, wait: int = PROTOCOL_WAIT) -> RoomInfo:
    deadline = time.monotonic() + wait
    while True:
        info = RoomInfo(set())
        for evt in client.get_room_state(room_id):
            kind = evt.get("type")
            content = evt.get("content", {})
            if kind in BRIDGE_INFO_TYPES:
                proto = content.get("protocol", {}).get("id")
                if proto:
                    info.protocols.add(str(proto))
                    if proto == "telegram":
                        info.telegram_chat = str(content.get("channel", {}).get("id", "")) or info.telegram_chat
            elif kind == "m.room.create" and content.get("type") == "m.space":
                info.space = True
            elif kind == "m.room.tombstone":
                info.tombstoned = True
                info.replacement = str(content.get("replacement_room", ""))
        if info.protocols or time.monotonic() >= deadline:
            return info
        time.sleep(1)


_last_create = [0.0]


def wait_create_slot() -> None:
    remaining = _last_create[0] + CREATE_DELAY - time.monotonic()
    if remaining > 0:
        log.info("Attente de %.0fs avant la prochaine création", remaining)
        time.sleep(remaining)


def refresh_token(client: MatrixClient, token: str) -> str:
    try:
        return client.sync(since=token, timeout_ms=0)["next_batch"]
    except InvalidSyncToken:
        return client.sync(since=None, timeout_ms=0)["next_batch"]


def get_stage(client: MatrixClient, room_id: str) -> str | None:
    state = client.get_state_event(room_id, CONFIGURED_STATE_TYPE)
    if not state:
        return None
    if state.get("configured"):
        return "configured"
    return state.get("stage")


def set_stage(client: MatrixClient, room_id: str, stage: str) -> None:
    client.set_state_event(room_id, CONFIGURED_STATE_TYPE, {"stage": stage, "at": int(time.time())})


def add_extra_ghosts(client: MatrixClient, room_id: str) -> None:
    for ghost in TELEGRAM_EXTRA_GHOSTS:
        try:
            client.invite(room_id, ghost)
            wait_for_member(client, room_id, ghost, EXTRA_GHOST_WAIT)
            log.info("%s ajouté au groupe", ghost)
        except BridgeCommandTimeout:
            log.warning("%s non confirmé dans le groupe, vérifie que le compte relais le connaît", ghost)
        except requests.RequestException as e:
            log.warning("%s non ajouté : %s", ghost, describe_error(e))


def recover_merge(client: MatrixClient, old_room: str, replacement: str, sync_token: str) -> tuple[str, str]:
    log.warning("Salon remplacé par %s, réparation", replacement)
    try:
        client.join_room(replacement)
    except requests.RequestException as e:
        raise BridgeCommandError(f"Salon de remplacement {replacement} inaccessible : {describe_error(e)}", sync_token)

    deadline = time.monotonic() + MERGE_WAIT
    while True:
        info = room_info(client, replacement, wait=0)
        if WHATSAPP_PROTOCOL in info.protocols or time.monotonic() >= deadline:
            break
        time.sleep(1)

    if WHATSAPP_PROTOCOL in info.protocols:
        log.info("Le bridge WhatsApp a suivi le salon de remplacement")
        sync_token = run_bridge_command(
            client, replacement, "!wa set-relay", WHATSAPP_BOT_MXID, sync_token,
            require_substring=WA_RELAY_MARKER, strict=True,
        )
        if TELEGRAM_RELAY_LOGIN_ID:
            sync_token = set_relay_when_ready(client, replacement, sync_token)
        return replacement, sync_token

    if not info.telegram_chat:
        raise BridgeCommandError("Identifiant du supergroupe introuvable dans le salon de remplacement", sync_token)
    log.info("Rattachement de %s au salon d'origine", info.telegram_chat)
    client.invite(old_room, TELEGRAM_BOT_MXID)
    wait_for_member(client, old_room, TELEGRAM_BOT_MXID)
    client.set_power_level(old_room, TELEGRAM_BOT_MXID, 100)
    sync_token = run_bridge_command(
        client, old_room, f"!tg bridge {TELEGRAM_MAIN_LOGIN_ID} {info.telegram_chat} --overwrite --ignore-permissions",
        TELEGRAM_BOT_MXID, sync_token, require_substring=PLUMB_SUCCESS_MARKER, strict=True,
    )
    if TELEGRAM_RELAY_LOGIN_ID:
        sync_token = set_relay_when_ready(client, old_room, sync_token, check_tombstone=False)
    return old_room, sync_token


def process_new_room(client: MatrixClient, room_id: str, sync_token: str) -> str:
    log.info("Salon %s", room_id)
    client.join_room(room_id)

    info = room_info(client, room_id)
    if WHATSAPP_PROTOCOL not in info.protocols or info.space:
        log.info("Salon ignoré : %s", ", ".join(sorted(info.protocols)) or "protocole inconnu")
        return sync_token

    client.make_room_admin(room_id)
    stage = get_stage(client, room_id)
    if stage == "configured":
        log.info("Salon déjà configuré")
        return sync_token
    if stage is None and "telegram" in info.protocols:
        log.info("Salon déjà relié à Telegram")
        return sync_token

    log.info("Nom : %s", client.get_room_name(room_id) or room_id)
    if stage:
        log.info("Reprise à l'étape %s", stage)

    if stage is None:
        client.invite(room_id, TELEGRAM_BOT_MXID)
        wait_for_member(client, room_id, TELEGRAM_BOT_MXID)
        client.set_power_level(room_id, TELEGRAM_BOT_MXID, 100)

    sync_token = run_bridge_command(
        client, room_id, "!wa set-relay", WHATSAPP_BOT_MXID, sync_token,
        require_substring=WA_RELAY_MARKER, strict=True,
    )

    if stage is None:
        invited: list[str] = []
        for ghost in [PARTICIPANT_GHOST_MXID, *TELEGRAM_GHOSTS_TO_INVITE]:
            if not ghost or ghost == TELEGRAM_BOT_MXID or ghost in invited:
                continue
            log.info("Invitation de %s", ghost)
            client.invite(room_id, ghost)
            invited.append(ghost)

        wait_create_slot()
        try:
            sync_token = run_bridge_command(
                client, room_id, TELEGRAM_GROUP_COMMAND, TELEGRAM_BOT_MXID, sync_token,
                require_substring=CREATE_SUCCESS_MARKER,
            )
        finally:
            _last_create[0] = time.monotonic()
        set_stage(client, room_id, "created")
        stage = "created"

    if UPGRADE_MODE and stage == "created":
        sync_token = run_bridge_command(
            client, room_id, TELEGRAM_UPGRADE_COMMAND, TELEGRAM_BOT_MXID, sync_token,
            reply_required=False, timeout=UPGRADE_WAIT,
        )
        set_stage(client, room_id, "upgraded")

    final_room = room_id
    if TELEGRAM_RELAY_LOGIN_ID:
        try:
            sync_token = set_relay_when_ready(client, room_id, sync_token)
        except RoomReplaced as e:
            final_room, sync_token = recover_merge(client, room_id, e.replacement, sync_token)

    add_extra_ghosts(client, final_room)
    set_stage(client, final_room, "configured")
    if final_room != room_id:
        set_stage(client, room_id, "configured")
    log.info("Salon %s configuré", room_id)
    return sync_token


def process_room(client: MatrixClient, room_id: str, sync_token: str) -> str:
    for attempt in range(1, RATE_LIMIT_RETRIES + 1):
        try:
            return process_new_room(client, room_id, sync_token)
        except TelegramRateLimit as e:
            if attempt == RATE_LIMIT_RETRIES:
                raise
            wait = (e.wait_seconds or RATE_LIMIT_DEFAULT_WAIT) + RATE_LIMIT_MARGIN
            log.warning("Telegram demande d'attendre %.0fs (%d/%d)", wait, attempt, RATE_LIMIT_RETRIES)
            time.sleep(wait)
            sync_token = refresh_token(client, e.sync_token or sync_token)
    return sync_token


def describe_error(e: Exception) -> str:
    if isinstance(e, requests.HTTPError) and e.response is not None:
        try:
            body = e.response.json()
            detail = f"{body.get('errcode', '')} {body.get('error', '')}".strip()
        except ValueError:
            detail = e.response.text[:100]
        return f"HTTP {e.response.status_code} {detail}".strip()
    return f"{type(e).__name__}"


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
    log.info("Mode de création : %s", "mise à niveau en supergroupe" if UPGRADE_MODE else "direct")

    try:
        client.disable_own_ratelimit()
    except requests.RequestException:
        log.warning("Limite de débit non désactivée")

    since = load_sync_token() or reset_sync(client)
    retry: dict[str, tuple[float, int]] = {}
    blocked_until = 0.0

    def schedule(room_id: str, delay: float, count_attempt: bool = True) -> None:
        attempts = retry.get(room_id, (0.0, 0))[1] + (1 if count_attempt else 0)
        if attempts >= MAX_ROOM_ATTEMPTS:
            retry.pop(room_id, None)
            log.error("Salon %s abandonné après %d essais, relance backfill_telegram.py", room_id, attempts)
            return
        retry[room_id] = (time.monotonic() + delay, attempts)

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

        invites = list(data.get("rooms", {}).get("invite", {}))
        now = time.monotonic()
        due = [r for r, (when, _) in retry.items() if when <= now and r not in invites]
        todo = invites + due

        if now < blocked_until:
            for room_id in todo:
                retry.setdefault(room_id, (blocked_until, 0))
            continue

        for i, room_id in enumerate(todo):
            if i > 0:
                time.sleep(ROOM_PROCESSING_DELAY)
            try:
                since = process_room(client, room_id, since)
                save_sync_token(since)
                retry.pop(room_id, None)
            except TelegramBlocked as e:
                blocked_until = time.monotonic() + BLOCKED_PAUSE
                log.error("Telegram bloque la création de groupes : %s. Pause de %ds, le salon %s sera réessayé", e, BLOCKED_PAUSE, room_id)
                retry[room_id] = (blocked_until, retry.get(room_id, (0.0, 0))[1])
                for other in todo[i + 1:]:
                    retry.setdefault(other, (blocked_until, 0))
                since = refresh_token(client, since)
                save_sync_token(since)
                break
            except TelegramRateLimit as e:
                log.error("Salon %s non configuré : %s", room_id, e)
                since = refresh_token(client, since)
                schedule(room_id, ROOM_RETRY_DELAY)
            except (BridgeCommandError, BridgeCommandTimeout) as e:
                log.error("Salon %s non configuré : %s", room_id, e)
                since = refresh_token(client, getattr(e, "sync_token", None) or since)
                schedule(room_id, ROOM_RETRY_DELAY)
            except Exception as e:
                log.error("Salon %s non configuré : %s", room_id, describe_error(e))
                schedule(room_id, ROOM_RETRY_DELAY)


if __name__ == "__main__":
    main()
