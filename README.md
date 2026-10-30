# bridge

Stack Matrix auto-hébergée qui relie **WhatsApp** et **Signal** grâce à [mautrix](https://docs.mau.fi/bridges/). Un seul client Matrix (Element, etc.) permet de lire et d'écrire dans les deux messageries, et les conversations WhatsApp sont automatiquement répliquées côté Signal. **Telegram** est optionnel (voir [Telegram](#telegram)).

```
 WhatsApp ──► mautrix-whatsapp ─┐
                                ├──► Synapse (Matrix) ──► Element / client Matrix
 Signal   ──► mautrix-signal  ──┘          │
                                       Postgres
                  auto-bridge-signal ──────┘  (orchestration des salons)
```


- Déploie via Docker Compose : Postgres, Synapse, `mautrix-whatsapp`, `mautrix-signal` et un service d'orchestration.
- Génère tous les secrets, les fichiers d'enregistrement des bridges (*registrations*) et le compte administrateur.
- Connecte Signal et WhatsApp en affichant le **QR code directement dans le terminal**.
- Détecte les nouveaux salons WhatsApp et crée automatiquement le groupe Signal correspondant (voir [Auto-bridge Signal](#auto-bridge-signal)).
- Fournit des scripts de maintenance (rattrapage des salons, mute du compte relais, etc.).

## Prérequis

- Docker avec le plugin Compose, Python 3.10+, `curl`, `awk`, `ss`.
- Un téléphone avec WhatsApp et Signal pour scanner les QR codes.

## Installation rapide

```bash
git clone https://github.com/kelian-4/bridge.git
cd bridge
./Docker/setup.sh
```

Le script gère toute la chaîne : prérequis, images, secrets, configuration, démarrage de la stack, compte administrateur, tokens, connexion de Signal et de WhatsApp, puis lancement du service `auto-bridge-signal`.Il détecte aussi les services déjà en marche (Synapse, Postgres, bridges) et ne les recrée pas.

À la fin, il affiche l'URL du serveur (par défaut `http://127.0.0.1:8008`) et le compte administrateur.

Variables d'environnement optionnelles :

| Variable | Effet |
| --- | --- |
| `ADMIN_PASSWORD` | mot de passe du compte administrateur |
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
  termux/                   script pour termux
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

Les groupes sont ainsi créés par un compte Signal lie et non par le compte personnel.
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

## Telegram

Optionnel : `setup.sh` propose de l'activer et demande `api_id` et `api_hash` (obtenus sur my.telegram.org). Ils sont stockés dans `.env` (`TELEGRAM_API_ID`, `TELEGRAM_API_HASH`) ; `TELEGRAM_ENABLED=0` désactive la question.

Le bridge est `mautrix-telegram` en Go (bridgev2). Il crée uniquement des **groupes Telegram normaux**, privés : sans lien public ni nom d'utilisateur, accessibles seulement à leurs participants.

Deux comptes Telegram sont connectés par QR code, comme pour Signal :

- ton compte principal (`bridge_login.py telegram`), dont l'identifiant est enregistré dans `TELEGRAM_MAIN_LOGIN_ID`. Son fantôme est invité dans chaque salon pour être participant du groupe : sans autre membre que le relais, Telegram refuse de créer le groupe ;
- le compte relais (`bridge_login.py telegram --relay`), qui crée les groupes et relaie les messages WhatsApp. Son identifiant est enregistré dans `TELEGRAM_RELAY_LOGIN_ID`. Un bot ne peut pas créer de groupe, il faut un vrai compte.

`TELEGRAM_GHOSTS_TO_INVITE` (liste de MXID de fantômes, séparés par des virgules) reste disponible pour inviter d'autres participants.

Pour chaque nouveau salon WhatsApp, le service `auto-bridge-telegram` (`auto_bridge.py`) :

1. invite `@telegrambot` ;
2. lance `!wa set-relay` côté WhatsApp ;
3. invite les fantômes du compte principal et du compte relais ;
4. lance `!tg create-group group` ;
5. lance `!tg set-relay <TELEGRAM_RELAY_LOGIN_ID>`.

Le service démarre quand `TELEGRAM_RELAY_LOGIN_ID` et `TELEGRAM_MAIN_LOGIN_ID` sont renseignés.

```bash
docker compose -f Docker/docker-compose.yml logs -f auto-bridge-telegram
```

La config de `mautrix-telegram` est générée par l'image au premier lancement puis ajustée par `setup.sh`. Elle n'est pas versionnée.
