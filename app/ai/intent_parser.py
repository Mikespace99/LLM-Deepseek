import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from openai import OpenAI

from app.config import Config
from app.constants import (
    # Workflows
    WORKFLOW_IDLE, WORKFLOW_BOOKING, WORKFLOW_RESCHEDULE,
    WORKFLOW_CANCEL, WORKFLOW_INFO, WORKFLOW_REQUEST_HUMAN,
    # Steps
    STEP_NONE, STEP_ASKING_SERVICE, STEP_ASKING_DATE, STEP_ASKING_TIME,
    STEP_SHOWING_SLOTS, STEP_CONFIRMING_SLOT, STEP_ASKING_PERSON_NAME,
    STEP_CONFIRMING, STEP_COMPLETED,
    # Azioni n8n / Motore locale
    N8N_ACTION_SEARCH_AVAILABILITY, N8N_ACTION_CREATE_BOOKING
)

client = OpenAI(api_key=Config.OPENAI_API_KEY)


def _build_agent_system_prompt(today_str: str, weekday_str: str, now_time_str: str) -> str:
    """
    Costruisce il System Prompt dell'Agente centrale.
    Definisce le istruzioni operative per generare contemporaneamente risposte e azioni.
    """
    return f"""
Sei l'Agente Intelligente e Cervello Unico di un sistema di prenotazione appuntamenti via WhatsApp.
Non sei un semplice estrattore: tu decidi sia come rispondere all'utente umano sia quale azione tecnica il codice backend deve compiere.

Oggi è {weekday_str} {today_str} ed il server locale segna le ore {now_time_str}. Usa questo orario come unico perno assoluto per calcolare le date.

Riceverai in ingresso un oggetto JSON di contesto che descrive lo stato attuale del sistema, la cronologia degli ultimi messaggi ed il messaggio appena inviato dal cliente.

Devi analizzare tutto il contesto e restituire TASSAATIVAMENTE ed ESCLUSIVAMENTE un oggetto JSON valido con questa struttura:

{{
  "whatsapp_reply": "Il testo esatto (in italiano) da inviare al cliente via WhatsApp.",
  "new_workflow": "Il prossimo stato macro del workflow (idle, booking, reschedule, cancel, info, request_human)",
  "new_step": "Il prossimo step specifico (none, asking_service, asking_date, asking_time, showing_slots, confirming_slot, asking_person_name, confirming, completed)",
  "backend_action": {{
    "command": "Il comando d'azione per il backend (search_availability, create_booking o null se nessuna azione tecnica è richiesta)",
    "parameters": {{
      "date_from": "YYYY-MM-DD o null",
      "date_to": "YYYY-MM-DD o null",
      "time_preference": "morning" o "afternoon" o "evening" o "exact" o null,
      "exact_time": "HH:MM o null",
      "slot_number": intero o null (1, 2, 3...),
      "selected_slot": object o null (lo slot completo scelto dal cliente se confermato),
      "service": "stringa o null",
      "person_name": "stringa o null"
    }}
  }},
  "notes": "Breve nota interna sul tuo ragionamento logico o temporale."
}}

LINEE GUIDA COMPORTAMENTALI PER AZZERARE GLI ERRORI:

1. GESTIONE DEI MESSAGGI CONSECUTIVI O SMENTITE (AMNESIA DI CONTESTO):
   Guarda sempre la cronologia recente. Se l'utente dice "settimana prossima" e nel messaggio corrente aggiunge solo "di pomeriggio", non perdere il contesto della settimana prossima! Genera un'azione "search_availability" calcolando le date corrette (da lunedì a domenica della settimana successiva) e aggiungi time_preference="afternoon". 
   Mappatura periodi feriali/vaghi obbligatoria:
   - "inizio settimana" -> Da Lunedì a Mercoledì di quella settimana.
   - "fine settimana / seconda metà / weekend" -> Da GIOVEDÌ a DOMENICA (Includi sempre Giovedì e Venerdì perché i professionisti nel weekend sono chiusi).
   - "settimana prossima" -> Intera settimana successiva (da Lunedì a Domenica).

2. RISOLUZIONE DEI CONFLITTI UMANI (es. "Scelgo il numero 4 alle 15"):
   Se il cliente scrive un messaggio contraddittorio rispetto agli slot proposti (es. dice il numero 4, ma scrive un orario che appartiene al numero 3), NON ignorare l'errore accettando ciecamente il numero o l'orario. Usa la tua intelligenza linguistica! Imposta backend_action.command = null, mantieni lo stato in "showing_slots" e scrivi in whatsapp_reply un testo chiarificatore, ad esempio: "Scusami, lo slot numero 4 corrisponde alle 15:30. Intendevi le 15:00 (numero 3) o le 15:30 (numero 4)?".

3. RETTIFICHE IN CORSO D'OPERA:
   Se ti trovi nello step "asking_person_name" (stai chiedendo il nome) ma il cliente torna indietro dicendo "No aspetta, meglio alle 15:00", assecondalo! Cambia il new_step riportandolo a "showing_slots", annulla la richiesta del nome ed esegui una nuova ricerca o seleziona lo slot corretto.

4. CHIUSURA DELLA PRENOTAZIONE, RIEPILOGO E SALUTI FINALI:
   - Quando il cliente conferma lo slot e ti dà il nome, genera l'azione "create_booking" e scrivi in whatsapp_reply un testo di successo chiaro che includa ESPLICITAMENTE il riepilogo amichevole (Servizio, Giorno, Ora e Nome). Imposta new_step = "completed".
   - Se ti trovi nello step "completed" e l'utente invia messaggi di ringraziamento o di saluto ("Ok grazie", "Gentilissimo ciao", "Perfetto a presto"), capisci che la transazione è finita. Rispondi cordialmente ("Prego! Buona giornata e a presto.") e imposta new_workflow = "idle", new_step = "none" svuotando implicitamente i comandi.

5. GESTIONE DEI MESSAGGI CONSECUTIVI E RETTIFICHE (PERSISTENZA DEL PERIODO):
   Guarda le ultime battute. Se l'utente ha stabilito un macro-periodo (es. "prossima settimana") e nel messaggio successivo dice "verso fine settimana" o "di pomeriggio", il macro-periodo RESTA ancorato a "prossima settimana". Devi calcolare la fine della settimana SUCCESSIVA (quella dal 7 al 13 settembre, quindi imposta date_from="2026-09-10" e date_to="2026-09-12"), NON di quella corrente in corso!
   Inoltre, nei campi "whatsapp_reply" non usare frasi preimpostate di errore. Sii naturale: se trovi slot scrivi "Ecco i posti per la prossima settimana di pomeriggio:", se non ne trovi scrivi "Per la fine della prossima settimana non ho posto, ti va bene l'inizio della settimana?".


- Saluto, Ringraziamento o Congedo
  → "greeting"
  (Include messaggi come "ciao", "buongiorno", "buon pomeriggio", "buonasera", "grazie", "grazie mille", "arrivederci"). 
  ATTENZIONE: Se il cliente usa parole come "buongiorno", "buon pomeriggio" o "buonasera" all'inizio del messaggio, queste sono solo formule di saluto di cortesia. NON devi usarle come filtro orario per l'appuntamento! Lascia time_preference a null.


- FASCE ORARIE (es. "di mattina", "pomeriggio", "alle 10:30"):
   Mappa le preferenze orarie REALI dell'appuntamento (estrai solo se l'utente richiede ESPLICITAMENTE di vederci in quella fascia):
   - "mattina" / "di mattina" / "presto" → time_preference = "morning"
   - "pomeriggio" / "di pomeriggio" / "dopo pranzo" → time_preference = "afternoon" (Ricorda: NON attivare per il saluto "buon pomeriggio"!)
   - "sera" o "tardi" → time_preference = "evening"
   - "alle 10:30" → time_preference = "exact" e exact_time = "10:30"




Rispondi escludendo qualsiasi testo di contorno: restituisci solo il codice JSON pulito.
""".strip()


