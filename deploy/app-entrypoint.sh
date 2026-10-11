#!/bin/sh
set -eu
mkdir -p /data/uploads /certs
/bin/sh /generate-cert.sh
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'
