"""
TABELLA DI ROUTING

(current_step, intent) -> funzione handler.

Aggiungere un dominio nuovo (RESCHEDULE, CANCEL, CHECK_APPOINTMENT,
ASK_INFORMATION) significa SOLO:
  1. creare app/flows/<dominio>.py con le sue funzioni handler
  2. aggiungere le righe corrispondenti qui sotto

Nessuna modifica a router/intent_router.py, context_manager.py, o agli
altri flow: questo e' il punto che rende l'aggiunta di un ramo additiva
e non invasiva.

Ogni handler ha la firma:
    handler(context, tenant=..., knowledge=...) -> (context, SystemResult)
"""

from app.context.models import ConversationStep, Intent
from app.flows import booking

ROUTING_TABLE = {
    # --- Ramo BOOK ---
    (ConversationStep.IDLE, Intent.BOOK): booking.start_search,
    (ConversationStep.WAITING_FOR_SLOT, Intent.CHANGE_PREFERENCE): booking.start_search,
    (ConversationStep.WAITING_FOR_SLOT, Intent.SELECT_SLOT): booking.slot_selected,
    (ConversationStep.COLLECTING_CUSTOMER_DATA, Intent.PROVIDE_DATA): booking.customer_data_provided,
    (ConversationStep.WAITING_FOR_CONFIRMATION, Intent.CONFIRM): booking.confirm_booking,
    (ConversationStep.WAITING_FOR_CONFIRMATION, Intent.REJECT): booking.reject_confirmation,

    # --- Ramo RESCHEDULE (esempio di come apparirà, non ancora implementato) ---
    # (ConversationStep.IDLE, Intent.RESCHEDULE): reschedule.identify_target,
    # (ConversationStep.WAITING_FOR_SLOT, Intent.SELECT_SLOT): reschedule.slot_selected,
    # ... riusa gli stessi handler generici di flows/common.py dove il
    #     comportamento è identico a BOOK (selezione slot, dati cliente).
}
