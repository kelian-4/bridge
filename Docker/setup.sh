#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE="../.env"
SERVER_NAME_DEFAULT="local.bridge"
YQ_IMAGE="mikefarah/yq:4"

die() { echo "Erreur: $*" >&2; exit 1; }

if ! command -v docker >/dev/null 2>&1; then
  command -v apt-get >/dev/null 2>&1 || die "docker est absent et apt-get introuvable : installe Docker manuellement (sur NixOS : virtualisation.docker.enable = true)"
  echo "Installation de Docker"
  sudo apt-get update
  sudo apt-get install -y docker.io docker-compose-v2
  sudo usermod -aG docker "$USER" || true
fi
DOCKER=(docker)
if ! docker info >/dev/null 2>&1; then
  DOCKER=(sudo docker)
  echo "Accès direct à Docker impossible : utilisation de sudo (reconnecte-toi pour que le groupe docker soit pris en compte)"
fi

rand() { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }

env_get() { { grep -E "^$1=" "$ENV_FILE" | head -n1 | cut -d= -f2- | sed 's/[[:space:]]*#.*//'; } || true; }

env_set() {
  V="$2" awk -v k="$1" 'BEGIN { v = ENVIRON["V"] } $0 ~ "^" k "=" { print k "=" v; f = 1; next } { print } END { if (!f) print k "=" v }' "$ENV_FILE" > "$ENV_FILE.tmp"
  mv "$ENV_FILE.tmp" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
}

is_placeholder() { case "$1" in ""|*CHANGE_ME*) return 0;; *) return 1;; esac; }

env_gen() {
  if is_placeholder "$(env_get "$1")"; then
    env_set "$1" "$(rand)"
    echo "  généré: $1"
  fi
}

ask() {
  [ -t 0 ] || die "$1 manquant dans .env et aucun terminal pour le demander"
  local val=""
  while true; do
    read -r -p "$2" val
    if [ -n "$val" ] && { [ -z "${3:-}" ] || [[ "$val" =~ $3 ]]; }; then break; fi
    echo "Valeur invalide." >&2
  done
  printf '%s' "$val"
}

if [ ! -f "$ENV_FILE" ]; then
  cp ../.env.example "$ENV_FILE"
  echo "Créé $ENV_FILE depuis .env.example"
fi
chmod 600 "$ENV_FILE"

SERVER_NAME="$(env_get SERVER_NAME)"
SERVER_NAME="${SERVER_NAME:-$SERVER_NAME_DEFAULT}"

ADMIN_MXID="$(env_get ADMIN_MXID)"
if is_placeholder "$ADMIN_MXID" || [[ "$ADMIN_MXID" == @admin:* ]]; then
  ADMIN_MXID="$(ask ADMIN_MXID "Identifiant Matrix de l'administrateur (nom seul, ex. kelian, ou complet @kelian:$SERVER_NAME) : " '^@?[^:@[:space:]]+(:[^[:space:]]+)?$')"
  ADMIN_MXID="@${ADMIN_MXID#@}"
  case "$ADMIN_MXID" in *:*) ;; *) ADMIN_MXID="$ADMIN_MXID:$SERVER_NAME";; esac
  env_set ADMIN_MXID "$ADMIN_MXID"
fi
is_placeholder "$(env_get MATRIX_USER_ID)" && env_set MATRIX_USER_ID "$ADMIN_MXID"
case "$(env_get MATRIX_USER_ID)" in @user:*) env_set MATRIX_USER_ID "$ADMIN_MXID";; esac

