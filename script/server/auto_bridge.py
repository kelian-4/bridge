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
from urllib.parse import quote

HOMESERVER_URL = os.environ.get("MATRIX_HOMESERVER_URL", "http://localhost:8008")
ACCESS_TOKEN = os.environ["MATRIX_ACCESS_TOKEN"]
USER_ID = os.environ.get("MATRIX_USER_ID", "@user:local.bridge")
DOMAIN = USER_ID.split(":", 1)[1] if ":" in USER_ID else "local.bridge"

WHATSAPP_BOT_MXID = os.environ.get("WHATSAPP_BOT_MXID", f"@whatsappbot:{DOMAIN}")
TELEGRAM_BOT_MXID = os.environ.get("TELEGRAM_BOT_MXID", f"@telegrambot:{DOMAIN}")
TELEGRAM_BOT_ID = os.environ.get("TELEGRAM_BOT_ID", "").strip()
RELAYBOT_GHOST_MXID = os.environ.get(
    "TELEGRAM_RELAYBOT_GHOST_MXID",
    f"@telegram_{TELEGRAM_BOT_ID}:{DOMAIN}" if TELEGRAM_BOT_ID else "",
)
TELEGRAM_GHOSTS_TO_INVITE = [
    g.strip() for g in os.environ.get("TELEGRAM_GHOSTS_TO_INVITE", "").split(",") if g.strip()
]
TELEGRAM_EXTRA_GHOSTS = [
    g.strip() for g in os.environ.get("TELEGRAM_EXTRA_GHOSTS", "").split(",") if g.strip()
]
EXTRA_GHOST_WAIT = int(os.environ.get("EXTRA_GHOST_WAIT", "20"))

PROVISIONING_URL = os.environ.get("TELEGRAM_PROVISIONING_URL", "http://127.0.0.1:29317").rstrip("/")
PROVISIONING_PREFIX = os.environ.get("TELEGRAM_PROVISIONING_PREFIX", "/_matrix/provision").rstrip("/")
PROVISIONING_SECRET = os.environ.get("TELEGRAM_PROVISIONING_SECRET", "")
CHAT_TYPE = os.environ.get("TELEGRAM_CHAT_TYPE", "supergroup")
CREATE_TIMEOUT = int(os.environ.get("TELEGRAM_CREATE_TIMEOUT", "120"))
CREATE_DELAY = float(os.environ.get("TELEGRAM_CREATE_DELAY", "30"))
RATE_LIMIT_MARGIN = float(os.environ.get("RATE_LIMIT_MARGIN", "5"))
RATE_LIMIT_DEFAULT_WAIT = int(os.environ.get("RATE_LIMIT_DEFAULT_WAIT", "600"))
RATE_LIMIT_RETRIES = int(os.environ.get("RATE_LIMIT_RETRIES", "5"))
BLOCKED_PAUSE = int(os.environ.get("TELEGRAM_BLOCKED_PAUSE", "3600"))

BOT_RESPONSE_TIMEOUT = int(os.environ.get("BOT_RESPONSE_TIMEOUT", "30"))
ROOM_PROCESSING_DELAY = float(os.environ.get("ROOM_PROCESSING_DELAY", "2"))
SYNC_TOKEN_FILE = os.environ.get("SYNC_TOKEN_FILE", "/var/lib/auto-bridge/telegram_next_batch")

ERROR_KEYWORDS = ("error", "failed", "⚠", "not found", "invalid")
WA_RELAY_MARKER = os.environ.get("WHATSAPP_RELAY_MARKER", "will now be relayed")
MAX_ROOM_ATTEMPTS = int(os.environ.get("MAX_ROOM_ATTEMPTS", "3"))
ROOM_RETRY_DELAY = float(os.environ.get("ROOM_RETRY_DELAY", "300"))
CONFIGURED_STATE_TYPE = "fr.kelian.auto_bridge_telegram_v1"
WHATSAPP_PROTOCOL = os.environ.get("WHATSAPP_PROTOCOL_ID", "whatsapp")
BRIDGE_INFO_TYPES = ("m.bridge", "uk.half-shot.bridge")
PROTOCOL_WAIT = int(os.environ.get("PROTOCOL_WAIT", "15"))
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


