"""
Genera la coppia di chiavi VAPID per le notifiche push.

Da eseguire UNA VOLTA SOLA. Le chiavi vanno poi messe fra le variabili
d'ambiente di Render:

    python genera_vapid.py

Se le rigeneri, tutte le iscrizioni esistenti smettono di funzionare e
gli utenti devono riattivare le notifiche.

A cosa servono: la chiave pubblica viaggia fino al browser e finisce
nell'iscrizione; quella privata resta sul server e firma ogni invio.
È il modo in cui Google, Mozilla e Apple verificano che le notifiche
arrivino davvero da te e non da chi ha intercettato un endpoint.
"""

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def b64(dati):
    """base64url senza padding, come richiede lo standard VAPID."""
    return base64.urlsafe_b64encode(dati).rstrip(b"=").decode()


def main():
    chiave = ec.generate_private_key(ec.SECP256R1())

    priv = b64(chiave.private_numbers().private_value.to_bytes(32, "big"))
    pub = b64(chiave.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint))

    print("Chiavi VAPID generate.\n")
    print("Aggiungi queste variabili d'ambiente su Render:\n")
    print(f"VAPID_PUBLIC_KEY={pub}")
    print(f"VAPID_PRIVATE_KEY={priv}")
    print("VAPID_CLAIM_EMAIL=mailto:latua@email.com")
    print("\nLa chiave privata non va nel codice ne' su GitHub.")


if __name__ == "__main__":
    main()