PROJECT="$(basename "$PWD" | tr '[:upper:]' '[:lower:]')"
SERVICES=(postgres synapse mautrix-signal mautrix-whatsapp auto-bridge-signal)
profile_of() {
  case "$1" in
    postgres) echo local-postgres;;
    synapse) echo local-synapse;;
    mautrix-signal) echo local-signal;;
    mautrix-whatsapp) echo local-whatsapp;;
    auto-bridge-signal) echo local-auto-bridge;;
  esac
}
is_stable_running() {
  local st started now
  st="$("${DOCKER[@]}" inspect -f '{{.State.Running}} {{.State.Restarting}} {{.State.StartedAt}}' "$1" 2>/dev/null)" || return 1
  set -- $st
  [ "$1" = "true" ] && [ "$2" = "false" ] || return 1
  started="$(date -d "$3" +%s 2>/dev/null)" || return 1
  now="$(date +%s)"
  [ $((now - started)) -ge 20 ]
}
declare -A EXT=()
profiles=()
stale=()
for c in "${SERVICES[@]}"; do
  EXT[$c]=0
  if "${DOCKER[@]}" inspect "$c" >/dev/null 2>&1; then
    owner="$("${DOCKER[@]}" inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$c" 2>/dev/null || true)"
    if [ "$owner" != "$PROJECT" ]; then
      if is_stable_running "$c"; then
        EXT[$c]=1
      else
        stale+=("$c")
      fi
    fi
  fi
  [ "${EXT[$c]}" = 1 ] || profiles+=("$(profile_of "$c")")
done
PROFILES_VALUE="$(IFS=,; echo "${profiles[*]-}")"

PG_PW_FILE="secrets/postgres_password.txt"
NEED_PG_PW=0
{ [ "${EXT[synapse]}" = 0 ] || [ "${EXT[postgres]}" = 0 ]; } && NEED_PG_PW=1
PG_EXISTS=0
{ [ -d postgres-data ] || [ "${EXT[postgres]}" = 1 ]; } && PG_EXISTS=1
if [ "$NEED_PG_PW" = 1 ] && is_placeholder "$(env_get POSTGRES_PASSWORD)"; then
  if [ -s "$PG_PW_FILE" ]; then
    env_set POSTGRES_PASSWORD "$(tr -d '\n' < "$PG_PW_FILE")"
    echo "  repris: POSTGRES_PASSWORD (depuis $PG_PW_FILE)"
  elif [ "$PG_EXISTS" = 1 ]; then
    echo "Une base Postgres existe déjà : son mot de passe ne peut pas être régénéré."
    pw="$(ask POSTGRES_PASSWORD "Mot de passe actuel du compte bridgeadmin : ")"
    env_set POSTGRES_PASSWORD "$pw"
  else
    env_gen POSTGRES_PASSWORD
  fi
fi

echo "Génération des secrets manquants"
for v in REGISTRATION_SHARED_SECRET MACAROON_SECRET_KEY FORM_SECRET SIGNAL_AS_TOKEN SIGNAL_HS_TOKEN WHATSAPP_AS_TOKEN WHATSAPP_HS_TOKEN; do
  env_gen "$v"
done
env_set COMPOSE_PROFILES "$PROFILES_VALUE"

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
export SERVER_NAME ADMIN_MXID

PG_PW="${POSTGRES_PASSWORD:-}"
if [ -n "$PG_PW" ] && ! [[ "$PG_PW" =~ ^[A-Za-z0-9_.,@+-]+$ ]]; then
  die "POSTGRES_PASSWORD contient des caractères non pris en charge (utilise lettres, chiffres et _ . , @ + -)"
fi

ln -sfn ../.env .env
mkdir -p secrets synapse mautrix-signal mautrix-whatsapp
chmod 700 secrets
write_secret() { printf '%s' "$2" > "secrets/$1"; chmod "${3:-644}" "secrets/$1"; }
if [ -n "$PG_PW" ]; then
  if [ -s "$PG_PW_FILE" ] && [ "$(tr -d '\n' < "$PG_PW_FILE")" != "$PG_PW" ] && [ "$PG_EXISTS" = 1 ]; then
    die "$PG_PW_FILE diffère de POSTGRES_PASSWORD alors qu'une base existe déjà : aligne les deux sur le mot de passe réel de la base"
  fi
  write_secret postgres_password.txt "$PG_PW" 600
