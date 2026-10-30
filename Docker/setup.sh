#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE="../.env"
SERVER_NAME_DEFAULT="local.bridge"
YQ_IMAGE="mikefarah/yq:4"
GEN=0

die() { echo "Erreur : $*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }

confirm() {
  [ "${SETUP_YES:-}" = 1 ] && return 0
  [ -t 0 ] || return 1
  local rep
  read -r -p "$1 [O/n] " rep
  case "$rep" in n|N|non|no) return 1;; *) return 0;; esac
}

quiet() {
  local out
  out="$("$@" 2>&1)" && return 0
  printf '%s\n' "$out" | tail -n 3 >&2
  return 1
}

as_root() { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi; }

SUDO_PID=""
acquire_sudo() {
  [ "$(id -u)" -eq 0 ] && return 0
  command -v sudo >/dev/null 2>&1 || die "sudo requis"
  sudo -v || die "mot de passe sudo refusé"
  ( while true; do sudo -n true 2>/dev/null || exit 0; sleep 40; done ) &
  SUDO_PID=$!
  trap 'if [ -n "$SUDO_PID" ]; then kill "$SUDO_PID" 2>/dev/null || true; fi' EXIT
}

PM=""
if command -v apt-get >/dev/null 2>&1; then PM=apt; elif command -v pacman >/dev/null 2>&1; then PM=pacman; fi

pkg_for() {
  case "$PM:$1" in
    apt:docker) echo docker.io;;           pacman:docker) echo docker;;
    apt:compose) echo docker-compose-v2;;  pacman:compose) echo docker-compose;;
    apt:python) echo python3;;             pacman:python) echo python;;
    apt:ss|pacman:ss) echo iproute2;;
    apt:awk|pacman:awk) echo gawk;;
    apt:curl|pacman:curl) echo curl;;
  esac
}

python_ok() { command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; }

check_prereqs() {
  missing=()
  command -v docker >/dev/null 2>&1 || missing+=(docker)
  if command -v docker >/dev/null 2>&1; then docker compose version >/dev/null 2>&1 || missing+=(compose); else missing+=(compose); fi
  python_ok || missing+=(python)
  command -v awk >/dev/null 2>&1 || missing+=(awk)
  command -v ss >/dev/null 2>&1 || missing+=(ss)
  command -v curl >/dev/null 2>&1 || missing+=(curl)
}

check_prereqs
need_sudo=0
if [ "${#missing[@]}" -gt 0 ]; then
  need_sudo=1
elif ! docker info >/dev/null 2>&1; then
  need_sudo=1
fi
if [ "$need_sudo" = 1 ]; then acquire_sudo; fi

if [ "${#missing[@]}" -gt 0 ]; then
  say "Prérequis manquants : ${missing[*]}"
  [ -n "$PM" ] || die "installe ${missing[*]} à la main"
  pkgs=()
  for m in "${missing[@]}"; do pkgs+=("$(pkg_for "$m")"); done
  confirm "Installer ${pkgs[*]} ?" || die "installe ${pkgs[*]} puis relance"
  say "Installation de ${pkgs[*]}"
  case "$PM" in
    apt) quiet as_root apt-get update && quiet as_root apt-get install -y "${pkgs[@]}" || die "installation impossible";;
    pacman) quiet as_root pacman -S --needed --noconfirm "${pkgs[@]}" || die "installation impossible, lance : sudo pacman -Syu";;
  esac
  if printf '%s\n' "${missing[@]}" | grep -qx docker; then quiet as_root usermod -aG docker "${USER:-$(id -un)}" || true; fi
  check_prereqs
  [ "${#missing[@]}" -eq 0 ] || die "toujours manquant : ${missing[*]}"
fi

DOCKER=(docker)
if ! docker info >/dev/null 2>&1; then
  docker_err="$(docker info 2>&1 || true)"
  case "$docker_err" in
    *[Pp]ermission\ denied*)
      DOCKER=(sudo docker)
      ;;
    *)
      if command -v systemctl >/dev/null 2>&1 && ! systemctl is-active --quiet docker 2>/dev/null; then
        confirm "Docker est arrêté. Le démarrer ?" || die "démarre docker : sudo systemctl enable --now docker"
        quiet as_root systemctl enable --now docker || die "docker ne démarre pas : systemctl status docker"
        sleep 2
      fi
      if ! docker info >/dev/null 2>&1; then DOCKER=(sudo docker); fi
      ;;
  esac
  "${DOCKER[@]}" info >/dev/null 2>&1 || die "docker injoignable"
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
    GEN=$((GEN + 1))
  fi
}

