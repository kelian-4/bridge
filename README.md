# bridge

Stack Matrix auto-hébergée qui relie **WhatsApp** et **Signal** grâce à [mautrix](https://docs.mau.fi/bridges/). Un seul client Matrix (Element, etc.) permet de lire et d'écrire dans les deux messageries, et les conversations WhatsApp sont automatiquement répliquées côté Signal. **Telegram** est prévu (voir [Feuille de route](#feuille-de-route)).

```
 WhatsApp ──► mautrix-whatsapp ─┐
                                ├──► Synapse (Matrix) ──► Element / client Matrix
 Signal   ──► mautrix-signal  ──┘          │
                                       Postgres
                  auto-bridge-signal ──────┘  (orchestration des salons)
```

## Sommaire

- [Ce que fait le projet](#ce-que-fait-le-projet)
- [Prérequis](#prérequis)
- [Installation rapide](#installation-rapide)
- [Configuration](#configuration)
- [Structure du dépôt](#structure-du-dépôt)
- [Scripts](#scripts)
- [Auto-bridge Signal](#auto-bridge-signal)
- [Déploiement NixOS](#déploiement-nixos)
- [Termux](#termux)
- [Feuille de route](#feuille-de-route)
- [Sécurité](#sécurité)

## Ce que fait le projet

- Déploie via Docker Compose : Postgres, Synapse, `mautrix-whatsapp`, `mautrix-signal` et un service d'orchestration.
- Génère tous les secrets, les fichiers d'enregistrement des bridges (*registrations*) et le compte administrateur.
- Connecte Signal et WhatsApp en affichant le **QR code directement dans le terminal**.
- Détecte les nouveaux salons WhatsApp et crée automatiquement le groupe Signal correspondant (voir [Auto-bridge Signal](#auto-bridge-signal)).
- Fournit des scripts de maintenance (rattrapage des salons, mute du compte relais, etc.).

## Prérequis

- Linux avec `apt` ou `pacman` (le script installe les prérequis manquants après confirmation).
- Docker avec le plugin Compose, Python 3.10+, `curl`, `awk`, `ss`.
- Un téléphone avec WhatsApp et Signal pour scanner les QR codes.

## Installation rapide

```bash
git clone -b dev https://github.com/kelian-4/bridge.git
cd bridge
./Docker/setup.sh
```

Le script gère toute la chaîne : prérequis, images, secrets, configuration, démarrage de la stack, compte administrateur, tokens, connexion de Signal et de WhatsApp, puis lancement du service `auto-bridge-signal`. Il est **idempotent** : on peut le relancer, ce qui existe est conservé. Il détecte aussi les services déjà en marche (Synapse, Postgres, bridges) et ne les recrée pas.

À la fin, il affiche l'URL du serveur (par défaut `http://127.0.0.1:8008`) et le compte administrateur.

Variables d'environnement optionnelles :

| Variable | Effet |
| --- | --- |
| `ADMIN_PASSWORD` | mot de passe du compte administrateur, sans saisie |
| `SETUP_BRIDGES=signal,whatsapp` | bridges à connecter sans question |
| `SETUP_YES=1` | installe les prérequis sans demander |
| `SETUP_SKIP_LOGIN=1` | ne connecte aucun bridge |
| `SETUP_WAIT` | nombre d'attentes de Synapse (60 par défaut, 2 s chacune) |

## Configuration

`setup.sh` crée `.env` à partir de `.env.example` et génère les secrets manquants. Les principales variables :

| Variable | Rôle |
| --- | --- |
| `SERVER_NAME` | nom du serveur Matrix (`local.bridge` par défaut) |
| `ADMIN_MXID` | compte administrateur, par ex. `@toi:local.bridge` |
| `MATRIX_HOMESERVER_URL` | URL de Synapse pour les scripts |
| `MATRIX_ACCESS_TOKEN`, `INVITER_ACCESS_TOKEN` | tokens utilisés par les scripts |
| `SIGNAL_RELAY_LOGIN_ID` | identifiant du compte Signal dédié au relais |
| `WHATSAPP_BOT_MXID`, `SIGNAL_BOT_MXID`, `TELEGRAM_BOT_MXID` | MXID des bots de gestion |
| `BOT_RESPONSE_TIMEOUT`, `ROOM_PROCESSING_DELAY`, `CONFIRM_DELAY` | délais de l'auto-bridge |
| `PG_DSN` | connexion Postgres pour les scripts qui lisent la base |
| `LOG_LEVEL` | niveau de logs des scripts |

Les images Docker se changent dans `.env` : `SIGNAL_IMAGE`, `WHATSAPP_IMAGE`, `SYNAPSE_IMAGE`, `POSTGRES_IMAGE`.

Les secrets vivent dans `.env` et `Docker/secrets/`, tous deux ignorés par git. Les services Compose sont activés par *profiles* (`COMPOSE_PROFILES`), ce qui permet de réutiliser un Synapse ou un Postgres déjà présent sur la machine.

## Structure du dépôt

```
Docker/
  docker-compose.yml        stack complète (réseau externe `bridgenet`)
  setup.sh                  installation et configuration automatiques
  synapse/                  homeserver.yaml, config de logs
  mautrix-signal/           config.yaml
  mautrix-whatsapp/         config.yaml
  postgres-init/            crée les bases synapse, mautrix_whatsapp, mautrix_signal
  auto-bridge/              image du service auto-bridge-signal
script/
  server/                   administration Matrix / Synapse
  termux/                   migration Postgres → SQLite
nix/                        module NixOS équivalent
with-env.sh                 lance une commande avec .env chargé
scan_secrets.py             détecte les secrets avant un commit
sanitize_scripts.py         remplace les secrets codés en dur par des variables d'environnement
```

## Scripts

Lancer un script avec `.env` chargé :

```bash
./with-env.sh python3 script/server/<script>.py
```

| Script | Rôle |
| --- | --- |
| `get_token.py --user @toi:domaine --create --admin --execute` | crée le compte et son token d'accès |
| `bridge_login.py <signal\|whatsapp\|telegram>` | connecte un bridge, QR code affiché dans le terminal |
| `auto_bridge_signal.py` | service continu qui réplique les salons WhatsApp vers Signal |
| `backfill_signal.py` | applique le flux Signal aux salons WhatsApp existants |
| `mute_relay_everywhere.py` | met le compte relais à un power level de -1 dans les salons WhatsApp |
| `rejoin_rooms.py` | rejoint de nouveau les salons après une désactivation/réactivation de compte |
| `unbridge_telegram.py` | retire les bridges Telegram existants (`!tg unbridge`) |

Les scripts de maintenance (`backfill_signal`, `mute_relay_everywhere`, `rejoin_rooms`, `unbridge_telegram`) tournent en **simulation par défaut** : ajouter `--execute` pour appliquer réellement.

## Auto-bridge Signal

`auto_bridge_signal.py` écoute les invitations Matrix. Pour chaque nouveau salon WhatsApp, il :

1. invite `@signalbot` ;
2. lance `!wa set-relay` côté WhatsApp ;
3. invite le compte fantôme (*ghost*) du compte Signal dédié au relais ;
4. lance `!signal create-group` ;
5. lance `!signal set-relay <SIGNAL_RELAY_LOGIN_ID>`.

Les groupes sont ainsi créés par un compte Signal dédié et non par le compte personnel, ce qui évite l'erreur « group master key not found in store » chez les membres qui n'ont jamais rejoint le groupe.

Le service tourne dans le conteneur `auto-bridge-signal` et démarre une fois `SIGNAL_RELAY_LOGIN_ID` renseigné (obtenu après la connexion de Signal). Son état de synchronisation est conservé dans le volume `auto_bridge_state`.

```bash
docker compose -f Docker/docker-compose.yml logs -f auto-bridge-signal
```

## Déploiement NixOS

`nix/bridge.nix` décrit la même stack sans Docker (Postgres 16, Synapse, mautrix-whatsapp, mautrix-signal et mautrix-telegram). `nix/0001-fix-empty-body-media-drop.patch` est un correctif appliqué à l'un des bridges pour éviter la perte de médias dont le corps est vide.

## Termux

`script/termux/` sert à faire tourner Synapse sur Android avec SQLite :

- `pg_to_sqlite.py` convertit une base Postgres Synapse en SQLite (requiert `PG_DSN` et `psycopg2`) ;
- `fix_sqlite_constraints.py` recrée les contraintes UNIQUE / PRIMARY KEY manquantes après la conversion.

## Feuille de route

- [ ] **Telegram** : ajouter `mautrix-telegram` à `Docker/docker-compose.yml` (service, profile `local-telegram`, base `mautrix_telegram`, registration et tokens générés par `setup.sh`).
- [ ] Étendre `setup.sh` : `connect_bridge telegram` et variables `TELEGRAM_AS_TOKEN` / `TELEGRAM_HS_TOKEN`.
- [ ] Définir le comportement de l'auto-bridge pour Telegram (équivalent du relais Signal).

Déjà en place pour Telegram : le module NixOS, `TELEGRAM_BOT_MXID`, `bridge_login.py telegram` et `unbridge_telegram.py`.

## Avertissement

Ne jamais lancer deux bridges sur le même compte Signal en même temps.

## Sécurité

- `python3 scan_secrets.py .` vérifie qu'aucun secret n'est présent avant un commit (code de sortie 1 si un secret est trouvé).
- `python3 sanitize_scripts.py` remplace les secrets codés en dur par des lectures de variables d'environnement (`--execute` pour écrire).
- `.env` et `Docker/secrets/` ne doivent jamais être commités.
- Synapse n'écoute que sur `127.0.0.1:8008` : pour un accès externe, placer un reverse proxy TLS devant.
