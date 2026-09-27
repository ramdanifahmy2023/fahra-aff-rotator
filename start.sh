#!/bin/zsh
set -euo pipefail
cd "$(dirname "$0")"
if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi
if [[ -f Credential/cookies.txt ]]; then chmod 600 Credential/cookies.txt; fi
listen_port="${PORT:-8788}"
if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"$listen_port" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Fahra Affiliate Rotator sudah berjalan di http://127.0.0.1:$listen_port"
  exit 0
fi
exec python3 app.py
