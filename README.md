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

Optionnel : `setup.sh` propose de l'activer et demande `api_id` et `api_hash` (obtenus sur my.telegram.org) ainsi que le token d'un bot créé avec @BotFather. Ils sont stockés dans `.env` (`TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_BOT_TOKEN`) ; `TELEGRAM_ENABLED=0` désactive la question.

Le bridge est `mautrix-telegram` en **Python, version 0.15.3** (`TELEGRAM_IMAGE`), la dernière avant la réécriture en Go. Cette version n'est plus maintenue. La version Go ne sait créer que des groupes normaux, liés au compte qui les a créés, ce qui impose un compte relais restreint par Telegram et provoque des fusions de salons après `!tg upgrade` : l'historique du dépôt garde ce travail.

Deux éléments interviennent :

- **ton compte Telegram**, connecté par QR code avec la commande `login-qr` (`bridge_login.py telegram`, ou `./Docker/setup.sh`). C'est lui qui crée les supergroupes ;
- **le bot relais** (token de BotFather). Il est ajouté à chaque groupe créé et relaie les messages des utilisateurs Matrix sans compte Telegram, comme tes contacts WhatsApp. Un bot ne crée pas de groupe : il n'est donc pas concerné par les restrictions de création de Telegram.

Pour chaque nouveau salon WhatsApp, le service `auto-bridge-telegram` (`auto_bridge.py`) :

1. invite `@telegrambot`, le bot du pont ;
2. lance `!wa set-relay` côté WhatsApp ;
3. invite le fantôme du bot relais et ceux de `TELEGRAM_GHOSTS_TO_INVITE` (le bridge exige un autre utilisateur Telegram que le créateur) ;
4. appelle l'API de provisionnement du bridge, `POST /_matrix/provision/v1/portal/<salon>/create`, qui crée un supergroupe privé pour ce salon ;
5. ajoute les fantômes de `TELEGRAM_EXTRA_GHOSTS`, sans bloquer si l'ajout échoue.

Le compte Telegram doit **connaître** le bot relais, sinon le bridge ne peut pas l'ajouter au groupe (« Failed to add the following users to the chat », puis « Not enough Telegram users »). Après la connexion, `setup.sh` obtient le nom du bot auprès de Telegram (`getMe`) et envoie `search -r @nom_du_bot` dans la discussion avec le bot du pont, ce qui le fait découvrir. À refaire à la main si besoin, dans cette discussion : `search -r @nom_du_bot`.

La création passe par l'API HTTP et non par une commande `!tg` : aucun message n'est envoyé dans le salon, donc rien ne peut être relayé vers WhatsApp. Le secret de l'API est `TELEGRAM_PROVISIONING_SECRET`, et le port est publié sur `127.0.0.1` seulement.

```bash
docker compose -f Docker/docker-compose.yml logs -f auto-bridge-telegram
```

Un salon dont le supergroupe existe déjà (`409 room_already_bridged`) n'est pas recréé, et l'étape atteinte est enregistrée dans l'état du salon.

La config de `mautrix-telegram` est générée par l'image au premier lancement puis ajustée par `setup.sh`. Elle contient des secrets et n'est pas versionnée. Une ancienne configuration du bridge Go est déplacée dans `mautrix-telegram.go-<date>`, sans rien supprimer.

### Limites de Telegram

Les erreurs de Telegram sont reconnues dans la réponse de l'API :

- « A wait of N seconds is required » : le script attend N secondes (plus `RATE_LIMIT_MARGIN`) puis reprend le même salon, sans recréer de groupe. Au bout de `RATE_LIMIT_RETRIES` essais, le salon est mis en file pour un nouvel essai.
- « Too many requests » : même chose, avec une attente de `RATE_LIMIT_DEFAULT_WAIT` secondes, Telegram n'indiquant pas la durée.
- « spamreported » ou « too many channels/supergroups » : le compte ne peut plus créer de groupes. Le script s'arrête au lieu d'insister, et le service fait une pause de `TELEGRAM_BLOCKED_PAUSE` secondes.

Entre deux créations, le script attend au moins `TELEGRAM_CREATE_DELAY` secondes (30 par défaut). Cette valeur est un choix prudent, pas une limite publiée par Telegram.

### Anciens salons WhatsApp

`auto_bridge.py` ne traite que les nouvelles invitations. Pour relier les salons WhatsApp qui existent déjà :

```bash
./with-env.sh python3 script/server/backfill_telegram.py
./with-env.sh python3 script/server/backfill_telegram.py --execute --limit 20
```

Sans `--execute`, le script compte et liste sans rien envoyer. Il ignore les salons déjà configurés, déjà reliés à Telegram (état demandé à l'API du bridge), remplacés et les espaces. Il s'arrête si Telegram bloque le compte, et se relance sans risque.

## Commandes non relayées

Dans un salon bridgé, un message n'est une commande que s'il commence par le préfixe de son propre bridge (`!wa`, `!signal`, `!tg`). Sinon, l'autre bridge le relaie comme un message normal.

Le module `Docker/synapse/modules/command_notice.py` règle ce problème pour `!wa` et `!signal` : il transforme en notice (`m.notice`) tout message texte qui commence par l'un de ces préfixes. Le bridge propriétaire exécute toujours la commande, et les bridges ont `bridge_notices: false`, donc aucun ne la relaie.

Les commandes `!tg` ne sont pas filtrées : le bridge Python ignore une commande qui n'est pas du texte (vérifié sur le vrai programme). Tape-les dans la discussion privée avec le bot du pont Telegram, jamais dans un salon bridgé, où le bridge WhatsApp les relaierait. `setup.sh` l'active dans `homeserver.yaml` après avoir vérifié que l'image Synapse le charge.

Le préfixe doit être suivi d'un espace ou de la fin du message : `!wally` et `bonjour !wa` ne sont pas touchés. Les préfixes se changent dans `homeserver.yaml` (`modules`, clé `prefixes`).

Le module ne s'applique qu'au Synapse du dépôt. Un Synapse installé ailleurs doit déclarer le module lui-même.

## Statuts WhatsApp vers stories Signal

Un statut reçu dans le salon des statuts est republié comme story dans le groupe Signal du contact. La discussion et le groupe sont créés s'ils manquent, avec une pause entre deux créations.

`./Docker/setup.sh` construit signal-cli, affiche un code à scanner avec le compte Signal du pont, puis démarre `auto-status`. Sans terminal, relancer le script plus tard. `SETUP_SKIP_STATUS=1` désactive cette étape.

Variables optionnelles : `STATUS_SKIP_LIDS` (identifiants exclus, séparés par des virgules), `STATUS_CREATE_DELAY` (secondes entre deux créations).
