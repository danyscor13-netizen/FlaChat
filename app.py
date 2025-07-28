from flask import Flask, render_template, request
from flask_socketio import SocketIO, join_room, send


app = Flask(__name__)
socketio = SocketIO(app)


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
    join_room(room)
    send({'msg': f"{username} è entrato nella chat!"}, room=room)
    
@socketio.on('message')
def handle_messages(data):
    username = data['username']
    room = data['room']
    msg = data['msg']
    send({'username' : username, 'msg' : msg}, room=room)
    
if __name__ == "__main__":
    allow_unsafe_werkzewg.run(app, debug=True)
