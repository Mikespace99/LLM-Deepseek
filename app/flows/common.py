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

from app.booking import engine
from app.context.models import (
    AvailableSlot,
    ConversationContext,
    ConversationStep,
    OfferedSlot,
    PendingAction,
    SystemResult,
)


def has_search_criteria(context: ConversationContext) -> bool:
    """L'utente ha già espresso ALMENO un criterio (anche generico, es. 'nessuna preferenza' -> period='any')?"""
    s = context.search
    return bool(s.period or s.preferred_weekday or s.date_from or s.preferred_time or s.time_from)


def search_or_ask_preference(
    context: ConversationContext,
    tenant: dict,
    knowledge: dict,
) -> tuple[ConversationContext, SystemResult]:
    """
    Punto di ingresso condiviso per "voglio cercare disponibilità":
    non lancia MAI una ricerca alla cieca. Se non c'è ancora nessun
    criterio, chiede la preferenza invece di cercare su una finestra
    aperta di default - identico per BOOK e RESCHEDULE.
    """
    if not has_search_criteria(context):
        context = context.model_copy(deep=True)
        context.conversation.current_step = ConversationStep.SEARCH_AVAILABILITY
        context.conversation.pending_action = PendingAction.PROVIDE_DATE
        return context, SystemResult(success=True, data={"next": "ask_preference"})

    return run_availability_search(context, tenant, knowledge)


def run_availability_search(
    context: ConversationContext,
    tenant: dict,
    knowledge: dict,
) -> tuple[ConversationContext, SystemResult]:
    """
    Cerca disponibilità e aggiorna il contesto. Identica per BOOK e
    RESCHEDULE: cambia solo cosa succede DOPO la conferma (create vs
    update), non come si trovano gli slot.
    """
    context = context.model_copy(deep=True)
    collected_data = build_search_collected_data(context)

    try:
        booking_res = engine.search_availability(tenant=tenant, knowledge=knowledge, collected_data=collected_data)
    except Exception as exc:
        print(f"[flows.common.run_availability_search] errore ricerca disponibilità: {exc!r}")
        return context, SystemResult(success=False, error_code="TECHNICAL_ERROR")

    candidate_slots = booking_res.get("candidate_slots") or []
    result = booking_res.get("result") or {}

    if not candidate_slots:
        previous_alternatives = bool(context.offered_slots)  # non li cancelliamo: restano validi
        error_code = "NO_SLOTS_FOUND"
        if result.get("is_studio_closed"):
            error_code = "STUDIO_CLOSED"
        elif result.get("is_studio_full"):
            error_code = "STUDIO_FULL"
        return context, SystemResult(
            success=False, error_code=error_code,
            data={**result, "previous_alternatives": previous_alternatives},
        )

    context.offered_slots = slots_to_offered(candidate_slots)
    context.conversation.current_step = ConversationStep.WAITING_FOR_SLOT
    context.conversation.pending_action = PendingAction.NONE

    return context, SystemResult(
        success=True,
        # Solo il conteggio, MAI le date/orari: l'AI non deve poter
        # riscrivere la lista con le sue parole. La lista vera la
        # mostra format_helpers.py in modo deterministico.
        data={"slots_count": len(context.offered_slots)},
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
