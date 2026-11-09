#!/usr/bin/env python3
import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def rpc(url, method, params=None, timeout=60):
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
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
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
    parser.add_argument("action", choices=["status", "start", "finish"])
    parser.add_argument("uri", nargs="?", default="")
    parser.add_argument("--url", default=os.environ.get("SIGNAL_CLI_URL", "http://127.0.0.1:18080"))
    parser.add_argument("--name", default="bridge-statuts")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    if args.action == "status":
        numbers = accounts(args.url)
        if numbers:
            print(numbers[0])
    elif args.action == "start":
        print((rpc(args.url, "startLink") or {})["deviceLinkUri"])
    else:
        if not args.uri:
            print("adresse de liaison manquante", file=sys.stderr)
            sys.exit(1)
        rpc(args.url, "finishLink", {"deviceLinkUri": args.uri, "deviceName": args.name}, timeout=args.timeout)
        numbers = accounts(args.url)
        if not numbers:
            print("compte non lié", file=sys.stderr)
            sys.exit(1)
        print(numbers[0])


if __name__ == "__main__":
    main()
