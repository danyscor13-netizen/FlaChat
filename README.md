# FlaChat

Chat a superstanze permanenti: canali, ruoli con permessi, cronologia
persistente. Flask + Socket.IO + Postgres.

## Cosa c'è dentro

| File | A cosa serve |
|---|---|
| `app.py` | applicazione: rotte, socket, permessi, comandi |
| `wsgi.py` | punto di ingresso per gunicorn (monkey patching gevent) |
| `schema_postgres.sql` | schema del database, da eseguire su Supabase |
| `templates/` | pagine HTML |
| `static/style.css` | tutto il tema, in un solo file |
| `menzioni.py` | parsing delle menzioni e invio push |
| `static/sw.js` | service worker per le notifiche |
| `genera_vapid.py` | genera le chiavi per le push (una volta sola) |
| `test_app.py` | 22 test end-to-end |
| `test_menzioni.py` | 38 test su menzioni e notifiche |
| `migrazione_retention.sql` | scadenza messaggi + tabelle allegati |
| `test_retention.py` | 19 test sulla scadenza |
| `test_perms.py` | 28 test sui permessi |
| `test_consegna.py` | 17 test su conferme di consegna e riconnessione |
| `check_js.py` | verifica la sintassi del JS nei template |
| `render.yaml` | configurazione dell'hosting |
| `DEPLOY.md` | istruzioni per il deploy su Render |

## Avvio in locale

```bash
pip install -r requirements.txt

export DATABASE_URL="postgresql://postgres.xxx:PW@aws-0-eu-west-1.pooler.supabase.com:6543/postgres"
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"

python app.py
```

Lo schema va applicato una volta sola, dal SQL Editor di Supabase:
incolla `schema_postgres.sql` ed esegui.

Attenzione: la sezione 0 di quel file fa `DROP TABLE`. Va eseguita solo
su un database vuoto.

## Test

```bash
python test_app.py
python test_consegna.py
python test_perms.py
python test_menzioni.py
python test_retention.py
```

Il test **azzera il database** prima di partire (riapplica lo schema),
quindi va puntato su un database di prova, mai su quello di produzione.

## Deploy

Vedi `DEPLOY.md`. In sintesi: repo su GitHub, Render legge `render.yaml`,
si aggiungono `DATABASE_URL` e `SECRET_KEY` fra le variabili d'ambiente.

## Come funziona

**Identità.** L'utente è `users.id` in sessione, non il `sid` del socket.
Ruoli, ban e messaggi sopravvivono a disconnessioni e refresh. In memoria
resta solo chi è connesso adesso (`online`, `sid_channel`, `sid_space`):
se il processo riparte non si perde nulla.

**Permessi.** Bitmask, non liste di stringhe:

```
1  SEND_MESSAGES      16  MANAGE_ROLES
2  MANAGE_CHANNELS    32  MENTION_EVERYONE
4  KICK               64  MANAGE_MESSAGES
8  BAN               128  ADMIN
```

Ogni canale può avere override per ruolo con due maschere, `allow` e
`deny`, così ogni permesso ha tre stati: consentito, ereditato, negato.
Il calcolo finale è `(base & ~deny) | allow`. Chi ha `ADMIN` salta gli
override.

I ruoli hanno una `position`: non puoi agire su chi è al tuo livello o
sopra. Senza questo un mod potrebbe bannare l'owner.

**Messaggi.** I nuovi passano dal socket, la cronologia da
`GET /api/messages/<id>?before=<cursore>`. Sono due percorsi separati:
così una riconnessione non rimanda tutto lo storico. La paginazione usa
l'id intero crescente, non il timestamp, perché due messaggi nello stesso
millisecondo si scavalcherebbero.

I messaggi eliminati usano `deleted_at`, non `DELETE`: altrimenti le
risposte punterebbero nel vuoto.

## Comandi in chat

```
/newchannel <nome>        /kick <utente>
/delchannel <nome>        /ban <utente> [secondi]
/newrole <nome>           /unban <utente>
/delrole <nome>           /help
/role <utente> <ruolo>
```

## Menzioni

`@utente`, `@ruolo`, `@everyone` (anche `@all`, `@tutti`) e `@here`,
che raggiunge solo chi è connesso in quel momento.

L'autocomplete si apre digitando `@`: frecce per scegliere, Invio o Tab
per completare, Esc per chiudere. L'elenco si aggiorna quando entra
qualcuno, quindi chi arriva dopo è subito menzionabile.

