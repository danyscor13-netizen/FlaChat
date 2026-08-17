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

## Da fare

- pannello permessi grafico al posto dei comandi
- rinomina ed eliminazione delle superstanze
- stanze temporanee accanto a quelle permanenti
- invio di file e immagini, sticker, GIF
- quota per utente e statistiche di spazio
- `base.html` con `{% extends %}`: le pagine ripetono head e struttura
