#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

import requests

import auto_bridge_signal as ab

SIGNAL_CLI_URL = os.environ.get("SIGNAL_CLI_URL", "http://127.0.0.1:18080")
SIGNAL_CLI_ACCOUNT = os.environ.get("SIGNAL_CLI_ACCOUNT", "")
STATE_DIR = Path(os.environ.get("STATUS_STATE_DIR", "/state"))
POLL_SECONDS = int(os.environ.get("STATUS_POLL_SECONDS", "10"))
CREATE_DELAY = int(os.environ.get("STATUS_CREATE_DELAY", "30"))
WAIT_SECONDS = int(os.environ.get("STATUS_WAIT_SECONDS", "180"))
START_CHAT_TIMEOUT = int(os.environ.get("STATUS_START_CHAT_TIMEOUT", "120"))
SYNC_WAIT = int(os.environ.get("STATUS_SYNC_WAIT", "20"))
UNKNOWN_GROUP_WAIT = int(os.environ.get("STATUS_UNKNOWN_GROUP_WAIT", "21600"))
UNKNOWN_GROUP_RETRY = int(os.environ.get("STATUS_UNKNOWN_GROUP_RETRY", "300"))
SYNC_COOLDOWN = int(os.environ.get("STATUS_SYNC_COOLDOWN", "90"))
MAX_ATTEMPTS = int(os.environ.get("STATUS_MAX_ATTEMPTS", "3"))
SKIP_LIDS = {value.strip() for value in os.environ.get("STATUS_SKIP_LIDS", "").split(",") if value.strip()}
HISTORY_LIMIT = 50
DONE_LIMIT = 2000
MEDIA_TYPES = {"m.image", "m.video"}
LID_RE = re.compile(r"^@whatsapp_lid-(\d+):")
ROOM_LINK = re.compile(r"matrix\.to/#/(![^\s)/?]+)")
REFUSAL_WORDS = ("failed", "error", "invalid", "unknown", "not found")
STATUS_RESCAN_SECONDS = 60

log = logging.getLogger("auto_status")


class StoryError(Exception):
    pass


class UnknownGroup(StoryError):
    pass


def quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def room_state(client, room_id: str) -> list[dict]:
    resp = client._request("GET", client._url(f"/_matrix/client/v3/rooms/{quote(room_id)}/state"))
    resp.raise_for_status()
    return resp.json()


def read_state(state: list[dict]) -> tuple[str, dict[str, str]]:
    name = ""
    channels: dict[str, str] = {}
    for event in state:
        content = event.get("content") or {}
        if event.get("type") == "m.room.name":
            name = content.get("name", "")
        elif event.get("type") == "m.bridge":
            protocol = (content.get("protocol") or {}).get("id")
            channel = (content.get("channel") or {}).get("id")
            if protocol and channel:
                channels.setdefault(protocol, channel)
    return name, channels


def signal_group(client, room_id: str) -> str | None:
    return read_state(room_state(client, room_id))[1].get("signal")


def scan(client) -> tuple[str | None, dict[str, tuple[str, str | None]]]:
    resp = client._request("GET", client._url("/_matrix/client/v3/joined_rooms"))
    resp.raise_for_status()
    status_room = None
    by_lid: dict[str, tuple[str, str | None]] = {}
    for room_id in resp.json()["joined_rooms"]:
        try:
            name, channels = read_state(room_state(client, room_id))
        except requests.RequestException:
            continue
        if name == ab.STATUS_BROADCAST_NAME:
            status_room = room_id
            continue
        whatsapp = channels.get("whatsapp", "")
        if not whatsapp.endswith("@lid"):
            continue
        digits = whatsapp.split("@")[0]
        group = channels.get("signal")
        if digits not in by_lid or (group and not by_lid[digits][1]):
            by_lid[digits] = (room_id, group)
    return status_room, by_lid


