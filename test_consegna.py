"""
Test sulla consegna dei messaggi.

Copre il bug per cui a volte bisognava inviare due volte: dopo una
riconnessione il socket ha un sid nuovo, il server non sa piu' in che
stanza sia e il messaggio spariva in silenzio. Ora ogni emit torna una
conferma, e il client sa quando rifare il join.

    python test_consegna.py
"""
import base64
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
    with A.get_db() as db:
        sp_id = A.uno(db, "SELECT id FROM spaces WHERE code=%s", (code,))["id"]

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

    print("\nICONE DEI RUOLI")

    check("la cartella static/icons e' stata letta",
          "owner" in A.ICONE and "admin" in A.ICONE and "mod" in A.ICONE)
    check("l'url dell'icona punta dentro static/icons",
          A.ICONE["owner"].startswith("/static/icons/owner."))
    check("'user' non ha icona (nessun file)", "user" not in A.ICONE)

    s1b.get_received()
    s1b.emit("message", {"msg": "con ruolo", "tmp": "t5"}, callback=True)
    eco = [dati(e) for e in eventi(s1b, "message")]
    mio = [d for d in eco if d.get("tmp") == "t5"]
    check("il messaggio porta il ruolo dell'autore",
          mio and mio[0].get("role") == "owner")

    r = c1.get(f"/api/messages/{ch2}")
    storico = r.get_json()
    check("anche la cronologia porta il ruolo",
          storico and all("role" in m for m in storico))
    check("e il ruolo e' quello giusto",
          any(m["role"] == "owner" for m in storico))

    print("\nRUOLI: ICONA E MODIFICA")

    disp = {i["file"] for i in A.icone_disponibili()}
    check("l'elenco per il pannello vede tutti i file",
          {"owner.svg", "admin.svg", "mod.svg"} <= disp)

    # retrocompatibilita': icon NULL -> ricade sul file omonimo, cosi'
    # le stanze nate prima della colonna non perdono le icone
    check("icon NULL ricade sul file omonimo",
          A.url_icona("owner", None) == "/static/icons/owner.svg")
    check("icon vuota vuol dire davvero nessuna icona",
          A.url_icona("owner", "") == "")
    check("icon valorizzata vince sul nome",
          A.url_icona("mod", "admin.svg") == "/static/icons/admin.svg")
    check("ruolo senza file e senza scelta: nessuna icona",
          A.url_icona("user", None) == "")

    # --- /newrole con icona scelta a mano
    s1b.get_received()
    s1b.emit("message", {"msg": "/newrole capo"}, callback=True)
    ev = [dati(e) for e in eventi(s1b, "open_role_creator")]
    check("/newrole apre il pannello in modalita' creazione",
          ev and ev[0].get("modifica") is False)
    check("e porta l'elenco delle icone disponibili",
          ev and len(ev[0].get("icone", [])) >= 3)

    s1b.emit("create_role", {"role_name": "capo", "color": "#ff0000",
                             "icon": "admin.svg", "modifica": False})
    with A.get_db() as db:
        r = A.uno(db, "SELECT * FROM roles WHERE space_id=%s AND name='capo'",
                  (sp_id,))
    check("il ruolo nasce con l'icona scelta",
          r and r["icon"] == "admin.svg" and r["color"] == "#ff0000")

    # --- /changerole
    s1b.get_received()
    s1b.emit("message", {"msg": "/changerole capo"}, callback=True)
    ev = [dati(e) for e in eventi(s1b, "open_role_creator")]
    check("/changerole apre il pannello in modalita' modifica",
          ev and ev[0].get("modifica") is True)
    check("e lo apre gia' compilato con colore e icona attuali",
          ev and ev[0].get("color") == "#ff0000"
          and ev[0].get("icon") == "admin.svg")

    s1b.emit("create_role", {"role_name": "capo", "color": "#00ff00",
                             "icon": "mod.svg", "modifica": True})
    with A.get_db() as db:
        r = A.uno(db, "SELECT * FROM roles WHERE space_id=%s AND name='capo'",
                  (sp_id,))
        n = A.uno(db, "SELECT COUNT(*) AS n FROM roles WHERE space_id=%s AND name='capo'",
                  (sp_id,))["n"]
    check("la modifica aggiorna colore e icona",
          r and r["color"] == "#00ff00" and r["icon"] == "mod.svg")
    check("e non crea un doppione", n == 1)

    s1b.get_received()
    s1b.emit("message", {"msg": "/changerole inesistente"}, callback=True)
    msgs = [dati(e).get("msg", "") for e in eventi(s1b, "message")]
    check("/changerole su un ruolo che non c'e' suggerisce /newrole",
          any("/newrole inesistente" in m for m in msgs))

    # --- un file inventato non deve finire nel database
    s1b.emit("create_role", {"role_name": "capo", "icon": "../../etc/passwd",
                             "color": "#00ff00", "modifica": True})
    with A.get_db() as db:
        r = A.uno(db, "SELECT icon FROM roles WHERE space_id=%s AND name='capo'",
                  (sp_id,))
    check("un file non presente in cartella viene scartato", r["icon"] == "")

    # --- l'icona del ruolo arriva nei messaggi
    # (rimessa: il test qui sopra l'ha azzerata di proposito)
    s1b.emit("create_role", {"role_name": "capo", "icon": "mod.svg",
                             "color": "#00ff00", "modifica": True})
    s1b.emit("message", {"msg": "/role dany capo"}, callback=True)
    s2b.get_received()
    s2b.emit("message", {"msg": "sono capo", "tmp": "t6"}, callback=True)
    eco = [dati(e) for e in eventi(s2b, "message")]
    mio = [d for d in eco if d.get("tmp") == "t6"]
    check("il messaggio porta l'url dell'icona del ruolo",
          mio and mio[0].get("icon") == "/static/icons/mod.svg")

    print("\nUPLOAD ICONA (SUPABASE STORAGE)")

    # Storage finto: qui non si raggiunge Supabase, e comunque i test
    # devono verificare le nostre regole, non le loro.
    caricati = []

    def finto_storage(percorso, blob, mime):
        caricati.append((percorso, len(blob), mime))
        return f"https://finto.supabase.co/storage/v1/object/public/role-icons/{percorso}", None

    A.SUPABASE_URL = "https://finto.supabase.co"
    A.SUPABASE_SERVICE_KEY = "service-key-finta"
    vero_carica = A.carica_su_storage
    A.carica_su_storage = finto_storage

    png = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfF"
           "cSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

    r = c1.post(f"/api/role-icon/{code}",
                json={"role": "capo", "data": png})
    check("l'upload va a buon fine", r.status_code == 200)
    url = r.get_json().get("url", "")
    check("torna l'url pubblico dello Storage",
          url.startswith("https://finto.supabase.co/storage/v1/object/public/"))
    check("con ?v= per bucare la cache del browser", "?v=" in url)
    # il percorso e' costruito dal server: <space_id>/<role_id>.png.
    # Niente che arrivi dal client ci finisce dentro.
    with A.get_db() as db:
        rid = A.uno(db, "SELECT id FROM roles WHERE space_id=%s AND name='capo'",
                    (sp_id,))["id"]
    check("il percorso lo decide il server, non il client",
          caricati and caricati[0][0] == f"{sp_id}/{rid}.png")
    check("l'estensione segue il tipo dichiarato",
          caricati and caricati[0][2] == "image/png")

    with A.get_db() as db:
        rr = A.uno(db, "SELECT icon FROM roles WHERE space_id=%s AND name='capo'",
                   (sp_id,))
    check("l'url finisce sul ruolo", rr["icon"] == url)
    check("e url_icona lo restituisce cosi' com'e'",
          A.url_icona("capo", url) == url)

    # --- cosa NON deve passare
    r = c1.post(f"/api/role-icon/{code}",
                json={"role": "capo", "data": "data:application/pdf;base64,AAAA"})
    check("formato non ammesso: rifiutato", r.status_code == 400)

    grosso = "data:image/png;base64," + base64.b64encode(b"x" * 300000).decode()
    r = c1.post(f"/api/role-icon/{code}", json={"role": "capo", "data": grosso})
    check("immagine oltre 256 KB: rifiutata", r.status_code == 400)

    svg = ("data:image/svg+xml;base64,"
           + base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg">'
                              b'<script>alert(1)</script></svg>').decode())
    r = c1.post(f"/api/role-icon/{code}", json={"role": "capo", "data": svg})
    check("SVG con script: rifiutato", r.status_code == 400)

    r = c2.post(f"/api/role-icon/{code}", json={"role": "capo", "data": png})
    check("chi non ha MANAGE_ROLES non carica", r.status_code == 403)

    r = c1.post(f"/api/role-icon/{code}", json={"role": "owner", "data": png})
    check("non si carica su un ruolo pari o superiore al proprio",
          r.status_code == 403)

    A.SUPABASE_SERVICE_KEY = ""
    r = c1.post(f"/api/role-icon/{code}", json={"role": "capo", "data": png})
    check("senza configurazione lo dice invece di rompersi",
          r.status_code == 503 and "SUPABASE" in r.get_json()["error"])
    A.SUPABASE_SERVICE_KEY = "service-key-finta"
    A.carica_su_storage = vero_carica

    # Le chiavi nuove (sb_secret_...) non sono JWT: Supabase le rifiuta
    # nell'header Authorization e vanno passate in 'apikey'. I progetti
    # creati da fine 2025 hanno solo queste.
    import http.server
    import threading

    visti = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            visti.clear()
            visti.update({k.lower(): v for k, v in self.headers.items()})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 5112), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    A.SUPABASE_URL = "http://127.0.0.1:5112"

    A.SUPABASE_SERVICE_KEY = "sb_secret_finta"
    A.carica_su_storage("1/2.png", b"\x89PNG", "image/png")
    check("chiave nuova: va in 'apikey'",
          visti.get("apikey") == "sb_secret_finta")
    check("chiave nuova: niente Authorization, la rifiuterebbe",
          "authorization" not in visti)

    A.SUPABASE_SERVICE_KEY = "eyJhbGciOiJIUzI1NiJ9.finta.jwt"
    A.carica_su_storage("1/2.png", b"\x89PNG", "image/png")
    check("chiave vecchia (JWT): anche in Authorization",
          visti.get("authorization", "").startswith("Bearer ey"))
    check("l'upsert e' sempre attivo: ricaricare sostituisce",
          visti.get("x-upsert") == "true")
    srv.shutdown()

    print("\nSENZA LA MIGRAZIONE")

    # La chat deve reggere anche se migrazione_icone.sql non e' ancora
    # stata eseguita: prima mancava la colonna e /api/messages tornava
    # 500 su ogni canale, cioe' chat completamente bloccata.
    with A.get_db() as db:
        db.execute("ALTER TABLE roles DROP COLUMN IF EXISTS icon")
        db.commit()
    A.rileva_colonna_icon()
    check("la colonna mancante viene rilevata", A.HA_COLONNA_ICON is False)

    r = c1.get(f"/api/messages/{ch2}?limit=50")
    check("la cronologia risponde lo stesso", r.status_code == 200)

    s1c = A.socketio.test_client(A.app, flask_test_client=c1)
    s1c.emit("join", {"code": code})
    s1c.get_received()
    ack = s1c.emit("message", {"msg": "senza colonna", "tmp": "t7"},
                   callback=True)
    check("si riesce comunque a scrivere", ack and ack.get("ok") is True)

    s1c.emit("create_role", {"role_name": "vice", "color": "#123456",
                             "icon": "mod.svg", "modifica": False})
    with A.get_db() as db:
        r = A.uno(db, "SELECT color FROM roles WHERE space_id=%s AND name='vice'",
                  (sp_id,))
    check("i ruoli si creano lo stesso, senza icona scelta",
          r and r["color"] == "#123456")

    storico = c1.get(f"/api/messages/{ch2}?limit=50").get_json()
    check("e il file omonimo continua a fare da icona",
          any(m["icon"] == "/static/icons/owner.svg" for m in storico))

    # rimessa: le altre suite girano sullo schema completo
    with A.get_db() as db:
        db.execute("ALTER TABLE roles ADD COLUMN IF NOT EXISTS icon TEXT")
        db.commit()
    A.rileva_colonna_icon()

    print("\nPOOLER SUPABASE")

    # Il pooler è in transaction mode: un prepared statement creato su
    # una connessione server non esiste sulla successiva. Se psycopg ne
    # crea anche uno solo, in produzione tornano i
    #   prepared statement "_pg3_N" does not exist
    with A.get_db() as db:
        for _ in range(12):           # oltre la soglia di 5 di psycopg
            A.uno(db, "SELECT 1 AS x")
        n = A.uno(db, "SELECT COUNT(*) AS n FROM pg_prepared_statements")["n"]
    check("nessun prepared statement creato sul server", n == 0)

    print("\nTUTTO OK" if ok else "\nCI SONO ERRORI")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
