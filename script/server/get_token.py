#!/usr/bin/env python3
import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path


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
        payload = e.read().decode()
        if e.code in tolerate:
            try:
                return {"_http_error": e.code, **json.loads(payload)}
            except ValueError:
                return {"_http_error": e.code}
        sys.exit(f"Erreur HTTP {e.code} sur {method} {url.split('/_', 1)[-1]} : {payload[:200]}")


def find_shared_secret(homeserver_yaml):
    text = Path(homeserver_yaml).read_text()
    m = re.search(r"(?m)^registration_shared_secret:\s*(.+)$", text)
    if m:
        value = m.group(1).strip().strip("\"'")
        if value and "CHANGE_ME" not in value:
            return value
    m = re.search(r"(?m)^registration_shared_secret_path:\s*(\S+)", text)
    if m:
        for candidate in (m.group(1), "Docker/secrets/" + Path(m.group(1)).name):
            p = Path(candidate)
            if p.is_file():
                return p.read_text().strip()
    env = os.environ.get("REGISTRATION_SHARED_SECRET", "").strip()
    if env:
        return env
    sys.exit("registration_shared_secret introuvable dans " + str(homeserver_yaml))


def shared_register(base, secret, name, password, admin):
    nonce = call("GET", f"{base}/_synapse/admin/v1/register")["nonce"]
    mac = hmac.new(secret.encode(), digestmod=hashlib.sha1)
    mac.update(nonce.encode() + b"\x00" + name.encode() + b"\x00" + password.encode() + b"\x00" + (b"admin" if admin else b"notadmin"))
    return call("POST", f"{base}/_synapse/admin/v1/register",
                {"nonce": nonce, "username": name, "password": password, "admin": admin, "mac": mac.hexdigest()},
                tolerate=(400,))


def ask_password(env_name):
    if env_name:
        value = os.environ.get(env_name, "")
        if not value:
            sys.exit(f"La variable {env_name} est vide ou absente")
        return value
    if not sys.stdin.isatty():
        sys.exit("Mot de passe requis : utilise --password-env NOM_DE_VARIABLE (aucun terminal pour le demander)")
    import getpass
    while True:
        first = getpass.getpass("Mot de passe du nouveau compte : ")
        if len(first) < 8:
            print("8 caractères minimum.")
            continue
        if first == getpass.getpass("Confirme le mot de passe : "):
            return first
        print("Les deux saisies diffèrent.")


def main():
    ap = argparse.ArgumentParser(description="Crée un token d'accès pour un utilisateur existant, via un compte d'aide administrateur conservé (sans mot de passe).")
    ap.add_argument("--user", required=True, help="identifiant complet, ex. @toi:local.bridge")
    ap.add_argument("--homeserver", default=os.environ.get("MATRIX_HOMESERVER_URL", "http://127.0.0.1:8008"))
    ap.add_argument("--homeserver-yaml", default="Docker/synapse/homeserver.yaml")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--create", action="store_true", help="crée le compte s'il n'existe pas")
    ap.add_argument("--admin", action="store_true", help="avec --create : le compte est administrateur")
    ap.add_argument("--password-env", help="nom d'une variable d'environnement contenant le mot de passe du compte créé")
    ap.add_argument("--execute", action="store_true", help="applique (sinon simulation)")
    args = ap.parse_args()

    base = args.homeserver.rstrip("/")
    if not args.execute:
        print(f"SIMULATION : utiliserait (ou créerait) le compte d'aide autobridge-admin sur {base}, "
              f"obtiendrait un token pour {args.user} et l'écrirait dans {args.env_file} "
              "(MATRIX_ACCESS_TOKEN). Avec --create, le compte est créé s'il n'existe pas. Le compte d'aide autobridge-admin est conservé. Relance avec --execute.")
        return

    secret = find_shared_secret(args.homeserver_yaml)
    helper_name = "autobridge-admin"
    helper_pw = hmac.new(secret.encode(), b"autobridge-admin-password", hashlib.sha256).hexdigest()[:32]
    helper_id = "@" + helper_name + ":" + args.user.split(":", 1)[1]

    reg = shared_register(base, secret, helper_name, helper_pw, True)
    if reg.get("_http_error") == 400 and reg.get("errcode") == "M_USER_IN_USE":
        login = call("POST", f"{base}/_matrix/client/v3/login",
                     {"type": "m.login.password", "identifier": {"type": "m.id.user", "user": helper_name},
                      "password": helper_pw, "initial_device_display_name": "autobridge-admin"})
        admin_token = login["access_token"]
    elif "_http_error" in reg:
        sys.exit(f"Erreur HTTP {reg['_http_error']} à la création du compte d'aide : {str(reg)[:200]}")
    else:
        admin_token = reg["access_token"]

    localpart = args.user[1:].split(":", 1)[0]
    exists = "_http_error" not in call("GET", f"{base}/_synapse/admin/v2/users/{urllib.request.quote(args.user)}",
                                       token=admin_token, tolerate=(404,))
    user_token = None
    created = False
    if not exists:
        if not args.create:
            sys.exit(f"{args.user} n'existe pas sur ce Synapse. Relance avec --create (et --admin pour un administrateur).")
        pw = ask_password(args.password_env)
        res = shared_register(base, secret, localpart, pw, args.admin)
        if "_http_error" in res:
            sys.exit(f"Erreur HTTP {res['_http_error']} à la création de {args.user} : {str(res)[:200]}")
        user_token = res["access_token"]
        created = True
        print(f"Compte {args.user} créé" + (" (administrateur)." if args.admin else "."))
    if user_token is None:
        user_token = call("POST", f"{base}/_synapse/admin/v1/users/{urllib.request.quote(args.user)}/login",
                          {}, token=admin_token)["access_token"]

    env_path = Path(args.env_file)
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    out, done = [], False
    for line in lines:
        if line.startswith("MATRIX_ACCESS_TOKEN="):
            out.append("MATRIX_ACCESS_TOKEN=" + user_token)
            done = True
        else:
            out.append(line)
    if not done:
        out.append("MATRIX_ACCESS_TOKEN=" + user_token)
    env_path.write_text("\n".join(out) + "\n")
    env_path.chmod(0o600)
    if created:
        print(f"Token écrit dans {args.env_file} pour {args.user} (token propre au compte).")
    else:
        print(f"Token écrit dans {args.env_file} pour {args.user}. "
              f"Il est rattaché au compte d'aide {helper_id} (administrateur) : ne le désactive pas et ne le supprime pas, sinon ce token cesse de fonctionner.")


if __name__ == "__main__":
    main()