def bot_activity(client, room_id: str) -> int:
    resp = client._request("GET", client._url(f"/_matrix/client/v3/rooms/{quote(room_id)}/messages?dir=b&limit=20"))
    if resp.status_code != 200:
        return 0
    stamps = [
        event.get("origin_server_ts", 0)
        for event in resp.json().get("chunk", [])
        if event.get("type") == "m.room.message" and event.get("sender") == ab.WHATSAPP_BOT_MXID
    ]
    return max(stamps, default=0)


def management_room(client) -> str:
    resp = client._request("GET", client._url("/_matrix/client/v3/joined_rooms"))
    resp.raise_for_status()
    wanted = {ab.USER_ID, ab.WHATSAPP_BOT_MXID}
    candidates = []
    for room_id in resp.json()["joined_rooms"]:
        try:
            if set(client.get_room_members(room_id)) == wanted:
                candidates.append(room_id)
        except requests.RequestException:
            continue
    if not candidates:
        raise StoryError("discussion avec le bot WhatsApp introuvable")
    if len(candidates) > 1:
        log.info("%d discussions avec le bot WhatsApp, la plus récemment active est utilisée", len(candidates))
        candidates.sort(key=lambda room_id: bot_activity(client, room_id), reverse=True)
    return candidates[0]


def wait_until(check, seconds: int = WAIT_SECONDS, step: int = 5):
    deadline = time.monotonic() + seconds
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() >= deadline:
            return None
        time.sleep(step)


def rpc(method: str, params: dict):
    try:
        resp = requests.post(
            f"{SIGNAL_CLI_URL}/api/v1/rpc",
            json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
            timeout=180,
        )
        body = resp.json()
    except (requests.RequestException, ValueError):
        raise StoryError("signal-cli injoignable")
    if "error" in body:
        raise StoryError(f"signal-cli : {(body['error'] or {}).get('message', 'erreur')}")
    return body.get("result")


_account: list[str] = []


def signal_account() -> str:
    if SIGNAL_CLI_ACCOUNT:
        return SIGNAL_CLI_ACCOUNT
    if not _account:
        numbers = [item.get("number") for item in (rpc("listAccounts", {}) or []) if item.get("number")]
        if len(numbers) != 1:
            raise StoryError("compte signal-cli introuvable")
        _account.append(numbers[0])
    return _account[0]


_last_sync: list[float] = []


def known_groups(account: str) -> set[str]:
    return {item["id"] for item in (rpc("listGroups", {"account": account}) or []) if item.get("id")}


def ensure_known(account: str, group: str) -> None:
    started = time.monotonic()
    known = known_groups(account)
    elapsed = int(time.monotonic() - started)
    if elapsed >= 10:
        log.info("Liste des groupes obtenue en %d s", elapsed)
    if group in known:
        return
    if not _last_sync or time.monotonic() - _last_sync[0] >= SYNC_COOLDOWN:
        log.info("Synchronisation des groupes demandée, %d groupes connus", len(known_groups(account)))
        rpc("sendSyncRequest", {"account": account})
        _last_sync[:] = [time.monotonic()]
    if wait_until(lambda: group in known_groups(account), seconds=SYNC_WAIT) is None:
        raise UnknownGroup(f"groupe inconnu de signal-cli après synchronisation, {len(known_groups(account))} groupes connus")


def fetch_media(client, content: dict) -> Path:
    match = re.match(r"mxc://([^/]+)/(.+)$", content.get("url", ""))
    if not match:
        raise StoryError("média sans adresse")
    resp = None
    for path in (
        f"/_matrix/client/v1/media/download/{match[1]}/{match[2]}",
        f"/_matrix/media/v3/download/{match[1]}/{match[2]}",
    ):
        resp = client._request("GET", client._url(path))
        if resp.status_code == 200:
            break
    if resp is None or resp.status_code != 200:
        raise StoryError("média introuvable")
    extension = mimetypes.guess_extension((content.get("info") or {}).get("mimetype", "")) or ".bin"
    folder = STATE_DIR / "stories"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{match[2]}{extension}"
    target.write_bytes(resp.content)
    return target


