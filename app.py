from flask import Flask, render_template, request, redirect, url_for, session
from flask_socketio import SocketIO, join_room, send, emit

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
            "admin": {"color": "red", "permissions": ["kick"]},
            "mod": {"color": "blue", "permissions": ["kick"]},
            "user": {"color": "white", "permissions": []}
        }

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
                c.execute(
                    "INSERT INTO users (username, password) VALUES (?, ?)",
                    (username, generate_password_hash(password))
                )
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
    return render_template('chat.html', username=session['username'], room=room)

# ---------- SOCKET ----------

@socketio.on('join')
def handle_join(data):
    username = data['username']
    room = data['room']
    sid = request.sid

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM bans WHERE room=? AND user_id=?", (room, sid))
    ban = c.fetchone()
    conn.close()

    if ban and ban['expire'] > time.time():
        emit("message", {"type": "system", "msg": "Sei bannato da questa stanza."}, room=sid)
        return
    elif ban:
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM bans WHERE room=? AND user_id=?", (room, sid))
        conn.commit()
        conn.close()

    welcomeMsg = choice(welcomes)

    if room not in rooms_users:
        rooms_users[room] = {}
        rooms_roles[room] = {}
        init_roles(room)

    rooms_users[room][sid] = username

    if "owner" not in rooms_roles[room].values():
        rooms_roles[room][sid] = "owner"
    else:
        rooms_roles[room][sid] = "user"

    join_room(room)

    send({'type': 'system', 'msg': f"{username}{welcomeMsg}"}, room=room)
    emit_users(room)

@socketio.on('message')
def handle_messages(data):
    username = data['username']
    room = data['room']
    msg = data['msg']
    sid = request.sid

    role = rooms_roles[room].get(sid, "user")

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

        send({
            "type": "msg",
            "msg": "Sei stato cacciato dalla stanza."
        }, room=target_sid)

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
        c.execute("INSERT INTO bans VALUES (?, ?, ?)", (room, target_sid, time.time() + seconds))
        conn.commit()
        conn.close()

        send({
            "type": "system",
            "msg": f"Sei stato bannato dalla stanza per {seconds}s."
        }, room=target_sid)

        socketio.server.disconnect(target_sid)
        send({"type": "system", "msg": f"{target_name} bannato per {seconds}s"}, room=room)
        emit_users(room)
        return

    role_data = rooms_role_defs[room].get(role, {"color": "white"})
    color = role_data["color"]
    send({'type': 'chat', 'username': username, 'msg': msg, 'color': color}, room=room)

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    for room, users in list(rooms_users.items()):
        if sid in users:
            username = rooms_users[room].pop(sid)
            rooms_roles[room].pop(sid, None)
            send({'type': 'system', 'msg': f"{username} ha lasciato la stanza."}, room=room)
            emit_users(room)
            if len(rooms_users[room]) == 0:
                del rooms_users[room]
                del rooms_roles[room]
                del rooms_role_defs[room]

if __name__ == "__main__":
    socketio.run(app, debug=True, allow_unsafe_werkzeug=True, host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
