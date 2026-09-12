from __future__ import annotations

import datetime
from app.context.models import ConversationContext, ConversationStep, Intent, PendingAction, SystemResult
from app.supabase_client import get_supabase


def _get_slots_from_db(params: dict) -> list[dict]:
    """Helper interno per la query degli slot."""
    supabase = get_supabase()
    
    # Query di base sugli slot disponibili
    query = supabase.table("slots").select("id, date, time").eq("status", "AVAILABLE")
    
    # Applica i filtri se presenti
    if params.get("date_from"):
        query = query.gte("date", params["date_from"])
    if params.get("date_to"):
        query = query.lte("date", params["date_to"])
    if params.get("time_from"):
        query = query.gte("time", params["time_from"])
    if params.get("time_to"):
        query = query.lte("time", params["time_to"])
        
    res = query.order("date").order("time").limit(10).execute()
    return res.data or []


def start_search(context: ConversationContext, tenant: dict, knowledge: dict) -> tuple[ConversationContext, SystemResult]:
    """Avvia la ricerca di disponibilità basata sui criteri utente."""
    context.conversation.current_step = ConversationStep.SEARCH_AVAILABILITY
    
    # Costruisce i parametri per la query dal contesto di ricerca
    params = {}
    if context.search.date_from:
        params["date_from"] = context.search.date_from.isoformat()
    else:
        params["date_from"] = datetime.date.today().isoformat()
        
    if context.search.date_to:
        params["date_to"] = context.search.date_to.isoformat()
    if context.search.time_from:
        params["time_from"] = context.search.time_from.isoformat()
    if context.search.time_to:
        params["time_to"] = context.search.time_to.isoformat()

    raw_slots = _get_slots_from_db(params)
    
    # ------------------------------------------------------------------
    # FIX PUNTO 3 (Parte A): Salvataggio Deterministico dei Giorni Mostrati
    # ------------------------------------------------------------------
    if raw_slots:
        unique_days = sorted(list(set([str(slot["date"]) for slot in raw_slots])))
        context.search.displayed_days = unique_days
    else:
        context.search.displayed_days = []

    # Popola gli offered_slots per i bottoni/liste
    context.offered_slots = []
    for i, s in enumerate(raw_slots[:10], start=1):
        context.offered_slots.append({
            "option": i,
            "slot": {
                "id": str(s["id"]),
                "date": s["date"],
                "time": s["time"]
            }
        })
        
    if not raw_slots:
        return context, SystemResult(success=True, data={"no_slots": True})
        
    context.conversation.current_step = ConversationStep.WAITING_FOR_SLOT
    context.conversation.pending_action = PendingAction.SELECT_SLOT
    
    return context, SystemResult(success=True, data={"slots": raw_slots})


def search_more(context: ConversationContext, tenant: dict, knowledge: dict) -> tuple[ConversationContext, SystemResult]:
    """Continua la ricerca spostando le date in avanti."""
    last_date = None
    if context.offered_slots:
        last_date = context.offered_slots[-1]["slot"]["date"]
    elif context.search.date_to:
        last_date = context.search.date_to
    else:
        last_date = datetime.date.today()
        
    if isinstance(last_date, str):
        last_date = datetime.date.fromisoformat(last_date)
        
    start_date = last_date + datetime.timedelta(days=1)
    
    params = {
        "date_from": start_date.isoformat()
    }
    if context.search.time_from:
        params["time_from"] = context.search.time_from.isoformat()
    if context.search.time_to:
        params["time_to"] = context.search.time_to.isoformat()
        
    raw_slots = _get_slots_from_db(params)
    
    # ------------------------------------------------------------------
    # FIX PUNTO 3 (Parte A): Salvataggio Deterministico dei Giorni Mostrati
    # ------------------------------------------------------------------
    if raw_slots:
        unique_days = sorted(list(set([str(slot["date"]) for slot in raw_slots])))
        context.search.displayed_days = unique_days
    else:
        context.search.displayed_days = []

    context.offered_slots = []
    for i, s in enumerate(raw_slots[:10], start=1):
        context.offered_slots.append({
            "option": i,
            "slot": {
                "id": str(s["id"]),
                "date": s["date"],
                "time": s["time"]
            }
        })
        
    if not raw_slots:
        return context, SystemResult(success=True, data={"no_slots": True})
        
    context.conversation.current_step = ConversationStep.WAITING_FOR_SLOT
    context.conversation.pending_action = PendingAction.SELECT_SLOT
    
    return context, SystemResult(success=True, data={"slots": raw_slots})


def customer_data_provided(context: ConversationContext, tenant: dict, knowledge: dict) -> tuple[ConversationContext, SystemResult]:
    """Gestisce l'inserimento dei dati del cliente."""
    if not context.customer.full_name.value:
        context.conversation.pending_action = PendingAction.PROVIDE_NAME
        return context, SystemResult(success=True, data={"missing": "name"})
        
    if not context.customer.phone.value:
        context.conversation.pending_action = PendingAction.PROVIDE_PHONE
        return context, SystemResult(success=True, data={"missing": "phone"})
        
    context.conversation.current_step = ConversationStep.WAITING_FOR_CONFIRMATION
    context.conversation.pending_action = PendingAction.CONFIRM
    
    return context, SystemResult(success=True)


def slot_selected(context: ConversationContext, tenant: dict, knowledge: dict) -> tuple[ConversationContext, SystemResult]:
    """Conferma la selezione dello slot orario e passa alla raccolta dati."""
    context.conversation.current_step = ConversationStep.COLLECTING_CUSTOMER_DATA
    
    if context.customer.full_name.value and context.customer.phone.value:
        context.conversation.current_step = ConversationStep.WAITING_FOR_CONFIRMATION
        context.conversation.pending_action = PendingAction.CONFIRM
    else:
        if not context.customer.full_name.value:
            context.conversation.pending_action = PendingAction.PROVIDE_NAME
        else:
            context.conversation.pending_action = PendingAction.PROVIDE_PHONE
            
    return context, SystemResult(success=True)


def confirm_booking(context: ConversationContext, tenant: dict, knowledge: dict) -> tuple[ConversationContext, SystemResult]:
    """Esegue la scrittura finale dell'appuntamento confermato."""
    context.conversation.current_step = ConversationStep.EXECUTING
    
    # Qui andrebbe la logica di update DB degli slot
    context.booking.status = "CONFIRMED"
    context.conversation.current_step = ConversationStep.COMPLETED
    context.conversation.pending_action = PendingAction.NONE
    
    return context, SystemResult(success=True)
