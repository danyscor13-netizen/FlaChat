"""
Test sulla consegna dei messaggi.

Copre il bug per cui a volte bisognava inviare due volte: dopo una
riconnessione il socket ha un sid nuovo, il server non sa piu' in che
stanza sia e il messaggio spariva in silenzio. Ora ogni emit torna una
conferma, e il client sa quando rifare il join.

    python test_consegna.py
"""
import os

os.environ.setdefault(
    "DATABASE_URL", "postgresql://postgres@/fla?host=/tmp/pg&port=5433")

import app as A  # noqa: E402

ok = True


def check(nome, cond):
    global ok
    ok &= bool(cond)
    print(f"  {'ok  ' if cond else 'FAIL'}  {nome}")


def reset_db():
    percorso = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "schema_postgres.sql")
    with open(percorso, encoding="utf-8") as f:
        sql = f.read()
    with A.get_db() as db:
        db.execute(sql)


def eventi(client, nome):
    return [e for e in client.get_received() if e["name"] == nome]


def dati(evento):
    a = evento.get("args")
    if isinstance(a, list):
        return a[0] if a else {}
    return a or {}


def main():
    reset_db()

    c1 = A.app.test_client()
    c1.post("/register", data={"username": "fla", "password": "pw"})
    r = c1.post("/lobby", data={"azione": "crea", "nome": "Prove"},
                follow_redirects=False)
    code = r.headers["Location"].rsplit("/", 1)[-1]

    c2 = A.app.test_client()
    c2.post("/register", data={"username": "dany", "password": "pw"})
    c2.post("/lobby", data={"azione": "entra", "code": code})

    print("\nCONFERME DI CONSEGNA")

    s1 = A.socketio.test_client(A.app, flask_test_client=c1)

    # --- il messaggio senza join non deve sparire in silenzio
    ack = s1.emit("message", {"msg": "prima del join", "tmp": "t0"},
                  callback=True)
    check("messaggio senza join viene rifiutato con 'nojoin'",
          ack and ack.get("ok") is False and ack.get("err") == "nojoin")
    check("il tmp torna indietro, il client sa quale messaggio e'",
          ack.get("tmp") == "t0")

    # --- join confermato
    ack = s1.emit("join", {"code": code}, callback=True)
    check("il join conferma e dice su che canale siamo",
          ack and ack.get("ok") is True and ack.get("channel_id"))
    canale_iniziale = ack["channel_id"]
    s1.get_received()

    # --- messaggio normale confermato, con id e tmp
    ack = s1.emit("message", {"msg": "ciao", "tmp": "t1"}, callback=True)
    check("messaggio confermato con id e tmp",
          ack and ack.get("ok") is True and ack.get("id")
          and ack.get("tmp") == "t1")

    # l'eco al mittente porta il tmp: serve a sostituire il messaggio
    # grigio invece di stamparlo una seconda volta
    eco = [dati(e) for e in eventi(s1, "message")]
    mio = [d for d in eco if d.get("tmp") == "t1"]
    check("l'eco al mittente contiene il tmp", len(mio) == 1)
    check("l'eco arriva una volta sola, non due", len(eco) == 1)

    with A.get_db() as db:
        n = A.uno(db, "SELECT COUNT(*) AS n FROM messages")["n"]
    check("un solo messaggio salvato nel database", n == 1)

    print("\nRICONNESSIONE")

    # --- secondo canale, per verificare che il join lo ripristini
    s1.emit("message", {"msg": "/newchannel prove"})
    s1.get_received()
    with A.get_db() as db:
        ch2 = A.uno(db, "SELECT id FROM channels WHERE name='prove'")["id"]

    ack = s1.emit("switch_channel", {"channel_id": ch2}, callback=True)
    check("il cambio canale conferma", ack and ack.get("ok") is True)
    s1.get_received()

    # --- cade la connessione e ne apre una nuova (sid diverso)
    s1.disconnect()
    s1b = A.socketio.test_client(A.app, flask_test_client=c1)

    ack = s1b.emit("message", {"msg": "dopo il crollo", "tmp": "t2"},
                   callback=True)
    check("dopo la riconnessione il messaggio e' rifiutato, non perso",
          ack and ack.get("err") == "nojoin")

    ack = s1b.emit("join", {"code": code, "channel_id": ch2}, callback=True)
    check("il join rimette sul canale che stavamo guardando",
          ack and ack.get("channel_id") == ch2)
    check("e non torna al primo canale come prima",
          ack.get("channel_id") != canale_iniziale)
    s1b.get_received()

    ack = s1b.emit("message", {"msg": "dopo il crollo", "tmp": "t2"},
                   callback=True)
    check("il rinvio dalla coda va a buon fine", ack and ack.get("ok") is True)

    with A.get_db() as db:
        r = A.uno(db, "SELECT channel_id FROM messages WHERE content=%s",
                  ("dopo il crollo",))
    check("ed e' finito nel canale giusto", r and r["channel_id"] == ch2)

    print("\nANNUNCI ENTRA / ESCE")

    s2 = A.socketio.test_client(A.app, flask_test_client=c2)
    s2.emit("join", {"code": code})
    s1b.get_received()
    s2.get_received()

    # una riconnessione entro la grazia non deve annunciare niente
    s2.disconnect()
    s2b = A.socketio.test_client(A.app, flask_test_client=c2)
    s2b.emit("join", {"code": code})

    sistemi = [dati(e).get("msg", "") for e in eventi(s1b, "message")
               if dati(e).get("type") == "system"]
    rumore = [m for m in sistemi if "lasciato" in m or "dany" in m]
    check("riconnettersi non riempie la chat di entra/esce", not rumore)

    # --- messaggio troppo lungo: rifiutato, ma il client lo sa
    ack = s1b.emit("message", {"msg": "x" * 2001, "tmp": "t3"}, callback=True)
    check("messaggio troppo lungo rifiutato con motivo",
          ack and ack.get("ok") is False and ack.get("err") == "lungo")

    ack = s1b.emit("message", {"msg": "   ", "tmp": "t4"}, callback=True)
    check("messaggio vuoto rifiutato", ack and ack.get("ok") is False)

    print("\nTUTTO OK" if ok else "\nCI SONO ERRORI")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
