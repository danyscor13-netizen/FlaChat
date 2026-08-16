"""
Test end-to-end del pezzo 1.
Simula due utenti connessi via socket e verifica che tutto persista.

    python test_app.py
"""
import os

os.environ.setdefault(
    "DATABASE_URL", "postgresql://postgres@/fla?host=/tmp/pg&port=5433")

import app as A  # noqa: E402


def reset_db():
    """
    Riapplica lo schema da zero: i test devono partire puliti,
    altrimenti utenti e stanze di una run precedente li fanno fallire.
    """
    percorso = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "schema_postgres.sql")
    with open(percorso, encoding="utf-8") as f:
        sql = f.read()
    with A.get_db() as db:
        db.execute(sql)

ok = True


def check(nome, cond):
    global ok
    ok &= bool(cond)
    print(f"  {'ok  ' if cond else 'FAIL'}  {nome}")


def payload(evento):
    """socketio test client restituisce args come dict o come lista."""
    a = evento.get("args")
    if isinstance(a, list):
        return a[0] if a else {}
    return a or {}


def main():
    reset_db()
    client = A.app.test_client()

    # --- registrazione
    client.post("/register", data={"username": "fla", "password": "pw"})
    r = client.get("/lobby")
    check("registrazione + lobby", r.status_code == 200)

    # --- crea superstanza
    r = client.post("/lobby", data={"azione": "crea", "nome": "Casa Mia"},
                    follow_redirects=False)
    code = r.headers["Location"].rsplit("/", 1)[-1]
    check(f"superstanza creata (codice {code})", len(code) == 6 and code.isdigit())

    ctx = A.get_db(); conn = ctx.__enter__()
    sp = A.uno(conn, "SELECT * FROM spaces WHERE code=%s", (code,))
    n_ruoli = A.uno(conn, "SELECT COUNT(*) AS n FROM roles WHERE space_id=%s",
                    (sp["id"],))["n"]
    check("4 ruoli di default creati", n_ruoli == 4)

    uid_fla = A.uno(conn, "SELECT id FROM users WHERE username='fla'")["id"]
    check("creatore è owner (ADMIN)",
          A.puo(conn, uid_fla, sp["id"], A.ADMIN))

    general = A.uno(conn, "SELECT id FROM channels WHERE space_id=%s",
                    (sp["id"],))["id"]

    # --- secondo utente entra col codice
    c2 = A.app.test_client()
    c2.post("/register", data={"username": "dany", "password": "pw"})
    c2.post("/lobby", data={"azione": "entra", "code": code})
    uid_dany = A.uno(conn, "SELECT id FROM users WHERE username='dany'")["id"]
    check("dany è membro", A.uno(conn,
        "SELECT 1 FROM members WHERE space_id=%s AND user_id=%s",
        (sp["id"], uid_dany)) is not None)
    check("dany ha ruolo user, non owner",
          not A.puo(conn, uid_dany, sp["id"], A.ADMIN)
          and A.puo(conn, uid_dany, sp["id"], A.SEND_MESSAGES))
    ctx.__exit__(None, None, None)

    # --- socket: due client
    s1 = A.socketio.test_client(A.app, flask_test_client=client)
    s2 = A.socketio.test_client(A.app, flask_test_client=c2)
    s1.emit("join", {"code": code})
    s2.emit("join", {"code": code})
    s1.get_received()
    s2.get_received()

    # --- messaggi persistenti
    s1.emit("message", {"msg": "ciao a tutti"})
    s2.emit("message", {"msg": "ciao fla"})

    ctx = A.get_db(); conn = ctx.__enter__()
    n = A.uno(conn, "SELECT COUNT(*) AS n FROM messages WHERE channel_id=%s",
              (general,))["n"]
    check("2 messaggi salvati nel database", n == 2)

    ricevuti = [e for e in s1.get_received() if e["name"] == "message"]
    check("il messaggio di dany arriva a fla in tempo reale",
          any(payload(a).get("msg") == "ciao fla" for a in ricevuti))

    # --- la cronologia sopravvive alla disconnessione
    s2.disconnect()
    s1.disconnect()
    r = client.get(f"/api/messages/{general}")
    storico = r.get_json()
    check("cronologia leggibile dopo disconnessione", len(storico) == 2)
    check("ordine cronologico corretto",
          storico[0]["msg"] == "ciao a tutti" and storico[1]["msg"] == "ciao fla")
    check("flag 'own' calcolato per utente",
          storico[0]["own"] is True and storico[1]["own"] is False)

    # --- ruolo persistente dopo riconnessione
    s1 = A.socketio.test_client(A.app, flask_test_client=client)
    s1.emit("join", {"code": code})
    s1.get_received()
    ctx = A.get_db(); conn = ctx.__enter__()
    check("fla è ancora owner dopo la riconnessione",
          A.puo(conn, uid_fla, sp["id"], A.ADMIN))
    check("dany NON è diventato owner riconnettendosi",
          not A.puo(conn, uid_dany, sp["id"], A.ADMIN))
    ctx.__exit__(None, None, None)

    # --- canale in sola lettura via override
    s1.emit("message", {"msg": "/newchannel annunci"})
    s1.get_received()
    ctx = A.get_db(); conn = ctx.__enter__()
    ann = A.uno(conn, "SELECT id FROM channels WHERE space_id=%s AND name='annunci'",
                (sp["id"],))
    check("canale #annunci creato", ann is not None)

    r_user = A.uno(conn, "SELECT id FROM roles WHERE space_id=%s AND name='user'",
                   (sp["id"],))["id"]
    conn.execute("""INSERT INTO channel_overrides (channel_id,role_id,allow,deny)
                    VALUES (%s,%s,0,%s)""", (ann["id"], r_user, A.SEND_MESSAGES))
    check("dany non può scrivere in #annunci",
          not A.puo(conn, uid_dany, sp["id"], A.SEND_MESSAGES, ann["id"]))
    check("fla (admin) può comunque scrivere in #annunci",
          A.puo(conn, uid_fla, sp["id"], A.SEND_MESSAGES, ann["id"]))
    ctx.__exit__(None, None, None)

    # --- non letti
    ctx = A.get_db(); conn = ctx.__enter__()
    A.segna_letto(conn, uid_dany, general)
    check("dany: 0 non letti dopo la lettura",
          A.non_letti(conn, uid_dany, general) == 0)
    ctx.__exit__(None, None, None)

    s1.emit("message", {"msg": "messaggio nuovo"})
    ctx = A.get_db(); conn = ctx.__enter__()
    check("dany: 1 non letto dopo un messaggio altrui",
          A.non_letti(conn, uid_dany, general) == 1)
    ctx.__exit__(None, None, None)

    # --- gerarchia
    s2 = A.socketio.test_client(A.app, flask_test_client=c2)
    s2.emit("join", {"code": code})
    s2.get_received()
    s2.emit("message", {"msg": "/kick fla"})
    ric = [payload(e).get("msg", "") for e in s2.get_received()
           if e["name"] == "message"]
    check("dany (user) non può kickare l'owner",
          any("permess" in m.lower() for m in ric))

    ctx = A.get_db(); conn = ctx.__enter__()
    check("fla è ancora membro", A.uno(conn,
        "SELECT 1 FROM members WHERE space_id=%s AND user_id=%s",
        (sp["id"], uid_fla)) is not None)
    ctx.__exit__(None, None, None)

    # --- ban persistente
    s1.emit("message", {"msg": "/ban dany"})
    s1.get_received()
    ctx = A.get_db(); conn = ctx.__enter__()
    check("ban registrato", A.ban_attivo(conn, sp["id"], uid_dany))
    ctx.__exit__(None, None, None)

    r = c2.get(f"/chat/{code}", follow_redirects=False)
    check("dany bannato viene respinto dalla stanza",
          "banned" in r.headers.get("Location", ""))

    print("\n" + ("TUTTO OK" if ok else "QUALCOSA NON TORNA"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
