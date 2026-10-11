#!/bin/sh
set -eu

mkdir -p /certs

IP=$(printf '%s' "${HERITAGE_PUBLIC_IP:-}" | tr -cd '0-9.')
STAMP_VALUE="${IP:-local}"
STAMP="/certs/ip-stamp"

if [ -f /certs/cert.pem ] && [ -f /certs/key.pem ] && [ "$(cat "$STAMP" 2>/dev/null || true)" = "$STAMP_VALUE" ]; then
  exit 0
fi

SAN="IP:127.0.0.1"
if printf '%s' "$IP" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}$'; then
  SAN="IP:${IP},IP:127.0.0.1"
fi

openssl req -x509 -newkey rsa:2048 \
  -keyout /certs/key.pem \
  -out /certs/cert.pem \
  -days 825 \
  -nodes \
  -subj "/CN=heritage" \
  -addext "subjectAltName=${SAN}"

chmod 644 /certs/cert.pem /certs/key.pem
printf '%s' "$STAMP_VALUE" > "$STAMP"
