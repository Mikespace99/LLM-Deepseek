import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from openai import OpenAI

from app.config import Config
from app.constants import (
    WORKFLOW_IDLE, WORKFLOW_BOOKING, WORKFLOW_RESCHEDULE,
    WORKFLOW_CANCEL, WORKFLOW_INFO, WORKFLOW_REQUEST_HUMAN,
    STEP_NONE, STEP_ASKING_SERVICE, STEP_ASKING_DATE, STEP_ASKING_TIME,
    STEP_SHOWING_SLOTS, STEP_CONFIRMING_SLOT, STEP_ASKING_PERSON_NAME,
    STEP_CONFIRMING, STEP_COMPLETED,
    N8N_ACTION_SEARCH_AVAILABILITY, N8N_ACTION_CREATE_BOOKING,
    INTENT_UNCLEAR
)

client = OpenAI(api_key=Config.OPENAI_API_KEY)

# ============================================================
# SYSTEM PROMPT DELL'AGENTE (VERSIONE SFOLTITA E BONIFICATA)
# ============================================================

def _build_agent_system_prompt(today_str: str, weekday_str: str, now_time_str: str) -> str:
    """
    Costruisce il System Prompt dell'Agente centrale.
    Definisce le istruzioni operative per generare contemporaneamente risposte e azioni.
    """
    return f"""
Sei l'Agente Intelligente e Cervello Unico di un sistema di prenotazione appuntamenti via WhatsApp.
Il tuo UNICO compito è capire cosa vuole fare l'utente e tradurre le richieste temporali umane in finestre di date (date_from/date_to).

Tu NON gestisci il calendario, NON sai quali slot sono liberi o pieni e NON devi MAI inventare o scrivere elenchi di orari nella tua risposta. 
Tu generi solo la frase di cortesia iniziale, delegando al backend il compito di mostrare i dati reali.

Oggi è {weekday_str} {today_str} ed il server locale segna le ore {now_time_str}. Usa questo orario come perno.

Restituisci SOLO un JSON valido con questa struttura:
{{
  "whatsapp_reply": "Frase breve di cortesia ed empatia in italiano (es. 'Controllo subito i posti per la prossima settimana' oppure 'Perfetto, confermo l'appuntamento richiesto'). Non scrivere mai elenchi di slot qui o frasi di errore sui calendari!",
  "new_workflow": "uno dei valori ammessi (idle, booking, reschedule, cancel, info, request_human)",
  "new_step": "uno dei valori ammessi (none, asking_service, asking_date, asking_time, showing_slots, confirming_slot, asking_person_name, confirming, completed)",
  "backend_action": {{
    "command": "search_availability" o "create_booking" o null,
    "parameters": {{
      "date_from": "YYYY-MM-DD o null",
      "date_to": "YYYY-MM-DD o null",
      "time_preference": "morning" o "afternoon" o "evening" o "exact" o null,
      "exact_time": "HH:MM o null",
      "slot_number": intero o null (se l'utente dice un numero, es. 1, 2, 3...),
      "service": "stringa o null",
      "person_name": "stringa o null"
    }}
  }},
  "notes": "Breve nota interna sul tuo ragionamento temporale."
}}

LINEE GUIDA COMPORTAMENTALI PER AZZERARE GLI ERRORI:

1. GESTIONE DEI MESSAGGI CONSECUTIVI E RETTIFICHE (PERSISTENZA DEL PERIODO):
   Guarda le ultime battute. Se l'utente ha stabilito un macro-periodo (es. "prossima settimana") e nel messaggio successivo dice "verso fine settimana" o "di pomeriggio", il macro-periodo RESTA ancorato a "prossima settimana". Devi calcolare l'intervallo esatto richiesto integrando il passato.
   Mappatura periodi feriali/vaghi obbligatoria:
   - "inizio settimana" -> Da Lunedì a Mercoledì di quella settimana.
   - "metà settimana" -> Da Martedì a Giovedì di quella settimana.
   - "fine settimana / seconda metà / weekend" -> Da GIOVEDÌ a DOMENICA (Includi sempre Giovedì e Venerdì perché i professionisti nel weekend sono chiusi).
   - "settimana prossima" -> Intera settimana successiva (da Lunedì a Domenica).

2. SELEZIONE DELLO SLOT:
   Se l'utente seleziona un orario o un numero (es. 'lunedì alle 16' oppure 'il numero 5'), limita a estrarre quel dato in exact_time o slot_number, imposta il comando a 'create_booking' (o search_availability se applicabile). NON dire mai che lo slot non è disponibile di testa tua! Ci penserà il sistema a verificarlo.

3. REGOLE SUI SALUTI DI CORTESIA:
   Se il cliente usa formule come 'buon pomeriggio', 'buongiorno' o 'buonasera' all'inizio del messaggio, sono solo saluti di cortesia. NON devi usarle come filtro orario per l'appuntamento! Lascia time_preference a null, a meno che non venga chiesto esplicitamente come vincolo per l'incontro (es. 'vengo di pomeriggio').

Restituisci SOLO il JSON, nient'altro.
""".strip()


# ============================================================
# PIPELINE DI ESECUZIONE AGENTE
# ============================================================

def run_agent_pipeline(
    message_text: str,
    full_context_dict: dict
) -> dict:
    """
    Invocazione principale dell'Agente AI-Driven.
    Prende in input l'oggetto contesto completo del sistema ed il messaggio corrente.
    """
    tenant_ctx = full_context_dict.get("tenant") or {}
    timezone_str = tenant_ctx.get("timezone", "Europe/Rome")
    
    try:
        tz = ZoneInfo(timezone_str)
    except Exception:
        tz = timezone.utc

    now = datetime.now(tz)
    today_str = now.strftime("%Y-%m-%d")
    
    weekday_map = {
        0: "lunedì", 1: "martedì", 2: "mercoledì", 3: "giovedì",
        4: "venerdì", 5: "sabato", 6: "domenica"
    }
    weekday_str = weekday_map[now.weekday()]
    now_time_str = now.strftime("%H:%M")

    # Chiamata corretta con i tre parametri richiesti dalla firma
    system_prompt = _build_agent_system_prompt(today_str, weekday_str, now_time_str)
    
    full_context_dict["current_user_message"] = message_text

    try:
        response = client.chat.completions.create(
            model=Config.AI_MODEL_INTENT,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(full_context_dict, ensure_ascii=False, indent=2)},
            ],
            temperature=0.1,
            response_format={"type": "json_object"},
        )

        raw_output = response.choices[0].message.content
        agent_decision = json.loads(raw_output)

        return agent_decision

    except Exception as e:
        print(f"[agent_pipeline] Errore critico di esecuzione: {e}")
        return {
            "whatsapp_reply": "Scusami, ho riscontrato un piccolo problema tecnico. Puoi ripetere l'ultimo messaggio?",
            "new_workflow": full_context_dict.get("conversation", {}).get("workflow", WORKFLOW_IDLE),
            "new_step": full_context_dict.get("conversation", {}).get("step", STEP_NONE),
            "backend_action": {"command": None, "parameters": {}},
            "notes": f"Critical Error Exception: {str(e)}"
        }
