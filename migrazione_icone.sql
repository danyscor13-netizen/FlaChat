-- =========================================================
-- FlaChat — icona scelta per ruolo
--
-- Da eseguire nel SQL Editor di Supabase, una volta sola.
-- Non cancella nulla: aggiunge soltanto una colonna.
-- =========================================================

-- Nome del file dentro static/icons/ (per esempio 'capo.svg').
--
-- NULL non vuol dire "nessuna icona": vuol dire "decidi tu". In quel
-- caso il server cerca un file che si chiami come il ruolo, cosi' le
-- stanze che gia' usavano owner.svg / admin.svg / mod.svg continuano a
-- funzionare senza toccare niente.
--
-- Per dire esplicitamente "nessuna icona" si salva la stringa vuota.

ALTER TABLE roles
    ADD COLUMN IF NOT EXISTS icon TEXT;

-- roles.icon puo' contenere tre cose:
--   NULL          -> il file in static/icons/ che si chiama come il ruolo
--   ''            -> nessuna icona
--   'capo.svg'    -> un file di static/icons/
--   'https://...' -> un'immagine caricata su Supabase Storage
