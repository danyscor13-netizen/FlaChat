from flask import Flask, render_template, request, redirect, url_for, session
from flask_socketio import SocketIO, join_room, leave_room, send, emit

from random import choice
from werkzeug.security import generate_password_hash, check_password_hash

import os
import sqlite3
import time

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "cambia-questa-chiave")
socketio = SocketIO(app)

rooms_users = {}
rooms_roles = {}
rooms_role_defs = {}
rooms_channels = {}        # room -> {channel_name: {permissions}}
rooms_user_channel = {}    # room -> {sid: channel_name}

welcomes = [
    " è entrato nella stanza!",
    ", spero che tu abbia portato la pizza!",
    " è appena atterrato!",
    " stava facendo un'entrata segreta, ma fu colto di sprovvista! Salutatelo!",
    ", sentiti libero di accomodarti!"
]

def get_db():
    conn = sqlite3.connect("flachat.db")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS bans (
        room TEXT,
        user_id TEXT,
        expire REAL
    )
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL
    )
    """)
    conn.commit()
    conn.close()

init_db()

def init_roles(room):
    if room not in rooms_role_defs:
        rooms_role_defs[room] = {
            "owner": {"color": "gold", "permissions": ["all"]},
            "admin": {"color": "red", "permissions": ["kick", "manage_channels"]},
            "mod": {"color": "blue", "permissions": ["kick"]},
            "user": {"color": "white", "permissions": []}
        }

def init_channels(room):
    if room not in rooms_channels:
        rooms_channels[room] = {
            "general": {
                "write": ["all"],   # tutti possono scrivere
                "read": ["all"]
            }
        }
    if room not in rooms_user_channel:
        rooms_user_channel[room] = {}

def can_write(room, sid, channel):
    role = rooms_roles[room].get(sid, "user")
    role_defs = rooms_role_defs[room]
    ch = rooms_channels[room].get(channel, {})
    write_perms = ch.get("write", ["all"])

    if "all" in write_perms:
        return True
    if role == "owner":
        return True
    if role in write_perms:
        return True
    if "manage_channels" in role_defs.get(role, {}).get("permissions", []):
        return True
    return False

def can_read(room, sid, channel):
    role = rooms_roles[room].get(sid, "user")
    ch = rooms_channels[room].get(channel, {})
    read_perms = ch.get("read", ["all"])

    if "all" in read_perms:
        return True
    if role == "owner":
        return True
    if role in read_perms:
        return True
    return False

def emit_users(room):
    users = []
    for sid, username in rooms_users.get(room, {}).items():
        role = rooms_roles[room].get(sid, "user")
        role_data = rooms_role_defs[room].get(role, {"color": "white"})
        users.append({
            "username": username,
            "role": role,
            "color": role_data["color"]
        })
    socketio.emit('update_users', users, room=room)

def emit_channels(room, sid=None):
    channels = []
    target_sid = sid
    for ch_name, ch_data in rooms_channels[room].items():
        # mostra solo canali leggibili
        if sid and not can_read(room, sid, ch_name):
            continue
        channels.append({"name": ch_name, "write": ch_data.get("write", ["all"])})

    if sid:
        socketio.emit('update_channels', channels, room=sid)
    else:
        # manda a tutti, filtrato per ruolo
        for s in rooms_users.get(room, {}):
            visible = []
            for ch_name, ch_data in rooms_channels[room].items():
                if can_read(room, s, ch_name):
                    visible.append({"name": ch_name, "write": ch_data.get("write", ["all"])})
            socketio.emit('update_channels', visible, room=s)

# ---------- AUTH ----------

@app.route('/')
def home():
    if 'username' in session:
        return redirect(url_for('lobby'))
    return render_template("home.html")

@app.route('/register', methods=['GET', 'POST'])
def register():
    error = None
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password'].strip()
        if not username or not password:
            error = "Compila tutti i campi."
        else:
            conn = get_db()
            c = conn.cursor()
            try:
                c.execute("INSERT INTO users (username, password) VALUES (?, ?)",
                    (username, generate_password_hash(password)))
                conn.commit()
                session['username'] = username
                conn.close()
                return redirect(url_for('lobby'))
            except sqlite3.IntegrityError:
                error = "Username già in uso."
            finally:
                conn.close()
    return render_template("register.html", error=error)

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password'].strip()
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE username=?", (username,))
        user = c.fetchone()
        conn.close()
        if user and check_password_hash(user['password'], password):
            session['username'] = user['username']
            return redirect(url_for('lobby'))
        else:
            error = "Credenziali errate."
    return render_template("login.html", error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))

@app.route('/lobby', methods=['GET', 'POST'])
def lobby():
    if 'username' not in session:
        return redirect(url_for('home'))
    if request.method == 'POST':
        room = request.form['room']
        return redirect(url_for('chat', room=room))
    return render_template("lobby.html", username=session['username'])

@app.route('/chat/<room>')
def chat(room):
    if 'username' not in session:
        return redirect(url_for('home'))
    username = session['username']
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM bans WHERE room=? AND user_id=?", (room, username.lower()))
    ban = c.fetchone()
    conn.close()
    if ban and ban['expire'] > time.time():
        return redirect(url_for('lobby', banned=1))
    elif ban:
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM bans WHERE room=? AND user_id=?", (room, username.lower()))
        conn.commit()
        conn.close()
    return render_template('chat.html', username=username, room=room)

# ---------- SOCKET ----------

@socketio.on('join')
def handle_join(data):
    username = data['username']
    room = data['room']
    sid = request.sid

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM bans WHERE room=? AND user_id=?", (room, username.lower()))
    ban = c.fetchone()
    conn.close()

    if ban and ban['expire'] > time.time():
        emit("message", {"type": "system", "msg": "Sei bannato da questa stanza."}, room=sid)
        return
    elif ban:
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM bans WHERE room=? AND user_id=?", (room, username.lower()))
        conn.commit()
        conn.close()

    welcomeMsg = choice(welcomes)

    if room not in rooms_users:
        rooms_users[room] = {}
        rooms_roles[room] = {}
        init_roles(room)
        init_channels(room)

    rooms_users[room][sid] = username
    rooms_user_channel[room][sid] = "general"

    if "owner" not in rooms_roles[room].values():
        rooms_roles[room][sid] = "owner"
    else:
        rooms_roles[room][sid] = "user"

    join_room(room)

    send({'type': 'system', 'msg': f"{username}{welcomeMsg}"}, room=room)
    emit_users(room)
    emit_channels(room, sid)
    emit('set_channel', {'channel': 'general'}, room=sid)

@socketio.on('switch_channel')
def handle_switch_channel(data):
    room = data['room']
    channel = data['channel']
    sid = request.sid

    if channel not in rooms_channels.get(room, {}):
        emit('message', {'type': 'system', 'msg': 'Canale non trovato.'}, room=sid)
        return

    if not can_read(room, sid, channel):
        emit('message', {'type': 'system', 'msg': 'Non hai accesso a questo canale.'}, room=sid)
        return

    rooms_user_channel[room][sid] = channel
    emit('set_channel', {'channel': channel}, room=sid)
    emit('message', {'type': 'system', 'msg': f'Sei entrato in #{channel}'}, room=sid)

@socketio.on('message')
def handle_messages(data):
    username = data['username']
    room = data['room']
    msg = data['msg']
    sid = request.sid

    role = rooms_roles[room].get(sid, "user")
    role_defs = rooms_role_defs[room]

    # permesso manage_channels: owner o chi ce l'ha nel ruolo
    def has_manage():
        if role == "owner":
            return True
        return "manage_channels" in role_defs.get(role, {}).get("permissions", [])

    # ---- COMANDI ----

    if msg.startswith("/newchannel "):
        if not has_manage():
            send({"type": "system", "msg": "Non hai i permessi per creare canali."}, room=sid)
            return
        ch_name = msg.split(" ", 1)[1].strip().lower().replace(" ", "-")
        if ch_name in rooms_channels[room]:
            send({"type": "system", "msg": "Canale già esistente."}, room=sid)
            return
        rooms_channels[room][ch_name] = {"write": ["all"], "read": ["all"]}
        send({"type": "system", "msg": f"Canale #{ch_name} creato!"}, room=room)
        emit_channels(room)
        return

    if msg.startswith("/delchannel "):
        if not has_manage():
            send({"type": "system", "msg": "Non hai i permessi per eliminare canali."}, room=sid)
            return
        ch_name = msg.split(" ", 1)[1].strip().lower()
        if ch_name == "general":
            send({"type": "system", "msg": "Non puoi eliminare #general."}, room=sid)
            return
        if ch_name not in rooms_channels[room]:
            send({"type": "system", "msg": "Canale non trovato."}, room=sid)
            return
        del rooms_channels[room][ch_name]
        # rimanda tutti in general se erano in quel canale
        for s, ch in rooms_user_channel[room].items():
            if ch == ch_name:
                rooms_user_channel[room][s] = "general"
                socketio.emit('set_channel', {'channel': 'general'}, room=s)
                socketio.emit('message', {'type': 'system', 'msg': f'#{ch_name} è stato eliminato. Sei stato spostato in #general.'}, room=s)
        send({"type": "system", "msg": f"Canale #{ch_name} eliminato."}, room=room)
        emit_channels(room)
        return

    if msg.startswith("/setchannel "):
        # uso: /setchannel <canale> <write|read> <all|none|ruolo1,ruolo2>
        if not has_manage():
            send({"type": "system", "msg": "Non hai i permessi."}, room=sid)
            return
        try:
            parts = msg.split(" ", 3)
            _, ch_name, perm_type, value = parts
        except:
            send({"type": "system", "msg": "Uso: /setchannel <canale> <write|read> <all|none|ruolo>"}, room=sid)
            return
        ch_name = ch_name.lower()
        if ch_name not in rooms_channels[room]:
            send({"type": "system", "msg": "Canale non trovato."}, room=sid)
            return
        if perm_type not in ["write", "read"]:
            send({"type": "system", "msg": "Tipo permesso: write o read"}, room=sid)
            return
        if value == "all":
            rooms_channels[room][ch_name][perm_type] = ["all"]
        elif value == "none":
            rooms_channels[room][ch_name][perm_type] = []
        else:
            roles_list = [r.strip() for r in value.split(",")]
            rooms_channels[room][ch_name][perm_type] = roles_list
        send({"type": "system", "msg": f"Permessi #{ch_name} aggiornati."}, room=room)
        emit_channels(room)
        return

    if msg.startswith("/role "):
        if role != "owner":
            send({"type": "system", "msg": "Solo il proprietario può usare '/role'"}, room=sid)
            return
        try:
            _, target_name, new_role = msg.split(" ", 2)
        except:
            send({"type": "system", "msg": "Uso: /role <utente> <ruolo>"}, room=sid)
            return
        if new_role not in rooms_role_defs[room]:
            send({"type": "system", "msg": "Ruolo non valido"}, room=sid)
            return
        if new_role == "owner":
            send({"type": "system", "msg": "Non puoi nominare qualcuno come erede al trono."}, room=sid)
            return
        target_sid = next((s for s, u in rooms_users[room].items() if u.strip().lower() == target_name.strip().lower()), None)
        if not target_sid:
            send({"type": "system", "msg": "Questo utente non esiste"}, room=sid)
            return
        rooms_roles[room][target_sid] = new_role
        send({"type": "system", "msg": f"{target_name} ora è {new_role}"}, room=room)
        emit_users(room)
        emit_channels(room)
        return

    if msg.startswith("/kick "):
        if role not in ["owner", "admin", "mod"]:
            return
        target_name = msg.split(" ", 1)[1]
        target_sid = next((s for s, u in rooms_users[room].items() if u.lower() == target_name.lower()), None)
        if not target_sid:
            return
        rooms_users[room].pop(target_sid, None)
        rooms_roles[room].pop(target_sid, None)
        rooms_user_channel[room].pop(target_sid, None)
        send({"type": "system", "msg": "Sei stato cacciato dalla stanza."}, room=target_sid)
        socketio.server.disconnect(target_sid)
        send({"type": "system", "msg": f"{target_name} è stato cacciato."}, room=room)
        emit_users(room)
        return

    if msg.startswith("/ban "):
        if role not in ["owner", "admin"]:
            return
        try:
            _, target_name, seconds = msg.split(" ", 2)
            seconds = int(seconds)
        except:
            send({"type": "system", "msg": "Uso: /ban <utente> <secondi>"}, room=sid)
            return
        target_sid = next((s for s, u in rooms_users[room].items() if u.lower() == target_name.lower()), None)
        if not target_sid:
            return
        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT INTO bans VALUES (?, ?, ?)", (room, target_name.lower(), time.time() + seconds))
        conn.commit()
        conn.close()
        send({"type": "banned", "msg": f"Sei stato bannato per {seconds}s."}, room=target_sid)
        socketio.server.disconnect(target_sid)
        send({"type": "system", "msg": f"{target_name} bannato per {seconds}s"}, room=room)
        emit_users(room)
        return

    if msg.startswith("/newrole "):
        if role != "owner":
            send({"type": "system", "msg": "Solo il proprietario può creare ruoli."}, room=sid)
            return
        role_name = msg.split(" ", 1)[1].strip().lower()
        if role_name in rooms_role_defs[room]:
            send({"type": "system", "msg": "Questo ruolo esiste già."}, room=sid)
            return
        emit("open_role_creator", {"role_name": role_name}, room=sid)
        return

    if msg.startswith("/delrole "):
        if role != "owner":
            send({"type": "system", "msg": "Solo il proprietario può eliminare ruoli."}, room=sid)
            return
        role_name = msg.split(" ", 1)[1].strip().lower()
        if role_name in ["owner", "admin", "mod", "user"]:
            send({"type": "system", "msg": "Non puoi eliminare i ruoli predefiniti."}, room=sid)
            return
        if role_name not in rooms_role_defs[room]:
            send({"type": "system", "msg": "Ruolo non trovato."}, room=sid)
            return
        del rooms_role_defs[room][role_name]
        for s in rooms_roles[room]:
            if rooms_roles[room][s] == role_name:
                rooms_roles[room][s] = "user"
        send({"type": "system", "msg": f"Ruolo '{role_name}' eliminato."}, room=room)
        emit_users(room)
        return

    # ---- MESSAGGIO NORMALE ----
    current_channel = rooms_user_channel[room].get(sid, "general")

    if not can_write(room, sid, current_channel):
        send({"type": "system", "msg": f"Non puoi scrivere in #{current_channel}."}, room=sid)
        return

    role_data = rooms_role_defs[room].get(role, {"color": "white"})
    color = role_data["color"]

    # manda solo a chi è nello stesso canale
    for target_sid, target_channel in rooms_user_channel[room].items():
        if target_channel == current_channel:
            socketio.emit('message', {
                'type': 'chat',
                'username': username,
                'msg': msg,
                'color': color,
                'channel': current_channel
            }, room=target_sid)

@socketio.on('create_role')
def handle_create_role(data):
    room = data['room']
    role_name = data['role_name'].strip().lower()
    color = data['color']
    sid = request.sid

    role = rooms_roles[room].get(sid, "user")
    if role != "owner":
        return
    if role_name in rooms_role_defs[room]:
        emit("message", {"type": "system", "msg": "Ruolo già esistente."}, room=sid)
        return
    rooms_role_defs[room][role_name] = {"color": color, "permissions": []}
    send({"type": "system", "msg": f"Ruolo '{role_name}' creato!"}, room=room)
    emit_users(room)

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    for room, users in list(rooms_users.items()):
        if sid in users:
            username = rooms_users[room].pop(sid)
            rooms_roles[room].pop(sid, None)
            rooms_user_channel[room].pop(sid, None)
            send({'type': 'system', 'msg': f"{username} ha lasciato la stanza."}, room=room)
            emit_users(room)
            if len(rooms_users[room]) == 0:
                del rooms_users[room]
                del rooms_roles[room]
                del rooms_role_defs[room]
                del rooms_channels[room]
                del rooms_user_channel[room]

if __name__ == "__main__":
    socketio.run(app, debug=True, allow_unsafe_werkzeug=True, host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))