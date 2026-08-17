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

import os
import random
import string

import psycopg
from psycopg.rows import dict_row

import menzioni
from psycopg_pool import ConnectionPool

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
pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=10,
                      kwargs={"row_factory": dict_row}, open=True)


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

RUOLI_DEFAULT = [
    ("owner", "#f0b232", ADMIN, 100, False),
    ("admin", "#e04b4b", SEND_MESSAGES | MANAGE_CHANNELS | KICK | BAN
                         | MANAGE_MESSAGES | MENTION_EVERYONE, 80, False),
    ("mod",   "#5b8dd9", SEND_MESSAGES | KICK | MANAGE_MESSAGES, 50, False),
    ("user",  "#cccccc", SEND_MESSAGES | MENTION_EVERYONE, 0, True),
]

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

    return render_template("lobby.html", username=session.get("username"),
                           stanze=stanze, error=error)


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

        sql = """SELECT m.id, m.content, m.created_at, m.edited_at,
                        u.id AS uid, u.username,
                        (SELECT r.color FROM member_roles mr
                         JOIN roles r ON r.id = mr.role_id
                         WHERE mr.user_id = u.id AND mr.space_id = %s
                         ORDER BY r."position" DESC LIMIT 1) AS color
                 FROM messages m
                 LEFT JOIN users u ON u.id = m.author_id
                 WHERE m.channel_id = %s AND m.deleted_at IS NULL"""
        par = [ch["space_id"], channel_id]
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
        "ts": ts(r["created_at"]),
        "own": r["uid"] == uid,
    } for r in reversed(righe)])


# ---------------------------------------------------------
# API: menzioni e notifiche
# ---------------------------------------------------------

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


# =========================================================
# SOCKET
# =========================================================

