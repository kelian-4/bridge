#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import time

import requests

import auto_bridge as ab


def classify(client: ab.MatrixClient, rooms: list[str]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {"pending": [], "configured": [], "linked": [], "skipped": []}
    for room_id in rooms:
        try:
            info = ab.room_info(client, room_id, wait=0)
        except requests.RequestException:
            result["skipped"].append(room_id)
            continue
        if ab.WHATSAPP_PROTOCOL not in info.protocols or info.space:
            continue
        stage = ab.get_stage(client, room_id)
        if stage == "configured":
            result["configured"].append(room_id)
        elif info.tombstoned and stage is None:
            result["skipped"].append(room_id)
        elif stage is None and "telegram" in info.protocols:
            result["linked"].append(room_id)
        else:
            result["pending"].append(room_id)
    return result


def label(client: ab.MatrixClient, room_id: str) -> str:
    try:
        return client.get_room_name(room_id) or room_id
    except requests.RequestException:
        return room_id


def main() -> int:
    parser = argparse.ArgumentParser(description="Relie les salons WhatsApp existants à Telegram")
    parser.add_argument("--execute", action="store_true", help="agit réellement, sinon simulation")
    parser.add_argument("--limit", type=int, default=0, help="nombre maximum de salons à traiter")
    args = parser.parse_args()

    client = ab.MatrixClient(ab.HOMESERVER_URL, ab.ACCESS_TOKEN, ab.USER_ID)
    if not ab.TELEGRAM_RELAY_LOGIN_ID or not ab.TELEGRAM_MAIN_LOGIN_ID:
        print("TELEGRAM_RELAY_LOGIN_ID et TELEGRAM_MAIN_LOGIN_ID doivent être renseignés dans .env")
        return 1

    groups = classify(client, client.joined_rooms())
    pending = groups["pending"]
    if args.limit > 0:
        pending = pending[: args.limit]

    print(f"Salons WhatsApp à traiter : {len(groups['pending'])}")
    print(f"Déjà configurés : {len(groups['configured'])}")
    print(f"Déjà reliés à Telegram : {len(groups['linked'])}")
    print(f"Ignorés : {len(groups['skipped'])}")
    minutes = round(len(pending) * ab.CREATE_DELAY / 60)
    print(f"Durée estimée pour {len(pending)} salons : {minutes} min")

    if not args.execute:
        for room_id in pending[:10]:
            print(f"  {label(client, room_id)}")
        if len(pending) > 10:
            print(f"  … et {len(pending) - 10} autres")
        print("Simulation. Relance avec --execute pour agir.")
        return 0

    try:
        client.disable_own_ratelimit()
    except requests.RequestException:
        pass

    token = client.sync(since=None, timeout_ms=0)["next_batch"]
    done = failed = 0
    for index, room_id in enumerate(pending, 1):
        name = label(client, room_id)
        token = ab.refresh_token(client, token)
        try:
            token = ab.process_room(client, room_id, token)
            done += 1
            print(f"[{index}/{len(pending)}] {name} : ok")
        except ab.TelegramBlocked as e:
            print(f"Telegram bloque la création de groupes : {e}")
            print(f"Arrêt après {done} salons configurés, {len(pending) - index + 1} restants")
            return 2
        except ab.TelegramRateLimit as e:
            failed += 1
            print(f"[{index}/{len(pending)}] {name} : limite Telegram, {e}")
            token = ab.refresh_token(client, token)
        except (ab.BridgeCommandError, ab.BridgeCommandTimeout) as e:
            failed += 1
            print(f"[{index}/{len(pending)}] {name} : {e}")
            token = ab.refresh_token(client, e.sync_token or token)
        except requests.RequestException as e:
            failed += 1
            print(f"[{index}/{len(pending)}] {name} : {ab.describe_error(e)}")
        except KeyboardInterrupt:
            print(f"Interrompu après {done} salons configurés")
            return 130

    print(f"Terminé : {done} configurés, {failed} en échec")
    return 0 if failed == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
