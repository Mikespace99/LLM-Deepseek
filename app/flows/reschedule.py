"""
FLOW: RESCHEDULE (spostamento di un appuntamento esistente)

Tre fasi distinte, ognuna con la propria conferma - non sono la stessa
domanda travestita:
  1. "È questo l'appuntamento che vuoi spostare?"   (conferma il TARGET)
  2. "Quando vorresti spostarlo?"                    (nessuna ricerca finché non risponde)
  3. "Confermi il nuovo orario?"                     (conferma il NUOVO SLOT)

Un solo stato di verità per distinguere fase 1 da fase 3 (che userebbero
altrimenti lo stesso intent CONFIRM): context.confirmation.confirmation_type
("reschedule_target" vs "reschedule"). Niente stato parallelo duplicato:
conversation.current_step resta l'unica fonte di verità sullo step.

Scope attuale (invariato, deliberatamente limitato): con più di un
appuntamento futuro non c'è ancora disambiguazione automatica - si
chiede di specificare la data a parole.
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
    search_or_ask_preference,
)
from app.repositories import appointment as appointment_repo


def identify_target(
    context: ConversationContext,
    tenant: dict,
    knowledge: dict,
) -> tuple[ConversationContext, SystemResult]:
    """
    Intent RESCHEDULE -> trova l'appuntamento e chiede conferma che sia
    quello giusto. NON cerca ancora nuova disponibilità: prima si
    conferma il target, poi si chiede la preferenza (vedi ask_preference).
    """
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
            Appointment(id=r["id"], date=r["appointment_date"], time=r["appointment_time"],
                        status=context.booking.status.__class__.CONFIRMED)
            for r in rows
        ]
        context.conversation.current_step = ConversationStep.IDENTIFY_APPOINTMENT
        context.conversation.pending_action = PendingAction.SELECT_APPOINTMENT
        return context, SystemResult(success=False, error_code="MULTIPLE_APPOINTMENTS_NOT_SUPPORTED")

    target = rows[0]
    context.operation.target_appointment_id = target["id"]
    context.appointments = [
        Appointment(id=target["id"], date=target["appointment_date"], time=target["appointment_time"],
                    status=context.booking.status.__class__.CONFIRMED)
    ]
    if not context.service.value and target.get("service"):
        context.service = ContextValue(value=target["service"], source="SYSTEM", confirmed=True)

    context.conversation.current_step = ConversationStep.IDENTIFY_APPOINTMENT
    context.conversation.pending_action = PendingAction.CONFIRM
    context.confirmation.required = True
    context.confirmation.confirmation_type = "reschedule_target"

    return context, SystemResult(success=True, data={"next": "confirm_target_appointment"})


def start_search(
    context: ConversationContext,
    tenant: dict,
    knowledge: dict,
) -> tuple[ConversationContext, SystemResult]:
    """Intent CHANGE_PREFERENCE -> il target è già confermato, cerca (o chiede la preferenza se ancora manca)."""
    return search_or_ask_preference(context, tenant, knowledge)


def slot_selected(context: ConversationContext, **_ignored) -> tuple[ConversationContext, SystemResult]:
    """
    Intent SELECT_SLOT -> a differenza di BOOK, si salta dritti alla
    conferma del NUOVO slot: il cliente esiste già, il target è già
    confermato, non serve chiedere altro.
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
    """
    Intent CONFIRM -> due significati diversi a seconda della fase,
    distinti da confirmation_type (mai da uno stato parallelo):
      - "reschedule_target": ha confermato QUALE appuntamento -> si passa
        a chiedere la preferenza per il nuovo orario (nessuna ricerca ancora).
      - "reschedule" (o assente): ha confermato il NUOVO slot -> si sposta
        davvero l'appuntamento (update, non create).
    """
    context = context.model_copy(deep=True)

    if context.confirmation.confirmation_type == "reschedule_target":
        context.confirmation.required = False
        context.confirmation.confirmation_type = None
        context.conversation.pending_action = PendingAction.NONE
        return search_or_ask_preference(context, tenant, knowledge)

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
            tenant["id"], target_id,
            appointment_date=new_slot["date"], appointment_time=new_slot["time"],
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
    """
    Intent REJECT -> anche qui due significati diversi:
      - "reschedule_target": ha rifiutato l'unico appuntamento trovato ->
        non c'è un'alternativa automatica (scope attuale), meglio un
        messaggio onesto che un ciclo a vuoto.
      - "reschedule": ha rifiutato il nuovo slot -> torna a proporre
        quelli già trovati.
    """
    context = context.model_copy(deep=True)

    if context.confirmation.confirmation_type == "reschedule_target":
        context.confirmation.required = False
        context.confirmation.confirmation_type = None
        context.conversation.pending_action = PendingAction.NONE
        return context, SystemResult(success=False, error_code="RESCHEDULE_TARGET_REJECTED")

    context.booking.slot_id = None
    context.confirmation.required = False
    context.confirmation.status = None
    context.conversation.current_step = ConversationStep.WAITING_FOR_SLOT
    context.conversation.pending_action = PendingAction.NONE
    return context, SystemResult(success=True, data={"next": "reask_slot"})