def utenti_stanza(db, space_id):
    connessi = set(online.get(space_id, {}).values())
    righe = tutti(db, """
        SELECT u.id, u.username,
               (SELECT r.color FROM member_roles mr JOIN roles r ON r.id=mr.role_id
                WHERE mr.user_id=u.id AND mr.space_id=%s
                ORDER BY r."position" DESC LIMIT 1) AS color,
               (SELECT r.name FROM member_roles mr JOIN roles r ON r.id=mr.role_id
                WHERE mr.user_id=u.id AND mr.space_id=%s
                ORDER BY r."position" DESC LIMIT 1) AS role
        FROM members m JOIN users u ON u.id=m.user_id
        WHERE m.space_id=%s
    """, (space_id, space_id, space_id))

    out = [{"id": r["id"], "username": r["username"],
            "color": r["color"] or "#cccccc", "role": r["role"] or "user",
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
        return

    sid = request.sid
    with get_db() as db:
        sp = uno(db, "SELECT * FROM spaces WHERE code=%s", (str(data.get("code")),))
        if not sp:
            return

        space_id = sp["id"]
        if ban_attivo(db, space_id, uid):
            emit("message", {"type": "banned", "msg": "Sei bannato da questa stanza."})
            return

        entra_space(db, space_id, uid)

        online.setdefault(space_id, {})[sid] = uid
        sid_space[sid] = space_id
        join_room(space_id)

        ch = uno(db, """SELECT id, name FROM channels WHERE space_id=%s
                        ORDER BY "position", id LIMIT 1""", (space_id,))
        sid_channel[sid] = ch["id"]
        segna_letto(db, uid, ch["id"])

    username = session.get("username")
    # annuncio solo se non era già connesso da un'altra scheda
    altri = [s for s, u in online[space_id].items() if u == uid and s != sid]
    if not altri:
        sistema(f"{username}{random.choice(WELCOMES)}", space_id=space_id)

    emit("set_channel", {"channel_id": ch["id"], "channel": ch["name"]}, room=sid)
    manda_canali(space_id, solo_sid=sid)
    manda_utenti(space_id)


@socketio.on("switch_channel")
def on_switch(data):
    uid = session.get("user_id")
    sid = request.sid
    space_id = sid_space.get(sid)
    if not uid or not space_id:
        return

    with get_db() as db:
        ch = uno(db, "SELECT * FROM channels WHERE id=%s AND space_id=%s",
                 (data.get("channel_id"), space_id))
        if not ch:
            return sistema("Canale non trovato.", sid=sid)

        if sid_channel.get(sid):
            segna_letto(db, uid, sid_channel[sid])
        sid_channel[sid] = ch["id"]
        segna_letto(db, uid, ch["id"])

    emit("set_channel", {"channel_id": ch["id"], "channel": ch["name"]}, room=sid)
    manda_canali(space_id, solo_sid=sid)


@socketio.on("message")
def on_message(data):
    uid = session.get("user_id")
    sid = request.sid
    space_id = sid_space.get(sid)
    if not uid or not space_id:
        return

    msg = (data.get("msg") or "").strip()
    if not msg:
        return
    if len(msg) > 2000:
        return sistema("Messaggio troppo lungo (max 2000).", sid=sid)

    if msg.startswith("/"):
        return comando(uid, sid, space_id, msg)

    channel_id = sid_channel.get(sid)

    with get_db() as db:
        if not puo(db, uid, space_id, SEND_MESSAGES, channel_id):
            return sistema("Non puoi scrivere in questo canale.", sid=sid)

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

        c = uno(db, """SELECT r.color FROM member_roles mr
                       JOIN roles r ON r.id=mr.role_id
                       WHERE mr.user_id=%s AND mr.space_id=%s
                       ORDER BY r."position" DESC LIMIT 1""", (uid, space_id))
        colore = c["color"] if c else "#cccccc"
        segna_letto(db, uid, channel_id)

        connessi = set(online.get(space_id, {}).values())
        da_notificare = menzioni.destinatari(
            db, space_id, channel_id, m_utenti, m_ruoli,
            m_everyone, m_here, uid, connessi)

        payload = {"type": "chat", "id": riga["id"], "username": session.get("username"),
                   "msg": msg, "color": colore, "channel_id": channel_id,
                   "ts": ts(riga["created_at"])}

        # chi guarda il canale riceve il messaggio, gli altri solo il badge
        visto_da = set()
        for s, u in list(online.get(space_id, {}).items()):
            if sid_channel.get(s) == channel_id:
                socketio.emit("message",
                              {**payload, "own": u == uid,
                               "mention": u in da_notificare}, room=s)
                if u != uid:
                    segna_letto(db, u, channel_id)
                    visto_da.add(u)   # ha il canale aperto: niente push
            else:
                socketio.emit("update_channels",
                              canali_visibili(db, u, space_id), room=s)

        db.commit()
        notifica_push(db, space_id, channel_id, uid, msg,
                      da_notificare, visto_da)


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
    parti = msg.split(" ", 2)
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

        if cmd == "/newrole":
            if not puo(db, uid, space_id, MANAGE_ROLES):
                return sistema("Non hai i permessi.", sid=sid)
            if len(parti) < 2:
                return sistema("Uso: /newrole <nome>", sid=sid)
            nome = parti[1].strip().lower()[:32]
            if uno(db, "SELECT 1 FROM roles WHERE space_id=%s AND name=%s",
                   (space_id, nome)):
                return sistema("Ruolo già esistente.", sid=sid)
            return socketio.emit("open_role_creator", {"role_name": nome}, room=sid)

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

        if cmd == "/help":
            return sistema("Comandi: /newchannel /delchannel /role /newrole "
                           "/delrole /kick /ban /unban /retention /help", sid=sid)

        return sistema(f"Comando sconosciuto: {cmd}", sid=sid)


@socketio.on("create_role")
def on_create_role(data):
    uid = session.get("user_id")
    sid = request.sid
    space_id = sid_space.get(sid)
    if not uid or not space_id:
        return

    nome = (data.get("role_name") or "").strip().lower()[:32]
    colore = data.get("color") or "#cccccc"
    if not nome:
        return

    with get_db() as db:
        if not puo(db, uid, space_id, MANAGE_ROLES):
            return
        try:
            db.execute("""INSERT INTO roles (space_id, name, color, permissions, "position")
                          VALUES (%s,%s,%s,%s,1)""",
                       (space_id, nome, colore, SEND_MESSAGES))
        except psycopg.errors.UniqueViolation:
            return sistema("Ruolo già esistente.", sid=sid)

    sistema(f"Ruolo '{nome}' creato.", space_id=space_id)
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
            # annuncio d'uscita solo se non ha altre schede aperte
            if uid not in online.get(space_id, {}).values():
                u = uno(db, "SELECT username FROM users WHERE id=%s", (uid,))
                if u:
                    sistema(f"{u['username']} ha lasciato la stanza.",
                            space_id=space_id)

    manda_utenti(space_id)
    if not online.get(space_id):
        online.pop(space_id, None)


if __name__ == "__main__":
    socketio.run(app, debug=True, allow_unsafe_werkzeug=True,
                 host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