def wait_reply(client, room_id: str, since: str, seconds: int) -> str:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        remaining = int((deadline - time.monotonic()) * 1000)
        data = client.sync(since=since, timeout_ms=max(1000, min(remaining, 10000)))
        since = data["next_batch"]
        events = ((data.get("rooms") or {}).get("join") or {}).get(room_id, {}).get("timeline", {}).get("events", [])
        for event in events:
            if event.get("sender") != ab.WHATSAPP_BOT_MXID or event.get("type") != "m.room.message":
                continue
            body = (event.get("content") or {}).get("body", "")
            if ROOM_LINK.search(body):
                return body
            if any(word in body.lower() for word in REFUSAL_WORDS):
                raise StoryError("start-chat refusé")
    raise StoryError("pas de réponse du bot WhatsApp")


def start_chat(client, digits: str) -> tuple[str, bool]:
    room = management_room(client)
    token = client.sync(since=None, timeout_ms=0)["next_batch"]
    log.info("  -> start-chat")
    client.send_message(room, f"start-chat lid-{digits}")
    body = wait_reply(client, room, token, START_CHAT_TIMEOUT)
    return ROOM_LINK.search(body)[1], body.lower().startswith("created")


def ensure_signal(client, room_id: str) -> None:
    token = client.sync(since=None, timeout_ms=0)["next_batch"]
    ab.process_new_room(client, room_id, token)


def handle(client, event: dict, rooms: dict[str, tuple[str, str | None]]) -> dict[str, tuple[str, str | None]]:
    digits = LID_RE.match(event["sender"])[1]
    if digits in SKIP_LIDS:
        log.info("Statut d'un compte exclu ignoré")
        return rooms

    created = False
    entry = rooms.get(digits)
    if entry is None:
        log.info("Recherche de la discussion")
        found_room, created = start_chat(client, digits)
        try:
            client.join_room(found_room)
            entry = (found_room, signal_group(client, found_room))
        except requests.RequestException:
            raise StoryError("salon de la discussion inaccessible")
        rooms[digits] = entry

    room_id, group = entry
    if not group and created:
        group = wait_until(lambda: signal_group(client, room_id))
    if not group:
        log.info("Création du groupe Signal")
        ensure_signal(client, room_id)
        created = True
        group = wait_until(lambda: signal_group(client, room_id))
    if not group:
        raise StoryError("groupe Signal non créé")
    rooms[digits] = (room_id, group)

    account = signal_account()
    ensure_known(account, group)
    media = fetch_media(client, event.get("content") or {})
    try:
        rpc("sendStory", {"account": account, "attachment": str(media), "groupId": group})
    finally:
        media.unlink(missing_ok=True)
    log.info("Story publiée")
    if created:
        time.sleep(CREATE_DELAY)
    return rooms


def load_done() -> list[str] | None:
    path = STATE_DIR / "status_done.json"
    if not path.exists():
        return None
    try:
        return list(json.loads(path.read_text()))
    except (OSError, ValueError):
        return []


