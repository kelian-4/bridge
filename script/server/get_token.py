#!/usr/bin/env python3
import argparse
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HELPER = "autobridge-admin"


def fail(message):
    sys.exit(f"Erreur : {message}")


def call(method, url, data=None, token=None, tolerate=()):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {}
        if e.code in tolerate:
            return {"_http_error": e.code, **payload}
        fail(f"{payload.get('error') or raw[:120]} ({e.code})")
    except urllib.error.URLError:
        fail(f"Synapse injoignable sur {url.split('/_', 1)[0]}")


def shared_secret(homeserver_yaml):
    text = Path(homeserver_yaml).read_text()
    m = re.search(r"(?m)^registration_shared_secret:\s*(.+)$", text)
    if m:
        value = m.group(1).strip().strip("\"'")
        if value and "CHANGE_ME" not in value:
            return value
    m = re.search(r"(?m)^registration_shared_secret_path:\s*(\S+)", text)
    if m:
        name = Path(m.group(1)).name
        for candidate in (m.group(1), f"secrets/{name}", f"Docker/secrets/{name}"):
            if Path(candidate).is_file():
                return Path(candidate).read_text().strip()
    value = os.environ.get("REGISTRATION_SHARED_SECRET", "").strip()
    if value:
        return value
    fail("secret d'enregistrement introuvable")


def register(base, secret, name, password, admin):
    nonce = call("GET", f"{base}/_synapse/admin/v1/register")["nonce"]
    mac = hmac.new(secret.encode(), digestmod=hashlib.sha1)
    kind = b"admin" if admin else b"notadmin"
    mac.update(nonce.encode() + b"\x00" + name.encode() + b"\x00" + password.encode() + b"\x00" + kind)
    return call("POST", f"{base}/_synapse/admin/v1/register",
                {"nonce": nonce, "username": name, "password": password, "admin": admin, "mac": mac.hexdigest()},
                tolerate=(400,))


def read_password(env_name):
    if env_name and os.environ.get(env_name):
        return os.environ[env_name]
    if not sys.stdin.isatty():
        fail("mot de passe requis")
    while True:
        first = getpass.getpass("Mot de passe du compte : ")
        if len(first) < 8:
            print("8 caractères minimum")
            continue
        if first == getpass.getpass("Confirmation : "):
            return first
        print("Les saisies diffèrent")


def env_read(path, key):
    p = Path(path)
    if not p.exists():
        return ""
    for line in p.read_text().splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].split("#")[0].strip()
    return ""


def env_write(path, key, value):
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
    p.chmod(0o600)


def token_valid(base, token, user):
    if not token or token == "CHANGE_ME":
        return False
    try:
        req = urllib.request.Request(f"{base}/_matrix/client/v3/account/whoami",
                                     headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp).get("user_id") == user
    except (urllib.error.URLError, ValueError):
        return False


def main():
    ap = argparse.ArgumentParser(description="Crée un token d'accès pour un compte Synapse.")
    ap.add_argument("--user", required=True)
    ap.add_argument("--homeserver", default=os.environ.get("MATRIX_HOMESERVER_URL", "http://127.0.0.1:8008"))
    ap.add_argument("--homeserver-yaml", default="Docker/synapse/homeserver.yaml")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--env-key", default="MATRIX_ACCESS_TOKEN")
    ap.add_argument("--create", action="store_true")
    ap.add_argument("--admin", action="store_true")
    ap.add_argument("--password-env")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    base = args.homeserver.rstrip("/")
    if not args.execute:
        print("Simulation : ajoute --execute pour appliquer")
        return
    if not args.force and token_valid(base, env_read(args.env_file, args.env_key), args.user):
        print(f"Token {args.env_key} déjà présent")
        return

    secret = shared_secret(args.homeserver_yaml)
    domain = args.user.split(":", 1)[1]
    helper_pw = hmac.new(secret.encode(), b"autobridge-admin-password", hashlib.sha256).hexdigest()[:32]
    res = register(base, secret, HELPER, helper_pw, True)
    if res.get("errcode") == "M_USER_IN_USE":
        admin_token = call("POST", f"{base}/_matrix/client/v3/login",
                           {"type": "m.login.password", "identifier": {"type": "m.id.user", "user": HELPER},
                            "password": helper_pw, "initial_device_display_name": HELPER})["access_token"]
    elif "_http_error" in res:
        fail(f"compte d'aide refusé ({res['_http_error']})")
    else:
        admin_token = res["access_token"]

    quoted = urllib.parse.quote(args.user)
    exists = "_http_error" not in call("GET", f"{base}/_synapse/admin/v2/users/{quoted}", token=admin_token, tolerate=(404,))
    token = None
    created = False
    if not exists:
        if not args.create:
            fail(f"compte {args.user} absent")
        res = register(base, secret, args.user[1:].split(":", 1)[0], read_password(args.password_env), args.admin)
        if "_http_error" in res:
            fail(f"création de {args.user} refusée ({res['_http_error']})")
        token = res["access_token"]
        created = True
        print(f"Compte {args.user} créé")
    if token is None:
        token = call("POST", f"{base}/_synapse/admin/v1/users/{quoted}/login", {}, token=admin_token)["access_token"]

    env_write(args.env_file, args.env_key, token)
    if created:
        print(f"Token {args.env_key} écrit")
    else:
        print(f"Token {args.env_key} écrit. Conserve le compte @{HELPER}:{domain}")


if __name__ == "__main__":
    main()
