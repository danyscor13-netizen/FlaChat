"""
Test della scadenza messaggi.

    python test_retention.py
"""
import os

os.environ.setdefault(
    "DATABASE_URL", "postgresql://postgres@/fla?host=/tmp/pg&port=5433")
os.environ.setdefault("SECRET_KEY", "test")

import app as A  # noqa: E402

ok = True


def check(nome, cond):
    global ok
    ok &= bool(cond)
    print(f"  {'ok  ' if cond else 'FAIL'}  {nome}")


def reset():
    base = os.path.dirname(os.path.abspath(__file__))
    with A.get_db() as db:
        db.execute(open(os.path.join(base, "schema_postgres.sql"),
                        encoding="utf-8").read())
        db.execute(open(os.path.join(base, "migrazione_retention.sql"),
                        encoding="utf-8").read())


def main():
    reset()

    owner = A.app.test_client()
    owner.post("/register", data={"username": "owner", "password": "pw"})
    code = owner.post("/lobby", data={"azione": "crea", "nome": "R"},
                      follow_redirects=False).headers["Location"].rsplit("/", 1)[-1]

    altro = A.app.test_client()
    altro.post("/register", data={"username": "altro", "password": "pw"})
    altro.post("/lobby", data={"azione": "entra", "code": code})

    print("PERMESSI")
    r = owner.get(f"/api/retention/{code}").get_json()
    check("default: nessuna scadenza", r["giorni"] is None)
    check("owner riconosciuto", r["owner"] is True)

    r = altro.get(f"/api/retention/{code}").get_json()
    check("non-owner vede l'impostazione", r["owner"] is False)

    r = altro.post(f"/api/retention/{code}", json={"giorni": 7})
    check("non-owner non puo' cambiarla", r.status_code == 403)

    r = owner.post(f"/api/retention/{code}", json={"giorni": 30})
    check("owner puo' impostarla", r.get_json().get("ok"))

    check("valore fuori range rifiutato",
          owner.post(f"/api/retention/{code}", json={"giorni": 99999}).status_code == 400)
    check("zero rifiutato",
          owner.post(f"/api/retention/{code}", json={"giorni": 0}).status_code == 400)

    estraneo = A.app.test_client()
    estraneo.post("/register", data={"username": "estraneo", "password": "pw"})
    check("non membro respinto",
          estraneo.get(f"/api/retention/{code}").status_code == 403)

    print("\nPULIZIA")
    with A.get_db() as db:
        sp = A.uno(db, "SELECT id FROM spaces WHERE code=%s", (code,))["id"]
        ch = A.uno(db, "SELECT id FROM channels WHERE space_id=%s", (sp,))["id"]
        uid = A.uno(db, "SELECT id FROM users WHERE username='owner'")["id"]

        db.execute("""INSERT INTO messages (channel_id,author_id,content,created_at)
                      SELECT %s,%s,'vecchio',now() - interval '60 days'
                      FROM generate_series(1,5)""", (ch, uid))
        db.execute("""INSERT INTO messages (channel_id,author_id,content,created_at)
                      SELECT %s,%s,'recente',now() - interval '2 days'
                      FROM generate_series(1,5)""", (ch, uid))
        m = A.uno(db, "SELECT MIN(id) AS i FROM messages WHERE channel_id=%s",
                  (ch,))["i"]
        db.execute("""INSERT INTO attachments
                        (message_id,uploader_id,space_id,storage_path,tipo,bytes)
                      VALUES (%s,%s,%s,'s/vecchia.webp','image',1000)""",
                   (m, uid, sp))
        db.commit()

    r = owner.get(f"/api/retention/{code}").get_json()
    check("conteggio anteprima corretto", r["da_eliminare"] == 5)

    with A.get_db() as db:
        res = A.uno(db, "SELECT * FROM pulisci_scaduti()")
        check("eliminati solo i vecchi", res["messaggi_eliminati"] == 5)
        check("file accodato per il bucket", res["file_accodati"] == 1)

        n = A.uno(db, "SELECT COUNT(*) AS n FROM messages WHERE channel_id=%s",
                  (ch,))["n"]
        check("i recenti sopravvivono", n == 5)

        q = A.uno(db, "SELECT storage_path AS p FROM storage_da_eliminare")
        check("percorso in coda", q and q["p"] == "s/vecchia.webp")

        a = A.uno(db, "SELECT COUNT(*) AS n FROM attachments")["n"]
        check("allegato rimosso col messaggio", a == 0)

        res = A.uno(db, "SELECT * FROM pulisci_scaduti()")
        check("seconda esecuzione non trova nulla",
              res["messaggi_eliminati"] == 0)

    print("\nSTANZA SENZA SCADENZA")
    c2 = A.app.test_client()
    c2.post("/register", data={"username": "eterno", "password": "pw"})
    code2 = c2.post("/lobby", data={"azione": "crea", "nome": "E"},
                    follow_redirects=False).headers["Location"].rsplit("/", 1)[-1]
    with A.get_db() as db:
        sp2 = A.uno(db, "SELECT id FROM spaces WHERE code=%s", (code2,))["id"]
        ch2 = A.uno(db, "SELECT id FROM channels WHERE space_id=%s", (sp2,))["id"]
        u2 = A.uno(db, "SELECT id FROM users WHERE username='eterno'")["id"]
        db.execute("""INSERT INTO messages (channel_id,author_id,content,created_at)
                      SELECT %s,%s,'antico',now() - interval '2000 days'
                      FROM generate_series(1,5)""", (ch2, u2))
        db.commit()
        A.uno(db, "SELECT * FROM pulisci_scaduti()")
        n = A.uno(db, "SELECT COUNT(*) AS n FROM messages WHERE channel_id=%s",
                  (ch2,))["n"]
        check("senza scadenza non si tocca nulla", n == 5)

    print("\nCOMANDO /retention")
    s = A.socketio.test_client(A.app, flask_test_client=owner)
    s.emit("join", {"code": code})
    s.get_received()
    s.emit("message", {"msg": "/retention mai"})
    s.get_received()
    with A.get_db() as db:
        v = A.uno(db, "SELECT retention_days AS d FROM spaces WHERE code=%s",
                  (code,))["d"]
        check("'/retention mai' disattiva la scadenza", v is None)

    s.emit("message", {"msg": "/retention 90"})
    s.get_received()
    with A.get_db() as db:
        v = A.uno(db, "SELECT retention_days AS d FROM spaces WHERE code=%s",
                  (code,))["d"]
        check("'/retention 90' imposta 90 giorni", v == 90)

    s2 = A.socketio.test_client(A.app, flask_test_client=altro)
    s2.emit("join", {"code": code})
    s2.get_received()
    s2.emit("message", {"msg": "/retention 5"})
    msg = [(e.get("args") if isinstance(e.get("args"), dict) else {}).get("msg", "")
           for e in s2.get_received()]
    check("non-owner respinto anche via comando",
          any("proprietario" in m for m in msg))

    print("\n" + ("TUTTO OK" if ok else "QUALCOSA NON TORNA"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
