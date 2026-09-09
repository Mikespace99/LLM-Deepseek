"""
CONTEXT MANAGER

Prende un AI1Result (l'interpretazione del messaggio) e lo applica al
ConversationContext, producendone uno aggiornato.

Principio guida: questo modulo e' "stupido" di proposito. Aggiorna lo
stato in modo deterministico e prevedibile, ma NON decide alcuna azione
di business (non cerca disponibilita', non crea prenotazioni, non
chiama servizi esterni). Quelle decisioni spettano al Router e alla
Business Logic, che leggeranno il contesto gia' aggiornato da qui.

Le uniche eccezioni "attive" sono delega pura a funzioni deterministiche
gia' isolate altrove (search_resolver): anche li' non c'e' business
logic, solo calcolo di date/orari.
"""

from __future__ import annotations

from datetime import datetime

from app.config import Config
from app.context.models import (
    ConversationContext,
    ConversationStatus,
    ContextValue,
    AI1Result,
    Intent,
    Message,
    OperationType,
    PendingAction,
)
from app.context.search_resolver import resolve_search_criteria

# Intent -> tipo di operazione corrente. Puramente descrittivo: dice
# "di cosa stiamo parlando", non "cosa fare adesso" (quello lo decide
# il Router in base anche allo step corrente).
_INTENT_TO_OPERATION = {
    Intent.BOOK: OperationType.CREATE,
    Intent.RESCHEDULE: OperationType.RESCHEDULE,
    Intent.CANCEL: OperationType.CANCEL,
    Intent.CHECK_APPOINTMENT: OperationType.CHECK,
}

# Intent che portano nuove/aggiornate preferenze di ricerca (data/ora).
_SEARCH_RELEVANT_INTENTS = {
    Intent.BOOK,
    Intent.RESCHEDULE,
    Intent.CHANGE_PREFERENCE,
}

_SEARCH_ENTITY_KEYS = (
    "period",
    "week_part",
    "weekday",
    "time_preference",
    "exact_time",
    "date_from",
    "date_to",
)


def _has_search_signal(entities: dict) -> bool:
    return any(entities.get(k) for k in _SEARCH_ENTITY_KEYS)


def _apply_customer_data(context: ConversationContext, entities: dict) -> None:
    """
    Aggiorna i dati anagrafici del cliente. Non confermati automaticamente:
    la conferma esplicita resta un passo separato (Confirmation), cosi'
    se l'utente corregge ("no, ho sbagliato il cognome") il dato puo'
    essere sovrascritto senza ambiguita' su cosa fosse "vero".
    """
    if entities.get("service"):
        context.service = ContextValue(value=entities["service"], source="USER", confirmed=False)

    if entities.get("full_name"):
        context.customer.full_name = ContextValue(
            value=entities["full_name"], source="USER", confirmed=False
        )
    if entities.get("phone"):
        context.customer.phone = ContextValue(
            value=entities["phone"], source="USER", confirmed=False
        )
    if entities.get("email"):
        context.customer.email = ContextValue(
            value=entities["email"], source="USER", confirmed=False
        )


def _apply_slot_selection(context: ConversationContext, entities: dict) -> None:
    """
    Traduce la scelta dell'utente (numero d'opzione) in uno slot reale
    tra quelli gia' proposti in precedenza. E' un lookup deterministico,
    non una decisione di business: lo slot esiste gia' in offered_slots,
    qui ci limitiamo a registrare quale l'utente ha scelto.
    """
    slot_number = entities.get("slot_number")
    if slot_number is None:
        return

    match = next(
        (offered for offered in context.offered_slots if offered.option == int(slot_number)),
        None,
    )
    if match is None:
        # Numero non corrispondente a nessuna opzione mostrata: non e'
        # compito del Context Manager capire cosa fare (es. chiedere
        # chiarimento) - lasciamo il segnale al Router tramite
        # booking.slot_id invariato.
        return

    context.booking.slot_id = match.slot.id
    context.confirmation.required = True
    context.confirmation.confirmation_type = "slot"
    context.confirmation.status = None
    context.conversation.pending_action = PendingAction.CONFIRM


def _apply_confirmation(context: ConversationContext, intent: Intent) -> None:
    if intent == Intent.CONFIRM:
        context.confirmation.status = "confirmed"
    elif intent == Intent.REJECT:
        context.confirmation.status = "rejected"


def apply_ai1_result(
    context: ConversationContext,
    ai1: AI1Result,
    message_text: str,
    now: datetime | None = None,
) -> ConversationContext:
    """
    Punto di ingresso unico del Context Manager.

    Ritorna un NUOVO ConversationContext (non muta l'originale), cosi'
    da poter essere testato e usato senza effetti collaterali nascosti.
    """
    now = now or datetime.now()
    context = context.model_copy(deep=True)

    # --- 1. Memoria: registra il messaggio utente ---
    context.memory.recent_messages.append(
        Message(role="user", text=message_text, timestamp=now)
    )
    context.memory.recent_messages = context.memory.recent_messages[-Config.MAX_RECENT_MESSAGES:]

    # --- 2. Timeout ---
    context.conversation.timeout.last_activity_at = now

    # --- 3. Stato "grezzo" della conversazione ---
    context.conversation.current_intent = ai1.intent
    context.confidence = ai1.confidence

    if context.conversation.status == ConversationStatus.NEW:
        context.conversation.status = ConversationStatus.ACTIVE

    if ai1.needs_clarification:
        context.escalation_required = False  # un chiarimento non e' ancora un'escalation
        return context

    operation = _INTENT_TO_OPERATION.get(ai1.intent)
    if operation:
        context.operation.type = operation
        context.conversation.current_operation = operation

    # --- 4. Applica le entities al resto del contesto ---
    _apply_customer_data(context, ai1.entities)

    if ai1.intent == Intent.SELECT_SLOT:
        _apply_slot_selection(context, ai1.entities)

    _apply_confirmation(context, ai1.intent)

    if ai1.intent in _SEARCH_RELEVANT_INTENTS and _has_search_signal(ai1.entities):
        context.search = resolve_search_criteria(
            entities=ai1.entities,
            current=context.search,
            today=now.date(),
            slot_search_days=Config.DEFAULT_SLOT_SEARCH_DAYS,
        )

    return context
