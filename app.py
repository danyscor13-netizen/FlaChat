"""
FlaChat — superstanze permanenti (Postgres / Supabase).

Porting da SQLite. Differenze rispetto alla versione sqlite3:
  - psycopg con pool di connessioni (Supabase ha un limite di connessioni)
  - placeholder %s invece di ?
  - INSERT ... RETURNING id invece di cur.lastrowid
  - TIMESTAMPTZ invece di float epoch
"""

from flask import (Flask, render_template, request, redirect,
                   url_for, session, jsonify, abort)
from flask_socketio import SocketIO, join_room, emit
from werkzeug.security import generate_password_hash, check_password_hash

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import base64
import os
import random
import string
import time
import urllib.error
import urllib.request

import psycopg
from psycopg.rows import dict_row

import menzioni
from psycopg_pool import ConnectionPool

import shlex # Ci serve

import resend # Verificazione e-mail
# Non dovrai farci molto, ti servirà solo per verificare che tu sei il proprietario dell'account
# E cambiare la tua password

# Sempre verificazione
import hashlib
import secrets

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "cambia-questa-chiave")
socketio = SocketIO(app)

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL non impostata.\n"
        "Prendila da Supabase: pulsante Connect -> Transaction pooler (porta 6543).\n"
        '  export DATABASE_URL="postgresql://postgres.xxx:PW@...pooler.supabase.com:6543/postgres"')

# Il pooler di Supabase (porta 6543) è già un pool lato server, ma un pool
# lato client evita di riaprire una connessione TCP a ogni query: su rete
# remota il handshake costa più della query stessa.
#
# prepare_threshold=None è obbligatorio con quel pooler. psycopg3, dopo
# cinque esecuzioni della stessa query, la "prepara" sul server e poi la
# richiama per nome (_pg3_0, _pg3_1...). Ma il pooler è in transaction
# mode: ogni transazione può finire su una connessione server diversa,
# dove quel nome non esiste mai stato. Da lì l'errore
#   prepared statement "_pg3_0" does not exist
# che compare a caso dopo un po' di traffico, sulle query più usate —
# esattamente quelle che rendono la chat inutilizzabile. Disattivandoli
# ogni query viaggia per intero: si perde una micro-ottimizzazione che
# con questo pooler non funzionerebbe comunque.
pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=10,
                      kwargs={"row_factory": dict_row,
                              "prepare_threshold": None},
                      open=True)


@contextmanager
def get_db():
    """
    Uso:  with get_db() as db:  ...
    Il commit è automatico all'uscita, il rollback se c'è un'eccezione.
    """
    with pool.connection() as conn:
        yield conn


def uno(db, sql, par=()):
    """Prima riga o None."""
    return db.execute(sql, par).fetchone()


def tutti(db, sql, par=()):
    return db.execute(sql, par).fetchall()


def inserisci(db, sql, par=()):
    """INSERT ... RETURNING id -> id. Sostituisce cur.lastrowid."""
    r = db.execute(sql, par).fetchone()
    return r["id"] if r else None


# =========================================================
# PERMESSI — bitmask
# =========================================================

SEND_MESSAGES    = 1
MANAGE_CHANNELS  = 2
KICK             = 4
BAN              = 8
MANAGE_ROLES     = 16
MENTION_EVERYONE = 32
MANAGE_MESSAGES  = 64
ADMIN            = 128

ALL_PERMS = 0xFFFF

PERM_NAMES = {
    SEND_MESSAGES:    "Inviare messaggi",
    MANAGE_CHANNELS:  "Gestire i canali",
    KICK:             "Cacciare membri",
    BAN:              "Bannare membri",
    MANAGE_ROLES:     "Gestire i ruoli",
    MENTION_EVERYONE: "Menzionare @everyone",
    MANAGE_MESSAGES:  "Eliminare messaggi altrui",
    ADMIN:            "Amministratore",
}

# Quali permessi ha senso sovrascrivere su un singolo canale.
# BAN, KICK, MANAGE_ROLES e ADMIN valgono sulla stanza intera:
# "puoi bannare solo in #random" non significherebbe nulla.
PERM_CANALE = [SEND_MESSAGES, MANAGE_MESSAGES, MENTION_EVERYONE, MANAGE_CHANNELS]

RUOLI_DEFAULT = [
    ("owner", "#f0b232", ADMIN, 100, False),
    ("admin", "#e04b4b", SEND_MESSAGES | MANAGE_CHANNELS | KICK | BAN
                         | MANAGE_MESSAGES | MENTION_EVERYONE, 80, False),
    ("mod",   "#5b8dd9", SEND_MESSAGES | KICK | MANAGE_MESSAGES, 50, False),
    ("user",  "#cccccc", SEND_MESSAGES | MENTION_EVERYONE, 0, True),
]

# ---------------------------------------------------------
# ICONE DEI RUOLI
# ---------------------------------------------------------
# Le icone stanno in static/icons/ e si chiamano come il ruolo:
# owner.png, admin.png, mod.png. La cartella viene letta all'avvio,
# quindi l'estensione non conta (svg, png, webp...) e non c'e' nessun
# elenco da tenere aggiornato a mano: se domani crei un ruolo "bot" ti
# basta mettere static/icons/bot.svg e compare da solo.

ICONE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "static", "icons")
ICONE_EXT = (".svg", ".png", ".webp", ".gif", ".jpg", ".jpeg")


def mappa_icone():
    """{nome_ruolo: url} per i ruoli che hanno un file omonimo."""
    out = {}
    try:
        for f in sorted(os.listdir(ICONE_DIR)):
            base, ext = os.path.splitext(f)
            if ext.lower() in ICONE_EXT and base.lower() not in out:
                out[base.lower()] = f"/static/icons/{f}"
    except FileNotFoundError:
        pass          # cartella non ancora creata: nessuna icona, fine
    return out


def icone_disponibili():
    """
    Tutti i file della cartella, per il menu di scelta.

    Riletta a ogni apertura del pannello, non una volta all'avvio: se
    butti dentro un file mentre il server gira lo trovi subito, senza
    riavviare. Sono una manciata di voci, costa niente.
    """
    out = []
    try:
        for f in sorted(os.listdir(ICONE_DIR)):
            base, ext = os.path.splitext(f)
            if ext.lower() in ICONE_EXT:
                out.append({"file": f, "nome": base.lower(),
                            "url": f"/static/icons/{f}"})
    except FileNotFoundError:
        pass
    return out


def url_icona(nome_ruolo, icon=None):
    """
    L'icona di un ruolo.

      icon = 'https://...'  -> immagine caricata su Supabase Storage
      icon = 'capo.svg'     -> quel file di static/icons/
      icon = stringa vuota  -> nessuna icona, scelta esplicita
      icon = NULL           -> il file che si chiama come il ruolo, se c'e'

    L'ultimo caso e' quello delle stanze nate prima che si potesse
    scegliere: continuano a mostrare owner/admin/mod senza migrazioni.
    """
    if icon:
        if icon.startswith("http"):
            return icon
        return f"/static/icons/{icon}"
    if icon == "":
        return ""
    return mappa_icone().get((nome_ruolo or "").lower(), "")


# ---------------------------------------------------------
# UPLOAD SU SUPABASE STORAGE
# ---------------------------------------------------------
# Il file viene caricato dal server, non dal browser. Le policy dello
# Storage sono severe di proposito: dal browser servirebbe una policy
# che permette la scrittura a chiunque sia loggato su Supabase — ma i
# nostri utenti non lo sono, la sessione e' di Flask. Passando dal
# server usiamo la service key, che sta solo qui e non arriva mai al
# client, e il bucket puo' restare chiuso in scrittura.
#
# Il controllo su chi puo' caricare lo facciamo noi (permesso
# MANAGE_ROLES + posizione del ruolo), che e' l'unico posto dove
# sappiamo cosa significhi "ruolo pari o superiore al tuo".

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET", "role-icons")

# Tenuti stretti: un'icona sta accanto a un nome, non serve altro.
MAX_ICONA = 256 * 1024
MIME_ICONA = {
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/svg+xml": ".svg",
}


def storage_attivo():
    return bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)


def carica_su_storage(percorso, blob, mime):
    """
    Carica (o sostituisce) un oggetto. Torna (url, None) oppure
    (None, messaggio_errore).

    Gli errori dello Storage vengono riportati come sono: quando una
    policy blocca la scrittura il corpo della risposta dice quale, ed
    e' l'unica cosa che permette di capirci qualcosa.
    """
    url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{percorso}"
    req = urllib.request.Request(url, data=blob, method="POST")

    # Le chiavi nuove (sb_secret_...) NON sono JWT e Supabase le rifiuta
    # nell'header Authorization. Vanno in 'apikey'. Le vecchie
    # (service_role, che comincia per 'ey' perche' e' un JWT) funzionano
    # in entrambi, ma le mettiamo comunque in tutti e due per non dover
    # distinguere piu' del necessario.
    req.add_header("apikey", SUPABASE_SERVICE_KEY)
    if SUPABASE_SERVICE_KEY.startswith("ey"):
        req.add_header("Authorization", f"Bearer {SUPABASE_SERVICE_KEY}")

    req.add_header("Content-Type", mime)
    req.add_header("x-upsert", "true")     # ricaricare sostituisce
    req.add_header("Cache-Control", "3600")
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except urllib.error.HTTPError as e:
        corpo = e.read().decode("utf-8", "replace")[:300]
        if e.code in (400, 404) and "Bucket not found" in corpo:
            return None, (f"Il bucket '{SUPABASE_BUCKET}' non esiste. "
                          "Crealo su Supabase (Storage > New bucket) "
                          "e mettilo pubblico in lettura.")
        if e.code in (401, 403):
            return None, ("Lo Storage ha rifiutato la scrittura: la chiave "
                          "deve essere quella segreta (sb_secret_... oppure "
                          "la vecchia service_role), non la publishable o "
                          f"anon. ({corpo})")
        return None, f"Storage: errore {e.code}. {corpo}"
    except Exception as e:
        return None, f"Storage irraggiungibile: {e}"

    return (f"{SUPABASE_URL}/storage/v1/object/public/"
            f"{SUPABASE_BUCKET}/{percorso}"), None


ICONE = mappa_icone()

# Diagnostica all'avvio. La causa piu' banale di "l'icona non compare"
# e' che la cartella non e' stata copiata nel deploy: senza questa riga
# non c'e' modo di accorgersene, perche' l'app funziona lo stesso e
# semplicemente non disegna niente.
if ICONE:
    print(f"Icone dei ruoli trovate in static/icons/: {', '.join(sorted(ICONE))}")
