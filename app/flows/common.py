"""
Adattatore condiviso tra il ConversationContext tipizzato e le funzioni
esistenti in app/booking/engine.py (che lavorano ancora su dict liberi,
"collected_data").

Questo modulo esiste apposta per essere condiviso da PIÙ domini: la
ricerca disponibilità e la selezione di uno slot funzionano allo stesso
modo sia per una prenotazione nuova (booking.py) sia per uno
spostamento (reschedule.py, quando lo aggiungeremo) - cambia solo cosa
succede dopo la conferma. Non duplicarlo nei singoli flow.

Nota: NON tocca app/booking/engine.py. È un ponte a senso unico,
cosi' l'engine esistente (già collaudato) resta l'unica fonte di verità
per il calcolo di slot e la scrittura a DB, finché non lo migreremo
anch'esso.
"""

from __future__ import annotations

from app.context.models import (
    AvailableSlot,
    ConversationContext,
    ConversationStep,
    OfferedSlot,
    PendingAction,
    SystemResult,
)


def build_search_collected_data(context: ConversationContext) -> dict:
    """Converte SearchCriteria + service nel dict "preferences" atteso da engine._compute_search_window."""
    search = context.search
    return {
        "service": context.service.value,
        "location_id": context.professional.location_id if context.professional else None,
        "preferences": {
            "period": search.period,
            "weekday": search.preferred_weekday,
            "week_part": search.week_part,
            "date_from": search.date_from.isoformat() if search.date_from else None,
            "date_to": search.date_to.isoformat() if search.date_to else None,
            "date": search.preferred_date.isoformat() if search.preferred_date else None,
            "time_preference": search.time_preference,
            "exact_time": search.preferred_time.strftime("%H:%M") if search.preferred_time else None,
        },
    }


def slots_to_offered(candidate_slots: list[dict]) -> list[OfferedSlot]:
    """
    Numera gli slot trovati dall'engine (1, 2, 3...) cosi' che "il
    secondo" o "2" nel messaggio dell'utente sia deterministicamente
    risolvibile dal Context Manager (vedi _apply_slot_selection).
    """
    offered = []
    for i, raw in enumerate(candidate_slots, start=1):
        slot_id = f"{raw['date']}_{raw['time']}"
        offered.append(
            OfferedSlot(
                option=i,
                slot=AvailableSlot.model_validate(
                    {"id": slot_id, "date": raw["date"], "time": raw["time"]}
                ),
            )
        )
    return offered


def offered_slot_to_engine_dict(offered: OfferedSlot) -> dict:
    """Ricostruisce la forma dict ("selected_slot") che create_booking si aspetta, a partire dallo slot scelto."""
    date_str = offered.slot.date.isoformat()
    time_str = offered.slot.time.strftime("%H:%M")
    return {
        "date": date_str,
        "time": time_str,
        "datetime": f"{date_str}T{time_str}",
    }


def find_selected_offered_slot(context: ConversationContext) -> OfferedSlot | None:
    if not context.booking.slot_id:
        return None
    return next(
        (o for o in context.offered_slots if o.slot.id == context.booking.slot_id),
        None,
    )


def advance_after_slot_selection(context: ConversationContext) -> tuple[ConversationContext, SystemResult]:
    """
    Passo generico, riusabile da qualunque dominio: dopo che l'utente ha
    scelto uno slot (gia' registrato in context.booking.slot_id dal
    Context Manager), decide se servono ancora dati anagrafici o se si
    puo' passare direttamente alla conferma.
    """
    context = context.model_copy(deep=True)
    customer = context.customer

    missing_name = not (customer.full_name.value or "").strip()

    if missing_name:
        context.conversation.current_step = ConversationStep.COLLECTING_CUSTOMER_DATA
        context.conversation.pending_action = PendingAction.PROVIDE_NAME
        context.confirmation.required = False
        return context, SystemResult(success=True, data={"next": "ask_name"})

    context.conversation.current_step = ConversationStep.WAITING_FOR_CONFIRMATION
    context.conversation.pending_action = PendingAction.CONFIRM
    context.confirmation.required = True
    context.confirmation.confirmation_type = "booking"
    return context, SystemResult(success=True, data={"next": "ask_confirmation"})


def advance_after_customer_data(context: ConversationContext) -> tuple[ConversationContext, SystemResult]:
    """Generico quanto sopra: una volta ottenuto il nome, si passa alla conferma."""
    context = context.model_copy(deep=True)

    if not (context.customer.full_name.value or "").strip():
        # Il dato non e' ancora arrivato (il messaggio non lo conteneva):
        # restiamo nello stesso step, il Router ripropone la domanda.
        return context, SystemResult(success=False, error_code="MISSING_CUSTOMER_NAME")

    context.conversation.current_step = ConversationStep.WAITING_FOR_CONFIRMATION
    context.conversation.pending_action = PendingAction.CONFIRM
    context.confirmation.required = True
    context.confirmation.confirmation_type = "booking"
    return context, SystemResult(success=True, data={"next": "ask_confirmation"})
