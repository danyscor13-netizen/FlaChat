"""
Test su profili e recupero password.

    python test_profili.py

L'invio email e' sostituito da una funzione finta: qui interessa la
nostra logica (token usa e getta, scadenza, tetto alle richieste,
nessuna informazione su chi e' iscritto), non l'API di Resend.
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
    with A.get_db() as db:
        # Lo schema ricrea users e gli id ripartono da 1: senza questo
        # i token della run precedente risultano del nuovo utente e
        # fanno scattare il tetto delle 3 richieste all'ora.
        db.execute("DROP TABLE IF EXISTS verify_tokens")
        db.execute(open(percorso, encoding="utf-8").read())
        for m in ("migrazione_icone.sql", "migrazione_verifica.sql"):
            db.execute(open(os.path.join(os.path.dirname(percorso), m),
                            encoding="utf-8").read())


def main():
    reset_db()
    A.rileva_colonna_icon()

    c1 = A.app.test_client()
    c1.post("/register", data={"username": "fla", "password": "pw"})
    c2 = A.app.test_client()
    c2.post("/register", data={"username": "dany", "password": "pw"})

    print("\nPROFILO")

    r = c1.post("/profile", data={"bio": "Suono e scrivo codice."})
    check("la bio si salva", r.status_code == 200
          and "Suono e scrivo codice." in r.get_data(as_text=True))

    lunga = "a" * 900
    c1.post("/profile", data={"bio": lunga})
    with A.get_db() as db:
        u = A.uno(db, "SELECT bio FROM users WHERE username='fla'")
    check("la bio viene troncata a 500 caratteri", len(u["bio"]) == 500)
    c1.post("/profile", data={"bio": "Suono e scrivo codice."})

    r = c2.get("/profile/fla")
    check("il profilo altrui si legge da loggati", r.status_code == 200)
    r = A.app.test_client().get("/profile/fla")
    check("ma non da sconosciuti", r.status_code == 302)

    r = c1.get("/profile")
    check("senza email lo dice", "Non hai una email" in r.get_data(as_text=True))

    print("\nVERIFICA EMAIL")

    r = c1.get("/verify-email")
    check("la pagina si apre (prima dava 500)", r.status_code == 200)

    r = c1.post("/verify-email", data={"email": "Fla@Example.com"})
    check("l'invio risponde", r.status_code == 200
          and "Controlla la posta" in r.get_data(as_text=True))

    with A.get_db() as db:
        u = A.uno(db, "SELECT email, is_email_verified FROM users WHERE username='fla'")
        t = A.uno(db, "SELECT * FROM verify_tokens ORDER BY id DESC LIMIT 1")
    check("l'email si salva in minuscolo", u["email"] == "fla@example.com")
    check("e resta NON verificata finche' non si clicca",
          u["is_email_verified"] is False)
    check("il token e' salvato come hash (64 caratteri)",
          len(t["token_hash"]) == 64)

    r = c1.get("/profile")
    check("il profilo dice che manca la conferma",
          "ancora confermata" in r.get_data(as_text=True))

    # il token in chiaro non esiste nel database: lo rigenero come fa
    # il link, per provare la conferma
    import hashlib
    import secrets as sec
    tok = sec.token_urlsafe(32)
    with A.get_db() as db:
        db.execute("UPDATE verify_tokens SET token_hash=%s WHERE id=%s",
                   (hashlib.sha256(tok.encode()).hexdigest(), t["id"]))
        db.commit()

    r = c1.get(f"/verify-email/{tok}")
    check("il link conferma l'indirizzo",
          "confermata" in r.get_data(as_text=True))
    with A.get_db() as db:
        u = A.uno(db, "SELECT is_email_verified FROM users WHERE username='fla'")
    check("e il flag diventa vero", u["is_email_verified"] is True)

    r = c1.get(f"/verify-email/{tok}")
    check("lo stesso link non si riusa",
          "non piu" in r.get_data(as_text=True))

    r = c1.get("/verify-email/inventato")
    check("un token inventato non vale",
          "non piu" in r.get_data(as_text=True))

    # cambiare email deve togliere la verifica
    c1.post("/verify-email", data={"email": "altra@example.com"})
    with A.get_db() as db:
        u = A.uno(db, "SELECT is_email_verified FROM users WHERE username='fla'")
    check("cambiando indirizzo la verifica decade",
          u["is_email_verified"] is False)

    with A.get_db() as db:
        n = A.uno(db, """SELECT COUNT(*) AS n FROM verify_tokens
                         WHERE used_at IS NULL""")["n"]
    check("resta aperto un solo token per volta", n == 1)

    for _ in range(4):
        r = c1.post("/verify-email", data={"email": "fla@example.com"})
    check("massimo 3 richieste all'ora",
          "Troppe richieste" in r.get_data(as_text=True))

    print("\nPANNELLO PROFILO IN CHAT")

    r = c1.post("/lobby", data={"azione": "crea", "nome": "Casa"},
                follow_redirects=False)
    code = r.headers["Location"].rsplit("/", 1)[-1]
    c2.post("/lobby", data={"azione": "entra", "code": code})

    s1 = A.socketio.test_client(A.app, flask_test_client=c1)
    s1.emit("join", {"code": code})
    s1.emit("message", {"msg": "/newrole capo"})
    s1.emit("create_role", {"role_name": "capo", "color": "#ff0000",
                            "icon": None, "modifica": False})
    s1.emit("message", {"msg": "/addrole dany capo"})

    d = c2.get(f"/api/profilo/{code}/fla").get_json()
    check("il pannello legge il profilo", d.get("username") == "fla")
    check("con la bio", d.get("bio") == "Suono e scrivo codice.")
    check("l'email NON esce mai dall'API", "email" not in d)
    check("c'e' la data di ingresso", d.get("dal"))

    d = c1.get(f"/api/profilo/{code}/dany").get_json()
    check("i ruoli multipli si vedono tutti", len(d["ruoli"]) == 2)
    check("il principale e' il primo", d["ruoli"][0]["nome"] == "capo")

    check("chi non e' nella stanza da' 404",
          c1.get(f"/api/profilo/{code}/nessuno").status_code == 404)
    check("chi non e' loggato non legge profili",
          A.app.test_client().get(f"/api/profilo/{code}/fla").status_code == 401)

    print("\nAVVISO EMAIL IN LOBBY")

    c5 = A.app.test_client()
    c5.post("/register", data={"username": "nuovo", "password": "pw"})
    r = c5.get("/lobby")
    check("chi non ha email vede l'avviso",
          "avviso-email" in r.get_data(as_text=True))
    check("il pulsante risponde",
          c5.post("/api/avviso-email/nascondi").status_code == 200)
    r = c5.get("/lobby")
    check("e l'avviso non torna",
          "avviso-email" not in r.get_data(as_text=True))
    check("chi non e' loggato non puo' chiamarla",
          A.app.test_client().post("/api/avviso-email/nascondi").status_code == 401)

    print("\nTUTTO OK" if ok else "\nCI SONO ERRORI")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