ask() {
  [ -t 0 ] || die "$1 absent de .env"
  local val=""
  while true; do
    read -r -p "$2" val
    if [ -n "$val" ] && { [ -z "${3:-}" ] || [[ "$val" =~ $3 ]]; }; then break; fi
    echo "Valeur invalide" >&2
  done
  printf '%s' "$val"
}

if [ ! -f "$ENV_FILE" ]; then
  cp ../.env.example "$ENV_FILE"
  say "Fichier .env créé"
fi
chmod 600 "$ENV_FILE"

SERVER_NAME="$(env_get SERVER_NAME)"
SERVER_NAME="${SERVER_NAME:-$SERVER_NAME_DEFAULT}"

ADMIN_MXID="$(env_get ADMIN_MXID)"
if is_placeholder "$ADMIN_MXID" || [[ "$ADMIN_MXID" == @admin:* ]]; then
  ADMIN_MXID="$(ask ADMIN_MXID "Compte administrateur (ex. kelian) : " '^@?[^:@[:space:]]+(:[^[:space:]]+)?$')"
  ADMIN_MXID="@${ADMIN_MXID#@}"
  case "$ADMIN_MXID" in *:*) ;; *) ADMIN_MXID="$ADMIN_MXID:$SERVER_NAME";; esac
  env_set ADMIN_MXID "$ADMIN_MXID"
fi
is_placeholder "$(env_get MATRIX_USER_ID)" && env_set MATRIX_USER_ID "$ADMIN_MXID"
case "$(env_get MATRIX_USER_ID)" in @user:*) env_set MATRIX_USER_ID "$ADMIN_MXID";; esac
for kind in signal whatsapp telegram; do
  key="$(printf '%s' "$kind" | tr a-z A-Z)_BOT_MXID"
  case "$(env_get "$key")" in *":$SERVER_NAME") ;; *) env_set "$key" "@${kind}bot:$SERVER_NAME";; esac
done
case "$(env_get INVITER_USER_ID)" in @inviter:*|"") env_set INVITER_USER_ID "@whatsappbot:$SERVER_NAME";; *":$SERVER_NAME") ;; *) env_set INVITER_USER_ID "@whatsappbot:$SERVER_NAME";; esac

TELEGRAM_ON=0
if [ "$(env_get TELEGRAM_ENABLED)" != 0 ]; then
  tg_id="$(env_get TELEGRAM_API_ID)"
  tg_hash="$(env_get TELEGRAM_API_HASH)"
  if is_placeholder "$tg_id" || is_placeholder "$tg_hash"; then
    if [ -t 0 ] && [ "${SETUP_YES:-}" != 1 ] && [ "${SETUP_TELEGRAM:-}" != 0 ] && confirm "Activer Telegram ?"; then
      tg_id="$(ask TELEGRAM_API_ID "Telegram api_id : " '^[0-9]+$')"
      tg_hash="$(ask TELEGRAM_API_HASH "Telegram api_hash : " '^[0-9a-fA-F]{32}$')"
      env_set TELEGRAM_API_ID "$tg_id"
      env_set TELEGRAM_API_HASH "$tg_hash"
    elif [ -t 0 ] && [ "${SETUP_YES:-}" != 1 ] && [ "${SETUP_TELEGRAM:-}" != 0 ]; then
      env_set TELEGRAM_ENABLED 0
      say "telegram ignoré"
    fi
  fi
  if ! is_placeholder "$(env_get TELEGRAM_API_ID)" && ! is_placeholder "$(env_get TELEGRAM_API_HASH)"; then
    TELEGRAM_ON=1
    env_set TELEGRAM_ENABLED 1
  fi
fi

