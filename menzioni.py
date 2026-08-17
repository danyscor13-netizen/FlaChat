"""
Menzioni e notifiche push.

Tenuto fuori da app.py perché è logica a sé: parsing del testo,
risoluzione dei bersagli, invio delle notifiche.
"""

import json
import os
import re
import unicodedata

# =========================================================
# MENZIONI
# =========================================================

# Cattura @qualcosa. I nomi utente e ruolo sono limitati a lettere,
# numeri, punto, trattino e underscore: se un nome contiene spazi la
# menzione si ferma al primo spazio, che è il comportamento atteso.
RE_MENZIONE = re.compile(r"@([\w.\-]+)", re.UNICODE)

SPECIALI = {"everyone", "all", "here", "tutti"}


def normalizza(s):
    """
    Confronto robusto: minuscole e accenti normalizzati.
    Senza questo "@José" non troverebbe l'utente "jose".
    """
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower()


def estrai_nomi(testo):
    """Nomi grezzi menzionati nel testo, senza duplicati."""
    visti, out = set(), []
    for m in RE_MENZIONE.finditer(testo or ""):
        n = normalizza(m.group(1))
        if n not in visti:
            visti.add(n)
            out.append(n)
    return out


def risolvi(db, space_id, testo, autore_id, puo_everyone):
    """
    Traduce le @ del testo in bersagli reali.

    Ritorna: (utenti, ruoli, everyone, here)
      utenti  -> [user_id]  destinatari diretti, autore escluso
      ruoli   -> [role_id]
      everyone/here -> bool, solo se l'autore ne ha il permesso

    Cerca solo fra membri e ruoli DI QUESTA stanza: menzionare qualcuno
    che non c'è non deve notificare nessuno.
    """
    nomi = estrai_nomi(testo)
    if not nomi:
        return [], [], False, False

    everyone = here = False
    normali = []
    for n in nomi:
        if n in ("everyone", "all", "tutti"):
            everyone = True
        elif n == "here":
            here = True
        else:
            normali.append(n)

    if not puo_everyone:
        everyone = here = False

    utenti, ruoli = [], []
    if normali:
        for r in db.execute("""
            SELECT u.id, u.username FROM members m
            JOIN users u ON u.id = m.user_id
            WHERE m.space_id = %s
        """, (space_id,)).fetchall():
            if normalizza(r["username"]) in normali and r["id"] != autore_id:
                utenti.append(r["id"])

        for r in db.execute("""
            SELECT id, name FROM roles
            WHERE space_id = %s AND mentionable
        """, (space_id,)).fetchall():
            if normalizza(r["name"]) in normali:
                ruoli.append(r["id"])

    return utenti, ruoli, everyone, here


def salva(db, message_id, utenti, ruoli):
    for u in utenti:
        db.execute("""INSERT INTO mentions (message_id, user_id) VALUES (%s,%s)
                      ON CONFLICT DO NOTHING""", (message_id, u))
    for r in ruoli:
        db.execute("""INSERT INTO mentions (message_id, role_id) VALUES (%s,%s)
                      ON CONFLICT DO NOTHING""", (message_id, r))


def destinatari(db, space_id, channel_id, utenti, ruoli, everyone, here,
                autore_id, online_ids):
    """
    Chi va effettivamente notificato.

      @utente   -> quell'utente
      @ruolo    -> tutti i membri con quel ruolo
      @everyone -> tutti i membri della stanza
      @here     -> solo i membri attualmente connessi

    Esclude l'autore e chi non può leggere il canale.
    """
    ids = set(utenti)

    if ruoli:
        for r in db.execute("""SELECT DISTINCT user_id FROM member_roles
                               WHERE space_id=%s AND role_id = ANY(%s)""",
                            (space_id, list(ruoli))).fetchall():
            ids.add(r["user_id"])

    if everyone or here:
        for r in db.execute("SELECT user_id FROM members WHERE space_id=%s",
                            (space_id,)).fetchall():
            if not here or r["user_id"] in online_ids:
                ids.add(r["user_id"])

    ids.discard(autore_id)
    return ids


# =========================================================
# NOTIFICHE PUSH
# =========================================================

VAPID_PUBLIC = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_EMAIL = os.environ.get("VAPID_CLAIM_EMAIL", "mailto:admin@example.com")

push_attive = bool(VAPID_PUBLIC and VAPID_PRIVATE)

# livelli di notification_prefs.level
NIENTE, MENZIONI, TUTTI = 0, 1, 2


def livello(db, user_id, space_id, channel_id):
    """
    Preferenza effettiva. L'override sul canale vince su quello della
    stanza; se non c'è nulla, il default è "solo menzioni".
    Un mute attivo azzera tutto.
    """
    r = db.execute("""
        SELECT level, muted_until FROM notification_prefs
        WHERE user_id=%s AND channel_id=%s
    """, (user_id, channel_id)).fetchone()

    if not r:
        r = db.execute("""
            SELECT level, muted_until FROM notification_prefs
            WHERE user_id=%s AND space_id=%s AND channel_id IS NULL
        """, (user_id, space_id)).fetchone()

    if not r:
        return MENZIONI

    if r["muted_until"]:
        from datetime import datetime, timezone
        if r["muted_until"] > datetime.now(timezone.utc):
            return NIENTE

    return r["level"]


def invia(db, user_id, titolo, corpo, url, tag=None):
    """
    Manda la notifica a tutti i dispositivi dell'utente.
    Gli endpoint morti (404/410) vengono cancellati: è il modo previsto
    dallo standard per sapere che l'utente ha disinstallato o revocato.
    """
    if not push_attive:
        return 0

    from pywebpush import webpush, WebPushException

    inviate = 0
    for s in db.execute("""SELECT id, endpoint, p256dh, auth
                           FROM push_subscriptions WHERE user_id=%s""",
                        (user_id,)).fetchall():
        try:
            webpush(
                subscription_info={
                    "endpoint": s["endpoint"],
                    "keys": {"p256dh": s["p256dh"], "auth": s["auth"]},
                },
                data=json.dumps({"title": titolo, "body": corpo,
                                 "url": url, "tag": tag or "flachat"}),
                vapid_private_key=VAPID_PRIVATE,
                vapid_claims={"sub": VAPID_EMAIL},
                # TTL: quanto il servizio push conserva la notifica se il
                # dispositivo e' spento. Il default e' 0, cioe' "consegna
                # subito o buttala": per una chat significa perdere quasi
                # tutte le notifiche utili. 12 ore e' un compromesso.
                ttl=43200,
                timeout=5,
            )
            inviate += 1
        except WebPushException as e:
            codice = getattr(e.response, "status_code", None)
            if codice in (404, 410):
                db.execute("DELETE FROM push_subscriptions WHERE id=%s",
                           (s["id"],))
        except Exception:
            # una notifica persa non deve mai far fallire l'invio del
            # messaggio: la chat viene prima
            pass
    return inviate
