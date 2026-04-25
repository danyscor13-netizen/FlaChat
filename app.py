from flask import Flask, render_template, request
from flask_socketio import SocketIO, join_room, send, emit

from random import choice

import os


app = Flask(__name__)
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

@app.route('/')
def home():
    return render_template("home.html")
    
@app.route('/chat', methods=['POST'])
def chat():
    username = request.form['username']
    room = request.form['room']
    return render_template('chat.html', username=username, room=room)
    
@socketio.on('join')
def handle_join(data):
    username = data['username']
    room = data['room']
    sid = request.sid
    
    welcomeMsg = choice(welcomes)
    
    if room not in rooms_users:
        rooms_users[room] = {}
        rooms_roles[room] = {}
        init_roles(room)
    
    rooms_users[room][sid] = username

    if len(rooms_roles[room]) == 0:
        rooms_roles[room][sid] = "owner"
    else:
        rooms_roles[room][sid] = "user"
    
    join_room(room)
    
    send({
        'type': 'system',
        'msg': f"{username}{welcomeMsg}"
    }, room=room)
    emit_users(room)
    
@socketio.on('message')
def handle_messages(data):
    username = data['username']
    room = data['room']
    msg = data['msg']
    sid = request.sid

    role = rooms_roles[room].get(sid, "user")

    # Comandi
    # Al momemnto solo /role <utente> <ruolo>
    if msg.startswith("/role "):
    
        if role != "owner":
            send({
                "type": "system",
                "msg": "Solo il proprietario può usare '/role'"
            }, room=sid)
            return
    
        try:
            _, target_name, new_role = msg.split(" ", 2)
        except:
            send({
                "type": "system",
                "msg": "Uso: /role <utente> <ruolo>"
            }, room=sid)
            return
    
        # verifica ruolo valido
        if new_role not in rooms_role_defs[room]:
            send({
                "type": "system",
                "msg": "Ruolo non valido"
            }, room=sid)
            return
    
        target_sid = None
    
        for s, u in rooms_users[room].items():
            if u.strip().lower() == target_name.strip().lower():
                target_sid = s
                break
    
        if not target_sid:
            send({
                "type": "system",
                "msg": "Questo utente non esiste"
            }, room=sid)
            return
    
        rooms_roles[room][target_sid] = new_role
    
        send({
            "type": "system",
            "msg": f"{target_name} ora è {new_role}"
        }, room=room)
    
        emit_users(room)
        return
    
    role_data = rooms_role_defs[room].get(role, {"color": "white"})
    color = role_data["color"]
    
    send({
        'type': 'chat',
        'username': username,
        'msg': msg,
        'color': color
    }, room=room)

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid

    for room, users in list(rooms_users.items()):
        if sid in users:

            username = rooms_users[room].pop(sid)
            rooms_roles[room].pop(sid, None)

            send({
                'type': 'system',
                'msg': f"{username} ha lasciato la stanza."
            }, room=room)

            emit_users(room)

            if len(rooms_users[room]) == 0:
                del rooms_users[room]
                del rooms_roles[room]
                del rooms_role_defs[room]
    
if __name__ == "__main__":
    socketio.run(app, debug=True, allow_unsafe_werkzeug=True, host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