class ConfigurationError(BridgeCommandError):
    pass


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
            log.warning("%s non confirmé dans le groupe, vérifie que ton compte Telegram le connaît", ghost)
        except requests.RequestException as e:
            log.warning("%s non ajouté : %s", ghost, describe_error(e))


def provisioning_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {PROVISIONING_SECRET}"}


def provisioning_url(room_id: str, suffix: str = "") -> str:
    return f"{PROVISIONING_URL}{PROVISIONING_PREFIX}/v1/portal/{quote(room_id, safe='')}{suffix}"


def portal_exists(room_id: str) -> bool:
    try:
        resp = requests.get(
            provisioning_url(room_id), params={"user_id": USER_ID},
            headers=provisioning_headers(), timeout=30,
        )
    except requests.RequestException as e:
        raise BridgeCommandError(f"API de provisionnement injoignable : {describe_error(e)}")
    if resp.status_code == 404:
        return False
    if resp.status_code == 401:
        raise BridgeCommandError("Secret de provisionnement refusé")
    resp.raise_for_status()
    return True


def create_telegram_chat(room_id: str, sync_token: str | None = None) -> None:
    try:
        resp = requests.post(
            provisioning_url(room_id, "/create"), params={"user_id": USER_ID}, json={"type": CHAT_TYPE},
            headers=provisioning_headers(), timeout=CREATE_TIMEOUT,
        )
    except requests.Timeout:
        raise BridgeCommandTimeout(f"Pas de réponse de l'API de provisionnement après {CREATE_TIMEOUT}s", sync_token)
    except requests.RequestException as e:
        raise BridgeCommandError(f"API de provisionnement injoignable : {describe_error(e)}", sync_token)

    if resp.status_code in (200, 201):
        return
    try:
        body = resp.json()
        errcode, text = str(body.get("errcode", "")), str(body.get("error", ""))
    except ValueError:
        errcode, text = "", resp.text[:200]

    if errcode == "room_already_bridged":
        log.info("Le salon est déjà relié à un chat Telegram")
        return
    limit = telegram_limit_error(text, sync_token)
    if limit:
        raise limit
    if "not enough telegram users" in text.lower():
        raise ConfigurationError(
            "Le compte Telegram ne connaît pas le bot relais. Dans la discussion avec le bot du pont, envoie : search -r @nom_du_bot",
            sync_token,
        )
    if errcode == "shared_secret_invalid":
        raise BridgeCommandError("Secret de provisionnement refusé", sync_token)
    if errcode == "not_logged_in_real_account":
        raise BridgeCommandError("Compte Telegram non connecté, lance bridge_login.py telegram", sync_token)
    raise BridgeCommandError(f"{errcode or resp.status_code} : {text}"[:200], sync_token)


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
    if stage is None and portal_exists(room_id):
        log.info("Salon déjà relié à Telegram")
        return sync_token

    log.info("Nom : %s", client.get_room_name(room_id) or room_id)
    if stage:
        log.info("Reprise à l'étape %s", stage)

    client.invite(room_id, TELEGRAM_BOT_MXID)
    wait_for_member(client, room_id, TELEGRAM_BOT_MXID)

    sync_token = run_bridge_command(
        client, room_id, "!wa set-relay", WHATSAPP_BOT_MXID, sync_token,
        require_substring=WA_RELAY_MARKER, strict=True,
    )

    if stage is None:
        invited: list[str] = []
        for ghost in [RELAYBOT_GHOST_MXID, *TELEGRAM_GHOSTS_TO_INVITE]:
            if not ghost or ghost in invited:
                continue
            log.info("Invitation de %s", ghost)
            client.invite(room_id, ghost)
            invited.append(ghost)

        wait_create_slot()
        try:
            log.info("Création du chat Telegram (%s)", CHAT_TYPE)
            create_telegram_chat(room_id, sync_token)
        finally:
            _last_create[0] = time.monotonic()
        set_stage(client, room_id, "created")

    add_extra_ghosts(client, room_id)
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
    log.info("Création par l'API de provisionnement : %s, type %s", PROVISIONING_URL, CHAT_TYPE)

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
