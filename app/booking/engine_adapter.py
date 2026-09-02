"""
Adapter tra lo stato del ConversationAgent e il booking engine esistente.
"""

from app.booking.engine import search_availability, create_booking
from app.state.manager import StateManager


def search_and_update_state(
    tenant: dict,
    knowledge: dict,
    state: dict,
) -> tuple[dict, dict]:
    """
    Cerca slot basandosi sullo stato e aggiorna lo stato con i risultati.
    Returns: (nuovo_stato, risultato_ricerca)
    """
    collected_data = StateManager.extract_collected_data(state)
    result = search_availability(tenant, knowledge, collected_data)

    new_state = dict(state)
    slots = result.get("candidate_slots", [])
    new_state["slots_shown"] = slots

    if slots:
        new_state["step"] = "showing_slots"
    else:
        new_state["step"] = "no_slots"
        if result.get("result", {}).get("search_was_narrow"):
            new_state["_narrow_search"] = True

    return new_state, result


def create_and_update_state(
    tenant: dict,
    knowledge: dict,
    state: dict,
    customer: dict,
    phone_number: str,
) -> tuple[dict, dict]:
    """
    Crea una prenotazione basandosi sullo stato e aggiorna lo stato.
    Returns: (nuovo_stato, risultato_prenotazione)
    """
    collected_data = StateManager.extract_collected_data(state)
    result = create_booking(tenant, knowledge, collected_data, customer, phone_number)

    new_state = dict(state)
    if result.get("result", {}).get("success"):
        new_state["step"] = "completed"
        new_state["conversation_ended"] = True
    else:
        new_state["step"] = "failed"
        new_state["_last_error"] = result.get("result", {}).get("error")

    return new_state, result