PROJECT="$(basename "$PWD" | tr '[:upper:]' '[:lower:]')"
SERVICES=(postgres synapse mautrix-signal mautrix-whatsapp mautrix-telegram auto-bridge-signal auto-bridge-telegram)
profile_of() {
  case "$1" in
    postgres) echo local-postgres;;
    synapse) echo local-synapse;;
    mautrix-signal) echo local-signal;;
    mautrix-whatsapp) echo local-whatsapp;;
    mautrix-telegram) echo local-telegram;;
    auto-bridge-signal|auto-bridge-telegram) echo local-auto-bridge;;
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
      if is_stable_running "$c"; then EXT[$c]=1; else stale+=("$c"); fi
    fi
  fi
  if [ "${EXT[$c]}" = 0 ]; then profiles+=("$(profile_of "$c")"); fi
done
PROFILES_VALUE="$(IFS=,; echo "${profiles[*]-}")"

for c in "${SERVICES[@]}"; do
  if [ "${EXT[$c]}" = 1 ]; then say "$c déjà en marche"; fi
done
if [ "${#stale[@]}" -gt 0 ]; then
  say "Anciens conteneurs : ${stale[*]}"
  die "supprime-les puis relance : docker rm ${stale[*]}"
fi

img() { local v; v="$(env_get "$1")"; if [ -n "$v" ]; then echo "$v"; else echo "$2"; fi; }
SIGNAL_IMG="$(img SIGNAL_IMAGE dock.mau.dev/mautrix/signal:latest)"
WHATSAPP_IMG="$(img WHATSAPP_IMAGE dock.mau.dev/mautrix/whatsapp:latest)"
TELEGRAM_IMG="$(img TELEGRAM_IMAGE dock.mau.dev/mautrix/telegram:latest)"
SYNAPSE_IMG="$(img SYNAPSE_IMAGE matrixdotorg/synapse:latest)"
POSTGRES_IMG="$(img POSTGRES_IMAGE postgres:16-alpine)"

diagnose_registry() {
  local host="$1" code4 code
  if command -v getent >/dev/null 2>&1 && ! getent hosts "$host" >/dev/null 2>&1; then
    say "$host : DNS introuvable"
    return
  fi
  code4="$(curl -4 -sS -m 15 -o /dev/null -w '%{http_code}' "https://$host/v2/" 2>/dev/null || true)"
  code="$(curl -sS -m 15 -o /dev/null -w '%{http_code}' "https://$host/v2/" 2>/dev/null || true)"
  case "$code" in
    200|401|403|404) say "$host : accessible, le démon Docker n'y accède pas";;
    *)
      case "$code4" in
        200|401|403|404) say "$host : IPv6 défaillant, désactive-le : sudo sysctl -w net.ipv6.conf.all.disable_ipv6=1";;
        *) say "$host : connexion coupée par le réseau";;
      esac;;
  esac
}

pull_image() {
  local image="$1" n host
  "${DOCKER[@]}" image inspect "$image" >/dev/null 2>&1 && return 0
  say "Téléchargement de $image"
  for n in 1 2 3; do
    "${DOCKER[@]}" pull -q "$image" >/dev/null 2>&1 && return 0
    sleep 3
  done
  host="${image%%/*}"
  case "$host" in *.*|*:*) ;; *) host="registry-1.docker.io";; esac
  say "Image indisponible : $image"
  diagnose_registry "$host"
  die "images : définis SIGNAL_IMAGE, WHATSAPP_IMAGE, TELEGRAM_IMAGE, SYNAPSE_IMAGE ou POSTGRES_IMAGE dans .env"
}

pull_image "$YQ_IMAGE"
[ "${EXT[postgres]}" = 1 ] || pull_image "$POSTGRES_IMG"
[ "${EXT[synapse]}" = 1 ] || pull_image "$SYNAPSE_IMG"
[ "${EXT[mautrix-signal]}" = 1 ] || pull_image "$SIGNAL_IMG"
[ "${EXT[mautrix-whatsapp]}" = 1 ] || pull_image "$WHATSAPP_IMG"
if [ "$TELEGRAM_ON" = 1 ] && [ "${EXT[mautrix-telegram]}" = 0 ]; then pull_image "$TELEGRAM_IMG"; fi
if [ "${EXT[auto-bridge-signal]}" = 0 ] || { [ "$TELEGRAM_ON" = 1 ] && [ "${EXT[auto-bridge-telegram]}" = 0 ]; }; then pull_image "python:3.12-slim"; fi

