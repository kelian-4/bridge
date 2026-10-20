# bridge

Stack Matrix auto-hébergée : Synapse + bridges `mautrix-whatsapp` et `mautrix-signal`, avec des scripts Python d'orchestration (création/rattachement de salons, relay, backfill, migration de base).

## Architecture

- `Docker/` : `docker-compose.yml` (Postgres 16, Synapse, mautrix-whatsapp, mautrix-signal). Le mot de passe Postgres est lu depuis `Docker/secrets/postgres_password.txt` (non versionné).
- `nix/` : module NixOS équivalent (`bridge.nix`) et patch pour mautrix-telegram. Les secrets sont référencés par chemin de fichier (`registration_shared_secret_path`), jamais en clair.
- `script/server/` : scripts d'administration qui parlent à l'API Matrix/Synapse ; `script/termux/` : scripts de migration de base pour le Synapse Termux.

## Prérequis

Docker + Docker Compose (ou NixOS), Python 3.10+, un homeserver Synapse joignable.

## Premier démarrage

```
./Docker/setup.sh
cd Docker; docker compose up -d
```

`Docker/setup.sh` demande l'identifiant Matrix de l'administrateur (`ADMIN_MXID`), génère les secrets manquants dans `.env`, puis écrit les fichiers qui en ont besoin dans `Docker/secrets/` : mot de passe Postgres, secrets Synapse (`*_path` dans `homeserver.yaml`) et fichiers `registration` des bridges. Les tokens des bridges passent par des variables d'environnement (`env_config_prefix: BRIDGE_`). Aucun secret n'est écrit dans un fichier versionné : seuls `.env` et `Docker/secrets/` en contiennent, tous deux ignorés par git. Si une base Postgres existe déjà, le script reprend son mot de passe (ou te le demande) au lieu d'en générer un nouveau.

## Premier compte Synapse et token

Une fois la stack démarrée, crée ton compte administrateur et son token (simulation par défaut, puis `--execute`) :

```
./with-env.sh python3 script/server/get_token.py --user @toi:local.bridge --create --admin
./with-env.sh python3 script/server/get_token.py --user @toi:local.bridge --create --admin --execute
```

Le mot de passe du compte est demandé (ou lu dans la variable donnée par `--password-env`). Le token est écrit dans `.env` (`MATRIX_ACCESS_TOKEN`). Pour un compte déjà existant, retire `--create` : le token est alors rattaché à un compte d'aide `autobridge-admin`, qu'il ne faut ni désactiver ni supprimer.

Il reste ensuite à renseigner dans `.env` `INVITER_ACCESS_TOKEN` (si tu utilises `rejoin_rooms.py`) et `SIGNAL_RELAY_LOGIN_ID`.

## Configuration des scripts

Les scripts lisent leurs secrets dans l'environnement. Lancer un script avec le `.env` chargé :

```
./with-env.sh python3 script/server/mute_relay_everywhere.py
```

Variables obligatoires : `MATRIX_ACCESS_TOKEN` (tous les scripts), `INVITER_ACCESS_TOKEN` (`rejoin_rooms.py`), `PG_DSN` (`pg_to_sqlite.py`), `SIGNAL_RELAY_LOGIN_ID` (scripts Signal et `mute_relay_everywhere.py`). Les autres sont optionnelles, voir `.env.example`.

## Service permanent

`auto_bridge_signal.py` tourne en continu dans le conteneur `auto-bridge-signal` (défini dans `docker-compose.yml`, redémarré automatiquement). Il lit `.env` : tant que `MATRIX_ACCESS_TOKEN` et `SIGNAL_RELAY_LOGIN_ID` ne sont pas renseignés, il attend. Après les avoir remplis : `docker compose up -d --force-recreate auto-bridge-signal`.

## Utilisation des scripts

Les scripts qui modifient l'état (`backfill_signal.py`, `mute_relay_everywhere.py`, `rejoin_rooms.py`, `unbridge_telegram.py`) sont en **dry-run par défaut** : lancer d'abord sans option, relire la sortie, puis relancer avec `--execute`.

```
./with-env.sh python3 script/server/mute_relay_everywhere.py
./with-env.sh python3 script/server/mute_relay_everywhere.py --execute
```

## Avertissement

Ne jamais lancer deux bridges sur le même compte Signal en même temps : cela déconnecte ou corrompt la session liée.

## Sécurité

Ne jamais committer de tokens, dumps SQL, bases SQLite, `registration.yaml` ou `homeserver.yaml`. `python3 scan_secrets.py .` permet de vérifier avant un commit.
