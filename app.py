from flask import Flask, render_template, request
from flask_socketio import SocketIO, join_room, send

from random import choice

import os


app = Flask(__name__)
socketio = SocketIO(app)

rooms_users = {}

welcomes = [
    " è entrato nella stanza!",
    ", spero che tu abbia portato la pizza!",
    " è appena atterrato!",
    " stava facendo un'entrata segreta, ma fu colto di sprovvista! Salutatelo!",
    ", sentiti libero di accomodarti!"
]

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
    rooms_users[room][sid] = username
    join_room(room)
    send({'msg': f"{username}" + welcomeMsg}, room=room)
    emit('update_users', list(rooms_users[room].values()), room=room)
    
@socketio.on('message')
def handle_messages(data):
    username = data['username']
    room = data['room']
    msg = data['msg']
    send({'username' : username, 'msg' : msg}, room=room)

def handle_disconnect():
    sid = request.sid
    for room, users in rooms_users.items():
        if sid in users:
            username = users.pop(sid)
            send({'msg': f"{username] ha lasciato la stanza."}, room=room)
            emit('update_users', list(users.values()), room=room)
            break
    
if __name__ == "__main__":
    socketio.run(app, debug=True, allow_unsafe_werkzeug=True, host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