PG_PW_FILE="secrets/postgres_password.txt"
NEED_PG_PW=0
{ [ "${EXT[synapse]}" = 0 ] || [ "${EXT[postgres]}" = 0 ]; } && NEED_PG_PW=1
PG_EXISTS=0
{ [ -d postgres-data ] || [ "${EXT[postgres]}" = 1 ]; } && PG_EXISTS=1
if [ "$NEED_PG_PW" = 1 ] && is_placeholder "$(env_get POSTGRES_PASSWORD)"; then
  if [ -s "$PG_PW_FILE" ]; then
    env_set POSTGRES_PASSWORD "$(tr -d '\n' < "$PG_PW_FILE")"
  elif [ "$PG_EXISTS" = 1 ]; then
    env_set POSTGRES_PASSWORD "$(ask POSTGRES_PASSWORD "Mot de passe Postgres bridgeadmin : ")"
  else
    env_gen POSTGRES_PASSWORD
  fi
fi

secret_keys=(REGISTRATION_SHARED_SECRET MACAROON_SECRET_KEY FORM_SECRET SIGNAL_AS_TOKEN SIGNAL_HS_TOKEN WHATSAPP_AS_TOKEN WHATSAPP_HS_TOKEN)
if [ "$TELEGRAM_ON" = 1 ]; then secret_keys+=(TELEGRAM_AS_TOKEN TELEGRAM_HS_TOKEN); fi
for v in "${secret_keys[@]}"; do
  env_gen "$v"
done
env_set COMPOSE_PROFILES "$PROFILES_VALUE"
if [ "$GEN" -gt 0 ]; then say "Secrets générés"; else say "Secrets déjà présents"; fi

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
export SERVER_NAME ADMIN_MXID

PG_PW="${POSTGRES_PASSWORD:-}"
if [ -n "$PG_PW" ] && ! [[ "$PG_PW" =~ ^[A-Za-z0-9_.,@+-]+$ ]]; then
  die "POSTGRES_PASSWORD : caractères autorisés A-Z a-z 0-9 _ . , @ + -"
fi

ln -sfn ../.env .env
mkdir -p secrets synapse mautrix-signal mautrix-whatsapp
if [ "$TELEGRAM_ON" = 1 ]; then mkdir -p mautrix-telegram; fi
chmod 700 secrets
write_secret() { printf '%s' "$2" > "secrets/$1"; chmod "${3:-644}" "secrets/$1"; }
if [ -n "$PG_PW" ]; then
  if [ -s "$PG_PW_FILE" ] && [ "$(tr -d '\n' < "$PG_PW_FILE")" != "$PG_PW" ] && [ "$PG_EXISTS" = 1 ]; then
    die "$PG_PW_FILE et POSTGRES_PASSWORD diffèrent"
  fi
  write_secret postgres_password.txt "$PG_PW" 600
fi
write_secret registration_shared_secret "$REGISTRATION_SHARED_SECRET"
write_secret macaroon_secret_key "$MACAROON_SECRET_KEY"
write_secret form_secret "$FORM_SECRET"
[ -e secrets/telegram_registration ] || write_secret telegram_registration ""

"${DOCKER[@]}" network inspect bridgenet >/dev/null 2>&1 || "${DOCKER[@]}" network create bridgenet >/dev/null
for c in "${SERVICES[@]}"; do
  if [ "${EXT[$c]}" = 1 ]; then
    "${DOCKER[@]}" network connect --alias "$c" bridgenet "$c" >/dev/null 2>&1 || true
  fi
done

MYUID="$(id -u)"
MYGID="$(id -g)"
env_set HOST_UID "$MYUID"
env_set HOST_GID "$MYGID"

fix_owner() {
  local dir="$1"
  [ -d "$dir" ] || return 0
  if find "$dir" -maxdepth 1 \( -type f -o -type d \) ! -user "$MYUID" 2>/dev/null | grep -q .; then
    quiet "${DOCKER[@]}" run --rm -u 0:0 -v "$PWD/$dir:/d" --entrypoint chown "$YQ_IMAGE" -R "$MYUID:$MYGID" /d \
      || die "droits de $dir : sudo chown -R $MYUID:$MYGID $PWD/$dir"
    say "Droits corrigés : $dir"
  fi
}
fix_owner mautrix-signal
fix_owner mautrix-whatsapp
if [ "$TELEGRAM_ON" = 1 ]; then fix_owner mautrix-telegram; fi
fix_owner synapse

