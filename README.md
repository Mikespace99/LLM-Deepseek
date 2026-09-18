# AI Booking Simple

Sistema di prenotazione appuntamenti via WhatsApp – versione semplificata e lineare.

## Stack

- **Backend**: FastAPI (Python)
- **Database**: Supabase (PostgreSQL)
- **AI**: OpenAI (intent parsing)
- **Automazioni**: n8n (Google Calendar + azioni)
- **Canale**: WhatsApp Cloud API
- **Deploy**: Render

## Struttura

```text
app/
├── main.py                 # Pipeline principale + webhook
├── config.py
├── constants.py
├── decision.py             # Logica workflow/step
├── context/builder.py
├── ai/intent_parser.py
├── repositories/
├── templates/messages.py
├── workflows/n8n_client.py
└── integrations/whatsapp.py
```

## Setup rapido

1. Crea progetto Supabase e lancia `migrations/001_initial_schema.sql`
2. Copia `.env.example` → `.env` e compila le variabili
3. Inserisci almeno un tenant di test (con `info.whatsapp_number`)
4. `pip install -r requirements.txt`
5. `uvicorn app.main:app --reload --port 8000`

## Deploy su Render

- Root Directory: `ai_booking_simple` (o la cartella del progetto)
- Build Command: `pip install -r requirements.txt`
- Start Command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Aggiungi tutte le Environment Variables dalla dashboard

## Flusso

1. Messaggio WhatsApp → webhook
2. Carica/crea conversazione (timeout 15 min)
3. AI#1 estrae intent + preferences
4. Backend decide workflow/step
5. Se serve → chiama n8n (Google Calendar)
6. Risposta con **template** (o AI#2 se necessario)
7. Aggiorna stato + invia WhatsApp

## Prossimi passi

- Landing + Onboarding tenant
- Collegamento Google Calendar da parte del tenant
- Arricchimento template laterali da `tenant.info`
- Reschedule / Cancel completi
