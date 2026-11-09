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
    channels = {}
    for event in state:
        kind = event.get("type")
        content = event.get("content") or {}
        if kind == "m.room.name":
            name = content.get("name", "")
        elif kind == "m.room.member" and content.get("membership") == "join":
            members.add(event.get("state_key", ""))
        elif kind in BRIDGE_TYPES:
            protocol = (content.get("protocol") or {}).get("id")
            channel = (content.get("channel") or {}).get("id")
            if protocol and channel:
                channels.setdefault(protocol, channel)
    return {"name": name, "members": members, "channels": channels}


def human_size(size):
    if not isinstance(size, int):
        return "taille inconnue"
    return f"{size / 1048576:.2f} Mo"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--homeserver", default=os.environ.get("MATRIX_HOMESERVER_URL", "http://127.0.0.1:8008"))
    parser.add_argument("--token", default=os.environ.get("MATRIX_ACCESS_TOKEN", ""))
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()
    if not args.token:
        fail("MATRIX_ACCESS_TOKEN manquant")

    client = Client(args.homeserver, args.token)
    rooms = {room_id: read_room(client, room_id) for room_id in client.get("/_matrix/client/v3/joined_rooms")["joined_rooms"]}
    status_id = next((room_id for room_id, info in rooms.items() if info["name"] == STATUS_NAME), None)
    if status_id is None:
        fail("Salon des statuts introuvable")

    by_member = {}
    for room_id, info in rooms.items():
        if room_id == status_id or "signal" not in info["channels"]:
            continue
        for member in info["members"]:
            by_member.setdefault(member, []).append(room_id)

    history = client.get(f"/_matrix/client/v3/rooms/{quote(status_id)}/messages?dir=b&limit={args.limit}")
    events = [event for event in history.get("chunk", []) if event.get("type") == "m.room.message"]
    print(f"Salon des statuts : {tag('r-', status_id)}")
    print(f"Statuts lus : {len(events)}")

    counts = {"un": 0, "aucun": 0, "plusieurs": 0}
    for event in events:
        sender = event.get("sender", "")
        content = event.get("content") or {}
        local = sender.split(":")[0].lstrip("@")
        shape = re.sub(r"\d", "N", local)
        candidates = by_member.get(sender, [])
        key = "aucun" if not candidates else "un" if len(candidates) == 1 else "plusieurs"
        counts[key] += 1
        groups = ", ".join(f"{tag('r-', c)}/{tag('g-', rooms[c]['channels']['signal'])}" for c in candidates)
        print(
            f"  {tag('s-', sender)}  {content.get('msgtype', '?'):<10} {human_size((content.get('info') or {}).get('size')):<14}"
            f" forme {shape}  salons Signal : {len(candidates)}{' (' + groups + ')' if groups else ''}"
        )
    print(f"Bilan : {counts['un']} avec un salon Signal, {counts['aucun']} sans, {counts['plusieurs']} ambigus")


if __name__ == "__main__":
    main()