yq_run() {
  "${DOCKER[@]}" run --rm -u "$MYUID:$MYGID" -v "$PWD:/workdir" -e "ADMIN_MXID=${ADMIN_MXID:-}" -e "SERVER_NAME=${SERVER_NAME:-}" -e "AS_EFF=${AS_EFF:-}" -e "HS_EFF=${HS_EFF:-}" "$YQ_IMAGE" "$@"
}

reg_token() { grep -m1 -E "^\s*$2:" "$1" | sed -E 's/^[^:]*:\s*//; s/^["\x27]|["\x27]$//g' | tr -d '\n'; }

gen_registration() {
  local dir="$1" bin="$2" image="$3" as="$4" hs="$5" out="$6" label="$7" prefix state
  [ -f "$dir/config.yaml" ] || die "$dir/config.yaml absent"
  quiet yq_run -i '
    .bridge.permissions[strenv(ADMIN_MXID)] = "admin" |
    del(.bridge.permissions["@admin:local.bridge"]) |
    del(.bridge.permissions["@CHANGE_ME:local.bridge"])
  ' "$dir/config.yaml" || die "config $label illisible"
  if [ -s "secrets/$out" ]; then
    state="déjà présente"
  else
    quiet "${DOCKER[@]}" run --rm -u "$MYUID:$MYGID" -v "$PWD/$dir:/data" -v "$PWD/secrets:/out" \
      -e "BRIDGE_APPSERVICE__AS_TOKEN=$as" -e "BRIDGE_APPSERVICE__HS_TOKEN=$hs" \
      --entrypoint "/usr/bin/$bin" "$image" -g -c /data/config.yaml -r "/out/$out" || die "registration $label impossible"
    state="créée"
  fi
  prefix="$(yq_run '.env_config_prefix' "$dir/config.yaml" 2>/dev/null)"
  if [ -z "$prefix" ] || [ "$prefix" = "null" ]; then
    AS_EFF="$(yq_run '.appservice.as_token' "$dir/config.yaml" 2>/dev/null)"
    HS_EFF="$(yq_run '.appservice.hs_token' "$dir/config.yaml" 2>/dev/null)"
  else
    AS_EFF="$as"
    HS_EFF="$hs"
  fi
  export AS_EFF HS_EFF
  [ -n "$AS_EFF" ] && [ -n "$HS_EFF" ] || die "token $label vide"
  quiet yq_run -i '.as_token = strenv(AS_EFF) | .hs_token = strenv(HS_EFF)' "secrets/$out" || die "registration $label illisible"
  [ "$(reg_token "secrets/$out" as_token)" = "$AS_EFF" ] || die "registration $label : token incorrect"
  chmod 644 "secrets/$out"
  say "Registration $label $state"
}

sync_synapse_registration() {
  local name="$1" out="$2"
  if grep -q "/data/$name" synapse/homeserver.yaml 2>/dev/null; then
    cp "secrets/$out" "synapse/$name"
  fi
}

reg_sum() { { cat secrets/signal_registration secrets/whatsapp_registration secrets/telegram_registration 2>/dev/null || true; } | sha256sum | cut -d' ' -f1; }
prep_telegram_config() {
  local cfg=mautrix-telegram/config.yaml
  if [ ! -s "$cfg" ]; then
    quiet "${DOCKER[@]}" run --rm -u "$MYUID:$MYGID" -v "$PWD/mautrix-telegram:/data" \
      --entrypoint /usr/bin/mautrix-telegram "$TELEGRAM_IMG" -e -c /data/config.yaml || die "config telegram impossible"
    [ -s "$cfg" ] || die "config telegram absente"
    say "Configuration telegram créée"
  fi
  quiet yq_run -i '
    .homeserver.address = "http://synapse:8008" |
    .homeserver.domain = strenv(SERVER_NAME) |
    .appservice.address = "http://mautrix-telegram:29317" |
    .appservice.hostname = "0.0.0.0" |
    .appservice.port = 29317 |
    .appservice.id = "telegram" |
    .appservice.bot.username = "telegrambot" |
    .appservice.username_template = "telegram_{{.}}" |
    .database.type = "sqlite3-fk-wal" |
    .database.uri = "file:/data/mautrix-telegram.db?_txlock=immediate" |
    .env_config_prefix = "BRIDGE_" |
    .bridge.command_prefix = "!tg" |
    .bridge.relay.enabled = true |
    .bridge.permissions = {"*": "relay"} |
    .bridge.permissions[strenv(SERVER_NAME)] = "user"
  ' "$cfg" || die "config telegram illisible"
}

