#!/usr/bin/env bash
# Local run, mirroring the Render start command.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a
exec gunicorn app.main:app --workers 1 --threads 4 --timeout 120 \
     --bind "0.0.0.0:${PORT:-10000}"
