-- ============================================================
-- Aggiunge la colonna per il nuovo ConversationContext tipizzato
-- (Pydantic), salvato come JSON.
--
-- Additiva e non distruttiva:
--   - nullable, senza default che tocchi le righe esistenti
--   - le colonne usate dalla pipeline attuale (workflow, step,
--     collected_data, recent_messages...) restano invariate e
--     continuano a funzionare esattamente come oggi
--   - permette alle due pipeline (vecchia e nuova) di convivere sulla
--     STESSA riga di "conversations" durante la fase di test
-- ============================================================

ALTER TABLE public.conversations
  ADD COLUMN IF NOT EXISTS context_v2 jsonb;

COMMENT ON COLUMN public.conversations.context_v2 IS
  'ConversationContext tipizzato (nuova pipeline). NULL finché la '
  'conversazione non è mai passata dalla nuova pipeline. Colonna '
  'indipendente da workflow/step/collected_data, usati dalla pipeline '
  'attuale.';
