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
| `test_app.py` | 22 test end-to-end |
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

## Da fare

- menzioni: `@utente`, `@ruolo`, `@here`, `@everyone`
- notifiche push (le tabelle ci sono già)
- pannello permessi grafico al posto dei comandi
- `base.html` con `{% extends %}`: le pagine ripetono head e struttura