`@everyone` e `@here` richiedono `MENTION_EVERYONE`, che il ruolo di
default possiede: di base possono tutti, e lo si toglie con un `deny`
sul ruolo o sul singolo canale.

Le menzioni finiscono nella tabella `mentions`, una riga per bersaglio.
Serve perché "dove sono stato citato" sia una query, e perché un cambio
di username non invalidi le menzioni passate.

## Notifiche push

Tre livelli per stanza: tutti i messaggi, solo menzioni (default),
nessuna. Chi ha il canale aperto non riceve nulla, perché ha già letto.

Servono le chiavi VAPID (`genera_vapid.py`) e HTTPS. Gli endpoint che
rispondono 404 o 410 vengono cancellati da soli: è così che lo standard
comunica che l'utente ha revocato il permesso.

## Scadenza dei messaggi

Ogni superstanza può eliminare i messaggi più vecchi di N giorni.
Il default è "mai": le stanze esistenti non cambiano comportamento.

Solo l'owner può modificarla, dal pannello "Stanza" o con
`/retention <giorni|mai>`. Chi entra vede un avviso in cima al canale,
così la scadenza non è una sorpresa.

La pulizia gira nel database con pg_cron (vedi
`migrazione_retention.sql`), non nell'app: continua a funzionare anche
quando Render sospende il servizio.

I file allegati non vengono cancellati da SQL — Postgres non parla con
Supabase Storage. Vengono accodati in `storage_da_eliminare`, e
`menzioni.svuota_coda_storage()` li rimuove dal bucket. Senza questo
passaggio resterebbero orfani, occupando spazio per sempre.

## Pannello permessi

`/perms` apre una GUI con due schede.

**Ruoli**: interruttori sì/no per i permessi di base di ogni ruolo.

**Canali**: per ogni ruolo e ogni canale, tre stati — consentito (verde),
eredita (neutro), negato (rosso). "Eredita" non è "no": se domani cambi
il ruolo, l'ereditato segue, il negato resta negato.

Tre regole impedite lato server, non solo nascoste nella UI:

- non puoi modificare un ruolo pari o superiore al tuo
- non puoi concedere permessi che tu stesso non possiedi
- non puoi toglierti la gestione dei permessi e restare chiuso fuori

Solo `SEND_MESSAGES`, `MANAGE_MESSAGES`, `MENTION_EVERYONE` e
`MANAGE_CHANNELS` sono sovrascrivibili per canale: `BAN`, `KICK`,
`MANAGE_ROLES` e `ADMIN` valgono sulla stanza intera.

## Da fare

- rinomina ed eliminazione delle superstanze
- stanze temporanee accanto a quelle permanenti
- invio di file e immagini, sticker, GIF
- quota per utente e statistiche di spazio
- `base.html` con `{% extends %}`: le pagine ripetono head e struttura


## Consegna dei messaggi

Ogni `emit` del client torna una conferma. Il messaggio compare subito
a schermo in grigio e resta in coda finche' il server non risponde
`{"ok": true}`; solo allora viene sostituito dalla versione confermata.

Serve perche' il socket cade spesso — schermo bloccato, wifi che salta,
il dyno free che si addormenta. Quando risale ha un `sid` nuovo e il
server non sa piu' in che stanza sia quel client: prima `on_message`
usciva in silenzio e il messaggio spariva senza che nessuno se ne
accorgesse. Ora risponde `{"ok": false, "err": "nojoin"}`, il client
rifa' il `join` da solo — passando il canale che stava guardando — e
svuota la coda.

Le conferme non arrivate entro dieci secondi non vengono rimandate in
automatico: il messaggio diventa cliccabile per riprovare a mano.
Rimandare alla cieca rischierebbe di sdoppiare un messaggio gia'
salvato, e un doppione e' peggio di un ritardo.

Gli annunci di entrata e uscita hanno venti secondi di grazia
(`GRAZIA` in `app.py`): chi rientra subito non fa comparire niente,
altrimenti ogni sfarfallio di rete riempirebbe la chat.

## Il tema

Regola di fondo: quello che dicono le persone e' testo normale, tutto
quello che dice la macchina — codici stanza, canali, orari, permessi,
badge — e' monospaziato. Si distinguono senza bisogno di riquadri.

I messaggi sono righe piatte con l'orario in colonna e il
raggruppamento per autore, non fumetti alternati: in una stanza con
dieci persone conta chi parla, non da che lato sta. I propri messaggi
si riconoscono dalla riga ambra nel margine.

Il favicon va messo in `static/favicon.png`: il tag `<link rel="icon">`
e' gia' in tutti i template.
