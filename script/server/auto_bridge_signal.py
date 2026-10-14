#!/usr/bin/env python3
from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

HOMESERVER_URL = os.environ.get("MATRIX_HOMESERVER_URL", "http://localhost:8008")
ACCESS_TOKEN = os.environ["MATRIX_ACCESS_TOKEN"]
USER_ID = os.environ.get("MATRIX_USER_ID", "@user:local.bridge")

WHATSAPP_BOT_MXID = os.environ.get("WHATSAPP_BOT_MXID", "@whatsappbot:local.bridge")
SIGNAL_BOT_MXID = os.environ.get("SIGNAL_BOT_MXID", "@signalbot:local.bridge")

# Login ID du compte Signal dédié au relais (obtenu via `list-logins` dans le
# management room), pour que les groupes soient créés par ce compte plutôt que
# par le compte personnel — évite l'erreur "group master key not found in store"
# côté membres qui n'ont jamais rejoint le groupe.
SIGNAL_RELAY_LOGIN_ID = os.environ["SIGNAL_RELAY_LOGIN_ID"]
SIGNAL_RELAY_GHOST_MXID = os.environ.get(
    "SIGNAL_RELAY_GHOST_MXID", f"@signal_{SIGNAL_RELAY_LOGIN_ID}:local.bridge"
)

BOT_RESPONSE_TIMEOUT = int(os.environ.get("BOT_RESPONSE_TIMEOUT", "30"))
ROOM_PROCESSING_DELAY = float(os.environ.get("ROOM_PROCESSING_DELAY", "2"))

SYNC_TOKEN_FILE = os.environ.get("SYNC_TOKEN_FILE", "/var/lib/auto-bridge/signal_next_batch")

ERROR_KEYWORDS = ("error", "failed", "⚠", "not found", "invalid")
CREATE_GROUP_SUCCESS_MARKER = "successfully created group"

CONFIGURED_STATE_TYPE = "fr.kelian.auto_bridge_signal"

SYNC_TIMEOUT_MS = 30_000

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("auto_bridge_signal")


class BridgeCommandError(Exception):
    pass


class BridgeCommandTimeout(Exception):
    pass


class InvalidSyncToken(Exception):
    """Levée quand Synapse rejette le token `since` (400 Bad Request)."""


