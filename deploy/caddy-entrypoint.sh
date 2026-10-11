#!/bin/sh
set -eu
i=0
while [ ! -f /certs/cert.pem ]; do
  i=$((i + 1))
  if [ "$i" -gt 60 ]; then
    echo "certificate missing" >&2
    exit 1
  fi
  sleep 1
done
exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
