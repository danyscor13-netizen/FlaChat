-- =========================================================
-- FlaChat — schema Postgres (Supabase)
--
-- ATTENZIONE: la sezione 0 CANCELLA le tabelle esistenti.
-- Eseguire solo su un database vuoto o di prova.
--
-- Tutto vive in "public". Nessuno schema separato: dopo il
-- reset non c'è più niente con cui collidere.
-- =========================================================

-- ---------------------------------------------------------
-- 0. RESET — via il vecchio impianto
-- ---------------------------------------------------------
-- CASCADE elimina anche le foreign key che puntano a queste
-- tabelle, quindi l'ordine non conta.

DROP TABLE IF EXISTS invites           CASCADE;
DROP TABLE IF EXISTS community_members CASCADE;
DROP TABLE IF EXISTS communities       CASCADE;
DROP TABLE IF EXISTS messages          CASCADE;
DROP TABLE IF EXISTS channels          CASCADE;
DROP TABLE IF EXISTS roles             CASCADE;
DROP TABLE IF EXISTS users             CASCADE;

-- tabelle di un tentativo precedente andato a metà
DROP TABLE IF EXISTS bans               CASCADE;
DROP TABLE IF EXISTS notification_prefs CASCADE;
DROP TABLE IF EXISTS push_subscriptions CASCADE;
DROP TABLE IF EXISTS read_state         CASCADE;
DROP TABLE IF EXISTS mentions           CASCADE;
DROP TABLE IF EXISTS channel_overrides  CASCADE;
DROP TABLE IF EXISTS member_roles       CASCADE;
DROP TABLE IF EXISTS members            CASCADE;
DROP TABLE IF EXISTS spaces             CASCADE;

-- tabelle aggiunte da migrazione_retention.sql: vanno rimosse anche
-- loro, altrimenti un reset lascia righe orfane con FK spezzate
DROP TABLE IF EXISTS attachments          CASCADE;
DROP TABLE IF EXISTS storage_da_eliminare CASCADE;
DROP FUNCTION IF EXISTS pulisci_scaduti(INTEGER);

DROP SCHEMA IF EXISTS flachat CASCADE;


-- ---------------------------------------------------------
-- 1. UTENTI
-- ---------------------------------------------------------
-- Restiamo su una tabella users nostra invece di auth.users:
-- il login passa da Flask, non dal client. Se un giorno
-- passerai a Supabase Auth, basterà aggiungere una colonna
-- auth_id UUID REFERENCES auth.users.

