# FlaChat — deploy su Render

## Prima di iniziare

Serve il repo su GitHub con questi file:

```
app.py              schema_postgres.sql
wsgi.py             requirements.txt
render.yaml         .gitignore
static/style.css    templates/
```

Elimina `netlify.toml` se c'è ancora: Netlify non regge i WebSocket, quel
file può solo confondere.

**Non committare mai** `DATABASE_URL` o `SECRET_KEY`. Il `.gitignore`
esclude già `.env` e i file `.db`.

---

## 1. Prendi la stringa di connessione da Supabase

Dashboard del progetto → pulsante **Connect** in alto → tab
**Transaction pooler**.

Copia l'URI. Ha questa forma:

```
postgresql://postgres.abcdefgh:PASSWORD@aws-0-eu-central-1.pooler.supabase.com:6543/postgres
```

Sostituisci `PASSWORD` con la password del database (quella scelta
alla creazione del progetto, non quella dell'account Supabase).

**Deve essere il pooler sulla porta 6543, non la connessione diretta
sulla 5432.** La connessione diretta di Supabase risponde solo su IPv6,
e Render esce in IPv4: otterresti un errore di rete impossibile da
diagnosticare dai log.

---

## 2. Crea il servizio

Su render.com → **New** → **Web Service** → collega il repo GitHub.

Se `render.yaml` è nel repo, Render legge tutto da lì e devi solo
aggiungere `DATABASE_URL`. Altrimenti compila a mano:

| Campo | Valore |
|---|---|
| Language | Python 3 |
| Region | Frankfurt |
| Build command | `pip install -r requirements.txt` |
| Start command | vedi sotto |

Start command, tutto su una riga:

```
gunicorn wsgi:application -k geventwebsocket.gunicorn.workers.GeventWebSocketWorker -w 1 -b 0.0.0.0:$PORT
```

---

## 3. Variabili d'ambiente

Environment → Add Environment Variable:

| Chiave | Valore |
|---|---|
| `DATABASE_URL` | l'URI del punto 1 |
| `SECRET_KEY` | una stringa lunga e casuale |
| `PYTHON_VERSION` | `3.12.3` |

### Notifiche push (opzionali)

Genera le chiavi una volta sola:

```bash
python genera_vapid.py
```

Aggiungi le tre variabili che stampa: `VAPID_PUBLIC_KEY`,
`VAPID_PRIVATE_KEY`, `VAPID_CLAIM_EMAIL`.

Senza queste FlaChat funziona lo stesso: il pannello notifiche dira'
che non sono configurate. Se le rigeneri, tutte le iscrizioni esistenti
smettono di funzionare.

Le push richiedono HTTPS, che su Render c'e' gia'.

Per generare la chiave:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Se `SECRET_KEY` resta quella di default nel codice, chiunque legga il
sorgente può forgiare una sessione ed entrare come chiunque altro.

---

## 4. Deploy

**Create Web Service.** Il primo build prende qualche minuto.

Quando i log mostrano `Using worker: GeventWebSocketWorker` e
`Booting worker`, apri l'URL: registrati, crea una stanza, scrivi un
messaggio. Se il messaggio compare, i WebSocket funzionano.

---

## Perché il comando è scritto così

**`-k geventwebsocket...GeventWebSocketWorker`** — il worker di default
di gunicorn gestisce una richiesta alla volta e non parla WebSocket.
Con quello la pagina si apre ma la chat resta muta, oppure ripiega sul
long-polling: sembra funzionare, ma ogni messaggio arriva con secondi
di ritardo.

**`-w 1`** — un solo worker, e non è un limite da alzare. Lo stato di
chi è connesso (`online`, `sid_channel`, `sid_space`) vive nella
memoria del processo. Con due worker, gli utenti finiscono su processi
diversi che non si vedono fra loro: metà dei messaggi sparisce. Per
scalare serve Redis come message queue di Socket.IO, che è un lavoro a
parte.

**`wsgi:application`** e non `app:app` — `wsgi.py` applica il monkey
patching di gevent prima di ogni altro import. Senza, `psycopg` si
porta dietro il socket bloccante e ogni query congela tutti gli utenti
connessi, non solo chi l'ha fatta.

---

## Il piano gratuito

Il servizio va in sospensione dopo circa 15 minuti di inattività, e la
prima richiesta successiva impiega quasi un minuto a rispondere.

Per una chat è più fastidioso che per un sito normale: alla sospensione
i WebSocket cadono e lo stato in memoria si azzera. I messaggi non si
perdono — sono su Supabase — ma chi era connesso viene disconnesso e
deve ricaricare.

Se FlaChat deve restare raggiungibile, il piano Starter elimina il
problema.

Anche il progetto Supabase gratuito si sospende dopo una settimana
senza query. Si riattiva dalla dashboard.

---

## Se qualcosa non va

**Il sito si apre ma i messaggi non arrivano.** Console del browser
(F12): se vedi errori su `socket.io` o richieste ripetute a
`/socket.io/?transport=polling`, il worker è sbagliato. Ricontrolla che
lo start command contenga `GeventWebSocketWorker`.

**`connection to server failed` nei log.** Stai usando la connessione
diretta invece del pooler. Verifica che la porta sia **6543**.

**`password authentication failed`.** Nell'URI c'è ancora il
segnaposto `PASSWORD`, oppure hai usato la password dell'account
Supabase invece di quella del database. Si rigenera da Settings →
Database → Reset database password.

**`relation "users" does not exist`.** Lo schema non è stato applicato:
esegui `schema_postgres.sql` nel SQL Editor di Supabase.

**Deploy fallito su `psycopg`.** Verifica che `PYTHON_VERSION` sia
impostata a `3.12.3`.

---

## Aggiornamenti

Ogni `git push` sul branch collegato fa ripartire il deploy in
automatico. Durante il riavvio gli utenti connessi cadono e devono
ricaricare la pagina.
