#!/usr/bin/env bash
# Aggiorna l'app sulla VPS: scarica il codice, ricostruisce il container e aspetta che risponda.
#
# Lo lancia il workflow GitHub via SSH a ogni push su main. La chiave usata da GitHub è legata a
# questo script in ~/.ssh/authorized_keys (command="..."): può eseguire solo questo, non una shell.
# A mano, sulla VPS: ~/daily-ai-news/deploy/deploy.sh
set -euo pipefail

# Tutto il lavoro sta in una funzione: bash la legge per intero prima di eseguirla, quindi il
# `git pull` può sostituire questo file mentre gira senza spezzare l'esecuzione.
main() {
  cd "$(dirname "$0")/.."

  # un solo deploy alla volta
  exec 9>/tmp/daily-ai-news-deploy.lock
  if ! flock -n 9; then
    echo "Un altro deploy è già in corso" >&2
    return 1
  fi

  echo "==> Scarico il codice"
  git pull --ff-only

  echo "==> Ricostruisco e riavvio l'app (il database in ./data resta intatto)"
  docker compose up -d --build
  docker image prune -f >/dev/null

  echo "==> Aspetto che l'app risponda"
  for _ in $(seq 1 30); do
    if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
      echo "App attiva: $(git log -1 --format='%h %s')"
      return 0
    fi
    sleep 2
  done

  echo "L'app non risponde dopo il deploy. Ultimi log:" >&2
  docker compose logs --tail 30 app >&2
  return 1
}

main "$@"
exit $?
