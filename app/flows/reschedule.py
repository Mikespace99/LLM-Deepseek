"""
FLOW: RESCHEDULE (spostamento di un appuntamento esistente)

Riusa quasi tutto da flows/common.py: la ricerca disponibilità è
IDENTICA a quella di BOOK (run_availability_search). Ciò che è
specifico di "spostare" è solo:
  1. identificare QUALE appuntamento esistente spostare
  2. alla conferma, aggiornarlo invece di crearne uno nuovo

Scope attuale (deliberatamente limitato, per restare semplice):
  - 0 appuntamenti futuri -> errore chiaro
  - 1 appuntamento futuro -> lo si sceglie automaticamente, nessuna
    domanda inutile (il cliente ne ha uno solo, è ovvio quale)
  - più di 1 -> per ora chiediamo di specificare la data a parole,
    non c'è ancora una disambiguazione automatica (prossimo incremento
    se emerge come caso reale frequente)

A differenza di BOOK, dopo la selezione dello slot NON si chiede il
nome: il cliente esiste già (ha un appuntamento a suo nome), quindi si
salta dritti alla conferma.
"""

from __future__ import annotations

from app.context.models import (
    Appointment,
    ConversationContext,
    ConversationStep,
    ContextValue,
    PendingAction,
    SystemResult,
)
from app.flows.common import (
    build_search_collected_data,
    find_selected_offered_slot,
    offered_slot_to_engine_dict,
    run_availability_search,
)
from app.repositories import appointment as appointment_repo


def identify_target(
    context: ConversationContext,
    tenant: dict,
    knowledge: dict,
) -> tuple[ConversationContext, SystemResult]:
    """Intent RESCHEDULE -> trova quale appuntamento spostare, poi cerca la nuova disponibilità."""
    context = context.model_copy(deep=True)

    if not context.customer.id:
        return context, SystemResult(success=False, error_code="CUSTOMER_NOT_IDENTIFIED")

    try:
        rows = appointment_repo.list_upcoming_for_customer(tenant["id"], context.customer.id, limit=5)
    except Exception as exc:
        print(f"[flows.reschedule.identify_target] errore lettura appuntamenti: {exc!r}")
        return context, SystemResult(success=False, error_code="TECHNICAL_ERROR")

    if not rows:
        return context, SystemResult(success=False, error_code="NO_UPCOMING_APPOINTMENTS")

    if len(rows) > 1:
        # Scope attuale: nessuna disambiguazione automatica ancora.
        context.appointments = [
            Appointment(
                id=r["id"], date=r["appointment_date"], time=r["appointment_time"],
                status=context.booking.status.__class__.CONFIRMED,
            )
            for r in rows
        ]
        context.conversation.current_step = ConversationStep.IDENTIFY_APPOINTMENT
        context.conversation.pending_action = PendingAction.SELECT_APPOINTMENT
        return context, SystemResult(success=False, error_code="MULTIPLE_APPOINTMENTS_NOT_SUPPORTED")

    target = rows[0]
    context.operation.target_appointment_id = target["id"]
    context.selected_appointment_id = target["id"]
    # Manteniamo lo stesso servizio dell'appuntamento originale, a meno
    # che il cliente non ne indichi uno diverso nel messaggio corrente
    # (già gestito a monte dal Context Manager se presente nelle entities).
    if not context.service.value and target.get("service"):
        context.service = ContextValue(value=target["service"], source="SYSTEM", confirmed=True)

    return run_availability_search(context, tenant, knowledge)


def start_search(
    context: ConversationContext,
    tenant: dict,
    knowledge: dict,
) -> tuple[ConversationContext, SystemResult]:
    """Intent CHANGE_PREFERENCE -> il target è già noto, si ricerca di nuovo con i criteri aggiornati."""
    return run_availability_search(context, tenant, knowledge)


def slot_selected(context: ConversationContext, **_ignored) -> tuple[ConversationContext, SystemResult]:
    """
    Intent SELECT_SLOT -> a differenza di BOOK, si salta dritti alla
    conferma: il cliente esiste già, non serve chiedere il nome.
    """
    context = context.model_copy(deep=True)
    if find_selected_offered_slot(context) is None:
        return context, SystemResult(success=False, error_code="SLOT_NOT_RECOGNIZED")

    context.conversation.current_step = ConversationStep.WAITING_FOR_CONFIRMATION
    context.conversation.pending_action = PendingAction.CONFIRM
    context.confirmation.required = True
    context.confirmation.confirmation_type = "reschedule"
    return context, SystemResult(success=True, data={"next": "ask_confirmation"})


def confirm(
    context: ConversationContext,
    tenant: dict,
    knowledge: dict,
) -> tuple[ConversationContext, SystemResult]:
    """Intent CONFIRM -> sposta davvero l'appuntamento (update, non create)."""
    context = context.model_copy(deep=True)

    offered = find_selected_offered_slot(context)
    if offered is None:
        return context, SystemResult(success=False, error_code="NO_SLOT_SELECTED")

    target_id = context.operation.target_appointment_id
    if not target_id:
        return context, SystemResult(success=False, error_code="NO_APPOINTMENT_TARGET")

    new_slot = offered_slot_to_engine_dict(offered)
    context.conversation.current_step = ConversationStep.EXECUTING

    try:
        updated = appointment_repo.update_appointment(
            tenant["id"],
            target_id,
            appointment_date=new_slot["date"],
            appointment_time=new_slot["time"],
        )
    except Exception as exc:
        if appointment_repo.is_overlap_error(exc):
            context.conversation.current_step = ConversationStep.WAITING_FOR_SLOT
            return context, SystemResult(success=False, error_code="SLOT_CONFLICT")
        print(f"[flows.reschedule.confirm] errore spostamento appuntamento: {exc!r}")
        return context, SystemResult(success=False, error_code="TECHNICAL_ERROR")

    if not updated:
        context.conversation.current_step = ConversationStep.WAITING_FOR_SLOT
        return context, SystemResult(success=False, error_code="APPOINTMENT_NOT_FOUND")

    context.booking.id = target_id
    context.booking.status = context.booking.status.__class__.RESCHEDULED
    context.conversation.current_step = ConversationStep.COMPLETED
    context.conversation.pending_action = PendingAction.NONE
    context.confirmation.required = False

    return context, SystemResult(success=True, data={"appointment_id": target_id})


def reject_confirmation(context: ConversationContext, **_ignored) -> tuple[ConversationContext, SystemResult]:
    """Intent REJECT -> torna a proporre gli slot già trovati."""
    context = context.model_copy(deep=True)
    context.booking.slot_id = None
    context.confirmation.required = False
    context.confirmation.status = None
    context.conversation.current_step = ConversationStep.WAITING_FOR_SLOT
    context.conversation.pending_action = PendingAction.NONE
    return context, SystemResult(success=True, data={"next": "reask_slot"})
