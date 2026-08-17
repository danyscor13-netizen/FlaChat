-- =========================================================
-- FlaChat — scadenza dei messaggi
--
-- Da eseguire nel SQL Editor di Supabase, una volta sola.
-- Non cancella nulla di esistente: aggiunge soltanto.
-- =========================================================

-- ---------------------------------------------------------
-- 1. Impostazione sulla stanza
-- ---------------------------------------------------------
-- NULL = non scade mai (default: nessuna sorpresa per le
-- stanze già esistenti).

ALTER TABLE spaces
    ADD COLUMN IF NOT EXISTS retention_days INTEGER
        CHECK (retention_days IS NULL OR retention_days BETWEEN 1 AND 3650);


-- ---------------------------------------------------------
-- 2. Allegati
-- ---------------------------------------------------------
-- Creata adesso anche se i file non ci sono ancora: la
-- pulizia deve saperli gestire fin da subito, altrimenti
-- quando li aggiungerai resteranno orfani nel bucket.

CREATE TABLE IF NOT EXISTS attachments (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    message_id   BIGINT REFERENCES messages(id) ON DELETE CASCADE,
    uploader_id  BIGINT REFERENCES users(id) ON DELETE SET NULL,
    space_id     BIGINT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    -- percorso dentro il bucket di Supabase Storage
    storage_path TEXT NOT NULL,
    tipo         TEXT NOT NULL,        -- image | audio | file | gif | sticker
    mime         TEXT,
    bytes        BIGINT NOT NULL DEFAULT 0,
    durata_sec   INTEGER,              -- per i vocali
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_attach_msg   ON attachments (message_id);
CREATE INDEX IF NOT EXISTS idx_attach_space ON attachments (space_id);
CREATE INDEX IF NOT EXISTS idx_attach_user  ON attachments (uploader_id);

ALTER TABLE attachments ENABLE ROW LEVEL SECURITY;


-- ---------------------------------------------------------
-- 3. Coda di cancellazione dal bucket
-- ---------------------------------------------------------
-- SQL non può parlare con Supabase Storage: può solo dire
-- "questo file va rimosso". L'app svuota la coda.
-- Senza questo passaggio i file resterebbero nel bucket per
-- sempre, continuando a occupare spazio e a costare.

CREATE TABLE IF NOT EXISTS storage_da_eliminare (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    storage_path TEXT NOT NULL,
    accodato_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    tentativi    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_coda_tentativi
    ON storage_da_eliminare (tentativi, id);

ALTER TABLE storage_da_eliminare ENABLE ROW LEVEL SECURITY;


-- ---------------------------------------------------------
-- 4. La funzione di pulizia
-- ---------------------------------------------------------
-- A blocchi (default 5000) per non tenere un lock lungo
-- sulla tabella messages mentre la chat è in uso.
-- Ritorna quanti messaggi ha eliminato.

CREATE OR REPLACE FUNCTION pulisci_scaduti(blocco INTEGER DEFAULT 5000)
RETURNS TABLE(messaggi_eliminati BIGINT, file_accodati BIGINT)
LANGUAGE plpgsql
AS $$
DECLARE
    v_msg  BIGINT := 0;
    v_file BIGINT := 0;
BEGIN
    -- Prima i file: vanno accodati PRIMA che il CASCADE
    -- cancelli le righe di attachments, altrimenti perdiamo
    -- il percorso e il file resta orfano nel bucket.
    WITH scaduti AS (
        SELECT a.storage_path
        FROM attachments a
        JOIN messages m ON m.id = a.message_id
        JOIN channels c ON c.id = m.channel_id
        JOIN spaces  s ON s.id = c.space_id
        WHERE s.retention_days IS NOT NULL
          AND m.created_at < now() - (s.retention_days || ' days')::interval
        LIMIT blocco
    ), inseriti AS (
        INSERT INTO storage_da_eliminare (storage_path)
        SELECT storage_path FROM scaduti
        RETURNING 1
    )
    SELECT count(*) INTO v_file FROM inseriti;

    -- Poi i messaggi. Il CASCADE porta via mentions e attachments.
    WITH scaduti AS (
        SELECT m.id
        FROM messages m
        JOIN channels c ON c.id = m.channel_id
        JOIN spaces  s ON s.id = c.space_id
        WHERE s.retention_days IS NOT NULL
          AND m.created_at < now() - (s.retention_days || ' days')::interval
        LIMIT blocco
    ), eliminati AS (
        DELETE FROM messages WHERE id IN (SELECT id FROM scaduti)
        RETURNING 1
    )
    SELECT count(*) INTO v_msg FROM eliminati;

    -- read_state può puntare a messaggi non più esistenti:
    -- non è un errore (è solo un numero), ma va riallineato
    -- o i badge "non letti" impazziscono.
    UPDATE read_state rs
    SET last_read_msg_id = COALESCE(
        (SELECT MIN(id) - 1 FROM messages WHERE channel_id = rs.channel_id), 0)
    WHERE rs.last_read_msg_id < COALESCE(
        (SELECT MIN(id) - 1 FROM messages WHERE channel_id = rs.channel_id), 0);

    RETURN QUERY SELECT v_msg, v_file;
END;
$$;


-- ---------------------------------------------------------
-- 5. Programmazione con pg_cron
-- ---------------------------------------------------------
-- Su Supabase pg_cron va prima abilitata:
--   Dashboard -> Database -> Extensions -> cerca "pg_cron" -> Enable
--
-- Poi esegui il blocco qui sotto. Gira alle 4 del mattino UTC.
-- Il vantaggio rispetto a un cron esterno: vive nel database,
-- quindi funziona anche quando l'app su Render è sospesa.

-- Scommenta dopo aver abilitato l'estensione:

-- SELECT cron.schedule(
--     'flachat-pulizia',
--     '0 4 * * *',
--     $$SELECT pulisci_scaduti(5000)$$
-- );

-- Per verificare che sia programmato:
--   SELECT * FROM cron.job;
-- Per vedere le ultime esecuzioni:
--   SELECT * FROM cron.job_run_details ORDER BY start_time DESC LIMIT 10;
-- Per rimuoverlo:
--   SELECT cron.unschedule('flachat-pulizia');
