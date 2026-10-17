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


def call(method, url, data=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        sys.exit(f"Erreur HTTP {e.code} sur {method} {url.split('/_', 1)[-1]} : {e.read().decode()[:200]}")


def find_shared_secret(homeserver_yaml):
    env = os.environ.get("REGISTRATION_SHARED_SECRET", "").strip()
    if env:
        return env
    text = Path(homeserver_yaml).read_text()
    m = re.search(r"(?m)^registration_shared_secret_path:\s*(\S+)", text)
    if m:
        for candidate in (m.group(1), "Docker/secrets/" + Path(m.group(1)).name):
            p = Path(candidate)
            if p.is_file():
                return p.read_text().strip()
    m = re.search(r"(?m)^registration_shared_secret:\s*(.+)$", text)
    if m:
        return m.group(1).strip().strip("\"'")
    sys.exit("registration_shared_secret introuvable (variable REGISTRATION_SHARED_SECRET, ou --homeserver-yaml)")


def main():
    ap = argparse.ArgumentParser(description="Crée un token d'accès pour un utilisateur existant, via un administrateur temporaire (sans mot de passe).")
    ap.add_argument("--user", required=True, help="identifiant complet, ex. @toi:local.bridge")
    ap.add_argument("--homeserver", default=os.environ.get("MATRIX_HOMESERVER_URL", "http://127.0.0.1:8008"))
    ap.add_argument("--homeserver-yaml", default="Docker/synapse/homeserver.yaml")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--execute", action="store_true", help="applique (sinon simulation)")
    args = ap.parse_args()

    base = args.homeserver.rstrip("/")
    if not args.execute:
        print(f"SIMULATION : créerait un administrateur temporaire sur {base}, "
              f"obtiendrait un token pour {args.user}, l'écrirait dans {args.env_file} "
              "(MATRIX_ACCESS_TOKEN) puis désactiverait l'administrateur temporaire. Relance avec --execute.")
        return

    secret = find_shared_secret(args.homeserver_yaml)
    tmp_name = "tokenhelper" + secrets.token_hex(4)
    tmp_pw = secrets.token_hex(16)
    nonce = call("GET", f"{base}/_synapse/admin/v1/register")["nonce"]
    mac = hmac.new(secret.encode(), digestmod=hashlib.sha1)
    mac.update(nonce.encode() + b"\x00" + tmp_name.encode() + b"\x00" + tmp_pw.encode() + b"\x00" + b"admin")
    reg = call("POST", f"{base}/_synapse/admin/v1/register",
               {"nonce": nonce, "username": tmp_name, "password": tmp_pw, "admin": True, "mac": mac.hexdigest()})
    admin_token, admin_id = reg["access_token"], reg["user_id"]
    try:
        user_token = call("POST", f"{base}/_synapse/admin/v1/users/{urllib.request.quote(args.user)}/login",
                          {}, token=admin_token)["access_token"]
    finally:
        call("POST", f"{base}/_synapse/admin/v1/deactivate/{urllib.request.quote(admin_id)}", {"erase": False}, token=admin_token)

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
    print(f"Token écrit dans {args.env_file} pour {args.user}. Administrateur temporaire {admin_id} désactivé.")


if __name__ == "__main__":
    main()
