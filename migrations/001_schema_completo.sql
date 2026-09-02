-- ============================================================
-- AI Booking – Ricreazione completa del database (multi-tenant)
-- ============================================================
-- ATTENZIONE — SCRIPT DISTRUTTIVO:
-- Cancella ED elimina tutte le tabelle elencate sotto (compresa la
-- vecchia "appointments" single-tenant con studio_id/start_time/
-- end_time testuali, e "studio_config", entrambe residuo del
-- prototipo pre-multi-tenant) e le ricrea da zero nella versione
-- multi-tenant corretta, allineata al codice attuale del backend
-- (app/repositories/appointment.py, app/booking/engine.py,
-- app/web/routes.py).
--
-- Usalo solo su un database che puoi svuotare (es. prima del primo
-- deploy in produzione, o su un progetto Supabase di test). NON
-- eseguirlo se hai già dati reali che vuoi conservare.
-- ============================================================


-- ------------------------------------------------------------
-- ESTENSIONI
-- ------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS btree_gist; -- vincolo anti-sovrapposizione su appointments


-- ============================================================
-- PARTE 0 — DROP di tutto (ordine inverso alle dipendenze, CASCADE
-- per sicurezza anche se l'ordine fosse impreciso)
-- ============================================================
DROP TABLE IF EXISTS public.appointments CASCADE;
DROP TABLE IF EXISTS public.studio_config CASCADE;      -- legacy, non più usata
DROP TABLE IF EXISTS public.tenant_holidays CASCADE;
DROP TABLE IF EXISTS public.calendar_exceptions CASCADE;
DROP TABLE IF EXISTS public.conversations CASCADE;
DROP TABLE IF EXISTS public.working_hours CASCADE;
DROP TABLE IF EXISTS public.services CASCADE;
DROP TABLE IF EXISTS public.customers CASCADE;
DROP TABLE IF EXISTS public.locations CASCADE;
DROP TABLE IF EXISTS public.tenants CASCADE;
DROP TABLE IF EXISTS public.holidays CASCADE;


-- ============================================================
-- PARTE 1 — TABELLE (ordine corretto per dipendenze)
-- ============================================================

-- ------------------------------------------------------------
-- holidays — festività nazionali (nessuna dipendenza)
-- ------------------------------------------------------------
CREATE TABLE public.holidays (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  country text NOT NULL DEFAULT 'IT'::text,
  date date NOT NULL,
  name text NOT NULL,
  CONSTRAINT holidays_pkey PRIMARY KEY (id)
);

-- ------------------------------------------------------------
-- tenants — lo studio/professionista
-- ------------------------------------------------------------
CREATE TABLE public.tenants (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  business_name text NOT NULL,
  assistant_name text DEFAULT 'Assistente'::text,
  timezone text DEFAULT 'Europe/Rome'::text,
  language text DEFAULT 'it'::text,
  slot_search_days integer DEFAULT 30,
  info jsonb DEFAULT '{}'::jsonb,
  created_at timestamp with time zone DEFAULT now(),
  updated_at timestamp with time zone DEFAULT now(),
  owner_id uuid,
  specialty text,
  min_lead_hours integer DEFAULT 2,
  max_appointments_per_day integer DEFAULT 12,
  google_calendar_id text,
  onboarding_completed boolean DEFAULT false,
  CONSTRAINT tenants_pkey PRIMARY KEY (id),
  CONSTRAINT tenants_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES auth.users(id)
);

-- ------------------------------------------------------------
-- locations — sedi dello studio
-- ------------------------------------------------------------
CREATE TABLE public.locations (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  name text NOT NULL,
  city text,
  address text,
  active boolean DEFAULT true,
  sort_order integer DEFAULT 0,
  created_at timestamp with time zone DEFAULT now(),
  updated_at timestamp with time zone DEFAULT now(),
  CONSTRAINT locations_pkey PRIMARY KEY (id),
  CONSTRAINT locations_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)
);

