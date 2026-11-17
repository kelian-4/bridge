#!/bin/sh
DATA="${SIGNAL_CLI_DATA:-/data}"
DELAY="${SIGNAL_CLI_CHECK_DELAY:-45}"
URL="${SIGNAL_CLI_URL:-http://127.0.0.1:18080}"

signal-cli --config "$DATA" daemon "--http=127.0.0.1:18080" &
pid=$!
trap 'kill "$pid" 2>/dev/null' TERM INT

sleep "$DELAY"
expected="$(ls "$DATA/data" 2>/dev/null | grep -cE '^[0-9]+$')"
loaded="$(curl -s -m 20 -X POST "$URL/api/v1/rpc" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"listAccounts","id":1}' | grep -o '"number"' | wc -l)"

FAILS="$DATA/.run-failures"
count="$(cat "$FAILS" 2>/dev/null || echo 0)"
if [ "$loaded" -ge "$expected" ]; then
  rm -f "$FAILS"
elif [ "$count" -lt 5 ]; then
  echo $((count + 1)) > "$FAILS"
  echo "Compte non chargé, redémarrage du conteneur"
  kill "$pid" 2>/dev/null
  wait "$pid" 2>/dev/null
  exit 1
else
  echo "Compte non chargé après $count redémarrages, poursuite sans lui"
fi

wait "$pid"
