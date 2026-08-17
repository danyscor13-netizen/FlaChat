"""
Test del pannello permessi.
Il grosso riguarda cosa NON deve essere possibile.

    python test_perms.py
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


def main():
    reset()

    capo = A.app.test_client()
    capo.post("/register", data={"username": "capo", "password": "pw"})
    code = capo.post("/lobby", data={"azione": "crea", "nome": "P"},
                     follow_redirects=False).headers["Location"].rsplit("/", 1)[-1]

    mod = A.app.test_client()
    mod.post("/register", data={"username": "mod1", "password": "pw"})
    mod.post("/lobby", data={"azione": "entra", "code": code})

    tizio = A.app.test_client()
    tizio.post("/register", data={"username": "tizio", "password": "pw"})
    tizio.post("/lobby", data={"azione": "entra", "code": code})

    with A.get_db() as db:
        sp = A.uno(db, "SELECT id FROM spaces WHERE code=%s", (code,))["id"]
        ch = A.uno(db, "SELECT id FROM channels WHERE space_id=%s", (sp,))["id"]
        rid = {r["name"]: r["id"] for r in
               A.tutti(db, "SELECT id, name FROM roles WHERE space_id=%s", (sp,))}
        uid_mod = A.uno(db, "SELECT id FROM users WHERE username='mod1'")["id"]
        db.execute("DELETE FROM member_roles WHERE space_id=%s AND user_id=%s",
                   (sp, uid_mod))
        db.execute("""INSERT INTO member_roles (space_id,user_id,role_id)
                      VALUES (%s,%s,%s)""", (sp, uid_mod, rid["mod"]))
        # Il ruolo 'mod' di default NON ha MANAGE_ROLES: senza, ogni
        # richiesta verrebbe respinta al primo controllo e i test
        # sull'escalation passerebbero per il motivo sbagliato.
        # Glielo diamo per esercitare davvero la logica sottostante.
        db.execute("""UPDATE roles SET permissions = permissions | %s
                      WHERE id=%s""", (A.MANAGE_ROLES, rid["mod"]))
        db.commit()

    print("LETTURA")
    d = capo.get(f"/api/perms/{code}").get_json()
    check("elenco ruoli completo", len(d["ruoli"]) == 4)
    check("elenco canali", len(d["canali"]) == 1)
    check("owner può gestire ruoli", d["posso_ruoli"])
    check("permessi di canale sono un sottoinsieme",
          len(d["permessi_canale"]) < len(d["permessi_ruolo"]))

    d2 = tizio.get(f"/api/perms/{code}").get_json()
    check("membro semplice legge ma non gestisce", not d2["posso_ruoli"])
    check("per lui nessun ruolo è modificabile",
          all(not r["modificabile"] for r in d2["ruoli"]))

    d3 = mod.get(f"/api/perms/{code}").get_json()
    r_user = [r for r in d3["ruoli"] if r["nome"] == "user"][0]
    r_admin = [r for r in d3["ruoli"] if r["nome"] == "admin"][0]
    check("il mod può modificare 'user'", r_user["modificabile"])
    check("il mod NON può modificare 'admin'", not r_admin["modificabile"])

    estraneo = A.app.test_client()
    estraneo.post("/register", data={"username": "fuori", "password": "pw"})
    check("non membro respinto",
          estraneo.get(f"/api/perms/{code}").status_code == 403)

    print("\nMODIFICA RUOLI")
    r = capo.post(f"/api/perms/{code}/role",
                  json={"role_id": rid["user"], "permessi": A.SEND_MESSAGES})
    check("owner modifica un ruolo", r.get_json().get("ok"))
    with A.get_db() as db:
        v = A.uno(db, "SELECT permissions AS p FROM roles WHERE id=%s",
                  (rid["user"],))["p"]
        check("valore salvato", v == A.SEND_MESSAGES)

    r = tizio.post(f"/api/perms/{code}/role",
                   json={"role_id": rid["user"], "permessi": A.ADMIN})
    check("senza MANAGE_ROLES viene rifiutato", r.status_code == 403)

    print("\nESCALATION (deve fallire tutto)")
    r = mod.post(f"/api/perms/{code}/role",
                 json={"role_id": rid["admin"], "permessi": A.ADMIN})
    check("il mod non modifica un ruolo superiore", r.status_code == 403)

    r = mod.post(f"/api/perms/{code}/role",
                 json={"role_id": rid["mod"], "permessi": A.ADMIN})
    check("il mod non modifica il proprio ruolo", r.status_code == 403)

    r = mod.post(f"/api/perms/{code}/role",
                 json={"role_id": rid["user"], "permessi": A.ADMIN})
    check("il mod non concede ADMIN a un ruolo inferiore", r.status_code == 403)

    r = mod.post(f"/api/perms/{code}/role",
                 json={"role_id": rid["user"], "permessi": A.BAN})
    check("il mod non concede un permesso che non ha (BAN)",
          r.status_code == 403)

    r = mod.post(f"/api/perms/{code}/role",
                 json={"role_id": rid["user"],
                       "permessi": A.SEND_MESSAGES | A.KICK})
    check("il mod concede un permesso che possiede (KICK)",
          r.get_json().get("ok"))

    with A.get_db() as db:
        v = A.uno(db, "SELECT permissions AS p FROM roles WHERE id=%s",
                  (rid["user"],))["p"]
        check("nessun ADMIN è passato", not (v & A.ADMIN))

    print("\nAUTOESCLUSIONE")
    r = capo.post(f"/api/perms/{code}/role",
                  json={"role_id": rid["owner"], "permessi": A.SEND_MESSAGES})
    check("l'owner non puo' togliersi ADMIN dal proprio ruolo",
          r.status_code == 400)
    with A.get_db() as db:
        v = A.uno(db, "SELECT permissions AS p FROM roles WHERE id=%s",
                  (rid["owner"],))["p"]
        check("il ruolo owner e' rimasto intatto", bool(v & A.ADMIN))

    uid_capo = None
    with A.get_db() as db:
        uid_capo = A.uno(db, "SELECT id FROM users WHERE username='capo'")["id"]
        check("l'owner puo' ancora gestire",
              A.puo(db, uid_capo, sp, A.MANAGE_ROLES))

    r = capo.post(f"/api/perms/{code}/role",
                  json={"role_id": rid["owner"],
                        "permessi": A.ADMIN | A.SEND_MESSAGES})
    check("puo' modificare il proprio ruolo se mantiene il controllo",
          r.get_json().get("ok"))

    print("\nOVERRIDE DI CANALE")
    r = capo.post(f"/api/perms/{code}/override",
                  json={"channel_id": ch, "role_id": rid["user"],
                        "bit": A.SEND_MESSAGES, "stato": -1})
    check("nega scrittura al ruolo user", r.get_json().get("ok"))

    with A.get_db() as db:
        uid_t = A.uno(db, "SELECT id FROM users WHERE username='tizio'")["id"]
        check("tizio non può più scrivere nel canale",
              not A.puo(db, uid_t, sp, A.SEND_MESSAGES, ch))
        uid_c = A.uno(db, "SELECT id FROM users WHERE username='capo'")["id"]
        check("l'owner (admin) scrive comunque",
              A.puo(db, uid_c, sp, A.SEND_MESSAGES, ch))

    r = capo.post(f"/api/perms/{code}/override",
                  json={"channel_id": ch, "role_id": rid["user"],
                        "bit": A.SEND_MESSAGES, "stato": 0})
    check("torna a 'eredita'", r.get_json().get("ok"))
    with A.get_db() as db:
        n = A.uno(db, """SELECT COUNT(*) AS n FROM channel_overrides
                         WHERE channel_id=%s AND role_id=%s""",
                  (ch, rid["user"]))["n"]
        check("override vuoto viene cancellato, non lasciato a zero", n == 0)
        check("tizio può scrivere di nuovo",
              A.puo(db, uid_t, sp, A.SEND_MESSAGES, ch))

    r = capo.post(f"/api/perms/{code}/override",
                  json={"channel_id": ch, "role_id": rid["user"],
                        "bit": A.SEND_MESSAGES, "stato": 1})
    with A.get_db() as db:
        o = A.uno(db, """SELECT allow, deny FROM channel_overrides
                         WHERE channel_id=%s AND role_id=%s""", (ch, rid["user"]))
        check("allow e deny non si sovrappongono mai",
              (o["allow"] & o["deny"]) == 0)

    r = capo.post(f"/api/perms/{code}/override",
                  json={"channel_id": ch, "role_id": rid["user"],
                        "bit": A.ADMIN, "stato": -1})
    check("permesso non sovrascrivibile per canale rifiutato",
          r.status_code == 400)

    r = capo.post(f"/api/perms/{code}/override",
                  json={"channel_id": ch, "role_id": rid["user"],
                        "bit": A.SEND_MESSAGES, "stato": 7})
    check("stato non valido rifiutato", r.status_code == 400)

    r = tizio.post(f"/api/perms/{code}/override",
                   json={"channel_id": ch, "role_id": rid["user"],
                         "bit": A.SEND_MESSAGES, "stato": -1})
    check("senza MANAGE_CHANNELS rifiutato", r.status_code == 403)

    print("\nCOMANDO /perms")
    s = A.socketio.test_client(A.app, flask_test_client=capo)
    s.emit("join", {"code": code})
    s.get_received()
    s.emit("message", {"msg": "/perms"})
    ev = [e["name"] for e in s.get_received()]
    check("l'owner apre il pannello", "open_perms" in ev)

    s2 = A.socketio.test_client(A.app, flask_test_client=tizio)
    s2.emit("join", {"code": code})
    s2.get_received()
    s2.emit("message", {"msg": "/perms"})
    ric = s2.get_received()
    check("membro semplice non lo apre",
          "open_perms" not in [e["name"] for e in ric])

    print("\n" + ("TUTTO OK" if ok else "QUALCOSA NON TORNA"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