-- ------------------------------------------------------------
-- customers — clienti dello studio
-- ------------------------------------------------------------
CREATE TABLE public.customers (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  phone_number text NOT NULL,
  full_name text,
  email text,
  created_at timestamp with time zone DEFAULT now(),
  updated_at timestamp with time zone DEFAULT now(),
  CONSTRAINT customers_pkey PRIMARY KEY (id),
  CONSTRAINT customers_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)
);

-- ------------------------------------------------------------
-- services — servizi offerti (durata, prezzo, buffer)
-- ------------------------------------------------------------
CREATE TABLE public.services (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  name text NOT NULL,
  duration_minutes integer DEFAULT 30,
  price numeric,
  active boolean DEFAULT true,
  created_at timestamp with time zone DEFAULT now(),
  updated_at timestamp with time zone DEFAULT now(),
  description text,
  buffer_before integer DEFAULT 0,
  buffer_after integer DEFAULT 5,
  sort_order integer DEFAULT 0,
  CONSTRAINT services_pkey PRIMARY KEY (id),
  CONSTRAINT services_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)
);

-- ------------------------------------------------------------
-- working_hours — orari di apertura per sede/giorno
-- ------------------------------------------------------------
CREATE TABLE public.working_hours (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  day_of_week integer NOT NULL CHECK (day_of_week >= 1 AND day_of_week <= 7),
  start_time time without time zone NOT NULL,
  end_time time without time zone NOT NULL,
  created_at timestamp with time zone DEFAULT now(),
  updated_at timestamp with time zone DEFAULT now(),
  location_id uuid,
  active boolean DEFAULT true,
  CONSTRAINT working_hours_pkey PRIMARY KEY (id),
  CONSTRAINT working_hours_location_id_fkey FOREIGN KEY (location_id) REFERENCES public.locations(id),
  CONSTRAINT working_hours_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)
);

-- ------------------------------------------------------------
-- conversations — stato della conversazione WhatsApp (buffer, step)
-- ------------------------------------------------------------
CREATE TABLE public.conversations (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  customer_id uuid NOT NULL,
  phone_number text NOT NULL,
  status text NOT NULL DEFAULT 'active'::text,
  workflow text NOT NULL DEFAULT 'idle'::text,
  step text NOT NULL DEFAULT 'none'::text,
  collected_data jsonb DEFAULT '{}'::jsonb,
  recent_messages jsonb DEFAULT '[]'::jsonb,
  retry_count integer DEFAULT 0,
  timeout_at timestamp with time zone,
  last_message_at timestamp with time zone,
  created_at timestamp with time zone DEFAULT now(),
  closed_at timestamp with time zone,
  close_reason text,
  CONSTRAINT conversations_pkey PRIMARY KEY (id),
  CONSTRAINT conversations_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id),
  CONSTRAINT conversations_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(id)
);

-- ------------------------------------------------------------
-- calendar_exceptions — chiusure/pause straordinarie
-- ------------------------------------------------------------
CREATE TABLE public.calendar_exceptions (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  date date NOT NULL,
  type text NOT NULL CHECK (type = ANY (ARRAY['closed'::text, 'closed_period'::text])),
  start_time time without time zone,
  end_time time without time zone,
  reason text,
  active boolean DEFAULT true,
  created_at timestamp with time zone DEFAULT now(),
  CONSTRAINT calendar_exceptions_pkey PRIMARY KEY (id),
  CONSTRAINT calendar_exceptions_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)
);

-- ------------------------------------------------------------
-- tenant_holidays — festività abilitate per tenant
-- ------------------------------------------------------------
CREATE TABLE public.tenant_holidays (
  tenant_id uuid NOT NULL,
  holiday_id uuid NOT NULL,
  enabled boolean DEFAULT true,
  CONSTRAINT tenant_holidays_pkey PRIMARY KEY (tenant_id, holiday_id),
  CONSTRAINT tenant_holidays_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id),
  CONSTRAINT tenant_holidays_holiday_id_fkey FOREIGN KEY (holiday_id) REFERENCES public.holidays(id)
);