@dataclass
class MatrixClient:
    homeserver_url: str
    access_token: str
    user_id: str
    session: requests.Session = field(default_factory=requests.Session)

    def __post_init__(self) -> None:
        self.session.headers.update({"Authorization": f"Bearer {self.access_token}"})

    def _url(self, path: str) -> str:
        return f"{self.homeserver_url}{path}"

    def _request(self, method: str, url: str, max_retries: int = 5, **kwargs: Any) -> requests.Response:
        """Wrapper générique avec retry/backoff sur 429 (rate limit Synapse)."""
        for attempt in range(max_retries + 1):
            resp = self.session.request(method, url, **kwargs)
            if resp.status_code != 429:
                return resp
            try:
                retry_after_ms = resp.json().get("retry_after_ms", 1000)
            except ValueError:
                retry_after_ms = 1000
            wait_s = max(retry_after_ms / 1000, 0.5)
            log.warning(
                "  429 Too Many Requests sur %s %s, attente %.1fs (tentative %d/%d)",
                method, url, wait_s, attempt + 1, max_retries,
            )
            time.sleep(wait_s)
        return resp

    def sync(self, since: str | None = None, timeout_ms: int = SYNC_TIMEOUT_MS) -> dict[str, Any]:
        params: dict[str, Any] = {"timeout": timeout_ms, "set_presence": "online"}
        if since:
            params["since"] = since
        resp = self._request(
            "GET", self._url("/_matrix/client/v3/sync"), params=params, timeout=(timeout_ms / 1000) + 15
        )
        if resp.status_code == 400 and since is not None:
            log.error("  /sync a rejeté le token `since` (400): %s", resp.text[:500])
            raise InvalidSyncToken(resp.text)
        if not resp.ok:
            log.error("  /sync a échoué (%s): %s", resp.status_code, resp.text[:500])
        resp.raise_for_status()
        return resp.json()

    def join_room(self, room_id: str) -> None:
        resp = self._request("POST", self._url(f"/_matrix/client/v3/join/{room_id}"))
        resp.raise_for_status()

    def disable_own_ratelimit(self) -> None:
        """Désactive le rate-limiting Synapse pour ce compte (messages_per_second=0, burst_count=0)."""
        resp = self._request(
            "POST",
            self._url(f"/_synapse/admin/v1/users/{self.user_id}/override_ratelimit"),
            json={"messages_per_second": 0, "burst_count": 0},
        )
        resp.raise_for_status()
        log.info("  Rate limit désactivé pour %s (messages_per_second=0, burst_count=0)", self.user_id)

    def make_room_admin(self, room_id: str, user_id: str | None = None) -> None:
        """Utilise l'API admin Synapse pour s'auto-promouvoir PL 100 dans le salon.

        Nécessite que access_token appartienne à un compte admin serveur.
        """
        target = user_id or self.user_id
        resp = self._request(
            "POST",
            self._url(f"/_synapse/admin/v1/rooms/{room_id}/make_room_admin"),
            json={"user_id": target},
        )
        resp.raise_for_status()

    def invite(self, room_id: str, user_id: str) -> None:
        resp = self._request(
            "POST",
            self._url(f"/_matrix/client/v3/rooms/{room_id}/invite"),
            json={"user_id": user_id},
        )
        if resp.status_code == 400 and "already in the room" in resp.text.lower():
            log.info("  %s est déjà dans le salon, on continue.", user_id)
            return
        resp.raise_for_status()

    def send_message(self, room_id: str, body: str) -> str:
        txn_id = str(int(time.time() * 1000))
        resp = self._request(
            "PUT",
            self._url(f"/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn_id}"),
            json={"msgtype": "m.text", "body": body},
        )
        resp.raise_for_status()
        return resp.json()["event_id"]

    def get_state_event(self, room_id: str, event_type: str, state_key: str = "") -> dict[str, Any] | None:
        resp = self._request(
            "GET", self._url(f"/_matrix/client/v3/rooms/{room_id}/state/{event_type}/{state_key}")
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def set_state_event(
        self, room_id: str, event_type: str, content: dict[str, Any], state_key: str = ""
    ) -> None:
        resp = self._request(
            "PUT",
            self._url(f"/_matrix/client/v3/rooms/{room_id}/state/{event_type}/{state_key}"),
            json=content,
        )
        resp.raise_for_status()

    def get_room_name(self, room_id: str) -> str | None:
        resp = self._request(
            "GET", self._url(f"/_matrix/client/v3/rooms/{room_id}/state/m.room.name/")
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json().get("name")

    def get_room_members(self, room_id: str) -> list[str]:
        resp = self._request("GET", self._url(f"/_matrix/client/v3/rooms/{room_id}/joined_members"))
        resp.raise_for_status()
        return list(resp.json().get("joined", {}).keys())

    def get_power_levels(self, room_id: str) -> dict[str, Any]:
        resp = self._request("GET", self._url(f"/_matrix/client/v3/rooms/{room_id}/state/m.room.power_levels/"))
        resp.raise_for_status()
        return resp.json()

    def set_power_level(self, room_id: str, user_id: str, level: int) -> None:
        content = self.get_power_levels(room_id)
        users = content.setdefault("users", {})
        users[user_id] = level
        put = self._request(
            "PUT",
            self._url(f"/_matrix/client/v3/rooms/{room_id}/state/m.room.power_levels/"),
            json=content,
        )
        if put.status_code == 403:
            users_default = content.get("users_default", 0)
            my_pl = content.get("users", {}).get(self.user_id, users_default)
            pl_event_required = content.get("events", {}).get(
                "m.room.power_levels", content.get("state_default", 50)
            )
            log.error(
                "  403 sur set_power_level: PL actuel de %s = %s, "
                "PL requis pour éditer m.room.power_levels = %s, "
                "PL cible demandé pour %s = %s.",
                self.user_id, my_pl, pl_event_required, user_id, level,
            )
        put.raise_for_status()


def wait_for_bot_reply(
    client: MatrixClient,
    room_id: str,
    expected_sender: str,
    after_sync_token: str,
    timeout: int = BOT_RESPONSE_TIMEOUT,
    reply_required: bool = True,
    require_substring: str | None = None,
) -> str:
    deadline = time.monotonic() + timeout
    since = after_sync_token

    while time.monotonic() < deadline:
        remaining_ms = max(1000, int((deadline - time.monotonic()) * 1000))
        data = client.sync(since=since, timeout_ms=min(remaining_ms, 10_000))
        since = data.get("next_batch", since)

        room_data = data.get("rooms", {}).get("join", {}).get(room_id)
        if room_data:
            for evt in room_data.get("timeline", {}).get("events", []):
                if evt.get("type") != "m.room.message":
                    continue
                if evt.get("sender") != expected_sender:
                    continue
                body = evt.get("content", {}).get("body", "")
                log.info("  Réponse de %s: %s", expected_sender, body.strip()[:200])
                lowered = body.lower()
                if any(kw in lowered for kw in ERROR_KEYWORDS):
                    raise BridgeCommandError(f"{expected_sender} a répondu avec une erreur: {body}")
                if require_substring and require_substring.lower() not in lowered:
                    continue
                return since

    if reply_required:
        raise BridgeCommandTimeout(
            f"Pas de réponse valide de {expected_sender} dans {room_id} après {timeout}s"
        )
    log.info("  Pas de réponse de %s après %ss (normal pour cette commande), on continue.", expected_sender, timeout)
    return since


def run_bridge_command(
    client: MatrixClient,
    room_id: str,
    command: str,
    expected_sender: str,
    sync_token: str,
    reply_required: bool = True,
    require_substring: str | None = None,
) -> str:
    log.info("  -> %s", command)
    client.send_message(room_id, command)
    return wait_for_bot_reply(
        client, room_id, expected_sender, sync_token,
        reply_required=reply_required, require_substring=require_substring,
    )


def process_new_room(client: MatrixClient, room_id: str, sync_token: str) -> str:
    log.info("Nouveau salon détecté: %s", room_id)

    client.join_room(room_id)
    log.info("  Rejoint le salon.")

    log.info("  Auto-promotion en admin du salon (via API admin Synapse)")
    client.make_room_admin(room_id)
    log.info("  Promotion réussie.")

    room_name = client.get_room_name(room_id) or room_id
    log.info("  Nom du salon: %s", room_name)

    already_configured = client.get_state_event(room_id, CONFIGURED_STATE_TYPE)
    if already_configured:
        log.info("  Salon %s déjà entièrement configuré, on ne rejoue rien.", room_id)
        return sync_token

    log.info("  Invitation de %s", SIGNAL_BOT_MXID)
    client.invite(room_id, SIGNAL_BOT_MXID)

    bot_deadline = time.monotonic() + 20
    while time.monotonic() < bot_deadline:
        members = set(client.get_room_members(room_id))
        if SIGNAL_BOT_MXID in members:
            break
        time.sleep(1)
    else:
        raise BridgeCommandTimeout(
            f"{SIGNAL_BOT_MXID} n'a pas rejoint {room_id} après 20s, abandon."
        )

    log.info("  Attribution du power level 100 à %s", SIGNAL_BOT_MXID)
    client.set_power_level(room_id, SIGNAL_BOT_MXID, 100)

    sync_token = run_bridge_command(client, room_id, "!wa set-relay", WHATSAPP_BOT_MXID, sync_token)

    log.info("  Invitation de %s", SIGNAL_RELAY_GHOST_MXID)
    client.invite(room_id, SIGNAL_RELAY_GHOST_MXID)

    sync_token = run_bridge_command(
        client, room_id, "!signal create-group", SIGNAL_BOT_MXID, sync_token,
        reply_required=True, require_substring=CREATE_GROUP_SUCCESS_MARKER,
    )

    sync_token = run_bridge_command(
        client, room_id, f"!signal set-relay {SIGNAL_RELAY_LOGIN_ID}", SIGNAL_BOT_MXID, sync_token
    )

    client.set_state_event(
        room_id, CONFIGURED_STATE_TYPE,
        {"configured": True, "configured_at": int(time.time())},
    )

    log.info("  Salon %s entièrement configuré.", room_id)
    return sync_token


def load_sync_token() -> str | None:
    if not SYNC_TOKEN_FILE:
        return None
    p = Path(SYNC_TOKEN_FILE)
    if p.exists():
        return p.read_text().strip() or None
    return None


def save_sync_token(token: str) -> None:
    if not SYNC_TOKEN_FILE:
        return
    p = Path(SYNC_TOKEN_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(token)


def main() -> None:
    client = MatrixClient(HOMESERVER_URL, ACCESS_TOKEN, USER_ID)
    log.info("Démarrage. Homeserver=%s, user=%s", HOMESERVER_URL, USER_ID)

    try:
        client.disable_own_ratelimit()
    except requests.RequestException as e:
        log.warning(
            "Impossible de désactiver le rate limit automatiquement (%s). "
            "Le script continuera, mais des 429 restent possibles.", e,
        )

    since = load_sync_token()
    if since is None:
        log.info("Pas de sync token sauvegardé, positionnement initial...")
        initial = client.sync(since=None, timeout_ms=0)
        since = initial["next_batch"]
        save_sync_token(since)

    while True:
        try:
            data = client.sync(since=since, timeout_ms=SYNC_TIMEOUT_MS)
        except InvalidSyncToken:
            log.warning(
                "Token de sync invalide/périmé (%s), suppression et repositionnement "
                "sur l'état courant du serveur.", SYNC_TOKEN_FILE,
            )
            try:
                Path(SYNC_TOKEN_FILE).unlink(missing_ok=True)
            except OSError as e:
                log.warning("Impossible de supprimer %s (%s), on continue quand même.", SYNC_TOKEN_FILE, e)
            initial = client.sync(since=None, timeout_ms=0)
            since = initial["next_batch"]
            save_sync_token(since)
            continue
        except requests.RequestException as e:
            log.warning("Erreur réseau pendant /sync (%s), retry dans 5s.", e)
            time.sleep(5)
            continue

        since = data.get("next_batch", since)
        save_sync_token(since)

        invites = data.get("rooms", {}).get("invite", {})
        if len(invites) > 1:
            log.info("%d nouveaux salons détectés simultanément, traitement séquentiel.", len(invites))

        for i, room_id in enumerate(invites):
            if i > 0:
                time.sleep(ROOM_PROCESSING_DELAY)
            try:
                since = process_new_room(client, room_id, since)
                save_sync_token(since)
            except (BridgeCommandError, BridgeCommandTimeout) as e:
                log.error("Échec de configuration du salon %s: %s", room_id, e)
            except requests.RequestException as e:
                log.error("Erreur réseau pendant le traitement de %s: %s", room_id, e)


if __name__ == "__main__":
    main()
