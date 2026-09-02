-- ============================================================
-- AI Booking – Seed festività nazionali italiane
-- ============================================================
-- Da eseguire DOPO 001_schema_completo.sql. Sicuro da rieseguire più
-- volte: usa un vincolo di unicità + ON CONFLICT DO NOTHING.
--
-- Le festività a data fissa vengono generate per gli anni indicati
-- sotto (di default 2025-2033). Pasqua e Pasquetta cambiano data ogni
-- anno: vengono calcolate con l'algoritmo standard di Gauss/Meeus
-- tramite la funzione public.easter_sunday(anno), che resta nel
-- database (è IMMUTABLE, innocua, e può tornare utile in futuro per
-- rigenerare gli anni successivi).
--
-- NB: qui non differenziamo per country oltre 'IT'. Se un domani
-- servissero festività di altri paesi, si aggiunge un blocco analogo
-- con country diverso.
-- ============================================================

-- ------------------------------------------------------------
-- Vincolo di unicità (necessario per l'ON CONFLICT DO NOTHING)
-- ------------------------------------------------------------
ALTER TABLE public.holidays
  DROP CONSTRAINT IF EXISTS holidays_country_date_name_key;
ALTER TABLE public.holidays
  ADD CONSTRAINT holidays_country_date_name_key UNIQUE (country, date, name);

-- ------------------------------------------------------------
-- Funzione: data di Pasqua (algoritmo di Gauss/Meeus, calendario
-- gregoriano). IMMUTABLE: dipende solo dall'anno passato.
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.easter_sunday(y integer)
RETURNS date
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
  a int; b int; c int; d int; e int; f int; g int; h int;
  i int; k int; l int; m int; mon int; day int;
BEGIN
  a := y % 19;
  b := y / 100;
  c := y % 100;
  d := b / 4;
  e := b % 4;
  f := (b + 8) / 25;
  g := (b - f + 1) / 3;
  h := (19*a + b - d - g + 15) % 30;
  i := c / 4;
  k := c % 4;
  l := (32 + 2*e + 2*i - h - k) % 7;
  m := (a + 11*h + 22*l) / 451;
  mon := (h + l - 7*m + 114) / 31;
  day := ((h + l - 7*m + 114) % 31) + 1;
  RETURN make_date(y, mon, day);
END;
$$;

-- ------------------------------------------------------------
-- Anni da coprire per le festività a data fissa
-- ------------------------------------------------------------
DO $$
DECLARE
  yr int;
BEGIN
  FOR yr IN 2025..2033 LOOP

    INSERT INTO public.holidays (country, date, name) VALUES
      ('IT', make_date(yr, 1, 1),   'Capodanno'),
      ('IT', make_date(yr, 1, 6),   'Epifania'),
      ('IT', public.easter_sunday(yr),                      'Pasqua'),
      ('IT', public.easter_sunday(yr) + 1,                   'Pasquetta (Lunedì dell''Angelo)'),
      ('IT', make_date(yr, 4, 25),  'Festa della Liberazione'),
      ('IT', make_date(yr, 5, 1),   'Festa dei Lavoratori'),
      ('IT', make_date(yr, 6, 2),   'Festa della Repubblica'),
      ('IT', make_date(yr, 8, 15),  'Ferragosto'),
      ('IT', make_date(yr, 11, 1),  'Tutti i Santi'),
      ('IT', make_date(yr, 12, 8),  'Immacolata Concezione'),
      ('IT', make_date(yr, 12, 25), 'Natale'),
      ('IT', make_date(yr, 12, 26), 'Santo Stefano')
    ON CONFLICT (country, date, name) DO NOTHING;

  END LOOP;
END $$;
