#!/usr/bin/env python3
import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def rpc(url, method, params=None, timeout=60, timeout_message="signal-cli injoignable"):
    payload = {"jsonrpc": "2.0", "method": method, "id": 1}
    if params:
        payload["params"] = params
    request = urllib.request.Request(
        url.rstrip("/") + "/api/v1/rpc",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read())
    except TimeoutError:
        print(timeout_message, file=sys.stderr)
        sys.exit(2)
    except (urllib.error.URLError, OSError, ValueError):
        print("signal-cli injoignable", file=sys.stderr)
        sys.exit(2)
    if "error" in body:
        print(f"signal-cli : {(body['error'] or {}).get('message', 'erreur')}", file=sys.stderr)
        sys.exit(1)
    return body.get("result")


def accounts(url):
    return [item["number"] for item in (rpc(url, "listAccounts") or []) if item.get("number")]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["status", "start", "finish", "groups"])
    parser.add_argument("uri", nargs="?", default="")
    parser.add_argument("--url", default=os.environ.get("SIGNAL_CLI_URL", "http://127.0.0.1:18080"))
    parser.add_argument("--name", default="bridge-statuts")
    parser.add_argument("--account", default=os.environ.get("SIGNAL_CLI_ACCOUNT", ""))
    parser.add_argument("--group", default="")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    if args.action == "status":
        numbers = accounts(args.url)
        if numbers:
            print(numbers[0])
    elif args.action == "groups":
        account = args.account or (accounts(args.url) or [""])[0]
        ids = [item.get("id") for item in (rpc(args.url, "listGroups", {"account": account}, timeout=180) or [])]
        print(f"{len(ids)} groupes connus")
        if args.group:
            print("groupe présent" if args.group in ids else "groupe absent")
    elif args.action == "start":
        print((rpc(args.url, "startLink") or {})["deviceLinkUri"])
    else:
        if not args.uri:
            print("adresse de liaison manquante", file=sys.stderr)
            sys.exit(1)
        before = set(accounts(args.url))
        rpc(
            args.url,
            "finishLink",
            {"deviceLinkUri": args.uri, "deviceName": args.name},
            timeout=args.timeout,
            timeout_message="Liaison expirée",
        )
        numbers = accounts(args.url)
        added = [number for number in numbers if number not in before]
        if not numbers:
            print("compte non lié", file=sys.stderr)
            sys.exit(1)
        print((added or numbers)[0])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Liaison annulée", file=sys.stderr)
        sys.exit(130)
