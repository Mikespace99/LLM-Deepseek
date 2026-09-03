import json
from openai import OpenAI
from app.config import Config

client = OpenAI(api_key=Config.OPENAI_API_KEY)

# ============================================================
# STEP 1: COMPRENSIONE INTENTO E CALCOLO FINESTRE TEMPORALI
# ============================================================

def run_step1_analysis(message_text: str, full_context_dict: dict) -> dict:
    """
    Step 1: L'IA agisce come un puro analista freddo. Non scrive testo per l'utente.
    Capisce cosa vuole fare il cliente e calcola le date feriali integrando la cronologia.
    """
    system_prompt = """
Sei un analista di intenzioni per un sistema di prenotazione appuntamenti via WhatsApp.
Il tuo unico compito è decodificare la richiesta del cliente e la cronologia recente, emettendo un comando tecnico per il backend.

Devi restituire TASSATIVAMENTE ed ESCLUSIVAMENTE un JSON valido con questa struttura:
{
  "action_requested": "SEARCH_SLOTS" o "CONFIRM_BOOKING" o "JUST_TALK",
  "parameters": {
    "date_from": "YYYY-MM-DD o null",
    "date_to": "YYYY-MM-DD o null",
    "time_preference": "morning" o "afternoon" o "evening" o "exact" o null,
    "exact_time": "HH:MM o null",
    "slot_number": intero o null (1, 2, 3...),
    "service": "stringa o null",
    "person_name": "stringa o null"
  }
}

REGOLE DI SELEZIONE RIGIDE:
1. GESTIONE CONTESTO E MESSAGGI CONSECUTIVI: Guarda le ultime battute. Se l'utente ha stabilito un macro-periodo (es. "prossima settimana") e nel messaggio corrente aggiunge "di pomeriggio" o "mercoledì", il macro-periodo RESTA ancorato a "prossima settimana". Calcola le date di conseguenza.
2. MAPPATURA PERIODI VAGHI IN ITALIA:
   - "inizio settimana" -> Da Lunedì a Mercoledì di quella settimana.
   - "metà settimana" -> Da Martedì a Giovedì di quella settimana.
   - "fine settimana / weekend" -> Da Giovedì a Sabato (Includi giovedì e venerdì feriali).
   - "settimana prossima" -> Intera settimana successiva (da Lunedì a Domenica).
3. FORMULE DI CORTESIA: Parole come "buongiorno", "buon pomeriggio" o "buonasera" all'inizio del testo sono solo saluti. NON usarle come filtro orario (pomeriggio/mattina), lasciali a null a meno che non sia specificato esplicitamente ("vengo di pomeriggio").
4. ANNULLAMENTI: Se l'utente dice "lascia stare", "annulla tutto" o "non voglio più prenotare", imposta action_requested="JUST_TALK".

Rispondi escludendo qualsiasi testo di contorno, restituisci solo il JSON pulito.
""".strip()

    try:
        response = client.chat.completions.create(
            model=Config.AI_MODEL_INTENT,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(full_context_dict, ensure_ascii=False)},
            ],
            temperature=0.0,  # Zero tolleranza per massima precisione analitica
            response_format={"type": "json_object"},
        )
        return json.loads(response.choices.message.content)
    except Exception as e:
        print(f"[AI STEP 1 ERROR] {e}")
        return {"action_requested": "JUST_TALK", "parameters": {}}


# ============================================================
# STEP 3: GENERAZIONE DELLA RISPOSTA BASATA SULLA VERITÀ REALE
# ============================================================

def run_step3_response(message_text: str, backend_results: dict, history_text: str) -> str:
    """
    Step 3: L'IA riceve la verità matematica dal backend e scrive il testo finale per WhatsApp.
    Segue le linee guida di stile richieste (conciso, professionale) e gestisce le proposte di ripiego.
    """
    system_prompt = """
Sei la segretaria virtuale ufficiale dello studio professionale. Il tuo compito è scrivere la risposta finale da inviare al cliente su WhatsApp.
Devi basarti RIGIDAMENTE sui dati reali che il backend ti fornisce nel payload. Non devi MAI inventare o allucinare la disponibilità di orari.

LE TUE LINEE GUIDA DI STILE:
- **Conciso e Diretto**: Massimo 2-3 frasi per messaggio. I clienti su WhatsApp leggono di fretta.
- **Professionale ed Educato**: Mantieni un tono cordiale, business, pulito ed empatico. Usa il "Tu" o il "Lei" coerentemente con lo storico della chat.
- **Nessun Elenco**: Tu NON devi MAI scrivere elenchi di slot o orari disponibili nel tuo testo. Ci penserà il sistema ad appenderli in automatico sotto il tuo messaggio. Limitati a fare l'introduzione cortese.

GESTIONE DEI RISULTATI DEL CALENDARIO:
1. SE IL BACKEND HA TROVATO APPUNTAMENTI (slot_found = True):
   Scrivi un'introduzione cortese ed empatica riferita al periodo richiesto (es. 'Buongiorno! Certamente, ecco le disponibilità per mercoledì prossimo di pomeriggio:').
2. SE IL BACKEND NON HA TROVATO APPUNTAMENTI (slot_found = False):
   - Controlla se nel dizionario sono presenti degli "slot_storici_proposti_prima". 
   - Se ci sono, sii proattiva! Spiega in modo cordiale che per il nuovo giorno richiesto non c'è posto, e riproponi esplicitamente le opzioni del messaggio precedente (es. 'Purtroppo per mercoledì non ho disponibilità. Le ripropongo le opzioni che avevamo valutato prima:').
   - Se non ci sono nemmeno slot storici, proponi gentilmente di valutare un'altra settimana o un altro mese.
3. SE IL BACKEND CONFERMA IL SUCCESSO DI UN APPUNTAMENTO (booking_success = True):
   Genera un messaggio di successo caloroso e professionale, che contenga il riepilogo testuale esplicito per il cliente (Servizio, Giorno, Ora e Nome dell'intestatario).
4. SE L'UTENTE HA ANNULLATO O SALUTATO (action = JUST_TALK):
   Rispondi salutando cordialmente e confermando che rimani a disposizione.

Restituisci solo il testo fluido della risposta da inviare, senza codice JSON e senza blocchi markdown.
""".strip()

    user_payload = {
        "current_user_message": message_text,
        "chat_history": history_text,
        "backend_real_data": backend_results
    }

    try:
        response = client.chat.completions.create(
            model=Config.AI_MODEL_RESPONSE,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            temperature=0.2, # Leggera flessibilità per rendere il linguaggio naturale ed empatico
        )
        return response.choices.message.content.strip()
    except Exception as e:
        print(f"[AI STEP 3 ERROR] {e}")
        return "Certamente, ecco le disponibilità trovate:"
