"""
FLOW: BOOK (creazione di un nuovo appuntamento)

Da quando esiste anche reschedule.py, questo file contiene SOLO ciò che
è davvero specifico di "prenotare": la ricerca disponibilità è condivisa
(flows/common.run_availability_search), la selezione slot generica pure
(advance_after_slot_selection). Qui restano: l'ingresso nel flusso e la
creazione vera e propria alla conferma.

Nomi delle funzioni allineati con reschedule.py (start_search,
slot_selected, confirm, reject_confirmation) cosi' il Router puo'
scegliere il modulo giusto in base a context.operation.type senza
bisogno di sapere altro (vedi router/routing_table.py).
"""

from __future__ import annotations

from datetime import date, timedelta

from app.booking import engine
from app.context.models import ConversationContext, ConversationStep, OfferedDay, PendingAction, SystemResult
from app.flows.common import (
    advance_after_customer_data,
    advance_after_slot_selection,
    build_search_collected_data,
    find_selected_offered_slot,
    has_search_criteria,
    offered_slot_to_engine_dict,
    run_availability_search,
)
from app.utils.it_dates import today_in_tz


def start_search(
    context: ConversationContext,
    tenant: dict,
    knowledge: dict,
) -> tuple[ConversationContext, SystemResult]:
    """
    Intent BOOK/CHANGE_PREFERENCE -> se il cliente ha già indicato un
    criterio, cerca direttamente. Altrimenti, invece di chiedere
    genericamente "quando vorrebbe?", gli mostriamo cosa c'è già
    disponibile (questa settimana / la prossima), così sceglie da una
    base concreta invece di dover indovinare cosa rispondere.
    """
    if has_search_criteria(context):
        return run_availability_search(context, tenant, knowledge)
    return show_week_overview(context, tenant, knowledge)


def show_week_overview(
    context: ConversationContext,
    tenant: dict,
    knowledge: dict,
) -> tuple[ConversationContext, SystemResult]:
    context = context.model_copy(deep=True)

    today = today_in_tz(tenant.get("timezone"))
    this_monday = today - timedelta(days=today.weekday())
    this_sunday = this_monday + timedelta(days=6)
    next_sunday = this_sunday + timedelta(days=7)

    base_data = {
        "service": context.service.value,
        "location_id": context.professional.location_id if context.professional else None,
    }

    try:
        two_weeks = engine.search_available_days(
            tenant, knowledge,
            {**base_data, "preferences": {"date_from": today.isoformat(), "date_to": next_sunday.isoformat()}},
            max_days=14,
        )
    except Exception as exc:
        print(f"[flows.booking.show_week_overview] errore ricerca giorni disponibili: {exc!r}")
        return context, SystemResult(success=False, error_code="TECHNICAL_ERROR")

    available_days = two_weeks.get("available_days") or []
    this_week = [d for d in available_days if d["date"] <= this_sunday.isoformat()]
    next_week = [d for d in available_days if d["date"] > this_sunday.isoformat()]

    first_available = None
    if not this_week and not next_week:
        try:
            wide = engine.search_available_days(tenant, knowledge, {**base_data, "preferences": {}}, max_days=1)
        except Exception as exc:
            print(f"[flows.booking.show_week_overview] errore ricerca prima disponibilità: {exc!r}")
            return context, SystemResult(success=False, error_code="TECHNICAL_ERROR")
        wide_days = wide.get("available_days") or []
        first_available = wide_days[0] if wide_days else None

    context.conversation.current_step = ConversationStep.SEARCH_AVAILABILITY
    context.conversation.pending_action = PendingAction.PROVIDE_DATE

    shown_days = (this_week[:3] + next_week[:3]) if (this_week or next_week) else []
    context.offered_days = [
        OfferedDay(option=i, date=date.fromisoformat(d["date"]), label=d["label"])
        for i, d in enumerate(shown_days, start=1)
    ]

    return context, SystemResult(
        success=True,
        data={"this_week": this_week[:3], "next_week": next_week[:3], "first_available": first_available},
    )


def slot_selected(context: ConversationContext, **_ignored) -> tuple[ConversationContext, SystemResult]:
    """Intent SELECT_SLOT -> passo generico: chiede il nome se manca, altrimenti chiede conferma."""
    if find_selected_offered_slot(context) is None:
        return context, SystemResult(success=False, error_code="SLOT_NOT_RECOGNIZED")
    return advance_after_slot_selection(context)


def customer_data_provided(context: ConversationContext, **_ignored) -> tuple[ConversationContext, SystemResult]:
    """Step: COLLECTING_CUSTOMER_DATA + intent PROVIDE_DATA -> passo generico."""
    return advance_after_customer_data(context)


def confirm(
    context: ConversationContext,
    tenant: dict,
    knowledge: dict,
) -> tuple[ConversationContext, SystemResult]:
    """Intent CONFIRM -> crea davvero l'appuntamento."""
    context = context.model_copy(deep=True)

    offered = find_selected_offered_slot(context)
    if offered is None:
        return context, SystemResult(success=False, error_code="NO_SLOT_SELECTED")

    if not (context.customer.full_name.value or "").strip():
        # Non dovrebbe succedere (il Router arriva qui solo dopo
        # COLLECTING_CUSTOMER_DATA), ma se succede è meglio un errore
        # chiaro che un tentativo di creazione destinato a fallire.
        context.conversation.current_step = ConversationStep.COLLECTING_CUSTOMER_DATA
        return context, SystemResult(success=False, error_code="MISSING_CUSTOMER_NAME")

    collected_data = build_search_collected_data(context)
    collected_data["selected_slot"] = offered_slot_to_engine_dict(offered)
    collected_data["person_name"] = context.customer.full_name.value

    context.conversation.current_step = ConversationStep.EXECUTING

    try:
        booking_res = engine.create_booking(
            tenant=tenant,
            knowledge=knowledge,
            collected_data=collected_data,
            customer={"id": context.customer.id} if context.customer.id else None,
            phone_number=context.customer.phone.value,
        )
    except Exception as exc:
        print(f"[flows.booking.confirm] errore creazione appuntamento: {exc!r}")
        return context, SystemResult(success=False, error_code="TECHNICAL_ERROR")

    result = booking_res.get("result") or {}

    if not result.get("success"):
        error = result.get("error") or "UNKNOWN_ERROR"
        # Torniamo a proporre lo slot: l'utente potrà scegliere un'altra opzione.
        context.conversation.current_step = ConversationStep.WAITING_FOR_SLOT
        context.booking.status = context.booking.status.__class__.FAILED
        return context, SystemResult(success=False, error_code=error.upper(), data=result)

    context.booking.id = result.get("appointment_id")
    context.booking.status = context.booking.status.__class__.CONFIRMED
    context.conversation.current_step = ConversationStep.COMPLETED
    context.conversation.pending_action = PendingAction.NONE
    context.confirmation.required = False

    return context, SystemResult(success=True, data={"appointment_id": context.booking.id})


def reject_confirmation(context: ConversationContext, **_ignored) -> tuple[ConversationContext, SystemResult]:
    """Intent REJECT -> torna a proporre gli slot già trovati."""
    context = context.model_copy(deep=True)
    context.booking.slot_id = None
    context.confirmation.required = False
    context.confirmation.status = None
    context.conversation.current_step = ConversationStep.WAITING_FOR_SLOT
    context.conversation.pending_action = PendingAction.NONE
    return context, SystemResult(success=True, data={"next": "reask_slot"})
