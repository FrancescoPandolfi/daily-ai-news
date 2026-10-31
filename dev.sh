#!/usr/bin/env bash
# Avvia l'app in locale su http://localhost:8000
#
#   ./dev.sh             avvio normale
#   ./dev.sh --reload    riavvio automatico quando modifichi il codice
#
# Crea il virtualenv e installa le dipendenze se mancano.
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "Manca il file .env: copialo da .env.example e compilalo." >&2
  exit 1
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -r requirements.txt

# In locale si va su http: con BASE_URL https il cookie di sessione è "Secure"
# e alcuni browser (Safari) non lo accettano su localhost. La variabile
# d'ambiente ha la precedenza su quella nel .env.
export BASE_URL="http://localhost:8000"

exec uvicorn app.main:app --port 8000 "$@"