-- ------------------------------------------------------------
-- appointments — prenotazioni, multi-tenant, con i campi per
-- l'Agenda locale già inclusi (sostituisce sia la versione legacy
-- single-tenant sia n8n/Google Calendar come fonte di "occupato")
-- ------------------------------------------------------------
CREATE TABLE public.appointments (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  customer_id uuid,
  phone_number text,
  service text,
  service_id uuid,
  location_id uuid,
  appointment_date date NOT NULL,
  appointment_time time without time zone NOT NULL,
  duration_minutes integer NOT NULL DEFAULT 30,
  status text NOT NULL DEFAULT 'confirmed'::text
    CHECK (status IN ('confirmed', 'cancelled', 'completed', 'no_show')),
  source text NOT NULL DEFAULT 'whatsapp'::text
    CHECK (source IN ('whatsapp', 'manual', 'block')),
  notes text,
  google_event_id text,
  created_by uuid,
  created_at timestamp with time zone DEFAULT now(),
  updated_at timestamp with time zone DEFAULT now(),

  -- Intervallo temporale locale (naive, senza fuso), calcolato da
  -- data + ora + durata. Usato dal vincolo anti-sovrapposizione sotto.
  -- NB: niente AT TIME ZONE / cast testo->interval qui dentro: sono
  -- entrambi STABLE in Postgres, non IMMUTABLE, e verrebbero rifiutati
  -- in una colonna generata (make_interval() invece lo è).
  time_range tsrange GENERATED ALWAYS AS (
    tsrange(
      (appointment_date + appointment_time),
      (appointment_date + appointment_time) + make_interval(mins => duration_minutes),
      '[)'
    )
  ) STORED,

  CONSTRAINT appointments_pkey PRIMARY KEY (id),
  CONSTRAINT appointments_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id),
  CONSTRAINT appointments_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(id),
  CONSTRAINT appointments_location_id_fkey FOREIGN KEY (location_id) REFERENCES public.locations(id),
  CONSTRAINT appointments_service_id_fkey FOREIGN KEY (service_id) REFERENCES public.services(id),
  CONSTRAINT appointments_created_by_fkey FOREIGN KEY (created_by) REFERENCES auth.users(id) ON DELETE SET NULL,

  -- Vincolo anti-sovrapposizione: nello stesso tenant, due righe
  -- 'confirmed' non possono avere time_range che si sovrappongono.
  -- Volutamente NON differenziato per sede: il motore di disponibilità
  -- tratta gli impegni come un unico calendario condiviso per tutto lo
  -- studio, indipendentemente dalla sede. Se in futuro avrai
  -- professionisti/calendari indipendenti per sede, aggiungi una
  -- colonna "resource_id" e includila qui.
  CONSTRAINT appointments_no_overlap EXCLUDE USING gist (
    tenant_id WITH =,
    time_range WITH &&
  ) WHERE (status = 'confirmed')
);

COMMENT ON COLUMN public.appointments.duration_minutes IS
  'Durata effettiva riservata (servizio + buffer, o durata libera per i blocchi manuali)';
COMMENT ON COLUMN public.appointments.source IS
  'whatsapp = creato dal bot, manual = creato dallo staff in agenda, block = blocco orario senza cliente';


-- ============================================================
-- PARTE 2 — INDICI
-- ============================================================

CREATE INDEX idx_appointments_tenant_range
  ON public.appointments USING gist (tenant_id, time_range);

-- Supporto alle query per intervallo di date (Agenda/Dashboard/Statistiche:
-- GET /api/agenda/events, /api/dashboard/summary, /api/stats/summary).
CREATE INDEX idx_appointments_tenant_date
  ON public.appointments (tenant_id, appointment_date);

-- Supporto alla ricerca clienti (GET /api/clients).
CREATE INDEX idx_customers_tenant_phone
  ON public.customers (tenant_id, phone_number);