def save_done(done: list[str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "status_done.json").write_text(json.dumps(done[-DONE_LIMIT:]))


def history(client, room_id: str) -> list[dict]:
    resp = client._request(
        "GET", client._url(f"/_matrix/client/v3/rooms/{quote(room_id)}/messages?dir=b&limit={HISTORY_LIMIT}")
    )
    resp.raise_for_status()
    events = [event for event in resp.json().get("chunk", []) if event.get("type") == "m.room.message"]
    return list(reversed(events))


def is_story(event: dict) -> bool:
    content = event.get("content") or {}
    return content.get("msgtype") in MEDIA_TYPES and bool(LID_RE.match(event.get("sender", "")))


def report(client) -> int:
    account = signal_account()
    listed = rpc("listGroups", {"account": account}) or []
    known = {item["id"] for item in listed if item.get("id")}
    cli_names = {item.get("name") for item in listed if item.get("name")}
    resp = client._request("GET", client._url("/_matrix/client/v3/joined_rooms"))
    resp.raise_for_status()
    now = time.time() * 1000
    total = 0
    same_name = 0
    ages = {"connus": [], "inconnus": []}
    for room_id in resp.json()["joined_rooms"]:
        try:
            state = room_state(client, room_id)
        except requests.RequestException:
            continue
        room_name, channels = read_state(state)
        group = channels.get("signal")
        if not group:
            continue
        total += 1
        if group not in known and room_name in cli_names:
            same_name += 1
        created = next((e.get("origin_server_ts", 0) for e in state if e.get("type") == "m.room.create"), 0)
        ages["connus" if group in known else "inconnus"].append(int((now - created) / 86400000) if created else -1)
    print(f"{total} groupes Signal du pont, {len(ages['connus'])} connus de signal-cli, {len(ages['inconnus'])} inconnus")
    for label in ("connus", "inconnus"):
        print(f"âge en jours des {label} : {sorted(ages[label])}")
    print(f"{same_name} groupes inconnus portent le même nom qu'un groupe de signal-cli")
    lengths = sorted({len(item["id"]) for item in listed if item.get("id")})
    urlsafe = any(("-" in item["id"] or "_" in item["id"]) for item in listed if item.get("id"))
    print(f"identifiants de signal-cli : longueurs {lengths}, alphabet URL-sûr : {'oui' if urlsafe else 'non'}")
    return 0


def replay(client, count: int) -> int:
    status_room = scan(client)[0]
    if status_room is None:
        print("Salon des statuts introuvable", file=sys.stderr)
        return 1
    ids = [event["event_id"] for event in history(client, status_room) if is_story(event)][-count:]
    done = [event_id for event_id in (load_done() or []) if event_id not in ids]
    save_done(done)
    print(f"{len(ids)} statut(s) à republier")
    return 0


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
    client = ab.MatrixClient(ab.HOMESERVER_URL, ab.ACCESS_TOKEN, ab.USER_ID)
    if len(sys.argv) == 2 and sys.argv[1] == "--report":
        sys.exit(report(client))
    if len(sys.argv) == 3 and sys.argv[1] == "--replay":
        sys.exit(replay(client, int(sys.argv[2])))
    try:
        client.disable_own_ratelimit()
    except requests.RequestException:
        log.warning("Limite de débit inchangée")

    done = load_done()
    first_run = done is None
    done = done or []
    attempts: dict[str, int] = {}
    unknown_since: dict[str, float] = {}
    retry_at: dict[str, float] = {}
    status_room = None
    last_scan = None
    log.info("Démarrage")

    while True:
        try:
            if status_room is None and (last_scan is None or time.monotonic() - last_scan >= STATUS_RESCAN_SECONDS):
                status_room = scan(client)[0]
                last_scan = time.monotonic()
            if status_room is not None:
                events = history(client, status_room)
                if first_run:
                    done = [event["event_id"] for event in events]
                    save_done(done)
                    first_run = False
                done_set = set(done)
                pending = [event for event in events if event["event_id"] not in done_set and is_story(event)]
                if pending:
                    rooms = scan(client)[1]
                    for event in pending:
                        event_id = event["event_id"]
                        if retry_at.get(event_id, 0) > time.monotonic():
                            continue
                        try:
                            rooms = handle(client, event, rooms)
                            done.append(event_id)
                        except UnknownGroup as error:
                            first = unknown_since.setdefault(event_id, time.monotonic())
                            if time.monotonic() - first > UNKNOWN_GROUP_WAIT:
                                log.error("Statut abandonné : %s", error)
                                done.append(event_id)
                            else:
                                if event_id not in retry_at:
                                    log.warning("%s, nouvel essai toutes les %d s", error, UNKNOWN_GROUP_RETRY)
                                retry_at[event_id] = time.monotonic() + UNKNOWN_GROUP_RETRY
                        except (StoryError, ab.BridgeCommandError, ab.BridgeCommandTimeout, requests.RequestException) as error:
                            attempts[event_id] = attempts.get(event_id, 0) + 1
                            log.error("Statut non publié : %s", error)
                            if attempts[event_id] >= MAX_ATTEMPTS:
                                log.error("Statut abandonné")
                                done.append(event_id)
                        save_done(done)
        except requests.RequestException as error:
            log.warning("Erreur réseau : %s", error)
            status_room = None
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