REG_BEFORE="$(reg_sum)"
cfg_sum() { { cat mautrix-signal/config.yaml mautrix-whatsapp/config.yaml mautrix-telegram/config.yaml 2>/dev/null || true; } | sha256sum | cut -d' ' -f1; }
CFG_BEFORE="$(cfg_sum)"

if [ "${EXT[mautrix-signal]}" = 0 ]; then
  gen_registration mautrix-signal mautrix-signal "$SIGNAL_IMG" "$SIGNAL_AS_TOKEN" "$SIGNAL_HS_TOKEN" signal_registration signal
fi
if [ "${EXT[mautrix-whatsapp]}" = 0 ]; then
  gen_registration mautrix-whatsapp mautrix-whatsapp "$WHATSAPP_IMG" "$WHATSAPP_AS_TOKEN" "$WHATSAPP_HS_TOKEN" whatsapp_registration whatsapp
fi

if [ "$TELEGRAM_ON" = 1 ] && [ "${EXT[mautrix-telegram]}" = 0 ]; then
  prep_telegram_config
  gen_registration mautrix-telegram mautrix-telegram "$TELEGRAM_IMG" "$TELEGRAM_AS_TOKEN" "$TELEGRAM_HS_TOKEN" telegram_registration telegram
fi

REG_CHANGED=0
[ "$(reg_sum)" = "$REG_BEFORE" ] || REG_CHANGED=1
CFG_CHANGED=0
[ "$(cfg_sum)" = "$CFG_BEFORE" ] || CFG_CHANGED=1

if [ "${EXT[mautrix-signal]}" = 0 ] && [ "${EXT[mautrix-whatsapp]}" = 0 ]; then
  [ "$(reg_token secrets/signal_registration as_token)" != "$(reg_token secrets/whatsapp_registration as_token)" ] || die "registrations signal et whatsapp identiques"
fi

if [ "${EXT[synapse]}" = 0 ]; then
  [ -f synapse/homeserver.yaml ] || die "synapse/homeserver.yaml absent"
  sync_synapse_registration signal-registration.yaml signal_registration
  sync_synapse_registration whatsapp-registration.yaml whatsapp_registration
  if [ "$TELEGRAM_ON" = 1 ] && ! grep -q '/run/secrets/telegram_registration' synapse/homeserver.yaml; then
    quiet yq_run -i '.app_service_config_files += ["/run/secrets/telegram_registration"]' synapse/homeserver.yaml || die "homeserver.yaml illisible"
    if git rev-parse --git-dir >/dev/null 2>&1; then git update-index --skip-worktree synapse/homeserver.yaml 2>/dev/null || true; fi
    REG_CHANGED=1
  fi
  for r in signal_registration whatsapp_registration; do
    [ -f "secrets/$r" ] || die "secrets/$r absent"
  done
  if [ ! -f synapse/local.bridge.signing.key ]; then
    quiet "${DOCKER[@]}" run --rm -u "$MYUID:$MYGID" --entrypoint python \
      -v "$PWD/synapse:/data" -v "$PWD/secrets:/run/secrets:ro" \
      "$SYNAPSE_IMG" -m synapse.app.homeserver --config-path /data/homeserver.yaml --generate-keys || die "clé de signature impossible"
    say "Clé de signature créée"
  fi
fi

if [ "${EXT[mautrix-signal]}" = 0 ] || [ "${EXT[mautrix-whatsapp]}" = 0 ] || [ "$TELEGRAM_ON" = 1 ]; then
  if git rev-parse --git-dir >/dev/null 2>&1; then
    git update-index --skip-worktree mautrix-signal/config.yaml mautrix-whatsapp/config.yaml 2>/dev/null || true
    if [ "$TELEGRAM_ON" = 1 ]; then git update-index --skip-worktree mautrix-telegram/config.yaml 2>/dev/null || true; fi
  fi
