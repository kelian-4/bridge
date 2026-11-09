#!/usr/bin/env python3
import argparse
import hashlib
import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

STATUS_NAME = "WhatsApp Status Broadcast"
BRIDGE_TYPES = ("m.bridge", "uk.half-shot.bridge")


def tag(prefix, value):
    return prefix + hashlib.sha256(value.encode()).hexdigest()[:6]


def fail(message):
    print(message, file=sys.stderr)
    sys.exit(1)


class Client:
    def __init__(self, base, token):
        self.base = base.rstrip("/")
        self.token = token

    def get(self, path):
        request = urllib.request.Request(self.base + path, headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            fail(f"Requête refusée ({error.code})")
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            fail("Serveur injoignable")


def quote(value):
    return urllib.parse.quote(value, safe="")


def read_room(client, room_id):
    state = client.get(f"/_matrix/client/v3/rooms/{quote(room_id)}/state")
    name = ""
    members = set()
    states = {}
    channels = {}
    for event in state:
        kind = event.get("type")
        content = event.get("content") or {}
        if kind == "m.room.name":
            name = content.get("name", "")
        elif kind == "m.room.member":
            states[event.get("state_key", "")] = content.get("membership", "")
            if content.get("membership") == "join":
                members.add(event.get("state_key", ""))
        elif kind in BRIDGE_TYPES:
            protocol = (content.get("protocol") or {}).get("id")
            channel = (content.get("channel") or {}).get("id")
            if protocol and channel:
                channels.setdefault(protocol, channel)
    return {"name": name, "members": members, "states": states, "channels": channels}


def human_size(size):
    if not isinstance(size, int):
        return "taille inconnue"
    return f"{size / 1048576:.2f} Mo"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--homeserver", default=os.environ.get("MATRIX_HOMESERVER_URL", "http://127.0.0.1:8008"))
    parser.add_argument("--token", default=os.environ.get("MATRIX_ACCESS_TOKEN", ""))
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--sender", default="")
    parser.add_argument("--names", action="store_true")
    args = parser.parse_args()
    if not args.token:
        fail("MATRIX_ACCESS_TOKEN manquant")

    client = Client(args.homeserver, args.token)
    rooms = {room_id: read_room(client, room_id) for room_id in client.get("/_matrix/client/v3/joined_rooms")["joined_rooms"]}
    status_id = next((room_id for room_id, info in rooms.items() if info["name"] == STATUS_NAME), None)
    if status_id is None:
        fail("Salon des statuts introuvable")

    if args.sender:
        digits = re.sub(r"\D", "", args.sender.split(":")[0])
        print(f"Expéditeur : {tag('s-', args.sender)}")
        found = 0
        for room_id, info in rooms.items():
            if room_id == status_id:
                continue
            channel = info["channels"].get("whatsapp", "")
            same = bool(digits) and channel.split("@")[0] == digits
            state = info["states"].get(args.sender, "")
            if not same and not state:
                continue
            found += 1
            kind = channel.split("@")[-1] if "@" in channel else "?"
            signal = info["channels"].get("signal")
            label = f"  {info['name']!r}" if args.names else ""
            print(
                f"  {tag('r-', room_id)} {kind or '?'} {'meme-id' if same else 'id-different'}"
                f" statut-du-fantome:{state or 'absent'} {len(info['members'])}m"
                f" {'signal/' + tag('g-', signal) if signal else 'sans-signal'} bridge-whatsapp:{'oui' if channel else 'non'}{label}"
            )
        if not found:
            print("  aucun salon lié à cet expéditeur")
        return

    by_lid = {}
    for room_id, info in rooms.items():
        channel = info["channels"].get("whatsapp", "")
        if room_id == status_id or not channel.endswith("@lid"):
            continue
        by_lid.setdefault(channel.split("@")[0], []).append(room_id)

    history = client.get(f"/_matrix/client/v3/rooms/{quote(status_id)}/messages?dir=b&limit={args.limit}")
    events = [event for event in history.get("chunk", []) if event.get("type") == "m.room.message"]
    print(f"Salon des statuts : {tag('r-', status_id)}")
    print(f"Statuts lus : {len(events)}")

    senders = {}
    for event in events:
        sender = event.get("sender", "")
        size = (event.get("content") or {}).get("info", {}).get("size")
        entry = senders.setdefault(sender, {"count": 0, "max": 0})
        entry["count"] += 1
        if isinstance(size, int):
            entry["max"] = max(entry["max"], size)

    counts = {"signal": 0, "sans_signal": 0, "aucun": 0, "plusieurs": 0}
    for sender, entry in senders.items():
        local = sender.split(":")[0].lstrip("@")
        digits = local.rsplit("-", 1)[-1] if "lid-" in local else re.sub(r"\D", "", local)
        candidates = by_lid.get(digits, [])
        with_signal = [room_id for room_id in candidates if "signal" in rooms[room_id]["channels"]]
        if len(with_signal) > 1 or len(candidates) > 1:
            key = "plusieurs"
        elif with_signal:
            key = "signal"
        elif candidates:
            key = "sans_signal"
        else:
            key = "aucun"
        counts[key] += 1
        if candidates:
            detail = ", ".join(
                f"{tag('r-', room_id)} {len(rooms[room_id]['members'])}m "
                + (f"signal/{tag('g-', rooms[room_id]['channels']['signal'])}" if room_id in with_signal else "sans-signal")
                for room_id in candidates
            )
        else:
            detail = "aucune discussion lid"
        print(f"  {tag('s-', sender)}  {entry['count']} statut(s), max {human_size(entry['max'] or None)}  ->  {detail}")
    print(
        f"Bilan sur {len(senders)} expéditeurs : {counts['signal']} avec un groupe Signal, "
        f"{counts['sans_signal']} avec une discussion sans Signal, {counts['aucun']} sans discussion, {counts['plusieurs']} ambigus"
    )


if __name__ == "__main__":
    main()
