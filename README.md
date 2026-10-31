# Daily AI News

Ogni mattina un agente cerca notizie sugli argomenti che scegli, le legge, le riassume
e ti manda un digest via email. Gli articoli vengono salvati e indicizzati: da una pagina
web protetta da password puoi fare domande all'archivio e ottenere risposte con le fonti.

```
scheduler → agente (OpenAI + Tavily) → SQLite + sqlite-vec → email (Resend)
                                              ↑
                       pagina web (FastAPI) → RAG → risposta con citazioni
```

Un solo container, un solo file di dati, nessun altro servizio.

## Avvio in locale

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # compila le chiavi
python -m app.pipeline      # prima raccolta (senza email)
uvicorn app.main:app --reload
```

Apri http://localhost:8000 ed entra con `APP_PASSWORD`.

## Deploy su EC2

Prerequisiti: Docker con il plugin compose, nginx con HTTPS già configurato.

1. **Swap** (la `t3.micro` ha 1 GiB: senza swap la build Docker può finire in out-of-memory):
   ```bash
   sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
   sudo mkswap /swapfile && sudo swapon /swapfile
   echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
   ```
2. **Codice e configurazione**:
   ```bash
   git clone <url-del-repo> ~/daily-ai-news && cd ~/daily-ai-news
   cp .env.example .env && nano .env      # chiavi, password, BASE_URL
   mkdir -p data
   docker compose up -d --build
   curl http://127.0.0.1:8000/health
   ```
3. **nginx**: incolla `deploy/nginx-location.conf` nel blocco `server` HTTPS del dominio,
   poi `sudo nginx -t && sudo systemctl reload nginx`.
4. **Deploy automatico da GitHub**: nel repo aggiungi i secret `VPS_HOST`, `VPS_USER`
   e `VPS_SSH_KEY` (chiave privata dedicata, con la pubblica in `~/.ssh/authorized_keys`
   della VPS). Ogni push su `main` esegue `git pull` e `docker compose up -d --build`.
   Se il repo è privato, la VPS ha bisogno di una deploy key in sola lettura.

Security group: apri solo 80 e 443 al mondo; la 22 solo dal tuo IP. La porta 8000
è già legata a `127.0.0.1`.

## Sicurezza

- Ogni rotta richiede il login, tranne `/login` e `/health` (che non rivela nulla).
- Cookie di sessione firmato, `HttpOnly`, `Secure` se `BASE_URL` è https, `SameSite=Lax`.
- Dopo 5 password sbagliate lo stesso IP è bloccato per 15 minuti.
- Documentazione automatica di FastAPI (`/docs`) disattivata.

## Dati e backup

- Il database è `data/news.db` (articoli, testi, chunk e vettori).
- Ogni notte alle 03:00 l'app crea una copia in `data/backups/` (ultime 7).
- Questa copia sta sullo stesso disco: per proteggerti da un guasto della VPS
  attiva anche gli snapshot EBS giornalieri (AWS Data Lifecycle Manager).

## Limiti da conoscere

- **Un solo worker**: lo scheduler è nel processo. Non aumentare i worker di uvicorn,
  altrimenti la pipeline partirebbe più volte.
- **Il container deve restare acceso** per far scattare il digest delle 07:30.
  Se è spento a quell'ora, APScheduler recupera il job entro un'ora dal riavvio.
- **Cambiare modello di embedding** richiede di ricreare il database
  (`EMBEDDING_DIM` deve combaciare con il modello).
- L'archivio è per uso personale: salva riassunti, link e testo per la ricerca,
  non ripubblicare gli articoli.
- Ogni raccolta usa l'API di OpenAI e di Tavily: tieni d'occhio i costi,
  soprattutto se aggiungi molti argomenti.