fi

port_busy() { ss -ltn "sport = :$1" 2>/dev/null | grep -q LISTEN; }
our_running() {
  local st owner
  st="$("${DOCKER[@]}" inspect -f '{{.State.Running}} {{.State.Restarting}}' "$1" 2>/dev/null)" || return 1
  [ "${st%% *}" = "true" ] || return 1
  owner="$("${DOCKER[@]}" inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$1" 2>/dev/null)"
  [ "$owner" = "$PROJECT" ]
}

if command -v systemctl >/dev/null 2>&1; then
  active=()
  for u in matrix-synapse postgresql mautrix-signal mautrix-whatsapp mautrix-telegram; do
    if systemctl is-active --quiet "$u" 2>/dev/null; then active+=("$u"); fi
  done
  if [ "${#active[@]}" -gt 0 ]; then
    say "Services actifs : ${active[*]}"
    say "Arrête-les : sudo systemctl stop ${active[*]}"
  fi
fi
if [ "${EXT[synapse]}" = 0 ] && ! our_running synapse && port_busy 8008; then
  die "port 8008 occupé"
fi
if [ "${EXT[postgres]}" = 0 ]; then
  pg_running="$("${DOCKER[@]}" inspect -f '{{.State.Running}}' postgres 2>/dev/null || true)"
  cur_port="$(env_get POSTGRES_HOST_PORT)"; cur_port="${cur_port:-5432}"
  if [ "$pg_running" != "true" ] && port_busy "$cur_port"; then
    new_port=""
    for p in 5433 5434 5435 5436 5437 5438 5439 5440; do
      if ! port_busy "$p"; then new_port="$p"; break; fi
    done
    [ -n "$new_port" ] || die "aucun port libre pour postgres"
    env_set POSTGRES_HOST_PORT "$new_port"
    pg_dsn="$(env_get PG_DSN)"
    case "$pg_dsn" in *127.0.0.1:"$cur_port"/*) env_set PG_DSN "${pg_dsn/127.0.0.1:$cur_port\//127.0.0.1:$new_port/}";; esac
    say "Port postgres : $new_port, $cur_port occupé"
  fi
fi

HS_URL="$(env_get MATRIX_HOMESERVER_URL)"
HS_URL="${HS_URL:-http://127.0.0.1:8008}"

up_services=()
for c in postgres synapse mautrix-signal mautrix-whatsapp; do
  if [ "${EXT[$c]}" = 0 ]; then up_services+=("$c"); fi
done
if [ "$TELEGRAM_ON" = 1 ] && [ "${EXT[mautrix-telegram]}" = 0 ]; then up_services+=(mautrix-telegram); fi
pre_existing=()
for c in synapse mautrix-signal mautrix-whatsapp mautrix-telegram; do
  if "${DOCKER[@]}" inspect "$c" >/dev/null 2>&1; then pre_existing+=("$c"); fi
done
if [ "${#up_services[@]}" -gt 0 ]; then
  say "Démarrage : ${up_services[*]}"
  quiet "${DOCKER[@]}" compose up -d "${up_services[@]}" || die "démarrage impossible"
  recreate=()
  for c in synapse mautrix-signal mautrix-whatsapp mautrix-telegram; do
    [ "${EXT[$c]}" = 0 ] || continue
    if [ "$c" = mautrix-telegram ] && [ "$TELEGRAM_ON" != 1 ]; then continue; fi
    case " ${pre_existing[*]-} " in *" $c "*) ;; *) continue;; esac
    if [ "$c" = synapse ] && [ "$REG_CHANGED" = 1 ]; then recreate+=("$c"); fi
    if [ "$c" != synapse ] && [ "$CFG_CHANGED" = 1 ]; then recreate+=("$c"); fi
  done
  if [ "${#recreate[@]}" -gt 0 ]; then
    say "Redémarrage : ${recreate[*]}"
    quiet "${DOCKER[@]}" compose up -d --force-recreate "${recreate[@]}" || die "redémarrage impossible"
  fi
fi

say "Attente de Synapse"
ready=0
looping=0
for i in $(seq 1 "${SETUP_WAIT:-60}"); do
  if curl -fsS -m 3 "$HS_URL/health" >/dev/null 2>&1; then ready=1; break; fi
  if [ "$("${DOCKER[@]}" inspect -f '{{.State.Restarting}}' synapse 2>/dev/null || true)" = "true" ]; then looping=$((looping + 1)); else looping=0; fi
  [ "$looping" -ge 3 ] && break
  sleep 2
done
if [ "$ready" != 1 ]; then
  reason="$("${DOCKER[@]}" logs --tail 300 synapse 2>&1 | grep -E 'Error|ERROR' | tail -n 1 | sed -E 's/^[[:space:]]+//' || true)"
  die "Synapse ne démarre pas${reason:+ : $reason}"
fi

py() { python3 "$@"; }
gettoken() { py ../script/server/get_token.py --homeserver "$HS_URL" --homeserver-yaml synapse/homeserver.yaml --env-file "$ENV_FILE" --execute "$@"; }

gettoken --user "$ADMIN_MXID" --create --admin --password-env ADMIN_PASSWORD || die "compte $ADMIN_MXID impossible"
INVITER_USER_ID="$(env_get INVITER_USER_ID)"
if out="$(gettoken --user "$INVITER_USER_ID" --env-key INVITER_ACCESS_TOKEN 2>/dev/null)"; then say "$out"; else say "Token INVITER_ACCESS_TOKEN indisponible"; fi

connect_bridge() {
  local kind="$1" label upper is_relay=0
  shift
  upper="$(printf '%s' "$kind" | tr a-z A-Z)"
  label="$kind"
  case " $* " in *" --relay "*) is_relay=1; label="$kind relais";; esac
  if [ "$is_relay" = 1 ]; then
    if ! is_placeholder "$(env_get "${upper}_RELAY_LOGIN_ID")"; then say "$label déjà connecté"; return 0; fi
  elif [ "$(env_get "${upper}_CONNECTED")" = 1 ]; then say "$label déjà connecté"; return 0; fi
  [ "${SETUP_SKIP_LOGIN:-}" = 1 ] && return 0
  if [ -n "${SETUP_BRIDGES:-}" ]; then
    case ",$SETUP_BRIDGES," in *",$kind,"*) ;; *) say "$label ignoré"; return 0;; esac
  else
    if [ ! -t 0 ]; then say "$label à connecter : ./with-env.sh python3 script/server/bridge_login.py $kind $*"; return 0; fi
    confirm "Connecter $label ?" || { say "$label ignoré"; return 0; }
  fi
  MATRIX_ACCESS_TOKEN="$(env_get MATRIX_ACCESS_TOKEN)" py ../script/server/bridge_login.py "$kind" "$@" --homeserver "$HS_URL" --env-file "$ENV_FILE" || say "$label non connecté"
}
connect_bridge signal
connect_bridge whatsapp
if [ "$TELEGRAM_ON" = 1 ]; then
  connect_bridge telegram
  connect_bridge telegram --relay
fi

relay="$(env_get SIGNAL_RELAY_LOGIN_ID)"
if [ "${EXT[auto-bridge-signal]}" = 0 ]; then
  if [[ "$relay" =~ ^[0-9a-fA-F-]{36}$ ]] && [ "$relay" != "00000000-0000-0000-0000-000000000000" ]; then
    quiet "${DOCKER[@]}" compose up -d --force-recreate auto-bridge-signal || die "auto-bridge-signal impossible"
    say "auto-bridge-signal démarré"
  else
    say "auto-bridge-signal en attente de la connexion signal"
  fi
fi

if [ "$TELEGRAM_ON" = 1 ] && [ "${EXT[auto-bridge-telegram]}" = 0 ]; then
  if ! is_placeholder "$(env_get TELEGRAM_RELAY_LOGIN_ID)" && ! is_placeholder "$(env_get TELEGRAM_MAIN_LOGIN_ID)"; then
    quiet "${DOCKER[@]}" compose up -d --force-recreate auto-bridge-telegram || die "auto-bridge-telegram impossible"
    say "auto-bridge-telegram démarré"
  else
    say "auto-bridge-telegram en attente de la connexion telegram"
  fi
fi

say "Terminé"
say "Serveur : $HS_URL"
say "Compte : $ADMIN_MXID"
