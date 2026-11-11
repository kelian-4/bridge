STATUS_CLI_IMAGE="bridge/signal-cli:0.14.10"
STATUS_CLI_URL="${STATUS_CLI_URL:-http://127.0.0.1:18080}"

setup_status() {
  [ "${SETUP_SKIP_STATUS:-}" = 1 ] && return 0
  [ "${EXT[auto-bridge-signal]}" = 0 ] || return 0
  local relay
  relay="$(env_get SIGNAL_RELAY_LOGIN_ID)"
  if ! [[ "$relay" =~ ^[0-9a-fA-F-]{36}$ ]] || [ "$relay" = "00000000-0000-0000-0000-000000000000" ]; then
    say "statuts en attente de la connexion signal"
    return 0
  fi
  if ! "${DOCKER[@]}" image inspect "$STATUS_CLI_IMAGE" >/dev/null 2>&1; then
    say "Construction de signal-cli"
    build_image "$STATUS_CLI_IMAGE" signal-cli signal-cli || return 1
  fi
  quiet "${DOCKER[@]}" compose --profile local-status up -d signal-cli || { say "signal-cli impossible"; return 1; }

  local number="" tries=0
  until number="$(py ../script/server/link_signal_cli.py status --url "$STATUS_CLI_URL" 2>/dev/null)"; do
    tries=$((tries + 1))
    [ "$tries" -ge 30 ] && { say "signal-cli ne répond pas"; return 1; }
    sleep 2
  done

  if [ -z "$number" ]; then
    if [ "${SETUP_SKIP_LOGIN:-}" = 1 ] || [ ! -t 0 ]; then
      say "signal-cli à lier : ./Docker/setup.sh"
      return 0
    fi
    local uri
    uri="$(py ../script/server/link_signal_cli.py start --url "$STATUS_CLI_URL")" || { say "liaison de signal-cli impossible"; return 1; }
    say "Scanne ce code avec le compte Signal du relais"
    "${DOCKER[@]}" exec signal-cli qrencode -t ANSIUTF8 "$uri" || say "$uri"
    number="$(py ../script/server/link_signal_cli.py finish "$uri" --url "$STATUS_CLI_URL")" || { say "signal-cli non lié"; return 1; }
  fi

  env_set SIGNAL_CLI_ACCOUNT "$number"
  quiet "${DOCKER[@]}" compose --profile local-status up -d --force-recreate auto-status || { say "auto-status impossible"; return 1; }
  say "auto-status démarré"
}

setup_status || say "statuts non configurés"
