"""
CONTEXT MANAGER

Prende un AI1Result (l'interpretazione del messaggio) e lo applica al
ConversationContext, producendone uno aggiornato.

Principio guida: questo modulo e' "stupido" di proposito. Aggiorna lo
stato in modo deterministico e prevedibile, ma NON decide alcuna azione
di business (non cerca disponibilita', non crea prenotazioni, non
chiama servizi esterni). Quelle decisioni spettano al Router e alla
Business Logic, che leggeranno il contesto gia' aggiornato da qui.

LIVELLO 1 (vedi slot_matcher.py): prima ancora di fidarsi dell'intent
dichiarato da AI#1, controlliamo se ciò che l'utente ha detto combacia
con uno slot già proposto. Se sì, quello vince - anche se AI#1 aveva
classificato il messaggio come CONFIRM, BOOK o l'aveva segnato come
ambiguo. Questo evita di dover insegnare ad AI#1 ogni possibile modo
di esprimere una selezione: basta confrontare i dati.
"""

from __future__ import annotations

from datetime import datetime

from app.config import Config
from app.context.models import (
    AI1Result,
    Appointment,
    ContextValue,
    ConversationContext,
    ConversationStatus,
    ConversationStep,
    Intent,
    Message,
    OfferedDay,
    OfferedSlot,
    OperationType,
    PendingAction,
)
from app.context.appointment_matcher import match_offered_appointment
from app.context.day_matcher import match_offered_day
from app.context.search_resolver import resolve_search_criteria
from app.context.slot_matcher import match_offered_slot

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
# Intent che portano nuove/aggiornate preferenze di ricerca (data/ora).
# RESCHEDULE NON c'è apposta: nel messaggio che avvia lo spostamento,
# un riferimento a una data ("l'appuntamento della prossima settimana")
# descrive quasi sempre QUALE appuntamento esistente spostare, non
# QUANDO vuole il nuovo - quello lo esprime solo DOPO aver confermato
# il target, con un messaggio successivo classificato CHANGE_PREFERENCE.
_SEARCH_RELEVANT_INTENTS = {
    Intent.BOOK,
    Intent.CHANGE_PREFERENCE,
}