CREATE TABLE IF NOT EXISTS users (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username     TEXT UNIQUE NOT NULL,
    password     TEXT NOT NULL,
    display_name TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    bio          TEXT DEFAULT 'Questo utente non ha una bio ancora :\'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_lower
    ON users (lower(username));


-- ---------------------------------------------------------
-- 2. SUPERSTANZE
-- ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS spaces (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code       TEXT UNIQUE NOT NULL,
    name       TEXT NOT NULL,
    owner_id   BIGINT NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- ---------------------------------------------------------
-- 3. RUOLI — permessi come bitmask
-- ---------------------------------------------------------
--   1  SEND_MESSAGES        16  MANAGE_ROLES
--   2  MANAGE_CHANNELS      32  MENTION_EVERYONE
--   4  KICK                 64  MANAGE_MESSAGES
--   8  BAN                 128  ADMIN
--
-- "position" è fra virgolette: in Postgres è parola riservata.

CREATE TABLE IF NOT EXISTS roles (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    space_id    BIGINT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    color       TEXT NOT NULL DEFAULT '#cccccc',
    permissions INTEGER NOT NULL DEFAULT 1,
    "position"  INTEGER NOT NULL DEFAULT 0,
    is_default  BOOLEAN NOT NULL DEFAULT false,
    mentionable BOOLEAN NOT NULL DEFAULT true,
    -- nome del file in static/icons/. NULL = usa il file che si chiama
    -- come il ruolo, se c'e'; altrimenti nessuna icona.
    icon        TEXT,
    UNIQUE (space_id, name)
);

CREATE INDEX IF NOT EXISTS idx_roles_space ON roles (space_id, "position" DESC);

-- Un solo ruolo di default per stanza: in SQLite era una convenzione,
-- qui possiamo imporlo davvero con un indice parziale.
CREATE UNIQUE INDEX IF NOT EXISTS idx_roles_one_default
    ON roles (space_id) WHERE is_default;


-- ---------------------------------------------------------
-- 4. MEMBRI
-- ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS members (
    space_id  BIGINT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    user_id   BIGINT NOT NULL REFERENCES users(id)  ON DELETE CASCADE,
    nickname  TEXT,
    joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (space_id, user_id)
);

CREATE TABLE IF NOT EXISTS member_roles (
    space_id BIGINT NOT NULL,
    user_id  BIGINT NOT NULL,
    role_id  BIGINT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    PRIMARY KEY (space_id, user_id, role_id),
    FOREIGN KEY (space_id, user_id)
        REFERENCES members(space_id, user_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_member_roles_role ON member_roles (role_id);
CREATE INDEX IF NOT EXISTS idx_member_roles_user ON member_roles (user_id, space_id);


-- ---------------------------------------------------------
-- 5. CANALI
-- ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS channels (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    space_id   BIGINT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    topic      TEXT,
    "position" INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (space_id, name)
);

CREATE INDEX IF NOT EXISTS idx_channels_space ON channels (space_id, "position");


-- ---------------------------------------------------------
-- 6. OVERRIDE PERMESSI PER CANALE
-- ---------------------------------------------------------
-- allow / deny: tre stati per permesso (consenti, eredita, nega).
-- perms = (base & ~deny) | allow

CREATE TABLE IF NOT EXISTS channel_overrides (
    channel_id BIGINT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    role_id    BIGINT NOT NULL REFERENCES roles(id)    ON DELETE CASCADE,
    allow      INTEGER NOT NULL DEFAULT 0,
    deny       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (channel_id, role_id),
    -- un permesso non può essere insieme concesso e negato
    CONSTRAINT no_overlap CHECK ((allow & deny) = 0)
);


-- ---------------------------------------------------------
-- 7. MESSAGGI
-- ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS messages (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    channel_id        BIGINT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    author_id         BIGINT REFERENCES users(id) ON DELETE SET NULL,
    content           TEXT NOT NULL,
    reply_to          BIGINT REFERENCES messages(id) ON DELETE SET NULL,
    mentions_everyone BOOLEAN NOT NULL DEFAULT false,
    mentions_here     BOOLEAN NOT NULL DEFAULT false,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    edited_at         TIMESTAMPTZ,
    deleted_at        TIMESTAMPTZ
);

-- L'indice che regge tutta la lettura della chat.
-- Parziale sui non cancellati: i messaggi eliminati non entrano
-- mai nelle query di lettura, quindi non serve indicizzarli.
CREATE INDEX IF NOT EXISTS idx_messages_channel
    ON messages (channel_id, id DESC) WHERE deleted_at IS NULL;


-- ---------------------------------------------------------
-- 8. MENZIONI
-- ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS mentions (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    message_id BIGINT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    user_id    BIGINT REFERENCES users(id) ON DELETE CASCADE,
    role_id    BIGINT REFERENCES roles(id) ON DELETE CASCADE,
    CHECK ((user_id IS NULL) <> (role_id IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_mentions_user ON mentions (user_id, message_id DESC);
CREATE INDEX IF NOT EXISTS idx_mentions_role ON mentions (role_id, message_id DESC);
CREATE INDEX IF NOT EXISTS idx_mentions_msg  ON mentions (message_id);

-- Niente menzioni duplicate sullo stesso messaggio
CREATE UNIQUE INDEX IF NOT EXISTS idx_mentions_uniq_user
    ON mentions (message_id, user_id) WHERE user_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_mentions_uniq_role
    ON mentions (message_id, role_id) WHERE role_id IS NOT NULL;


-- ---------------------------------------------------------
-- 9. STATO DI LETTURA
-- ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS read_state (
    user_id          BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel_id       BIGINT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    last_read_msg_id BIGINT NOT NULL DEFAULT 0,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, channel_id)
);


-- ---------------------------------------------------------
-- 10. NOTIFICHE PUSH
-- ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    endpoint   TEXT UNIQUE NOT NULL,
    p256dh     TEXT NOT NULL,
    auth       TEXT NOT NULL,
    user_agent TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_push_user ON push_subscriptions (user_id);

-- level: 0 = niente, 1 = solo menzioni, 2 = tutti i messaggi
CREATE TABLE IF NOT EXISTS notification_prefs (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    space_id    BIGINT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    channel_id  BIGINT REFERENCES channels(id) ON DELETE CASCADE,
    level       SMALLINT NOT NULL DEFAULT 1 CHECK (level BETWEEN 0 AND 2),
    muted_until TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_notif_space
    ON notification_prefs (user_id, space_id) WHERE channel_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_notif_channel
    ON notification_prefs (user_id, channel_id) WHERE channel_id IS NOT NULL;


-- ---------------------------------------------------------
-- 11. BAN
-- ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS bans (
    space_id   BIGINT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    user_id    BIGINT NOT NULL REFERENCES users(id)  ON DELETE CASCADE,
    banned_by  BIGINT REFERENCES users(id) ON DELETE SET NULL,
    reason     TEXT,
    expire     TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (space_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_bans_expire ON bans (expire) WHERE expire IS NOT NULL;


-- ---------------------------------------------------------
-- 12. INVITI
-- ---------------------------------------------------------
-- spaces.code è il codice permanente della stanza.
-- Questi sono inviti usa-e-getta o a scadenza, ripresi dallo
-- schema precedente: utili per far entrare qualcuno senza
-- divulgare il codice fisso.

CREATE TABLE IF NOT EXISTS invites (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    space_id   BIGINT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    code       TEXT UNIQUE NOT NULL,
    created_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
    max_uses   INTEGER,
    uses       INTEGER NOT NULL DEFAULT 0,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_invites_space ON invites (space_id);


-- ---------------------------------------------------------
-- 13. AVATAR E IMMAGINI
-- ---------------------------------------------------------
-- Ripresi anch'essi dallo schema precedente (avatar_url,
-- image_url): non costano nulla e ti servirebbero comunque.

ALTER TABLE users  ADD COLUMN IF NOT EXISTS avatar_url TEXT;
ALTER TABLE spaces ADD COLUMN IF NOT EXISTS image_url  TEXT;


-- =========================================================
-- 12. SICUREZZA
-- =========================================================
-- Il client non parla mai col database: tutto passa da Flask
-- con la service_role key. Attiviamo comunque RLS su ogni
-- tabella SENZA policy, così la chiave pubblica (anon) non
-- legge nulla. Senza questo, chiunque abbia la anon key —
-- che è pubblica per definizione — leggerebbe tutti i messaggi.
-- La service_role key salta RLS, quindi Flask continua a funzionare.

ALTER TABLE users              ENABLE ROW LEVEL SECURITY;
ALTER TABLE spaces             ENABLE ROW LEVEL SECURITY;
ALTER TABLE roles              ENABLE ROW LEVEL SECURITY;
ALTER TABLE members            ENABLE ROW LEVEL SECURITY;
ALTER TABLE member_roles       ENABLE ROW LEVEL SECURITY;
ALTER TABLE channels           ENABLE ROW LEVEL SECURITY;
ALTER TABLE channel_overrides  ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages           ENABLE ROW LEVEL SECURITY;
ALTER TABLE mentions           ENABLE ROW LEVEL SECURITY;
ALTER TABLE read_state         ENABLE ROW LEVEL SECURITY;
ALTER TABLE push_subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE notification_prefs ENABLE ROW LEVEL SECURITY;
ALTER TABLE bans               ENABLE ROW LEVEL SECURITY;
ALTER TABLE invites            ENABLE ROW LEVEL SECURITY;

