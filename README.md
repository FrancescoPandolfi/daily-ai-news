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
cp .env.example .env        # compila le chiavi
./dev.sh                    # crea il venv, installa le dipendenze e avvia l'app
./dev.sh --reload           # come sopra, con riavvio automatico alle modifiche
```

Apri http://localhost:8000 ed entra con `APP_PASSWORD`. Lo script imposta `BASE_URL` su
`http://localhost:8000` solo per quell'avvio, così il `.env` può restare con l'URL di produzione.

Per fare una raccolta da terminale, senza email e senza avviare l'app:
`source .venv/bin/activate && python -m app.pipeline`.

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

## Destinatari del digest

Dal pulsante **Destinatari** in alto a destra aggiungi o rimuovi gli indirizzi che ricevono
il digest (massimo 20). Ognuno riceve una email separata, quindi nessuno vede gli altri.

- `EMAIL_TO` nel `.env` serve solo a inizializzare l'elenco al primo avvio; dopo vale l'elenco nell'app.
- Con il mittente di prova `onboarding@resend.dev`, Resend consegna **solo all'indirizzo con cui ti sei
  registrato**: per inviare ad altre persone devi verificare un dominio su Resend e impostare `EMAIL_FROM`.

## Risposte in streaming

Le risposte compaiono mentre il modello le scrive (`POST /ask/stream`, una riga JSON per evento).
A fine risposta la pagina sostituisce il testo con la versione definitiva: le citazioni vengono
rinumerate e restano solo le fonti citate davvero. Senza JavaScript il form usa ancora `POST /ask`
e mostra la risposta intera. Dietro nginx lo streaming funziona grazie all'intestazione
`X-Accel-Buffering: no` mandata dall'app.

## Immagini degli articoli

Ogni articolo ha una miniatura nell'archivio e nelle fonti delle risposte (non nell'email).
Si salva solo l'indirizzo dell'immagine: il browser la scarica dal sito originale, con
`referrerpolicy="no-referrer"`, e se non si carica la miniatura sparisce da sola.

- La copertina è quella dichiarata dalla pagina (`og:image`). Se manca si ripiega sulle immagini
  trovate da Tavily, scartando loghi, icone e pixel di tracciamento. Nessun credito Tavily in più.
- Per leggere la pagina il server fa una richiesta web: rifiuta indirizzi interni (rete locale,
  metadati AWS), anche dopo un reindirizzamento, e ammette solo le porte 80 e 443.
- Gli articoli salvati prima di questa funzione non hanno l'immagine. Per cercarla (solo dai
  metadati, senza costi): `python -m app.images` in locale, oppure
  `docker compose exec app python -m app.images` sul server.

## Monitoraggio

La pagina **Monitoraggio** (pulsante in alto a destra) mostra:

- **Stato**: ultima e prossima raccolta, ultimo digest e backup, con un avviso se la raccolta è ferma da più di 30 ore.
- **Costi**: spesa del mese, di oggi, per raccolta e per articolo, divisa per servizio, più l'andamento di 30 giorni.
- **Raccolte**: per ogni giorno argomenti, articoli proposti e nuovi, **scartati con il motivo** (già in archivio, embedding fallito), errori ed email inviate.
- **Risposte**: tempo mediano e peggiore, errori e costo delle domande. Il **testo delle domande viene salvato** nel database.
- **Sistema**: dimensione del database, disco, memoria, scadenza del certificato.

I costi sono **stime** (prezzi di listino in `app/pricing.py`, da aggiornare se cambiano): confrontali con le fatture.
Tavily è gratuito fino a 1000 crediti al mese, quindi il suo costo resta zero finché non li superi.
Se l'app si ferma non può avvisarti da sola: per questo serve un controllo esterno su `/health` (ad esempio UptimeRobot).

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