_SEARCH_ENTITY_KEYS = (
    "period",
    "week_part",
    "weekday",
    "month",        # <-- aggiungi
    "month_part",   # <-- aggiungi
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


def _apply_slot_selection(context: ConversationContext, matched: OfferedSlot) -> None:
    """
    Registra quale slot è stato scelto. Il "quale" è già stato deciso
    da slot_matcher (per numero o per descrizione): qui ci limitiamo a
    scriverlo nel contesto. La decisione su COSA fare dopo (chiedere il
    nome? chiedere conferma?) spetta al Router.
    """
    context.booking.slot_id = matched.slot.id
    context.confirmation.status = None


def _is_awaiting_appointment_selection(context: ConversationContext) -> bool:
    """
    Vero solo nella finestra esatta in cui abbiamo appena mostrato i
    bottoni "quale appuntamento vuole spostare?" (più di uno trovato).
    Non va confuso con la conferma sì/no di un SINGOLO appuntamento
    (confirmation_type="reschedule_target"): quella resta gestita dal
    flusso CONFIRM esistente, questa è una scelta tra più opzioni.
    """
    return (
        context.conversation.current_step == ConversationStep.IDENTIFY_APPOINTMENT
        and context.conversation.pending_action == PendingAction.SELECT_APPOINTMENT
        and len(context.appointments) > 1
    )


def _apply_appointment_selection(context: ConversationContext, matched: Appointment) -> None:
    """
    Registra quale, tra gli appuntamenti mostrati, è stato scelto. Il
    "quale" è già stato deciso da appointment_matcher (per bottone,
    numero o descrizione): qui ci limitiamo a scriverlo nel contesto.
    """
    context.selected_appointment_id = matched.id


def _apply_day_selection(
    context: ConversationContext,
    matched: OfferedDay,
    entities: dict | None = None,
) -> None:
    """
    Ancora la ricerca al giorno scelto dalla panoramica.
    Se nello stesso messaggio c'è anche una fascia oraria
    (es. \"mercoledì tardo pomeriggio\"), la applica subito:
    altrimenti si perderebbe perché resolve_search_criteria
    non viene chiamato quando matched_day è True.
    """
    context.search.date_from = matched.date
    context.search.date_to = matched.date
    context.search.preferred_date = matched.date
    context.search.period = None
    context.search.week_part = None
    context.search.preferred_weekday = None
    context.offered_days = []

    entities = entities or {}
    if entities.get("time_preference"):
        context.search.time_preference = entities["time_preference"]
    if entities.get("exact_time"):
        try:
            hh, mm = str(entities["exact_time"]).split(":")
            from datetime import time as _time
            context.search.preferred_time = _time(hour=int(hh), minute=int(mm))
            context.search.time_preference = "exact"
        except Exception:
            pass


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
    context.confidence = ai1.confidence
    if context.conversation.status == ConversationStatus.NEW:
        context.conversation.status = ConversationStatus.ACTIVE

    # --- 3. LIVELLO 1: il messaggio combacia con uno slot già proposto? ---
    # Vale la pena controllare anche se AI#1 ha classificato altro (es.
    # CONFIRM: "va bene per lunedì alle 10") o non è sicura (needs_clarification):
    # un match sui dati è un segnale più forte della sola classificazione.
    matched_slot = match_offered_slot(ai1.entities, context.offered_slots, message_text)

    # Stesso principio, per i GIORNI mostrati nella panoramica settimanale
    # (solo se non abbiamo già trovato uno slot - i due casi non si
    # sovrappongono mai nella pratica, ma per chiarezza sono esclusivi).
    matched_day = None
    if not matched_slot and context.offered_days:
        matched_day = match_offered_day(ai1.entities, context.offered_days, now.date())

    # Stesso principio, per la scelta tra PIU' appuntamenti futuri
    # mostrati come bottoni (vedi flows/reschedule.py::identify_target).
    matched_appointment = None
    if not matched_slot and not matched_day and _is_awaiting_appointment_selection(context):
        matched_appointment = match_offered_appointment(ai1.entities, context.appointments, message_text)

    if matched_slot:
        effective_intent = Intent.SELECT_SLOT
    elif matched_day:
        effective_intent = Intent.CHANGE_PREFERENCE
    elif matched_appointment:
        effective_intent = Intent.SELECT_APPOINTMENT
    else:
        effective_intent = ai1.intent

    effective_needs_clarification = (
        ai1.needs_clarification and not matched_slot and not matched_day and not matched_appointment
    )

    context.conversation.current_intent = effective_intent

    if effective_needs_clarification:
        return context

    # --- 4. Stato dell'operazione ---
    operation = _INTENT_TO_OPERATION.get(effective_intent)
    if operation:
        context.operation.type = operation
        context.conversation.current_operation = operation

    # --- 5. Applica le entities al resto del contesto ---
    _apply_customer_data(context, ai1.entities)

    if matched_slot:
        _apply_slot_selection(context, matched_slot)
    elif matched_day:
        _apply_day_selection(context, matched_day, ai1.entities)
    elif matched_appointment:
        _apply_appointment_selection(context, matched_appointment)

    _apply_confirmation(context, effective_intent)

    if not matched_day and effective_intent in _SEARCH_RELEVANT_INTENTS and _has_search_signal(ai1.entities):
        # Per RESCHEDULE, "la settimana successiva"/"tra due settimane"
        # ecc. sono relative alla data dell'APPUNTAMENTO da spostare,
        # non a oggi: è quello il punto di riferimento naturale per il
        # cliente ("la settimana dopo quella del mio appuntamento"),
        # non la data odierna della conversazione.
        anchor_date = now.date()
        if context.operation.type == OperationType.RESCHEDULE and context.appointments:
            anchor_date = context.appointments[0].date

        context.search = resolve_search_criteria(
            entities=ai1.entities,
            current=context.search,
            today=anchor_date,
            slot_search_days=Config.DEFAULT_SLOT_SEARCH_DAYS,
        )
        print(f"[DIAGNOSTICA CONTEXT] anchor_date={anchor_date} -> search.date_from={context.search.date_from} search.date_to={context.search.date_to}")

    return context
