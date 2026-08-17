"""
Test di menzioni e notifiche push.

    python test_menzioni.py
"""
import os

os.environ.setdefault(
    "DATABASE_URL", "postgresql://postgres@/fla?host=/tmp/pg&port=5433")
os.environ.setdefault("SECRET_KEY", "test")

import app as A      # noqa: E402
import menzioni as M  # noqa: E402

ok = True


def check(nome, cond):
    global ok
    ok &= bool(cond)
    print(f"  {'ok  ' if cond else 'FAIL'}  {nome}")


def reset():
    percorso = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "schema_postgres.sql")
    with A.get_db() as db:
        db.execute(open(percorso, encoding="utf-8").read())


def main():
    reset()

    # ---------- parser puro ----------
    print("PARSER")
    check("estrae piu' menzioni",
          M.estrai_nomi("ciao @mario e @lucia") == ["mario", "lucia"])
    check("niente duplicati",
          M.estrai_nomi("@bob @bob @bob") == ["bob"])
    check("email non diventa menzione",
          M.estrai_nomi("scrivi a tizio@example.com") == ["example.com"])
    check("accenti normalizzati", M.normalizza("José") == "jose")
    check("si ferma allo spazio",
          M.estrai_nomi("@mario rossi") == ["mario"])
    check("underscore e trattini ammessi",
          M.estrai_nomi("@beta_tester-1") == ["beta_tester-1"])
    check("testo senza @", M.estrai_nomi("nessuna menzione qui") == [])

    # ---------- setup ----------
    client = A.app.test_client()
    client.post("/register", data={"username": "anna", "password": "pw"})
    r = client.post("/lobby", data={"azione": "crea", "nome": "Test"},
                    follow_redirects=False)
    code = r.headers["Location"].rsplit("/", 1)[-1]

    c2 = A.app.test_client()
    c2.post("/register", data={"username": "bruno", "password": "pw"})
    c2.post("/lobby", data={"azione": "entra", "code": code})

    c3 = A.app.test_client()
    c3.post("/register", data={"username": "carla", "password": "pw"})
    c3.post("/lobby", data={"azione": "entra", "code": code})

    with A.get_db() as db:
        sp = A.uno(db, "SELECT id FROM spaces WHERE code=%s", (code,))["id"]
        ch = A.uno(db, "SELECT id FROM channels WHERE space_id=%s", (sp,))["id"]
        uid = {n: A.uno(db, "SELECT id FROM users WHERE username=%s", (n,))["id"]
               for n in ("anna", "bruno", "carla")}
        rid_mod = A.uno(db, "SELECT id FROM roles WHERE space_id=%s AND name='mod'",
                        (sp,))["id"]
        db.execute("""INSERT INTO member_roles (space_id,user_id,role_id)
                      VALUES (%s,%s,%s)""", (sp, uid["bruno"], rid_mod))

    print("\nRISOLUZIONE")
    with A.get_db() as db:
        u, r_, ev, hr = M.risolvi(db, sp, "ciao @bruno", uid["anna"], True)
        check("menzione a utente", u == [uid["bruno"]])

        u, r_, ev, hr = M.risolvi(db, sp, "@mod venite", uid["anna"], True)
        check("menzione a ruolo", r_ == [rid_mod])

        u, r_, ev, hr = M.risolvi(db, sp, "@anna parlo da solo", uid["anna"], True)
        check("l'autore non menziona se stesso", u == [])

        u, r_, ev, hr = M.risolvi(db, sp, "@sconosciuto", uid["anna"], True)
        check("utente inesistente ignorato", u == [] and r_ == [])

        u, r_, ev, hr = M.risolvi(db, sp, "@everyone", uid["anna"], True)
        check("@everyone con permesso", ev is True)

        u, r_, ev, hr = M.risolvi(db, sp, "@everyone", uid["anna"], False)
        check("@everyone senza permesso viene ignorato", ev is False)

        u, r_, ev, hr = M.risolvi(db, sp, "@here", uid["anna"], True)
        check("@here riconosciuto", hr is True)

        u, r_, ev, hr = M.risolvi(db, sp, "@BRUNO", uid["anna"], True)
        check("maiuscole indifferenti", u == [uid["bruno"]])

    print("\nDESTINATARI")
    with A.get_db() as db:
        d = M.destinatari(db, sp, ch, [uid["bruno"]], [], False, False,
                          uid["anna"], set())
        check("menzione diretta", d == {uid["bruno"]})

        d = M.destinatari(db, sp, ch, [], [rid_mod], False, False,
                          uid["anna"], set())
        check("menzione di ruolo raggiunge i membri", d == {uid["bruno"]})

        d = M.destinatari(db, sp, ch, [], [], True, False, uid["anna"], set())
        check("@everyone raggiunge tutti tranne l'autore",
              d == {uid["bruno"], uid["carla"]})

        d = M.destinatari(db, sp, ch, [], [], False, True, uid["anna"],
                          {uid["carla"]})
        check("@here raggiunge solo i connessi", d == {uid["carla"]})

    print("\nSALVATAGGIO")
    s1 = A.socketio.test_client(A.app, flask_test_client=client)
    s1.emit("join", {"code": code})
    s1.get_received()
    s1.emit("message", {"msg": "ehi @bruno guarda"})
    s1.emit("message", {"msg": "@everyone avviso"})

    with A.get_db() as db:
        n = A.uno(db, """SELECT COUNT(*) AS n FROM mentions mn
                         JOIN messages m ON m.id=mn.message_id
                         WHERE m.channel_id=%s AND mn.user_id=%s""",
                  (ch, uid["bruno"]))["n"]
        check("menzione salvata in tabella", n == 1)

        f = A.uno(db, """SELECT mentions_everyone FROM messages
                         WHERE channel_id=%s ORDER BY id DESC LIMIT 1""",
                  (ch,))["mentions_everyone"]
        check("flag @everyone salvato sul messaggio", f is True)

        q = A.uno(db, """SELECT COUNT(*) AS n FROM mentions mn
                         JOIN messages m ON m.id=mn.message_id
                         WHERE mn.user_id=%s""", (uid["bruno"],))["n"]
        check("query 'dove sono citato' funziona", q == 1)

    print("\nPREFERENZE")
    with A.get_db() as db:
        check("default = solo menzioni",
              M.livello(db, uid["bruno"], sp, ch) == M.MENZIONI)

    r = c2.post(f"/api/notifications/{code}", json={"level": 2})
    check("salvataggio preferenza", r.get_json().get("ok"))
    with A.get_db() as db:
        check("preferenza applicata", M.livello(db, uid["bruno"], sp, ch) == M.TUTTI)

    r = c2.post(f"/api/notifications/{code}", json={"level": 0})
    with A.get_db() as db:
        check("silenzia stanza", M.livello(db, uid["bruno"], sp, ch) == M.NIENTE)

    r = c2.post(f"/api/notifications/{code}", json={"level": 9})
    check("livello non valido rifiutato", r.status_code == 400)

    print("\nISCRIZIONI PUSH")
    sub = {"endpoint": "https://push.example.com/abc",
           "keys": {"p256dh": "chiave-pubblica", "auth": "segreto"}}
    r = c2.post("/api/push/subscribe", json=sub)
    check("iscrizione accettata", r.get_json().get("ok"))

    c2.post("/api/push/subscribe", json=sub)
    with A.get_db() as db:
        n = A.uno(db, """SELECT COUNT(*) AS n FROM push_subscriptions
                         WHERE user_id=%s""", (uid["bruno"],))["n"]
        check("stesso endpoint non duplicato", n == 1)

    r = c2.post("/api/push/subscribe", json={"endpoint": "x"})
    check("iscrizione incompleta rifiutata", r.status_code == 400)

    r = c2.post("/api/push/unsubscribe", json={"endpoint": sub["endpoint"]})
    with A.get_db() as db:
        n = A.uno(db, """SELECT COUNT(*) AS n FROM push_subscriptions
                         WHERE user_id=%s""", (uid["bruno"],))["n"]
        check("disiscrizione", n == 0)

    print("\nAPI")
    r = client.get(f"/api/mentionables/{code}")
    d = r.get_json()
    nomi = [x["nome"] for x in d]
    check("elenco contiene i membri", "bruno" in nomi and "carla" in nomi)
    check("elenco contiene i ruoli", "mod" in nomi)
    check("elenco contiene everyone/here", "everyone" in nomi and "here" in nomi)

    estraneo = A.app.test_client()
    estraneo.post("/register", data={"username": "intruso", "password": "pw"})
    check("non membro non vede l'elenco",
          estraneo.get(f"/api/mentionables/{code}").status_code == 403)

    r = client.get("/api/push/key")
    check("endpoint chiave risponde", "attive" in r.get_json())

    r = client.get("/sw.js")
    check("service worker servito dalla radice",
          r.status_code == 200 and b"push" in r.data)

    print("\n" + ("TUTTO OK" if ok else "QUALCOSA NON TORNA"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
