#!/bin/sh
for v in ${REQUIRED_VARS:-MATRIX_ACCESS_TOKEN SIGNAL_RELAY_LOGIN_ID}; do
  eval "val=\${$v:-}"
  case "$val" in
    ""|CHANGE_ME|00000000-0000-0000-0000-000000000000)
      echo "$v n'est pas renseigné dans .env. Renseigne-le puis lance : docker compose up -d --force-recreate ${SERVICE_NAME:-auto-bridge-signal}"
      sleep 300
      exit 1;;
  esac
done
exec python -u "$@"