else:
    print("Nessuna icona in static/icons/ (cartella vuota o non copiata "
          "nel deploy): i ruoli non mostreranno nessuna icona.")

# La colonna roles.icon arriva con migrazione_icone.sql. Se non e' ancora
# stata eseguita l'app deve continuare a funzionare: senza questo
# controllo ogni query sui ruoli esplode e la chat non carica piu'
# niente, con un 500 su /api/messages che non dice cosa fare.
# Si perde solo la scelta manuale dell'icona; il file omonimo continua
# a funzionare, perche' quello non passa dal database.
HA_COLONNA_ICON = False


def rileva_colonna_icon():
    global HA_COLONNA_ICON
    try:
        with get_db() as db:
            HA_COLONNA_ICON = uno(db, """
                SELECT 1 AS x FROM information_schema.columns
                WHERE table_name='roles' AND column_name='icon'""") is not None
    except Exception:
        HA_COLONNA_ICON = False

    if not HA_COLONNA_ICON:
        print("ATTENZIONE: manca la colonna roles.icon. "
              "Esegui migrazione_icone.sql sul database. "
              "Nel frattempo le icone dei ruoli usano solo il file omonimo.")
    return HA_COLONNA_ICON


def sel_icon(alias="r"):
    """Il pezzo di SELECT per l'icona, o NULL se la colonna non c'e'."""
    return f"{alias}.icon" if HA_COLONNA_ICON else "NULL"


rileva_colonna_icon()

# =========================================================
# STATO VOLATILE — solo "chi è connesso adesso"
# =========================================================
online = {}        # space_id -> {sid: user_id}
sid_channel = {}   # sid -> channel_id
sid_space = {}     # sid -> space_id

WELCOMES = [
    " è entrato nella stanza!",
    ", spero che tu abbia portato la pizza!",
    " è appena atterrato!",
    ", sentiti libero di accomodarti!",
]


def ora():
    return datetime.now(timezone.utc)


def ts(dt):
    """TIMESTAMPTZ -> float epoch, per il client JS."""
    return dt.timestamp() if dt else None


# =========================================================
# PERMESSI — calcolo
# =========================================================

def permessi(db, user_id, space_id, channel_id=None):
    base = 0
    for r in tutti(db, """
        SELECT r.permissions FROM member_roles mr
        JOIN roles r ON r.id = mr.role_id
        WHERE mr.user_id = %s AND mr.space_id = %s
    """, (user_id, space_id)):
        base |= r["permissions"]

    if base & ADMIN:
        return ALL_PERMS
    if channel_id is None:
        return base

    allow = deny = 0
    for r in tutti(db, """
        SELECT o.allow, o.deny FROM channel_overrides o
        JOIN member_roles mr ON mr.role_id = o.role_id
        WHERE o.channel_id = %s AND mr.user_id = %s
    """, (channel_id, user_id)):
        allow |= r["allow"]
        deny |= r["deny"]

    return (base & ~deny) | allow


def puo(db, user_id, space_id, perm, channel_id=None):
    return bool(permessi(db, user_id, space_id, channel_id) & perm)


def posizione(db, user_id, space_id):
    r = uno(db, """
        SELECT COALESCE(MAX(r."position"), -1) AS p FROM member_roles mr
        JOIN roles r ON r.id = mr.role_id
        WHERE mr.user_id = %s AND mr.space_id = %s
    """, (user_id, space_id))
    return r["p"]


def puo_agire_su(db, attore, bersaglio, space_id):
    if attore == bersaglio:
        return False
    return posizione(db, attore, space_id) > posizione(db, bersaglio, space_id)


def non_letti(db, user_id, channel_id):
    r = uno(db, """
        SELECT COUNT(*) AS n FROM messages m
        WHERE m.channel_id = %s
          AND m.deleted_at IS NULL
          AND m.author_id <> %s
          AND m.id > COALESCE(
              (SELECT last_read_msg_id FROM read_state
               WHERE user_id = %s AND channel_id = %s), 0)
    """, (channel_id, user_id, user_id, channel_id))
    return r["n"]


def canali_visibili(db, user_id, space_id):
    out = []
    for ch in tutti(db, """SELECT id, name, topic FROM channels
                           WHERE space_id = %s ORDER BY "position", id""",
                    (space_id,)):
        p = permessi(db, user_id, space_id, ch["id"])
        out.append({
            "id": ch["id"],
            "name": ch["name"],
            "topic": ch["topic"],
            "can_write": bool(p & SEND_MESSAGES),
            "unread": non_letti(db, user_id, ch["id"]),
        })
    return out


def segna_letto(db, user_id, channel_id):
    db.execute("""
        INSERT INTO read_state (user_id, channel_id, last_read_msg_id)
        VALUES (%s, %s, COALESCE((SELECT MAX(id) FROM messages
                                  WHERE channel_id = %s), 0))
        ON CONFLICT (user_id, channel_id) DO UPDATE
          SET last_read_msg_id = EXCLUDED.last_read_msg_id,
              updated_at = now()
    """, (user_id, channel_id, channel_id))


# =========================================================
# SUPERSTANZE
# =========================================================

def codice_libero(db):
    while True:
        code = "".join(random.choices(string.digits, k=6))
        if not uno(db, "SELECT 1 FROM spaces WHERE code = %s", (code,)):
            return code