fi
write_secret registration_shared_secret "$REGISTRATION_SHARED_SECRET"
write_secret macaroon_secret_key "$MACAROON_SECRET_KEY"
write_secret form_secret "$FORM_SECRET"

"${DOCKER[@]}" network inspect bridgenet >/dev/null 2>&1 || "${DOCKER[@]}" network create bridgenet >/dev/null
for c in "${SERVICES[@]}"; do
  if [ "${EXT[$c]}" = 1 ]; then
    "${DOCKER[@]}" network connect --alias "$c" bridgenet "$c" >/dev/null 2>&1 || true
    echo "En marche, réutilisé tel quel (aucune modification) : $c"
  fi
done
if [ "${#stale[@]}" -gt 0 ]; then
  echo "Conteneurs existants arrêtés, instables ou en redémarrage : ${stale[*]}"
  echo "  Ils gardent d'anciens chemins de volumes et bloquent le nom : pour les remplacer par ceux du projet (mêmes dossiers de données, rien n'est supprimé côté données) :"
  echo "    docker rm ${stale[*]}"
fi

MYUID="$(id -u)"
MYGID="$(id -g)"
env_set HOST_UID "$MYUID"
env_set HOST_GID "$MYGID"

fix_owner() {
  local dir="$1"
  [ -d "$dir" ] || return 0
  if find "$dir" -maxdepth 1 \( -type f -o -type d \) ! -user "$MYUID" 2>/dev/null | grep -q .; then
    echo "Droits corrigés sur $dir (les fichiers appartenaient à un conteneur ; contenu inchangé)"
    "${DOCKER[@]}" run --rm -u 0:0 -v "$PWD/$dir:/d" --entrypoint chown "$YQ_IMAGE" -R "$MYUID:$MYGID" /d \
      || die "impossible de corriger les droits de $dir : lance  sudo chown -R $MYUID:$MYGID $PWD/$dir  puis relance"
  fi
}
fix_owner mautrix-signal
fix_owner mautrix-whatsapp
fix_owner synapse

yq_run() {
  "${DOCKER[@]}" run --rm -u "$MYUID:$MYGID" -v "$PWD:/workdir" -e ADMIN_MXID "$YQ_IMAGE" "$@"
}

gen_registration() {
  local dir="$1" bin="$2" image="$3" as="$4" hs="$5" out="$6"
  echo "Bridge $dir"
  [ -f "$dir/config.yaml" ] || die "$dir/config.yaml absent (il est versionné dans le dépôt)"
  yq_run -i '
    .bridge.permissions[strenv(ADMIN_MXID)] = "admin" |
    del(.bridge.permissions["@admin:local.bridge"]) |
    del(.bridge.permissions["@CHANGE_ME:local.bridge"])
  ' "$dir/config.yaml"
  "${DOCKER[@]}" run --rm -u "$MYUID:$MYGID" -v "$PWD/$dir:/data" -v "$PWD/secrets:/out" \
    -e "BRIDGE_APPSERVICE__AS_TOKEN=$as" -e "BRIDGE_APPSERVICE__HS_TOKEN=$hs" \
    --entrypoint "/usr/bin/$bin" "$image" -g -c /data/config.yaml -r "/out/$out"
  chmod 644 "secrets/$out"
}

if [ "${EXT[mautrix-signal]}" = 0 ]; then
  gen_registration mautrix-signal mautrix-signal dock.mau.dev/mautrix/signal:latest "$SIGNAL_AS_TOKEN" "$SIGNAL_HS_TOKEN" signal_registration
fi
if [ "${EXT[mautrix-whatsapp]}" = 0 ]; then
  gen_registration mautrix-whatsapp mautrix-whatsapp dock.mau.dev/mautrix/whatsapp:latest "$WHATSAPP_AS_TOKEN" "$WHATSAPP_HS_TOKEN" whatsapp_registration
