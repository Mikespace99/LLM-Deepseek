"""
FLOW: BOOK (creazione di un nuovo appuntamento)

Unico file che conosce il significato di "prenotare": cerca
disponibilita' e, alla conferma, crea l'appuntamento. Le parti generiche
(numerazione slot, passaggio ai dati anagrafici, conferma) sono in
flows/common.py e sono condivise con gli altri domini futuri.

Ogni funzione qui dentro:
  - riceve il ConversationContext (gia' aggiornato dal Context Manager)
    piu' tenant/knowledge (config del tenant, non fanno parte dello
    stato conversazionale)
  - non decide MAI cosa dire all'utente: restituisce (context, SystemResult)
  - il SystemResult e' lo schema unico che anche reschedule/cancel/info
    dovranti restituire, cosi' AI#2 legge sempre la stessa forma.
"""

from __future__ import annotations

from app.booking import engine
from app.context.models import ConversationContext, ConversationStep, PendingAction, SystemResult
from app.flows.common import (
    advance_after_customer_data,
    advance_after_slot_selection,
    build_search_collected_data,
    find_selected_offered_slot,
    offered_slot_to_engine_dict,
    slots_to_offered,
)


def start_search(
    context: ConversationContext,
    tenant: dict,
    knowledge: dict,
) -> tuple[ConversationContext, SystemResult]:
    """Step: IDLE/WAITING_FOR_SLOT + intent BOOK/CHANGE_PREFERENCE -> cerca disponibilità."""
    context = context.model_copy(deep=True)
    collected_data = build_search_collected_data(context)

    try:
        booking_res = engine.search_availability(tenant=tenant, knowledge=knowledge, collected_data=collected_data)
    except Exception as exc:
        print(f"[flows.booking.start_search] errore ricerca disponibilità: {exc!r}")
        return context, SystemResult(success=False, error_code="TECHNICAL_ERROR")

    candidate_slots = booking_res.get("candidate_slots") or []
    result = booking_res.get("result") or {}

    if not candidate_slots:
        previous_alternatives = bool(context.offered_slots)  # non li cancelliamo: restano validi
        if result.get("is_studio_closed"):
            return context, SystemResult(
                success=False, error_code="STUDIO_CLOSED",
                data={**result, "previous_alternatives": previous_alternatives},
            )
        if result.get("is_studio_full"):
            return context, SystemResult(
                success=False, error_code="STUDIO_FULL",
                data={**result, "previous_alternatives": previous_alternatives},
            )
        return context, SystemResult(
            success=False, error_code="NO_SLOTS_FOUND",
            data={**result, "previous_alternatives": previous_alternatives},
        )

    context.offered_slots = slots_to_offered(candidate_slots)
    context.conversation.current_step = ConversationStep.WAITING_FOR_SLOT
    context.conversation.pending_action = PendingAction.NONE

    return context, SystemResult(
        success=True,
        data={"slots": [o.model_dump(mode="json") for o in context.offered_slots]},
    )


def slot_selected(context: ConversationContext, **_ignored) -> tuple[ConversationContext, SystemResult]:
    """Step: WAITING_FOR_SLOT + intent SELECT_SLOT -> passo generico (comune ad altri domini)."""
    if find_selected_offered_slot(context) is None:
        return context, SystemResult(success=False, error_code="SLOT_NOT_RECOGNIZED")
    return advance_after_slot_selection(context)


def customer_data_provided(context: ConversationContext, **_ignored) -> tuple[ConversationContext, SystemResult]:
    """Step: COLLECTING_CUSTOMER_DATA + intent PROVIDE_DATA -> passo generico."""
    return advance_after_customer_data(context)


def confirm_booking(
    context: ConversationContext,
    tenant: dict,
    knowledge: dict,
) -> tuple[ConversationContext, SystemResult]:
    """Step: WAITING_FOR_CONFIRMATION + intent CONFIRM -> crea davvero l'appuntamento."""
    context = context.model_copy(deep=True)

    offered = find_selected_offered_slot(context)
    if offered is None:
        return context, SystemResult(success=False, error_code="NO_SLOT_SELECTED")

    collected_data = build_search_collected_data(context)
    collected_data["selected_slot"] = offered_slot_to_engine_dict(offered)

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
        print(f"[flows.booking.confirm_booking] errore creazione appuntamento: {exc!r}")
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
    """Step: WAITING_FOR_CONFIRMATION + intent REJECT -> torna a proporre gli slot."""
    context = context.model_copy(deep=True)
    context.booking.slot_id = None
    context.confirmation.required = False
    context.confirmation.status = None
    context.conversation.current_step = ConversationStep.WAITING_FOR_SLOT
    context.conversation.pending_action = PendingAction.NONE
    return context, SystemResult(success=True, data={"next": "reask_slot"})
