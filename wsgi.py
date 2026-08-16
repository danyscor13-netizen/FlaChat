"""
Punto di ingresso per Render (gunicorn).

Il monkey patching di gevent DEVE avvenire prima di qualsiasi altro
import: rimpiazza socket, ssl e threading con versioni cooperative.
Se psycopg o flask vengono importati prima, si portano dietro il
socket bloccante originale e ogni query blocca l'intero processo —
cioè tutti gli utenti connessi, non solo chi ha fatto la query.

Per questo esiste questo file invece di lanciare app.py direttamente.
"""

from gevent import monkey
monkey.patch_all()

from app import app, socketio  # noqa: E402

# gunicorn cerca "application" per convenzione
application = app

if __name__ == "__main__":
    import os
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