def crea_space(db, nome, owner_id, code=None):
    code = code or codice_libero(db)
    space_id = inserisci(db, """INSERT INTO spaces (code, name, owner_id)
                                VALUES (%s, %s, %s) RETURNING id""",
                         (code, nome, owner_id))

    rid = {}
    for nome_r, colore, perms, pos, dflt in RUOLI_DEFAULT:
        rid[nome_r] = inserisci(db, """
            INSERT INTO roles (space_id, name, color, permissions, "position", is_default)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
            (space_id, nome_r, colore, perms, pos, dflt))

    db.execute("""INSERT INTO channels (space_id, name, "position")
                  VALUES (%s, 'general', 0)""", (space_id,))
    db.execute("INSERT INTO members (space_id, user_id) VALUES (%s, %s)",
               (space_id, owner_id))
    db.execute("""INSERT INTO member_roles (space_id, user_id, role_id)
                  VALUES (%s,%s,%s)""", (space_id, owner_id, rid["owner"]))
    return space_id, code


def entra_space(db, space_id, user_id):
    if uno(db, "SELECT 1 FROM members WHERE space_id=%s AND user_id=%s",
           (space_id, user_id)):
        return False
    db.execute("INSERT INTO members (space_id, user_id) VALUES (%s,%s)",
               (space_id, user_id))
    r = uno(db, "SELECT id FROM roles WHERE space_id=%s AND is_default", (space_id,))
    if r:
        db.execute("""INSERT INTO member_roles (space_id, user_id, role_id)
                      VALUES (%s,%s,%s)""", (space_id, user_id, r["id"]))
    return True


def ban_attivo(db, space_id, user_id):
    b = uno(db, "SELECT expire FROM bans WHERE space_id=%s AND user_id=%s",
            (space_id, user_id))
    if not b:
        return False
    if b["expire"] is None or b["expire"] > ora():
        return True
    db.execute("DELETE FROM bans WHERE space_id=%s AND user_id=%s",
               (space_id, user_id))
    return False


# =========================================================
# ROTTE
# =========================================================

def utente_corrente():
    return session.get("user_id")


@app.route("/")
def home():
    if utente_corrente():
        return redirect(url_for("lobby"))
    return render_template("home.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    error = None
    if request.method == "POST":
        u = request.form["username"].strip()
        p = request.form["password"].strip()
        if not u or not p:
            error = "Compila tutti i campi."
        elif len(u) > 32:
            error = "Nome utente troppo lungo (max 32)."
        else:
            try:
                with get_db() as db:
                    uid = inserisci(db, """INSERT INTO users (username, password)
                                           VALUES (%s,%s) RETURNING id""",
                                    (u, generate_password_hash(p)))
                session["user_id"] = uid
                session["username"] = u
                return redirect(url_for("lobby"))
            except psycopg.errors.UniqueViolation:
                error = "Username già in uso."
    return render_template("register.html", error=error)


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        with get_db() as db:
            user = uno(db, "SELECT * FROM users WHERE lower(username)=%s",
                       (request.form["username"].strip().lower(),))
        if user and check_password_hash(user["password"],
                                        request.form["password"].strip()):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            return redirect(url_for("lobby"))
        error = "Credenziali errate."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.route("/lobby", methods=["GET", "POST"])
def lobby():
    uid = utente_corrente()
    if not uid:
        return redirect(url_for("home"))

    error = None
    with get_db() as db:
        if request.method == "POST":
            azione = request.form.get("azione")

            if azione == "crea":
                nome = request.form.get("nome", "").strip()
                if not nome:
                    error = "Dai un nome alla superstanza."
                else:
                    _, code = crea_space(db, nome[:60], uid)
                    return redirect(url_for("chat", code=code))

            elif azione == "entra":
                code = request.form.get("code", "").strip()
                sp = uno(db, "SELECT * FROM spaces WHERE code=%s", (code,))
                if not sp:
                    error = "Nessuna superstanza con questo codice."
                elif ban_attivo(db, sp["id"], uid):
                    return redirect(url_for("lobby", banned=1))
                else:
                    entra_space(db, sp["id"], uid)
                    return redirect(url_for("chat", code=code))

        stanze = tutti(db, """
            SELECT s.code, s.name,
                   (SELECT COUNT(*) FROM members WHERE space_id=s.id) AS membri
            FROM spaces s
            JOIN members m ON m.space_id = s.id
            WHERE m.user_id = %s
            ORDER BY s.name
        """, (uid,))

        me = uno(db, """SELECT email, avviso_email_nascosto
                        FROM users WHERE id=%s""", (uid,))
        if not me:
            session.clear()
            return redirect(url_for("home"))
        mostra_avviso = not me["email"] and not me["avviso_email_nascosto"]

    return render_template("lobby.html", username=session.get("username"),
                           stanze=stanze, error=error,
                           mostra_avviso=mostra_avviso)

@app.route("/profile", methods=["GET", "POST"])
def profile():
    username = session.get("username")
    can_mod = True

    if not username:
        return redirect("/login")

    with get_db() as db:
        if request.method == "POST":
            bio = request.form.get("bio", "").strip()[:500]

            db.execute(
                "UPDATE users SET bio=%s WHERE username=%s",
                (bio, username)
            )
            db.commit()

        user = db.execute("""
            SELECT username, bio
            FROM users
            WHERE username=%s
        """, (username,)).fetchone()

        def checkIfMail():
            userMail = db.execute("""
            select email 
            from users 
            where username = %s
            """, (username,)).fetchone()

            if not userMail:
                return False
            else:
                return True

        doesUserHaveMail = checkIfMail()

    if not user:
        return "Utente non trovato :(", 404

    return render_template(
        "profile.html",
        username=user["username"],
        bio=user["bio"],
        can_mod=can_mod,
        doesUserHaveMail=doesUserHaveMail
    )

@app.route("/profile/<username>")
def pub_profile(username):
    can_mod = False
    with get_db() as db:
        user = db.execute("select username, bio from users where username = %s", (username,)).fetchone()

        if not user:
            return "Utente non trovato :(", 404

    return render_template("profile.html",
                           username=user["username"],
                           bio=user["bio"],
                           can_mod=can_mod)

# ---------------------------------------------------------
# Resend: Verificazione e-mail
# ---------------------------------------------------------

@app.route("/verify-email", methods=["GET", "POST"])
def vemail():
    uid = utente_corrente()
    if not uid:
        return redirect(url_for("login"))

    if request.method == "POST":
        mail = request.form.get("email", "").strip().lower()[:200]

        if not mail:
            return redirect(url_for("vemail"))

        with get_db() as db:
            db.execute("""update users set email=%s, is_email_verified=false
                where id=%s""", (mail, uid))

            token = secrets.token_urlsafe(32)

            db.execute("""
                DELETE FROM verify_tokens
                WHERE user_id=%s
            """, (uid,))

            db.execute("""insert into verify_tokens
                        (user_id, email, token_hash, expires_at)
                        values (%s, %s, %s, %s)""",
                        (uid, mail,
                         hashlib.sha256(token.encode()).hexdigest(),
                         ora() + timedelta(minutes=15)))
            db.commit()

        link = f"{request.url_root.rstrip('/')}/verify-email/{token}"
        resend.api_key = os.environ.get("RESEND_API_KEY")
        resend.Emails.send({
            "from": "FlaChat <onboarding@resend.dev>",
            "to": [mail],
            "subject": "FlaChat - Conferma la tua e-mail",
            "html": f"""
                <style>
                    @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=JetBrains+Mono:wght@400;700&display=swap');
                </style>

                <div style="
                    margin: 0;
                    padding: 40px 20px;
                    background: #15121f;
                    color: #efecf7;
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
                ">

                    <div style="
                        max-width: 520px;
                        margin: auto;
                        padding: 32px;
                        background: #1c1829;
                        border: 1px solid #332b49;
                        border-radius: 4px;
                        box-shadow: 0 8px 30px rgba(0, 0, 0, 0.25);
                    ">

                        <!-- Branding -->
                        <div style="
                            margin-bottom: 24px;
                            font-family: 'JetBrains Mono', 'Courier New', monospace;
                            font-size: 17px;
                            font-weight: 700;
                            letter-spacing: -0.02em;
                            color: #f2c14e;
                        ">
                            FlaChat
                        </div>

                        <!-- Title -->
                        <h1 style="
                            margin: 0 0 12px;
                            font-family: 'Space Grotesk', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                            font-size: 26px;
                            line-height: 1.2;
                            font-weight: 700;
                            color: #efecf7;
                        ">
                            Conferma la tua e-mail
                        </h1>

                        <!-- Main text -->
                        <p style="
                            margin: 0 0 24px;
                            color: #958dab;
                            font-size: 15px;
                            line-height: 1.65;
                        ">
                            Hai aggiunto l'indirizzo e-mail al tuo account.
                            Verificalo per confermare che sia realmente tuo.
                        </p>

                        <!-- Additional information -->
                        <div style="
                            margin: 0 0 24px;
                            padding: 14px 16px;
                            background: #251f36;
                            border-left: 3px solid #b98cff;
                            border-radius: 4px;
                        ">
                            <p style="
                                margin: 0;
                                color: #958dab;
                                font-size: 13px;
                                line-height: 1.6;
                            ">
                                L'e-mail ti servirà per recuperare il tuo account,
                                per esempio, se ti sei dimenticato la password.
                            </p>

                            <p style="
                                margin: 10px 0 0;
                                color: #efecf7;
                                font-size: 13px;
                                line-height: 1.6;
                            ">
                                Non vorrai mica perdere accesso al tuo account...
                            </p>
                        </div>

                        <!-- Verification button -->
                        <a href="{link}" style="
                            display: inline-block;
                            padding: 12px 22px;
                            background: #f2c14e;
                            color: #1a1626;
                            border-radius: 4px;
                            text-decoration: none;
                            font-family: 'Space Grotesk', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                            font-size: 15px;
                            font-weight: 700;
                        ">
                            Conferma e-mail
                        </a>

                        <!-- Expiration -->
                        <div style="
                            margin-top: 14px;
                            color: #958dab;
                            font-family: 'JetBrains Mono', 'Courier New', monospace;
                            font-size: 11px;
                        ">
                            ⏱ Il link scade tra 15 minuti.
                        </div>

                        <!-- Divider -->
                        <div style="
                            height: 1px;
                            margin: 28px 0 20px;
                            background: #332b49;
                        "></div>

                        <!-- Security notice -->
                        <p style="
                            margin: 0;
                            color: #958dab;
                            font-size: 12px;
                            line-height: 1.6;
                        ">
                            Se non hai aggiunto nessuna e-mail,
                            puoi tranquillamente ignorare questa e-mail.
                        </p>

                        <!-- Signature -->
                        <div style="
                            margin-top: 22px;
                            padding-top: 16px;
                            border-top: 1px solid #332b49;
                        ">
                            <p style="
                                margin: 0;
                                font-family: 'JetBrains Mono', 'Courier New', monospace;
                                font-size: 12px;
                                font-weight: 700;
                                color: #f2c14e;
                            ">
                                D.P. - FlaChat
                            </p>
                        </div>

                    </div>
                </div>
            """
        })

@app.route("/forgotpassword")
def fpassword():
    return render_template("resetpassword.html")

@app.route("/chat/<code>")
def chat(code):
    uid = utente_corrente()
    if not uid:
        return redirect(url_for("home"))

    with get_db() as db:
        sp = uno(db, "SELECT * FROM spaces WHERE code=%s", (code,))
        if not sp:
            return redirect(url_for("lobby"))
        if ban_attivo(db, sp["id"], uid):
            return redirect(url_for("lobby", banned=1))
        entra_space(db, sp["id"], uid)

    return render_template("chat.html", username=session["username"],
                           space_name=sp["name"], code=sp["code"])

@app.route("/api/avviso-email/nascondi", methods=["POST"])
def api_avviso_email():
    uid = utente_corrente()
    if not uid:
        abort(401)
    with get_db() as db:
        db.execute("UPDATE users SET avviso_email_nascosto=true WHERE id=%s",
                   (uid,))
        db.commit()
    return jsonify({"ok": True})

@app.route("/api/messages/<int:channel_id>")
def api_messages(channel_id):
    """Cronologia paginata all'indietro. before=<id> per il 'carica altri'."""
    uid = utente_corrente()
    if not uid:
        abort(401)

    before = request.args.get("before", type=int)
    limite = min(request.args.get("limit", 50, type=int), 100)

    with get_db() as db:
        ch = uno(db, "SELECT * FROM channels WHERE id=%s", (channel_id,))
        if not ch or not uno(db, """SELECT 1 FROM members
                                    WHERE space_id=%s AND user_id=%s""",
                             (ch["space_id"], uid)):
            abort(403)

        sql = f"""SELECT m.id, m.content, m.created_at, m.edited_at,
                        u.id AS uid, u.username,
                        (SELECT r.color FROM member_roles mr
                         JOIN roles r ON r.id = mr.role_id
                         WHERE mr.user_id = u.id AND mr.space_id = %s
                         ORDER BY r."position" DESC LIMIT 1) AS color,
                        (SELECT r.name FROM member_roles mr
                         JOIN roles r ON r.id = mr.role_id
                         WHERE mr.user_id = u.id AND mr.space_id = %s
                         ORDER BY r."position" DESC LIMIT 1) AS role,
                        (SELECT {sel_icon()} FROM member_roles mr
                         JOIN roles r ON r.id = mr.role_id
                         WHERE mr.user_id = u.id AND mr.space_id = %s
                         ORDER BY r."position" DESC LIMIT 1) AS role_icon
                 FROM messages m
                 LEFT JOIN users u ON u.id = m.author_id
                 WHERE m.channel_id = %s AND m.deleted_at IS NULL"""
        par = [ch["space_id"], ch["space_id"], ch["space_id"], channel_id]
        if before:
            sql += " AND m.id < %s"
            par.append(before)
        sql += " ORDER BY m.id DESC LIMIT %s"
        par.append(limite)

        righe = tutti(db, sql, par)

    return jsonify([{
        "id": r["id"],
        "username": r["username"] or "utente eliminato",
        "msg": r["content"],
        "color": r["color"] or "#cccccc",
        "role": r["role"] or "",
        "icon": url_icona(r["role"], r["role_icon"]),
        "ts": ts(r["created_at"]),
        "own": r["uid"] == uid,
    } for r in reversed(righe)])


# ---------------------------------------------------------
# API: menzioni e notifiche
# ---------------------------------------------------------

@app.route("/api/role-icon/<code>", methods=["POST"])
def api_role_icon(code):
    """
    Carica un'immagine e la assegna come icona di un ruolo.

    Il browser manda il file gia' letto come data URL. Tutti i controlli
    stanno qui: sul client servono solo a dare un errore veloce, non
    fanno testo.
    """
    uid = session.get("user_id")
    if not uid:
        return jsonify(error="Sessione scaduta."), 401
    if not storage_attivo():
        return jsonify(error="Storage non configurato: mancano SUPABASE_URL "
                             "e SUPABASE_SERVICE_KEY."), 503

    dati = request.get_json(silent=True) or {}
    nome_ruolo = (dati.get("role") or "").strip().lower()[:32]
    data_url = dati.get("data") or ""

    with get_db() as db:
        sp = uno(db, "SELECT id FROM spaces WHERE code=%s", (str(code),))
        if not sp:
            return jsonify(error="Stanza non trovata."), 404
        space_id = sp["id"]

        if not puo(db, uid, space_id, MANAGE_ROLES):
            return jsonify(error="Non hai i permessi."), 403

        r = uno(db, "SELECT * FROM roles WHERE space_id=%s AND name=%s",
                (space_id, nome_ruolo))
        if not r:
            return jsonify(error="Ruolo non trovato."), 404
        if r["position"] >= posizione(db, uid, space_id):
            return jsonify(error="Non puoi modificare un ruolo pari o "
                                 "superiore al tuo."), 403

        # --- il file
        if not data_url.startswith("data:"):
            return jsonify(error="Immagine non valida."), 400
        try:
            testa, b64 = data_url.split(",", 1)
            mime = testa[5:].split(";")[0].strip().lower()
            blob = base64.b64decode(b64, validate=True)
        except Exception:
            return jsonify(error="Immagine illeggibile."), 400

        if mime not in MIME_ICONA:
            return jsonify(error="Formato non ammesso. Usa PNG, WEBP, GIF, "
                                 "JPG o SVG."), 400
        if not blob:
            return jsonify(error="File vuoto."), 400
        if len(blob) > MAX_ICONA:
            return jsonify(error=f"Immagine troppo grande "
                                 f"({len(blob)//1024} KB, massimo "
                                 f"{MAX_ICONA//1024} KB)."), 400

        # Un SVG e' un documento, non solo un'immagine: se contiene
        # script e qualcuno lo apre per URL diretto, quello script gira
        # sul dominio dello Storage. Nei nostri <img> non verrebbe
        # eseguito, ma non e' un buon motivo per accettarlo.
        if mime == "image/svg+xml":
            testo = blob.decode("utf-8", "replace").lower()
            if "<script" in testo or "javascript:" in testo or "onload=" in testo:
                return jsonify(error="SVG con script: rifiutato."), 400

        # Il percorso lo decidiamo noi, non il client: niente nomi che
        # arrivano da fuori dentro un path.
        percorso = f"{space_id}/{r['id']}{MIME_ICONA[mime]}"
        url, errore = carica_su_storage(percorso, blob, mime)
        if errore:
            return jsonify(error=errore), 502

        # ?v= cambia a ogni caricamento: senza, il browser continuerebbe
        # a mostrare la vecchia immagine allo stesso URL
        url = f"{url}?v={int(time.time())}"

        if HA_COLONNA_ICON:
            db.execute("UPDATE roles SET icon=%s WHERE id=%s", (url, r["id"]))
            db.commit()
        else:
            return jsonify(error="Manca la colonna roles.icon: esegui "
                                 "migrazione_icone.sql."), 503

        manda_utenti(space_id)

    return jsonify(ok=True, url=url)


@app.route("/api/mentionables/<code>")
def api_mentionables(code):
    """Utenti e ruoli menzionabili, per l'autocomplete del client."""
    uid = utente_corrente()
    if not uid:
        abort(401)

    with get_db() as db:
        sp = uno(db, "SELECT id FROM spaces WHERE code=%s", (code,))
        if not sp or not uno(db, """SELECT 1 FROM members
                                    WHERE space_id=%s AND user_id=%s""",
                             (sp["id"], uid)):
            abort(403)

        utenti = tutti(db, """SELECT u.username AS nome,
                                (SELECT r.color FROM member_roles mr
                                 JOIN roles r ON r.id=mr.role_id
                                 WHERE mr.user_id=u.id AND mr.space_id=%s
                                 ORDER BY r."position" DESC LIMIT 1) AS colore
                              FROM members m JOIN users u ON u.id=m.user_id
                              WHERE m.space_id=%s ORDER BY u.username""",
                     (sp["id"], sp["id"]))
        ruoli = tutti(db, """SELECT name AS nome, color AS colore FROM roles
                             WHERE space_id=%s AND mentionable
                             ORDER BY "position" DESC""", (sp["id"],))
        puo_ev = puo(db, uid, sp["id"], MENTION_EVERYONE)

    out = [{"tipo": "utente", "nome": u["nome"],
            "colore": u["colore"] or "#cccccc"} for u in utenti]
    out += [{"tipo": "ruolo", "nome": r["nome"],
             "colore": r["colore"]} for r in ruoli]
    if puo_ev:
        out += [{"tipo": "speciale", "nome": "everyone",
                 "colore": "#f0b232", "desc": "tutti i membri"},
                {"tipo": "speciale", "nome": "here",
                 "colore": "#f0b232", "desc": "chi è online adesso"}]
    return jsonify(out)


@app.route("/api/push/key")
def api_push_key():
    """Chiave pubblica VAPID. Se manca, il client nasconde le notifiche."""
    return jsonify({"key": menzioni.VAPID_PUBLIC,
                    "attive": menzioni.push_attive})


@app.route("/api/push/subscribe", methods=["POST"])
def api_push_subscribe():
    uid = utente_corrente()
    if not uid:
        abort(401)

    d = request.get_json(silent=True) or {}
    endpoint = d.get("endpoint")
    chiavi = d.get("keys") or {}
    if not endpoint or not chiavi.get("p256dh") or not chiavi.get("auth"):
        return jsonify({"ok": False, "errore": "iscrizione incompleta"}), 400

    with get_db() as db:
        # stesso endpoint gia' presente: aggiorna, non duplicare
        db.execute("""INSERT INTO push_subscriptions
                        (user_id, endpoint, p256dh, auth, user_agent)
                      VALUES (%s,%s,%s,%s,%s)
                      ON CONFLICT (endpoint) DO UPDATE
                        SET user_id=EXCLUDED.user_id,
                            p256dh=EXCLUDED.p256dh,
                            auth=EXCLUDED.auth""",
                   (uid, endpoint, chiavi["p256dh"], chiavi["auth"],
                    request.headers.get("User-Agent", "")[:200]))
    return jsonify({"ok": True})


@app.route("/api/push/unsubscribe", methods=["POST"])
def api_push_unsubscribe():
    uid = utente_corrente()
    if not uid:
        abort(401)
    endpoint = (request.get_json(silent=True) or {}).get("endpoint")
    with get_db() as db:
        db.execute("""DELETE FROM push_subscriptions
                      WHERE user_id=%s AND endpoint=%s""", (uid, endpoint))
    return jsonify({"ok": True})


@app.route("/api/notifications/<code>", methods=["GET", "POST"])
def api_notifications(code):
    """Legge e imposta il livello di notifica per una stanza."""
    uid = utente_corrente()
    if not uid:
        abort(401)

    with get_db() as db:
        sp = uno(db, "SELECT id FROM spaces WHERE code=%s", (code,))
        if not sp or not uno(db, """SELECT 1 FROM members
                                    WHERE space_id=%s AND user_id=%s""",
                             (sp["id"], uid)):
            abort(403)

        if request.method == "POST":
            lv = (request.get_json(silent=True) or {}).get("level")
            if lv not in (0, 1, 2):
                return jsonify({"ok": False}), 400
            db.execute("""INSERT INTO notification_prefs
                            (user_id, space_id, channel_id, level)
                          VALUES (%s,%s,NULL,%s)
                          ON CONFLICT (user_id, space_id)
                            WHERE channel_id IS NULL
                          DO UPDATE SET level=EXCLUDED.level,
                                        muted_until=NULL""",
                       (uid, sp["id"], lv))
            return jsonify({"ok": True, "level": lv})

        r = uno(db, """SELECT level FROM notification_prefs
                       WHERE user_id=%s AND space_id=%s AND channel_id IS NULL""",
                (uid, sp["id"]))
    return jsonify({"level": r["level"] if r else menzioni.MENZIONI})


@app.route("/sw.js")
def service_worker():
    """
    Servito dalla radice, non da /static: un service worker può
    controllare solo le pagine nella sua cartella o sotto.
    """
    return app.send_static_file("sw.js"), 200, {
        "Content-Type": "application/javascript",
        "Service-Worker-Allowed": "/",
    }


@app.route("/api/retention/<code>", methods=["GET", "POST"])
def api_retention(code):
    """
    Scadenza dei messaggi. Solo l'owner della stanza può cambiarla:
    cancellare la cronologia è più grave di creare un canale, quindi
    non basta MANAGE_CHANNELS.
    """
    uid = utente_corrente()
    if not uid:
        abort(401)

    with get_db() as db:
        sp = uno(db, "SELECT * FROM spaces WHERE code=%s", (code,))
        if not sp:
            abort(404)
        if not uno(db, """SELECT 1 FROM members WHERE space_id=%s AND user_id=%s""",
                   (sp["id"], uid)):
            abort(403)

        if request.method == "POST":
            if sp["owner_id"] != uid:
                return jsonify({"ok": False,
                                "errore": "Solo il proprietario può cambiarla."}), 403

            giorni = (request.get_json(silent=True) or {}).get("giorni")
            if giorni is not None:
                try:
                    giorni = int(giorni)
                except (TypeError, ValueError):
                    return jsonify({"ok": False}), 400
                if not 1 <= giorni <= 3650:
                    return jsonify({"ok": False,
                                    "errore": "Da 1 a 3650 giorni."}), 400

            db.execute("UPDATE spaces SET retention_days=%s WHERE id=%s",
                       (giorni, sp["id"]))
            db.commit()

            testo = ("I messaggi non verranno più eliminati automaticamente."
                     if giorni is None else
                     f"I messaggi più vecchi di {giorni} giorni verranno eliminati.")
            sistema(testo, space_id=sp["id"])
            return jsonify({"ok": True, "giorni": giorni})

        # quanti messaggi sparirebbero con l'impostazione attuale
        da_eliminare = 0
        if sp["retention_days"]:
            r = uno(db, """SELECT COUNT(*) AS n FROM messages m
                           JOIN channels c ON c.id=m.channel_id
                           WHERE c.space_id=%s
                             AND m.created_at < now() - (%s || ' days')::interval""",
                    (sp["id"], sp["retention_days"]))
            da_eliminare = r["n"]

    return jsonify({"giorni": sp["retention_days"],
                    "owner": sp["owner_id"] == uid,
                    "da_eliminare": da_eliminare})


# ---------------------------------------------------------
# API: pannello permessi
# ---------------------------------------------------------

def _elenco_permessi(quali):
    return [{"bit": b, "nome": PERM_NAMES[b]} for b in quali]


@app.route("/api/perms/<code>")
def api_perms(code):
    """
    Tutto ciò che serve al pannello: ruoli, canali, override.

    Include anche la posizione dell'utente e i suoi permessi, così il
    client può disabilitare ciò che non può toccare. I controlli veri
    restano comunque lato server.
    """
    uid = utente_corrente()
    if not uid:
        abort(401)

    with get_db() as db:
        sp = uno(db, "SELECT id FROM spaces WHERE code=%s", (code,))
        if not sp or not uno(db, """SELECT 1 FROM members
                                    WHERE space_id=%s AND user_id=%s""",
                             (sp["id"], uid)):
            abort(403)

        mia_pos = posizione(db, uid, sp["id"])
        miei = permessi(db, uid, sp["id"])

        ruoli = [{"id": r["id"], "nome": r["name"], "colore": r["color"],
                  "permessi": r["permissions"], "posizione": r["position"],
                  "default": r["is_default"],
                  # non puoi modificare un ruolo pari o superiore al tuo
                  "modificabile": bool(miei & ADMIN) or r["position"] < mia_pos}
                 for r in tutti(db, """SELECT * FROM roles WHERE space_id=%s
                                       ORDER BY "position" DESC""", (sp["id"],))]

        canali = [{"id": c["id"], "nome": c["name"]}
                  for c in tutti(db, """SELECT id, name FROM channels
                                        WHERE space_id=%s
                                        ORDER BY "position", id""", (sp["id"],))]

        over = {}
        for o in tutti(db, """SELECT o.channel_id, o.role_id, o.allow, o.deny
                              FROM channel_overrides o
                              JOIN channels c ON c.id=o.channel_id
                              WHERE c.space_id=%s""", (sp["id"],)):
            over[f"{o['channel_id']}:{o['role_id']}"] = {"allow": o["allow"],
                                                         "deny": o["deny"]}

    return jsonify({
        "ruoli": ruoli,
        "canali": canali,
        "override": over,
        "permessi_ruolo": _elenco_permessi(sorted(PERM_NAMES)),
        "permessi_canale": _elenco_permessi(PERM_CANALE),
        "miei_permessi": miei,
        "mia_posizione": mia_pos,
        "posso_ruoli": bool(miei & MANAGE_ROLES),
        "posso_canali": bool(miei & MANAGE_CHANNELS),
    })


@app.route("/api/perms/<code>/role", methods=["POST"])
def api_perms_role(code):
    """Cambia i permessi di base di un ruolo."""
    uid = utente_corrente()
    if not uid:
        abort(401)

    d = request.get_json(silent=True) or {}
    try:
        role_id = int(d.get("role_id"))
        valore = int(d.get("permessi"))
    except (TypeError, ValueError):
        return jsonify({"ok": False}), 400

    with get_db() as db:
        sp = uno(db, "SELECT id FROM spaces WHERE code=%s", (code,))
        if not sp:
            abort(404)
        if not puo(db, uid, sp["id"], MANAGE_ROLES):
            return jsonify({"ok": False, "errore": "Non hai i permessi."}), 403

        ruolo = uno(db, "SELECT * FROM roles WHERE id=%s AND space_id=%s",
                    (role_id, sp["id"]))
        if not ruolo:
            abort(404)

        mia_pos = posizione(db, uid, sp["id"])
        miei = permessi(db, uid, sp["id"])
        sono_admin = bool(miei & ADMIN)

        if not sono_admin and ruolo["position"] >= mia_pos:
            return jsonify({"ok": False,
                            "errore": "Non puoi modificare un ruolo pari o "
                                      "superiore al tuo."}), 403

        # Non puoi concedere permessi che tu stesso non hai: altrimenti
        # un mod si crea un ruolo con ADMIN e se lo assegna.
        if not sono_admin and (valore & ~miei):
            return jsonify({"ok": False,
                            "errore": "Non puoi concedere permessi che non "
                                      "possiedi."}), 403

        # Blocco anti-autoesclusione: se il ruolo è uno dei tuoi e la
        # modifica ti toglierebbe la capacità di gestire i permessi,
        # rifiuta. Senza questo si può spegnere ADMIN sul proprio ruolo
        # e restare chiusi fuori dalla propria stanza, senza modo di
        # rientrare se non dal database.
        mio = uno(db, """SELECT 1 FROM member_roles
                         WHERE space_id=%s AND user_id=%s AND role_id=%s""",
                  (sp["id"], uid, role_id))
        if mio:
            restanti = 0
            for r in tutti(db, """SELECT r.permissions AS p FROM member_roles mr
                                  JOIN roles r ON r.id=mr.role_id
                                  WHERE mr.user_id=%s AND mr.space_id=%s
                                    AND mr.role_id<>%s""",
                           (uid, sp["id"], role_id)):
                restanti |= r["p"]
            dopo = restanti | (valore & ALL_PERMS)
            if not (dopo & (ADMIN | MANAGE_ROLES)):
                return jsonify({"ok": False,
                                "errore": "Così perderesti la gestione dei "
                                          "permessi e non potresti più "
                                          "rientrare."}), 400

        db.execute("UPDATE roles SET permissions=%s WHERE id=%s",
                   (valore & ALL_PERMS, role_id))
        db.commit()
        manda_utenti(sp["id"])
        manda_canali(sp["id"])

    return jsonify({"ok": True})


@app.route("/api/perms/<code>/override", methods=["POST"])
def api_perms_override(code):
    """
    Imposta un permesso su un canale per un ruolo.
    stato: 1 = consenti, 0 = eredita, -1 = nega
    """
    uid = utente_corrente()
    if not uid:
        abort(401)

    d = request.get_json(silent=True) or {}
    try:
        channel_id = int(d.get("channel_id"))
        role_id = int(d.get("role_id"))
        bit = int(d.get("bit"))
        stato = int(d.get("stato"))
    except (TypeError, ValueError):
        return jsonify({"ok": False}), 400

    if bit not in PERM_CANALE or stato not in (-1, 0, 1):
        return jsonify({"ok": False}), 400

    with get_db() as db:
        sp = uno(db, "SELECT id FROM spaces WHERE code=%s", (code,))
        if not sp:
            abort(404)
        if not puo(db, uid, sp["id"], MANAGE_CHANNELS):
            return jsonify({"ok": False, "errore": "Non hai i permessi."}), 403

        ch = uno(db, "SELECT id FROM channels WHERE id=%s AND space_id=%s",
                 (channel_id, sp["id"]))
        ruolo = uno(db, "SELECT * FROM roles WHERE id=%s AND space_id=%s",
                    (role_id, sp["id"]))
        if not ch or not ruolo:
            abort(404)

        miei = permessi(db, uid, sp["id"])
        if not (miei & ADMIN) and ruolo["position"] >= posizione(db, uid, sp["id"]):
            return jsonify({"ok": False,
                            "errore": "Non puoi modificare un ruolo pari o "
                                      "superiore al tuo."}), 403

        r = uno(db, """SELECT allow, deny FROM channel_overrides
                       WHERE channel_id=%s AND role_id=%s""", (channel_id, role_id))
        allow, deny = (r["allow"], r["deny"]) if r else (0, 0)

        # il vincolo no_overlap impone che un bit non sia in entrambe
        allow &= ~bit
        deny &= ~bit
        if stato == 1:
            allow |= bit
        elif stato == -1:
            deny |= bit

        if allow == 0 and deny == 0:
            db.execute("""DELETE FROM channel_overrides
                          WHERE channel_id=%s AND role_id=%s""",
                       (channel_id, role_id))
        else:
            db.execute("""INSERT INTO channel_overrides
                            (channel_id, role_id, allow, deny)
                          VALUES (%s,%s,%s,%s)
                          ON CONFLICT (channel_id, role_id) DO UPDATE
                            SET allow=EXCLUDED.allow, deny=EXCLUDED.deny""",
                       (channel_id, role_id, allow, deny))
        db.commit()
        manda_canali(sp["id"])

    return jsonify({"ok": True, "allow": allow, "deny": deny})



# =========================================================
# SOCKET
# =========================================================

def utenti_stanza(db, space_id):
    connessi = set(online.get(space_id, {}).values())
    righe = tutti(db, f"""
        SELECT u.id, u.username,
               (SELECT r.color FROM member_roles mr JOIN roles r ON r.id=mr.role_id
                WHERE mr.user_id=u.id AND mr.space_id=%s
                ORDER BY r."position" DESC LIMIT 1) AS color,
               (SELECT r.name FROM member_roles mr JOIN roles r ON r.id=mr.role_id
                WHERE mr.user_id=u.id AND mr.space_id=%s
                ORDER BY r."position" DESC LIMIT 1) AS role,
               (SELECT {sel_icon()} FROM member_roles mr JOIN roles r ON r.id=mr.role_id
                WHERE mr.user_id=u.id AND mr.space_id=%s
                ORDER BY r."position" DESC LIMIT 1) AS role_icon
        FROM members m JOIN users u ON u.id=m.user_id
        WHERE m.space_id=%s
    """, (space_id, space_id, space_id, space_id))

    out = [{"id": r["id"], "username": r["username"],
            "color": r["color"] or "#cccccc", "role": r["role"] or "user",
            "icon": url_icona(r["role"], r["role_icon"]),
            "online": r["id"] in connessi} for r in righe]
    out.sort(key=lambda u: (not u["online"], u["username"].lower()))
    return out


def manda_utenti(space_id):
    with get_db() as db:
        dati = utenti_stanza(db, space_id)
    for sid in list(online.get(space_id, {})):
        socketio.emit("update_users", dati, room=sid)


def manda_canali(space_id, solo_sid=None):
    """Per utente: la visibilità dei canali dipende dai ruoli."""
    bersagli = ([(solo_sid, online.get(space_id, {}).get(solo_sid))]
                if solo_sid else list(online.get(space_id, {}).items()))
    with get_db() as db:
        for sid, uid in bersagli:
            if uid:
                socketio.emit("update_channels",
                              canali_visibili(db, uid, space_id), room=sid)


# Chi si è disconnesso di recente: (space_id, user_id) -> epoch.
# Serve a non annunciare "è entrato / ha lasciato la stanza" a ogni
# sfarfallio di rete, che su mobile succede di continuo.
uscite = {}
GRAZIA = 20   # secondi


def rientro_recente(space_id, uid):
    t = uscite.get((space_id, uid))
    return t is not None and (time.time() - t) < GRAZIA


def annuncia_uscita(space_id, uid):
    """Chiamata dopo la grazia: se nel frattempo è rientrato, tace."""
    socketio.sleep(GRAZIA)
    if uid in online.get(space_id, {}).values():
        return
    if not rientro_recente(space_id, uid):
        return          # già annunciato o rientrato e riuscito
    uscite.pop((space_id, uid), None)
    with get_db() as db:
        u = uno(db, "SELECT username FROM users WHERE id=%s", (uid,))
    if u:
        sistema(f"{u['username']} ha lasciato la stanza.", space_id=space_id)
    manda_utenti(space_id)


def sistema(msg, sid=None, space_id=None):
    dati = {"type": "system", "msg": msg}
    if sid:
        socketio.emit("message", dati, room=sid)
    elif space_id:
        for s in list(online.get(space_id, {})):
            socketio.emit("message", dati, room=s)


@socketio.on("join")
def on_join(data):
    uid = session.get("user_id")
    if not uid:
        emit("message", {"type": "system", "msg": "Sessione scaduta, ricarica."})
        return {"ok": False, "err": "sessione"}

    sid = request.sid
    with get_db() as db:
        sp = uno(db, "SELECT * FROM spaces WHERE code=%s", (str(data.get("code")),))
        if not sp:
            return {"ok": False, "err": "stanza"}

        space_id = sp["id"]
        if ban_attivo(db, space_id, uid):
            emit("message", {"type": "banned", "msg": "Sei bannato da questa stanza."})
            return {"ok": False, "err": "ban"}

        entra_space(db, space_id, uid)

        online.setdefault(space_id, {})[sid] = uid
        sid_space[sid] = space_id
        join_room(space_id)

        # dopo una riconnessione il client rimanda il canale che stava
        # guardando: senza questo tornerebbe sempre sul primo.
        ch = None
        voluto = data.get("channel_id")
        if voluto:
            ch = uno(db, """SELECT id, name FROM channels
                            WHERE id=%s AND space_id=%s""", (voluto, space_id))
        if not ch:
            ch = uno(db, """SELECT id, name FROM channels WHERE space_id=%s
                            ORDER BY "position", id LIMIT 1""", (space_id,))
        sid_channel[sid] = ch["id"]
        segna_letto(db, uid, ch["id"])

    username = session.get("username")
    # Annuncio l'ingresso solo se è un ingresso vero: non se ha un'altra
    # scheda aperta e non se si è appena riconnesso dopo un buco di rete.
    altri = [s for s, u in online[space_id].items() if u == uid and s != sid]
    if not altri and not rientro_recente(space_id, uid):
        sistema(f"{username}{random.choice(WELCOMES)}", space_id=space_id)
    uscite.pop((space_id, uid), None)

    emit("set_channel", {"channel_id": ch["id"], "channel": ch["name"]}, room=sid)
    manda_canali(space_id, solo_sid=sid)
    manda_utenti(space_id)
    # il client aspetta questo per svuotare la coda dei messaggi non inviati
    return {"ok": True, "channel_id": ch["id"], "channel": ch["name"]}


@socketio.on("switch_channel")
def on_switch(data):
    uid = session.get("user_id")
    sid = request.sid
    space_id = sid_space.get(sid)
    if not uid or not space_id:
        return {"ok": False, "err": "nojoin"}

    with get_db() as db:
        ch = uno(db, "SELECT * FROM channels WHERE id=%s AND space_id=%s",
                 (data.get("channel_id"), space_id))
        if not ch:
            sistema("Canale non trovato.", sid=sid)
            return {"ok": False, "err": "canale"}

        if sid_channel.get(sid):
            segna_letto(db, uid, sid_channel[sid])
        sid_channel[sid] = ch["id"]
        segna_letto(db, uid, ch["id"])

    emit("set_channel", {"channel_id": ch["id"], "channel": ch["name"]}, room=sid)
    manda_canali(space_id, solo_sid=sid)
    return {"ok": True, "channel_id": ch["id"]}


@socketio.on("message")
def on_message(data):
    uid = session.get("user_id")
    sid = request.sid
    space_id = sid_space.get(sid)
    tmp = data.get("tmp")

    # Questo è il caso che faceva "sparire" i messaggi: il socket si era
    # riconnesso con un sid nuovo e il server non sapeva più dove fosse.
    # Prima si usciva in silenzio, ora il client lo sa e rifà il join.
    if not uid or not space_id:
        return {"ok": False, "err": "nojoin", "tmp": tmp}

    msg = (data.get("msg") or "").strip()
    if not msg:
        return {"ok": False, "err": "vuoto", "tmp": tmp}
    if len(msg) > 2000:
        sistema("Messaggio troppo lungo (max 2000).", sid=sid)
        return {"ok": False, "err": "lungo", "tmp": tmp}

    if msg.startswith("/"):
        comando(uid, sid, space_id, msg)
        return {"ok": True, "comando": True, "tmp": tmp}

    channel_id = sid_channel.get(sid)

    with get_db() as db:
        if not puo(db, uid, space_id, SEND_MESSAGES, channel_id):
            sistema("Non puoi scrivere in questo canale.", sid=sid)
            return {"ok": False, "err": "permessi", "tmp": tmp}

        # --- menzioni: risolte prima dell'insert, per salvare i flag
        puo_ev = puo(db, uid, space_id, MENTION_EVERYONE, channel_id)
        m_utenti, m_ruoli, m_everyone, m_here = menzioni.risolvi(
            db, space_id, msg, uid, puo_ev)

        riga = uno(db, """INSERT INTO messages
                            (channel_id, author_id, content,
                             mentions_everyone, mentions_here)
                          VALUES (%s,%s,%s,%s,%s)
                          RETURNING id, created_at""",
                   (channel_id, uid, msg, m_everyone, m_here))
        menzioni.salva(db, riga["id"], m_utenti, m_ruoli)

        c = uno(db, f"""SELECT r.color, r.name, {sel_icon()} AS icon
                       FROM member_roles mr
                       JOIN roles r ON r.id=mr.role_id
                       WHERE mr.user_id=%s AND mr.space_id=%s
                       ORDER BY r."position" DESC LIMIT 1""", (uid, space_id))
        colore = c["color"] if c else "#cccccc"
        ruolo = c["name"] if c else ""
        icona = url_icona(ruolo, c["icon"] if c else None)
        segna_letto(db, uid, channel_id)

        connessi = set(online.get(space_id, {}).values())
        da_notificare = menzioni.destinatari(
            db, space_id, channel_id, m_utenti, m_ruoli,
            m_everyone, m_here, uid, connessi)

        payload = {"type": "chat", "id": riga["id"], "username": session.get("username"),
                   "msg": msg, "color": colore, "role": ruolo, "icon": icona,
                   "channel_id": channel_id,
                   "ts": ts(riga["created_at"])}

        # chi guarda il canale riceve il messaggio, gli altri solo il badge
        visto_da = set()
        for s, u in list(online.get(space_id, {}).items()):
            if sid_channel.get(s) == channel_id:
                extra = {"tmp": tmp} if s == sid else {}
                socketio.emit("message",
                              {**payload, "own": u == uid,
                               "mention": u in da_notificare, **extra}, room=s)
                if u != uid:
                    segna_letto(db, u, channel_id)
                    visto_da.add(u)   # ha il canale aperto: niente push
            else:
                socketio.emit("update_channels",
                              canali_visibili(db, u, space_id), room=s)

        db.commit()
        notifica_push(db, space_id, channel_id, uid, msg,
                      da_notificare, visto_da)

    return {"ok": True, "id": riga["id"], "tmp": tmp}


def notifica_push(db, space_id, channel_id, autore_id, testo,
                  menzionati, gia_visto):
    """
    Decide chi merita una notifica e la manda.

    Regole:
      - chi ha il canale aperto adesso non riceve nulla (l'ha già letto)
      - i menzionati ricevono se il livello e' MENZIONI o TUTTI
      - gli altri membri solo se hanno chiesto TUTTI
    """
    if not menzioni.push_attive:
        return

    sp = uno(db, "SELECT name, code FROM spaces WHERE id=%s", (space_id,))
    ch = uno(db, "SELECT name FROM channels WHERE id=%s", (channel_id,))
    au = uno(db, "SELECT username FROM users WHERE id=%s", (autore_id,))
    if not (sp and ch and au):
        return

    url = f"/chat/{sp['code']}"
    anteprima = testo if len(testo) <= 120 else testo[:117] + "..."

    candidati = set(menzionati)
    for r in db.execute("SELECT user_id FROM members WHERE space_id=%s",
                        (space_id,)).fetchall():
        candidati.add(r["user_id"])
    candidati.discard(autore_id)
    candidati -= gia_visto

    for u in candidati:
        lv = menzioni.livello(db, u, space_id, channel_id)
        if lv == menzioni.NIENTE:
            continue
        e_menzione = u in menzionati
        if lv == menzioni.MENZIONI and not e_menzione:
            continue
        if not puo(db, u, space_id, SEND_MESSAGES, channel_id) \
                and not puo(db, u, space_id, ADMIN):
            continue   # non vede il canale, non ha senso notificarlo

        titolo = (f"{au['username']} ti ha menzionato in #{ch['name']}"
                  if e_menzione else f"{au['username']} in #{ch['name']}")
        menzioni.invia(db, u, titolo, anteprima, url,
                       tag=f"flachat-{channel_id}")


# ---------------------------------------------------------
# COMANDI
# ---------------------------------------------------------

def comando(uid, sid, space_id, msg):
    try:
        parti = shlex.split(msg)
    except ValueError:
        return sistema("Virgolette non chiuse.", sid=sid)
    cmd = parti[0].lower()

    with get_db() as db:

        # ---- canali ----
        if cmd == "/newchannel":
            if not puo(db, uid, space_id, MANAGE_CHANNELS):
                return sistema("Non hai i permessi per creare canali.", sid=sid)
            if len(parti) < 2:
                return sistema("Uso: /newchannel <nome>", sid=sid)
            nome = parti[1].strip().lower().replace(" ", "-")[:32]
            try:
                db.execute("""INSERT INTO channels (space_id, name, "position")
                    VALUES (%s,%s,(SELECT COALESCE(MAX("position"),0)+1
                                   FROM channels WHERE space_id=%s))""",
                    (space_id, nome, space_id))
            except psycopg.errors.UniqueViolation:
                return sistema("Canale già esistente.", sid=sid)
            sistema(f"Canale #{nome} creato.", space_id=space_id)
            # commit prima di trasmettere: manda_canali/manda_utenti
            # usano un'altra connessione del pool e non vedrebbero
            # le scritture ancora aperte in questa transazione
            db.commit()
            return manda_canali(space_id)

        if cmd == "/delchannel":
            if not puo(db, uid, space_id, MANAGE_CHANNELS):
                return sistema("Non hai i permessi.", sid=sid)
            if len(parti) < 2:
                return sistema("Uso: /delchannel <nome>", sid=sid)
            nome = parti[1].strip().lower()
            ch = uno(db, "SELECT * FROM channels WHERE space_id=%s AND name=%s",
                     (space_id, nome))
            if not ch:
                return sistema("Canale non trovato.", sid=sid)
            n = uno(db, "SELECT COUNT(*) AS n FROM channels WHERE space_id=%s",
                    (space_id,))["n"]
            if n <= 1:
                return sistema("Non puoi eliminare l'ultimo canale.", sid=sid)

            primo = uno(db, """SELECT id, name FROM channels
                               WHERE space_id=%s AND id<>%s
                               ORDER BY "position", id LIMIT 1""",
                        (space_id, ch["id"]))
            db.execute("DELETE FROM channels WHERE id=%s", (ch["id"],))

            for s in list(online.get(space_id, {})):
                if sid_channel.get(s) == ch["id"]:
                    sid_channel[s] = primo["id"]
                    socketio.emit("set_channel",
                                  {"channel_id": primo["id"], "channel": primo["name"]},
                                  room=s)
            sistema(f"Canale #{nome} eliminato (con tutti i suoi messaggi).",
                    space_id=space_id)
            # commit prima di trasmettere: manda_canali/manda_utenti
            # usano un'altra connessione del pool e non vedrebbero
            # le scritture ancora aperte in questa transazione
            db.commit()
            return manda_canali(space_id)

        # ---- ruoli ----
        if cmd == "/role":
            if not puo(db, uid, space_id, MANAGE_ROLES):
                return sistema("Non hai i permessi per assegnare ruoli.", sid=sid)
            if len(parti) < 3:
                return sistema("Uso: /role <utente> <ruolo>", sid=sid)
            target = uno(db, """SELECT u.id, u.username FROM users u
                                JOIN members m ON m.user_id=u.id
                                WHERE m.space_id=%s AND lower(u.username)=%s""",
                         (space_id, parti[1].strip().lower()))
            if not target:
                return sistema("Utente non presente in questa stanza.", sid=sid)
            ruolo = uno(db, "SELECT * FROM roles WHERE space_id=%s AND name=%s",
                        (space_id, parti[2].strip().lower()))
            if not ruolo:
                return sistema("Ruolo inesistente.", sid=sid)
            if not puo_agire_su(db, uid, target["id"], space_id):
                return sistema("Non puoi modificare qualcuno di pari o superiore livello.",
                               sid=sid)
            if ruolo["position"] >= posizione(db, uid, space_id):
                return sistema("Non puoi assegnare un ruolo pari o superiore al tuo.",
                               sid=sid)

            db.execute("DELETE FROM member_roles WHERE space_id=%s AND user_id=%s",
                       (space_id, target["id"]))
            db.execute("""INSERT INTO member_roles (space_id, user_id, role_id)
                          VALUES (%s,%s,%s)""",
                       (space_id, target["id"], ruolo["id"]))
            sistema(f"{target['username']} ora è {ruolo['name']}.", space_id=space_id)
            # commit prima di trasmettere: manda_canali/manda_utenti
            # usano un'altra connessione del pool e non vedrebbero
            # le scritture ancora aperte in questa transazione
            db.commit()
            manda_utenti(space_id)
            return manda_canali(space_id)

        if cmd == "/addrole":
            if not puo(db, uid, space_id, MANAGE_ROLES):
                return sistema("Non hai i permessi per assegnare ruoli.", sid=sid)
            if len(parti) < 3:
                return sistema("Uso: /addrole <utente> <ruolo>",sid=sid)

            target = uno(db, """SELECT u.id, u.username FROM users u
                                JOIN members m ON m.user_id=u.id
                                WHERE m.space_id=%s AND lower(u.username)=%s""",
                         (space_id, parti[1].strip().lower()))

            if not target:
                return sistema("L'utente non è presente nella stanza.", sid=sid)

            ruolo = uno(db, "SELECT * FROM roles WHERE space_id=%s AND name=%s",
                        (space_id, parti[2].strip().lower()))

            if not ruolo:
                return sistema("Il ruolo non esiste nella stanza.", sid=sid)

            if not puo_agire_su(db, uid, target["id"], space_id):
                return sistema("Non puoi modificare qualcuno pari o superiore di livello.", sid=sid)

            if ruolo["position"] >= posizione(db, uid, space_id):
                return sistema("Non puoi assegnare un ruolo pari o superiore al tuo.", sid=sid)

            gia = uno(db, """SELECT 1 FROM member_roles
                             WHERE space_id=%s AND user_id=%s AND role_id=%s""",
                      (space_id, target["id"], ruolo["id"]))
            if gia:
                return sistema(f"{target['username']} ha già il ruolo "
                               f"{ruolo['name']}.", sid=sid)

            db.execute("""INSERT INTO member_roles (space_id, user_id, role_id)
                          VALUES (%s, %s, %s)""",
                       (space_id, target["id"], ruolo["id"]))

            silenzioso = "silent" in [p.lower() for p in parti[3:]]
            testo = f"{target['username']} ha anche il ruolo {ruolo['name']}."
            if silenzioso:
                sistema(testo + " (silent)", sid=sid)
            else:
                sistema(testo, space_id=space_id)

            db.commit()
            manda_utenti(space_id)
            return manda_canali(space_id)

        if cmd == "/removerole":
            if not puo(db, uid, space_id, MANAGE_ROLES):
                return sistema("Non hai i permessi per assegnare ruoli.", sid=sid)
            if len(parti) < 3:
                return sistema("Uso: /removerole <utente> <ruolo>", sid=sid)

            target = uno(db, """SELECT u.id, u.username FROM users u
                                JOIN members m ON m.user_id=u.id
                                WHERE m.space_id=%s AND lower(u.username)=%s""",
                         (space_id, parti[1].strip().lower()))

            if not target:
                return sistema("L'utente non è presente nella stanza.", sid=sid)

            ruolo = uno(db, "SELECT * FROM roles WHERE space_id=%s AND name=%s",
                        (space_id, parti[2].strip().lower()))

            if not ruolo:
                return sistema("Il ruolo non esiste nella stanza.", sid=sid)

            if not puo_agire_su(db, uid, target["id"], space_id):
                return sistema("Non puoi modificare qualcuno pari o superiore di livello.", sid=sid)

            if ruolo["position"] >= posizione(db, uid, space_id):
                return sistema("Non puoi togliere un ruolo pari o superiore al tuo.", sid=sid)

            gia = uno(db, """SELECT 1 FROM member_roles
                             WHERE space_id=%s AND user_id=%s AND role_id=%s""",
                      (space_id, target["id"], ruolo["id"]))
            if not gia:
                return sistema(f"{target['username']} non ha il ruolo "
                               f"{ruolo['name']}.", sid=sid)

            # Senza questo si può lasciare qualcuno a zero ruoli: da lì
            # posizione() torna -1 e permessi() torna 0, quindi non può
            # più nemmeno scrivere e non se ne accorge nessuno.
            n = uno(db, """SELECT COUNT(*) AS n FROM member_roles
                           WHERE space_id=%s AND user_id=%s""",
                    (space_id, target["id"]))["n"]
            if n <= 1:
                return sistema(f"{ruolo['name']} è l'unico ruolo di "
                               f"{target['username']}. Prima assegnagliene "
                               "un altro.", sid=sid)

            db.execute("""DELETE FROM member_roles
                          WHERE space_id=%s AND user_id=%s AND role_id=%s""",
                       (space_id, target["id"], ruolo["id"]))

            silenzioso = "silent" in [p.lower() for p in parti[3:]]
            testo = f"{target['username']} non ha più il ruolo {ruolo['name']}."
            if silenzioso:
                sistema(testo + " (silent)", sid=sid)
            else:
                sistema(testo, space_id=space_id)

            db.commit()
            manda_utenti(space_id)
            return manda_canali(space_id)

        if cmd == "/newrole":
            if not puo(db, uid, space_id, MANAGE_ROLES):
                return sistema("Non hai i permessi.", sid=sid)
            if len(parti) < 2:
                return sistema("Uso: /newrole <nome>", sid=sid)
            nome = parti[1].strip().lower()[:32]
            if uno(db, "SELECT 1 FROM roles WHERE space_id=%s AND name=%s",
                   (space_id, nome)):
                return sistema("Ruolo già esistente.", sid=sid)
            return socketio.emit("open_role_creator",
                                 {"role_name": nome, "modifica": False,
                                  "color": "#cccccc", "icon": None,
                                  "migrazione": HA_COLONNA_ICON,
                                  "icone": icone_disponibili()}, room=sid)

        if cmd == "/changerole":
            if not puo(db, uid, space_id, MANAGE_ROLES):
                return sistema("Non hai i permessi.", sid=sid)
            if len(parti) < 2:
                return sistema("Uso: /changerole <nome>", sid=sid)
            nome = parti[1].strip().lower()[:32]
            r = uno(db, "SELECT * FROM roles WHERE space_id=%s AND name=%s",
                    (space_id, nome))
            if not r:
                return sistema("Ruolo non trovato. Per crearlo: "
                               f"/newrole {nome}", sid=sid)
            # stessa regola di /delrole: non si tocca chi sta al tuo
            # livello o sopra, altrimenti un mod ridipinge il ruolo owner
            if r["position"] >= posizione(db, uid, space_id):
                return sistema("Non puoi modificare un ruolo pari o superiore al tuo.",
                               sid=sid)
            if not HA_COLONNA_ICON:
                sistema("Nota: manca la colonna roles.icon (esegui "
                        "migrazione_icone.sql). Il colore si salva, "
                        "l'icona scelta no.", sid=sid)
            return socketio.emit("open_role_creator",
                                 {"role_name": nome, "modifica": True,
                                  "color": r["color"], "icon": r.get("icon"),
                                  "migrazione": HA_COLONNA_ICON,
                                  "icone": icone_disponibili()}, room=sid)

        if cmd == "/delrole":
            if not puo(db, uid, space_id, MANAGE_ROLES):
                return sistema("Non hai i permessi.", sid=sid)
            if len(parti) < 2:
                return sistema("Uso: /delrole <nome>", sid=sid)
            nome = parti[1].strip().lower()
            if nome in ("owner", "user"):
                return sistema("Non puoi eliminare i ruoli owner e user.", sid=sid)
            r = uno(db, "SELECT * FROM roles WHERE space_id=%s AND name=%s",
                    (space_id, nome))
            if not r:
                return sistema("Ruolo non trovato.", sid=sid)
            if r["position"] >= posizione(db, uid, space_id):
                return sistema("Non puoi eliminare un ruolo pari o superiore al tuo.",
                               sid=sid)

            orfani = [x["user_id"] for x in
                      tutti(db, "SELECT user_id FROM member_roles WHERE role_id=%s",
                            (r["id"],))]
            db.execute("DELETE FROM roles WHERE id=%s", (r["id"],))
            dflt = uno(db, "SELECT id FROM roles WHERE space_id=%s AND is_default",
                       (space_id,))
            for o in orfani:
                if dflt and not uno(db, """SELECT 1 FROM member_roles
                                           WHERE space_id=%s AND user_id=%s""",
                                    (space_id, o)):
                    db.execute("""INSERT INTO member_roles (space_id, user_id, role_id)
                                  VALUES (%s,%s,%s)""", (space_id, o, dflt["id"]))
            sistema(f"Ruolo '{nome}' eliminato.", space_id=space_id)
            # commit prima di trasmettere: manda_canali/manda_utenti
            # usano un'altra connessione del pool e non vedrebbero
            # le scritture ancora aperte in questa transazione
            db.commit()
            manda_utenti(space_id)
            return manda_canali(space_id)

        # ---- moderazione ----
        if cmd in ("/kick", "/ban"):
            perm = KICK if cmd == "/kick" else BAN
            if not puo(db, uid, space_id, perm):
                return sistema("Non hai i permessi.", sid=sid)
            if len(parti) < 2:
                return sistema(f"Uso: {cmd} <utente>" +
                               (" <secondi>" if cmd == "/ban" else ""), sid=sid)
            target = uno(db, """SELECT u.id, u.username FROM users u
                                JOIN members m ON m.user_id=u.id
                                WHERE m.space_id=%s AND lower(u.username)=%s""",
                         (space_id, parti[1].strip().lower()))
            if not target:
                return sistema("Utente non presente in questa stanza.", sid=sid)
            if not puo_agire_su(db, uid, target["id"], space_id):
                return sistema("Non puoi agire su qualcuno di pari o superiore livello.",
                               sid=sid)

            durata = None
            if cmd == "/ban" and len(parti) > 2:
                try:
                    durata = int(parti[2])
                except ValueError:
                    return sistema("I secondi devono essere un numero.", sid=sid)

            if cmd == "/ban":
                scadenza = ora() + timedelta(seconds=durata) if durata else None
                db.execute("""INSERT INTO bans (space_id, user_id, banned_by, expire)
                              VALUES (%s,%s,%s,%s)
                              ON CONFLICT (space_id, user_id) DO UPDATE
                                SET expire = EXCLUDED.expire,
                                    banned_by = EXCLUDED.banned_by""",
                           (space_id, target["id"], uid, scadenza))
            db.execute("DELETE FROM members WHERE space_id=%s AND user_id=%s",
                       (space_id, target["id"]))

            for s, u in list(online.get(space_id, {}).items()):
                if u == target["id"]:
                    socketio.emit("message",
                                  {"type": "banned" if cmd == "/ban" else "kicked",
                                   "msg": "Sei stato allontanato dalla stanza."}, room=s)
                    online[space_id].pop(s, None)
                    sid_channel.pop(s, None)
                    sid_space.pop(s, None)

            testo = (f"{target['username']} è stato cacciato." if cmd == "/kick"
                     else f"{target['username']} è stato bannato" +
                          (f" per {durata}s." if durata else " permanentemente."))
            sistema(testo, space_id=space_id)
            # commit prima di trasmettere: manda_canali/manda_utenti
            # usano un'altra connessione del pool e non vedrebbero
            # le scritture ancora aperte in questa transazione
            db.commit()
            return manda_utenti(space_id)

        if cmd == "/unban":
            if not puo(db, uid, space_id, BAN):
                return sistema("Non hai i permessi.", sid=sid)
            if len(parti) < 2:
                return sistema("Uso: /unban <utente>", sid=sid)
            u = uno(db, "SELECT id, username FROM users WHERE lower(username)=%s",
                    (parti[1].strip().lower(),))
            if not u:
                return sistema("Utente inesistente.", sid=sid)
            db.execute("DELETE FROM bans WHERE space_id=%s AND user_id=%s",
                       (space_id, u["id"]))
            return sistema(f"{u['username']} è stato sbannato.", space_id=space_id)

        if cmd == "/retention":
            sp = uno(db, "SELECT owner_id, code FROM spaces WHERE id=%s", (space_id,))
            if sp["owner_id"] != uid:
                return sistema("Solo il proprietario può cambiare la scadenza.",
                               sid=sid)
            if len(parti) < 2:
                r = uno(db, "SELECT retention_days AS d FROM spaces WHERE id=%s",
                        (space_id,))
                attuale = (f"{r['d']} giorni" if r["d"] else "mai")
                return sistema(f"Scadenza messaggi: {attuale}. "
                               "Uso: /retention <giorni|mai>", sid=sid)

            arg = parti[1].strip().lower()
            if arg in ("mai", "no", "off", "0"):
                giorni = None
            else:
                try:
                    giorni = int(arg)
                except ValueError:
                    return sistema("Uso: /retention <giorni|mai>", sid=sid)
                if not 1 <= giorni <= 3650:
                    return sistema("Il valore deve essere fra 1 e 3650 giorni.",
                                   sid=sid)

            db.execute("UPDATE spaces SET retention_days=%s WHERE id=%s",
                       (giorni, space_id))
            db.commit()
            return sistema(
                "I messaggi non verranno più eliminati automaticamente."
                if giorni is None else
                f"I messaggi più vecchi di {giorni} giorni verranno eliminati.",
                space_id=space_id)

        if cmd == "/perms":
            if not (puo(db, uid, space_id, MANAGE_ROLES)
                    or puo(db, uid, space_id, MANAGE_CHANNELS)):
                return sistema("Non hai i permessi per gestire ruoli o canali.",
                               sid=sid)
            return socketio.emit("open_perms", {}, room=sid)

        if cmd == "/help":
            return sistema("Comandi: /newchannel /delchannel /role /newrole "
                           "/changerole /delrole /perms /kick /ban /unban "
                           "/retention /help", sid=sid)

        return sistema(f"Comando sconosciuto: {cmd}", sid=sid)


@socketio.on("create_role")
def on_create_role(data):
    """Crea un ruolo, oppure ne aggiorna colore e icona (/changerole)."""
    uid = session.get("user_id")
    sid = request.sid
    space_id = sid_space.get(sid)
    if not uid or not space_id:
        return

    nome = (data.get("role_name") or "").strip().lower()[:32]
    colore = data.get("color") or "#cccccc"
    if not nome:
        return

    # None = decidi tu (file omonimo), "" = nessuna icona, "x.svg" = quella
    icona = data.get("icon")
    if icona is not None and not (icona or "").startswith(f"{SUPABASE_URL}/"):
        # un nome di file arriva dal client: vale solo se esiste davvero
        validi = {i["file"] for i in icone_disponibili()}
        icona = icona if icona in validi else ""

    with get_db() as db:
        if not puo(db, uid, space_id, MANAGE_ROLES):
            return

        esistente = uno(db, "SELECT * FROM roles WHERE space_id=%s AND name=%s",
                        (space_id, nome))

        if data.get("modifica"):
            if not esistente:
                return sistema("Ruolo non trovato.", sid=sid)
            # ricontrollato qui: fra l'apertura del pannello e il salvataggio
            # il ruolo di chi salva puo' essere cambiato
            if esistente["position"] >= posizione(db, uid, space_id):
                return sistema("Non puoi modificare un ruolo pari o superiore al tuo.",
                               sid=sid)
            if HA_COLONNA_ICON:
                db.execute("UPDATE roles SET color=%s, icon=%s WHERE id=%s",
                           (colore, icona, esistente["id"]))
            else:
                db.execute("UPDATE roles SET color=%s WHERE id=%s",
                           (colore, esistente["id"]))
                if icona:
                    sistema("Icona NON salvata: manca la colonna roles.icon. "
                            "Esegui migrazione_icone.sql e riprova.", sid=sid)
            testo = f"Ruolo '{nome}' aggiornato."
        else:
            if esistente:
                return sistema("Ruolo già esistente.", sid=sid)
            try:
                if HA_COLONNA_ICON:
                    db.execute("""INSERT INTO roles
                                    (space_id, name, color, permissions,
                                     "position", icon)
                                  VALUES (%s,%s,%s,%s,1,%s)""",
                               (space_id, nome, colore, SEND_MESSAGES, icona))
                else:
                    db.execute("""INSERT INTO roles
                                    (space_id, name, color, permissions, "position")
                                  VALUES (%s,%s,%s,%s,1)""",
                               (space_id, nome, colore, SEND_MESSAGES))
            except psycopg.errors.UniqueViolation:
                return sistema("Ruolo già esistente.", sid=sid)
            testo = f"Ruolo '{nome}' creato."

        # commit prima di trasmettere: manda_utenti usa un'altra
        # connessione del pool e non vedrebbe le scritture ancora aperte
        db.commit()

    sistema(testo, space_id=space_id)
    manda_utenti(space_id)


@socketio.on("typing")
def on_typing(data):
    sid = request.sid
    space_id = sid_space.get(sid)
    ch = sid_channel.get(sid)
    if not space_id:
        return
    for s in list(online.get(space_id, {})):
        if s != sid and sid_channel.get(s) == ch:
            socketio.emit("typing", {"username": session.get("username"),
                                     "typing": data.get("typing", False)}, room=s)


@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    space_id = sid_space.pop(sid, None)
    ch = sid_channel.pop(sid, None)
    if not space_id:
        return

    uid = online.get(space_id, {}).pop(sid, None)

    if uid:
        with get_db() as db:
            if ch:
                segna_letto(db, uid, ch)
        # L'annuncio d'uscita parte solo se non ha altre schede aperte, e
        # comunque dopo la grazia: chi si riconnette entro pochi secondi
        # non fa comparire niente in chat.
        if uid not in online.get(space_id, {}).values():
            uscite[(space_id, uid)] = time.time()
            socketio.start_background_task(annuncia_uscita, space_id, uid)

    manda_utenti(space_id)
    if not online.get(space_id):
        online.pop(space_id, None)


if __name__ == "__main__":
    socketio.run(app, debug=True, allow_unsafe_werkzeug=True,
                 host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
