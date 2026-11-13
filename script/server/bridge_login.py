#!/usr/bin/env python3
import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _qr_terminal import png_to_terminal  # noqa: E402

DEFAULT_BOTS = {"whatsapp": "whatsappbot", "signal": "signalbot", "telegram": "telegrambot"}
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
SUCCESS_RE = re.compile(r"successfully logged in|you are already logged in", re.I)
FAILURE_RE = re.compile(r"login failed|failed to log ?in|login timed out|does not support logging in", re.I)
DEFAULT_COMMANDS = {"telegram": "login-qr"}


class Client:
    def __init__(self, base, token):
        self.base = base.rstrip("/")
        self.token = token

    def req(self, method, path, data=None, raw=False, timeout=60):
        headers = {"Authorization": f"Bearer {self.token}"}
        body = None
        if data is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(data).encode()
        r = urllib.request.Request(self.base + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                content = resp.read()
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            try:
                detail = json.loads(raw).get("error", raw[:100])
            except ValueError:
                detail = raw[:100]
            raise RuntimeError(f"{detail} ({e.code})")
        except urllib.error.URLError:
            raise RuntimeError("Synapse injoignable")
        return content if raw else json.loads(content)

    def whoami(self):
        return self.req("GET", "/_matrix/client/v3/account/whoami")["user_id"]

    def joined_rooms(self):
        return self.req("GET", "/_matrix/client/v3/joined_rooms")["joined_rooms"]

    def members(self, room):
        data = self.req("GET", f"/_matrix/client/v3/rooms/{urllib.parse.quote(room)}/joined_members")
        return set(data.get("joined", {}).keys())

    def can_post(self, room, me):
        try:
            pl = self.req("GET", f"/_matrix/client/v3/rooms/{urllib.parse.quote(room)}/state/m.room.power_levels/")
        except RuntimeError:
            return False
        level = pl.get("users", {}).get(me, pl.get("users_default", 0))
        needed = pl.get("events", {}).get("m.room.message", pl.get("events_default", 0))
        return level >= needed

    def create_dm(self, bot):
        return self.req("POST", "/_matrix/client/v3/createRoom",
                        {"invite": [bot], "is_direct": True, "preset": "trusted_private_chat"})["room_id"]

    def send(self, room, text):
        txn = str(time.time_ns())
        return self.req("PUT", f"/_matrix/client/v3/rooms/{urllib.parse.quote(room)}/send/m.room.message/{txn}",
                        {"msgtype": "m.text", "body": text})["event_id"]

    def sync(self, since=None, timeout_ms=0):
        flt = {"room": {"timeline": {"limit": 20}}, "presence": {"not_types": ["*"]}}
        if not since:
            flt["account_data"] = {"types": ["nonce." + str(time.time_ns())]}
        params = {"timeout": str(timeout_ms), "filter": json.dumps(flt)}
        if since:
            params["since"] = since
        return self.req("GET", "/_matrix/client/v3/sync?" + urllib.parse.urlencode(params), timeout=timeout_ms // 1000 + 30)

    def download(self, mxc):
        server, media_id = mxc[len("mxc://"):].split("/", 1)
        for path in (f"/_matrix/client/v1/media/download/{server}/{media_id}",
                     f"/_matrix/media/v3/download/{server}/{media_id}"):
            try:
                return self.req("GET", path, raw=True)
            except RuntimeError:
                continue
        raise RuntimeError("image introuvable")


def find_dm(client, me, bot):
    for room in client.joined_rooms():
        if client.members(room) == {me, bot} and client.can_post(room, me):
            return room
    return None


def search_remote(client, room, bot, since, query, timeout=30):
    sent = client.send(room, f"search -r {query}")
    seen = False
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.sync(since, 5000)
        since = data["next_batch"]
        events = data.get("rooms", {}).get("join", {}).get(room, {}).get("timeline", {}).get("events", [])
        for ev in events:
            if ev.get("event_id") == sent:
                seen = True
                continue
            if seen and ev.get("sender") == bot and ev.get("type") == "m.room.message":
                return event_content(ev).get("body", "")
    return None


def read_env_value(path, key):
    p = Path(path)
    if not p.exists():
        return None
    for line in p.read_text().splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].split("#")[0].strip()
    return None


def write_env_value(path, key, value):
    p = Path(path)
    lines = p.read_text().splitlines() if p.exists() else []
    out, done = [], False
    for line in lines:
        if line.startswith(key + "="):
            out.append(f"{key}={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{key}={value}")
    p.write_text("\n".join(out) + "\n")


def event_content(event):
    content = event.get("content", {})
    rel = content.get("m.relates_to", {})
    if rel.get("rel_type") == "m.replace" and "m.new_content" in content:
        return content["m.new_content"]
    return content


def show(client, content):
    msgtype = content.get("msgtype")
    if msgtype == "m.image":
        url = content.get("url") or content.get("file", {}).get("url")
        body = content.get("body", "")
        if url:
            try:
                print(png_to_terminal(client.download(url)))
                print("Scanne le QR code")
                return
            except Exception as exc:
                print("Image reçue, affichage impossible")
                return
    body = content.get("body", "")
    if body:
        print(body)


UNSET_IDS = (None, "", "CHANGE_ME", "00000000-0000-0000-0000-000000000000")


def record_signal_login(env_file, login_id, relay):
    if not relay:
        write_env_value(env_file, "SIGNAL_MAIN_LOGIN_ID", login_id)
        return "principal"
    main = read_env_value(env_file, "SIGNAL_MAIN_LOGIN_ID")
    if main in UNSET_IDS and read_env_value(env_file, "SIGNAL_RELAY_CONNECTED") != "1":
        main = read_env_value(env_file, "SIGNAL_RELAY_LOGIN_ID")
    if main not in UNSET_IDS and main == login_id:
        return "identique"
    write_env_value(env_file, "SIGNAL_RELAY_LOGIN_ID", login_id)
    write_env_value(env_file, "SIGNAL_RELAY_CONNECTED", "1")
    return "relais"


def main():
    ap = argparse.ArgumentParser(description="Connecte ton compte à un bridge (WhatsApp, Signal, Telegram) depuis le terminal.")
    ap.add_argument("bridge", help="whatsapp, signal, telegram, ou l'identifiant complet d'un bot (@bot:domaine)")
    ap.add_argument("--flow", help="méthode de connexion à demander tout de suite (ex. qr, phone-code) ; sinon le bot propose")
    ap.add_argument("--command", default=None, help="commande envoyée au bot (défaut : login, login-qr pour telegram)")
    ap.add_argument("--homeserver", default=os.environ.get("MATRIX_HOMESERVER_URL", "http://127.0.0.1:8008"))
    ap.add_argument("--token", default=os.environ.get("MATRIX_ACCESS_TOKEN", ""))
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--search", default=None, help="après la connexion, fait connaître cet utilisateur Telegram au compte (ex. @mon_bot)")
    ap.add_argument("--timeout", type=int, default=600, help="durée maximale en secondes (défaut : 600)")
    ap.add_argument("--debug", action="store_true", help="affiche chaque évènement reçu (diagnostic)")
    ap.add_argument("--relay", action="store_true", help="connecte le second compte Signal, celui du relais")
    args = ap.parse_args()

    if not args.token or args.token == "CHANGE_ME":
        sys.exit("MATRIX_ACCESS_TOKEN manquant")
    client = Client(args.homeserver, args.token)
    me = client.whoami()
    domain = me.split(":", 1)[1]
    if args.bridge.startswith("@"):
        bot, kind = args.bridge, "custom"
    else:
        kind = args.bridge.lower()
        if kind not in DEFAULT_BOTS:
            sys.exit("Bridge inconnu")
        bot = os.environ.get(kind.upper() + "_BOT_MXID") or f"@{DEFAULT_BOTS[kind]}:{domain}"

    args.command = args.command or DEFAULT_COMMANDS.get(kind, "login")
    room = find_dm(client, me, bot)
    if room:
        print(f"Discussion {bot} ouverte")
    else:
        room = client.create_dm(bot)
        print(f"Discussion {bot} créée")
        deadline = time.time() + 40
        while time.time() < deadline:
            if bot in client.members(room):
                break
            time.sleep(1.5)
        else:
            sys.exit(f"{bot} n'a pas rejoint la discussion")
        time.sleep(2)

    since = client.sync(None, 0)["next_batch"]
    command = args.command + (f" {args.flow}" if args.flow else "")
    sent_id = client.send(room, command)
    print(f"> {command}")

    stop = threading.Event()

    def read_input():
        for line in sys.stdin:
            line = line.strip()
            if line == "/quit":
                stop.set()
                return
            if line:
                try:
                    client.send(room, line)
                except RuntimeError as exc:
                    print("Envoi impossible")

    threading.Thread(target=read_input, daemon=True).start()
    if sys.stdin.isatty():
        print("Réponds au bot. /quit pour quitter")

    deadline = time.time() + args.timeout
    flow_chosen = bool(args.flow)
    result = None
    last_ids = set()
    seen_own = False
    while time.time() < deadline and not stop.is_set():
        try:
            data = client.sync(since, 8000)
        except RuntimeError as exc:
            print("Synchronisation interrompue")
            time.sleep(3)
            continue
        since = data["next_batch"]
        events = data.get("rooms", {}).get("join", {}).get(room, {}).get("timeline", {}).get("events", [])
        for ev in events:
            if ev.get("event_id") == sent_id:
                seen_own = True
                continue
            if not seen_own:
                continue
            if ev.get("sender") != bot or ev.get("type") != "m.room.message" or ev.get("event_id") in last_ids:
                continue
            last_ids.add(ev["event_id"])
            content = event_content(ev)
            if args.debug:
                print(f"[debug {time.strftime('%H:%M:%S')}] {ev['event_id'][:8]} {content.get('msgtype')} {content.get('body', '')[:40]!r}", file=sys.stderr)
            show(client, content)
            text = content.get("body", "")
            if not flow_chosen and "`qr`" in text and re.search(r"flows?", text, re.I):
                flow_chosen = True
                client.send(room, f"{args.command} qr")
                print(f"> {args.command} qr")
            if SUCCESS_RE.search(text):
                result = ("ok", text)
            elif FAILURE_RE.search(text):
                result = ("echec", text)
        if result:
            break

    if stop.is_set():
        print("Abandon")
        return 130
    if not result:
        print("Délai dépassé")
        return 2
    if result[0] == "echec":
        return 1
    print("Connexion réussie")
    if kind in DEFAULT_BOTS and not (kind == "signal" and args.relay):
        write_env_value(args.env_file, kind.upper() + "_CONNECTED", "1")
    if kind == "telegram" and args.search:
        reply = search_remote(client, room, bot, since, args.search)
        if reply is None:
            print(f"Recherche de {args.search} sans réponse")
        elif re.search(r"no results", reply, re.I):
            print(f"{args.search} introuvable sur Telegram")
        else:
            write_env_value(args.env_file, "TELEGRAM_BOT_KNOWN", "1")
            print(f"{args.search} connu du compte Telegram")
    found = UUID_RE.search(result[1])
    if kind == "signal" and found:
        outcome = record_signal_login(args.env_file, found.group(0), args.relay)
        if outcome == "identique":
            print("Le compte relais doit être différent du compte principal")
            return 1
        print("Compte relais enregistré" if args.relay else "Compte principal enregistré")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as exc:
        sys.exit(f"Erreur : {exc}")
    except KeyboardInterrupt:
        sys.exit(130)
