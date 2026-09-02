"""
Logica di decisione del Backend (AI-Driven Mode).
Prende le decisioni operative strutturate dall'Agente centrale (AI)
ed esegue l'aggiornamento dei dati raccolti e l'attivazione dei comandi tecnici.
"""

from copy import deepcopy
from app.constants import (
    WORKFLOW_IDLE, WORKFLOW_BOOKING,
    STEP_NONE, STEP_SHOWING_SLOTS, STEP_COMPLETED,
    N8N_ACTION_SEARCH_AVAILABILITY, N8N_ACTION_CREATE_BOOKING
)


def decide(agent_result: dict, conversation: dict) -> dict:
    """
    Esegue le direttive strutturate emesse dall'Agente AI.
    """
    # 1. Recupero degli stati decisi dall'IA
    new_workflow = agent_result.get("new_workflow", WORKFLOW_IDLE)
    new_step = agent_result.get("new_step", STEP_NONE)
    whatsapp_reply = agent_result.get("whatsapp_reply", "")
    
    backend_action = agent_result.get("backend_action") or {}
    command = backend_action.get("command")
    parameters = backend_action.get("parameters") or {}

    # 2. Caricamento dei vecchi dati raccolti
    collected = conversation.get("collected_data") or {}
    updated = deepcopy(collected)

    # 3. Allineamento del dizionario 'collected_data' in base alle istruzioni dell'IA
    # CASO A: RICERCA DISPONIBILITÀ (Con pulizia radicale dei vecchi rimasugli)
    if command == N8N_ACTION_SEARCH_AVAILABILITY:
        updated["slot_context_status"] = "searching"
        updated["last_slots"] = []  # Svuota i vecchi slot per la nuova ricerca
        
        # Sovrascriviamo interamente le preferenze per evitare che vecchi "period" sporchino il motore locale
        updated["preferences"] = {
            "date_from": parameters.get("date_from"),
            "date_to": parameters.get("date_to"),
            "time_preference": parameters.get("time_preference"),
            "exact_time": parameters.get("exact_time"),
            
            # Azzeriamo le chiavi legacy che ingannavano gli if/elif del motore locale
            "date": None,
            "period": None,
            "weekday": None,
            "ignore_preferences": None
        }
        
        if parameters.get("service"):
            updated["service"] = parameters.get("service")

    # CASO B: CONFERMA E CREAZIONE PRENOTAZIONE
    elif command == N8N_ACTION_CREATE_BOOKING:
        if parameters.get("person_name"):
            updated["person_name"] = parameters.get("person_name")
        if parameters.get("selected_slot"):
            updated["selected_slot"] = parameters.get("selected_slot")
        updated["slot_context_status"] = "completed"

    # CASO C: NESSUNA AZIONE TECNICA (Conversazione fluida, saluti o Abbandono)
    else:
        if parameters.get("service"):
            updated["service"] = parameters.get("service")
        if parameters.get("person_name"):
            updated["person_name"] = parameters.get("person_name")
        if parameters.get("slot_number") is not None:
            updated["slot_number"] = parameters.get("slot_number")
        if parameters.get("selected_time"):
            updated["selected_time"] = parameters.get("selected_time")
            
        # Se l'IA ha deciso di tornare in IDLE (Abbandono o Ringraziamento finale),
        # puliamo i filtri di ricerca ma non distruggiamo l'oggetto per non interrompere il thread di main.py
        if new_workflow == WORKFLOW_IDLE and new_step == STEP_NONE:
            updated = {"status": "reset_completed"}

    # 4. Costruzione del dizionario di decisione universale per la pipeline
    action_type = "call_n8n" if command else "reply_template"
    
    decision = {
        "workflow": new_workflow,
        "step": new_step,
        "action": action_type,
        "template_key": "custom_reply" if whatsapp_reply else "unclear",
        "is_lateral": False,
        "change_workflow": conversation.get("workflow") != new_workflow,
        "message_hint": None,
        "updated_collected": updated,
        "n8n_action": command,
        "whatsapp_reply_override": whatsapp_reply if whatsapp_reply else "Operazione completata."
    }

    return decision
