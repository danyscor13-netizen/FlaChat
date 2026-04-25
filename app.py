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

    if len(rooms_roles[room])) == 0:
        rooms_roles[room][sid] = "owner"
    else:
        rooms_roles[room][sid] = "user"
    
    join_room(room)
    
    send({'msg': f"{username}" + welcomeMsg}, room=room)
    emit('update_users', list(rooms_users[room].values()), room=room)
    
@socketio.on('message')
def handle_messages(data):
    username = data['username']
    room = data['room']
    msg = data['msg']
    send({'username' : username, 'msg' : msg}, room=room)

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

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    for room, users in rooms_users.items():
        if sid in users:
            username = users.pop(sid)
            send({'msg': f"{username} ha lasciato la stanza."}, room=room)
            emit('update_users', list(users.values()), room=room)
            break
    
if __name__ == "__main__":
    socketio.run(app, debug=True, allow_unsafe_werkzeug=True, host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