fi

if [ "${EXT[synapse]}" = 0 ]; then
  echo "Synapse"
  [ -f synapse/homeserver.yaml ] || die "synapse/homeserver.yaml absent (il est versionné dans le dépôt)"
  for r in signal_registration whatsapp_registration; do
    [ -f "secrets/$r" ] || die "secrets/$r absent : un bridge existant est réutilisé mais le Synapse du projet a besoin de son fichier registration (copie-le dans secrets/$r)"
  done
  if [ ! -f synapse/local.bridge.signing.key ]; then
    "${DOCKER[@]}" run --rm -u "$MYUID:$MYGID" --entrypoint python \
      -v "$PWD/synapse:/data" -v "$PWD/secrets:/run/secrets:ro" \
      matrixdotorg/synapse:latest -m synapse.app.homeserver --config-path /data/homeserver.yaml --generate-keys
  fi
fi

if [ "${EXT[mautrix-signal]}" = 0 ] || [ "${EXT[mautrix-whatsapp]}" = 0 ]; then
  if git rev-parse --git-dir >/dev/null 2>&1; then
    git update-index --skip-worktree mautrix-signal/config.yaml mautrix-whatsapp/config.yaml 2>/dev/null || true
  fi
fi

if command -v systemctl >/dev/null 2>&1; then
  active=()
  for u in matrix-synapse postgresql mautrix-signal mautrix-whatsapp; do
    systemctl is-active --quiet "$u" 2>/dev/null && active+=("$u")
  done
  if [ "${#active[@]}" -gt 0 ]; then
    echo "ATTENTION : services système actifs : ${active[*]}"
    echo "  Arrête-les avant docker compose up (ports 8008/5432 déjà pris ; jamais deux bridges sur le même compte Signal)."
  fi
fi
port_busy() { ss -ltn "sport = :$1" 2>/dev/null | grep -q LISTEN; }
if command -v ss >/dev/null 2>&1; then
  if [ "${EXT[synapse]}" = 0 ] && port_busy 8008; then
    echo "ATTENTION : le port 8008 est déjà utilisé : le Synapse du projet ne pourra pas démarrer tant qu'il n'est pas libéré."
  fi
  if [ "${EXT[postgres]}" = 0 ]; then
    pg_running="$("${DOCKER[@]}" inspect -f '{{.State.Running}}' postgres 2>/dev/null || true)"
    cur_port="$(env_get POSTGRES_HOST_PORT)"; cur_port="${cur_port:-5432}"
    if [ "$pg_running" != "true" ] && port_busy "$cur_port"; then
      new_port=""
      for p in 5433 5434 5435 5436 5437 5438 5439 5440; do
        port_busy "$p" || { new_port="$p"; break; }
      done
      [ -n "$new_port" ] || die "aucun port libre trouvé entre 5433 et 5440 pour Postgres"
      env_set POSTGRES_HOST_PORT "$new_port"
      pg_dsn="$(env_get PG_DSN)"
      case "$pg_dsn" in *127.0.0.1:"$cur_port"/*) env_set PG_DSN "${pg_dsn/127.0.0.1:$cur_port\//127.0.0.1:$new_port/}";; esac
      echo "Le port $cur_port est pris par un autre processus : Postgres du projet publié sur 127.0.0.1:$new_port à la place (POSTGRES_HOST_PORT dans .env)."
    fi
  fi
fi

echo
echo "Services qui seront créés : ${PROFILES_VALUE:-aucun (tout existe déjà)} (+ ceux sans profil)"
cat <<MSG
Démarrage :
  ${DOCKER[*]} compose up -d
Il ne reste qu'à renseigner dans $ENV_FILE les tokens Matrix obtenus après connexion :
  MATRIX_ACCESS_TOKEN, INVITER_ACCESS_TOKEN (selon les scripts), SIGNAL_RELAY_LOGIN_ID
MSG