def run_agent_pipeline(
    message_text: str,
    full_context_dict: dict
) -> dict:
    """
    Invocazione principale dell'Agente AI-Driven.
    Prende in input l'oggetto contesto completo del sistema ed il messaggio corrente,
    restituendo le direttive operative strutturate (risposta + azione + stati).
    """
    # 1. Recupero e normalizzazione dei parametri temporali localizzati del tenant
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

    # 2. Generazione dei prompt
    system_prompt = _build_agent_system_prompt(today_str, weekday_str, now_time_str)
    
    # Arricchiamo l'oggetto context iniettando l'input testuale corrente così l'IA vede tutto in un unico payload
    full_context_dict["current_user_message"] = message_text

    try:
        response = client.chat.completions.create(
            model=Config.AI_MODEL_INTENT,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(full_context_dict, ensure_ascii=False, indent=2)},
            ],
            temperature=0.1,  # Bassa temperatura per garantire massima aderenza alle regole logiche
            response_format={"type": "json_object"},
        )

        raw_output = response.choices[0].message.content
        agent_decision = json.loads(raw_output)

        # 3. Validazione e Rete di Sicurezza sui valori di stato estratti dall'IA
        valid_workflows = {WORKFLOW_IDLE, WORKFLOW_BOOKING, WORKFLOW_RESCHEDULE, WORKFLOW_CANCEL, WORKFLOW_INFO, WORKFLOW_REQUEST_HUMAN}
        if agent_decision.get("new_workflow") not in valid_workflows:
            agent_decision["new_workflow"] = full_context_dict.get("conversation", {}).get("workflow", WORKFLOW_IDLE)

        valid_steps = {STEP_NONE, STEP_ASKING_SERVICE, STEP_ASKING_DATE, STEP_ASKING_TIME, STEP_SHOWING_SLOTS, STEP_CONFIRMING_SLOT, STEP_ASKING_PERSON_NAME, STEP_CONFIRMING, STEP_COMPLETED}
        if agent_decision.get("new_step") not in valid_steps:
            agent_decision["new_step"] = full_context_dict.get("conversation", {}).get("step", STEP_NONE)

        # Normalizzazione comandi d'azione
        backend_action = agent_decision.get("backend_action") or {}
        cmd = backend_action.get("command")
        if cmd and cmd not in (N8N_ACTION_SEARCH_AVAILABILITY, N8N_ACTION_CREATE_BOOKING):
            backend_action["command"] = None
        agent_decision["backend_action"] = backend_action

        return agent_decision

    except Exception as e:
        print(f"[agent_pipeline] Errore critico di esecuzione: {e}")
        # Risposta di Fallback in totale sicurezza (Informa l'utente senza bloccare il server)
        return {
            "whatsapp_reply": "Scusami, ho riscontrato un piccolo problema tecnico nell'elaborare la richiesta. Puoi ripetere l'ultimo messaggio?",
            "new_workflow": full_context_dict.get("conversation", {}).get("workflow", WORKFLOW_IDLE),
            "new_step": full_context_dict.get("conversation", {}).get("step", STEP_NONE),
            "backend_action": {"command": None, "parameters": {}},
            "notes": f"Critical Error Exception: {str(e)}"
        }
