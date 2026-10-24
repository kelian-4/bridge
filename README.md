# bridge

Stack Matrix auto-hébergée : Synapse, Postgres, mautrix-whatsapp, mautrix-signal et scripts d'orchestration.

## Installation

```
./Docker/setup.sh
```

Le script fait tout : prérequis (Docker, Python 3.10+), images, secrets, configuration, démarrage de la stack, compte administrateur, tokens, connexion de Signal et de WhatsApp par QR code dans le terminal, service `auto-bridge-signal`. Il peut être relancé : ce qui existe est conservé.

Variables d'environnement optionnelles :

| Variable | Effet |
| --- | --- |
| `ADMIN_PASSWORD` | mot de passe du compte administrateur sans saisie |
| `SETUP_BRIDGES=signal,whatsapp` | bridges à connecter sans question |
| `SETUP_YES=1` | installation des prérequis sans question |
| `SETUP_SKIP_LOGIN=1` | aucune connexion de bridge |

Les images Docker se changent dans `.env` : `SIGNAL_IMAGE`, `WHATSAPP_IMAGE`, `SYNAPSE_IMAGE`, `POSTGRES_IMAGE`.

## Structure

- `Docker/` : `docker-compose.yml`, configuration de Synapse et des bridges, `setup.sh`.
- `script/server/` : scripts d'administration Matrix et Synapse.
- `script/termux/` : migration de base pour le Synapse Termux.
- `nix/` : module NixOS équivalent.

Les secrets sont dans `.env` et `Docker/secrets/`, ignorés par git.

## Scripts

Lancer un script avec `.env` chargé :

```
./with-env.sh python3 script/server/<script>.py
```

| Script | Rôle |
| --- | --- |
| `get_token.py --user @toi:domaine --create --admin --execute` | crée le compte et son token |
| `bridge_login.py signal` | connecte un bridge depuis le terminal, aussi `whatsapp` et `telegram` |
| `backfill_signal.py`, `mute_relay_everywhere.py`, `rejoin_rooms.py`, `unbridge_telegram.py` | maintenance des salons, simulation par défaut, `--execute` pour appliquer |

`auto_bridge_signal.py` tourne en continu dans le conteneur `auto-bridge-signal`.

## Avertissement

Ne jamais lancer deux bridges sur le même compte Signal en même temps.

## Sécurité

`python3 scan_secrets.py .` vérifie qu'aucun secret n'est présent avant un commit.
